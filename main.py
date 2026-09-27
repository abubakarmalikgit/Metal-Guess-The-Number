"""
=====================================================================
  SPEED MESSAGE EVENT BOT — PREMIUM MINIMAL BUILD
  Python 3.11+ | discord.py 2.4+ | aiohttp
  Host sets a phrase; the first 1-3 members to send it exactly win.
  Deploy-ready for Render Web Services (keep-alive HTTP server).
=====================================================================
"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

import discord
from aiohttp import web
from discord import app_commands, ui
from discord.ext import commands

# ---------------------------------------------------------
# CONFIG
# ---------------------------------------------------------

TOKEN = os.environ.get("DISCORD_TOKEN")
PORT = int(os.environ.get("PORT", 8080))
GUILD_ID = os.environ.get("GUILD_ID")  # optional: instant command sync
STATE_FILE = Path(os.environ.get("STATE_PATH", "events.json"))

COLOR_GOLD = 0xF1C40F
COLOR_WIN = 0x2ECC71
COLOR_RED = 0xE74C3C
COLOR_INFO = 0x5865F2

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
PLACE_LABELS = {1: "🥇 1st", 2: "🥈 2nd", 3: "🥉 3rd"}
EVENT_TTL = 24 * 3600  # events older than this are dropped on restart

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
logger = logging.getLogger("speed_event")

_WHITESPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
  """Case-insensitive, whitespace-tolerant comparison key."""
  return _WHITESPACE.sub(" ", text.strip()).lower()


# ---------------------------------------------------------
# EVENT SESSION
# ---------------------------------------------------------


class ChatRaceSession:
  """One active speed-message event, scoped to a single channel."""

  __slots__ = (
      "channel_id",
      "guild_id",
      "host_id",
      "phrase",
      "phrase_key",
      "winner_count",
      "rewards",
      "winners",
      "is_active",
      "started_at",
      "lock",
  )

  def __init__(
      self,
      channel_id: int,
      guild_id: int,
      host_id: int,
      phrase: str,
      winner_count: int,
      rewards: dict[int, str],
      winners: list[int] | None = None,
      started_at: int | None = None,
  ):
    self.channel_id = channel_id
    self.guild_id = guild_id
    self.host_id = host_id
    self.phrase = phrase
    self.phrase_key = normalize(phrase)
    self.winner_count = winner_count
    self.rewards = rewards
    self.winners: list[int] = winners or []
    self.is_active = True
    self.started_at = started_at or int(time.time())
    # Serializes winner claims so simultaneous messages can never take the
    # same place or overfill the podium.
    self.lock = asyncio.Lock()

  def reward_for(self, place: int) -> str:
    return self.rewards.get(place, "Prize")

  @property
  def remaining(self) -> int:
    return max(self.winner_count - len(self.winners), 0)

  def to_dict(self) -> dict:
    return {
        "channel_id": self.channel_id,
        "guild_id": self.guild_id,
        "host_id": self.host_id,
        "phrase": self.phrase,
        "winner_count": self.winner_count,
        "rewards": {str(k): v for k, v in self.rewards.items()},
        "winners": self.winners,
        "started_at": self.started_at,
    }

  @classmethod
  def from_dict(cls, data: dict) -> "ChatRaceSession":
    return cls(
        channel_id=data["channel_id"],
        guild_id=data["guild_id"],
        host_id=data["host_id"],
        phrase=data["phrase"],
        winner_count=data["winner_count"],
        rewards={int(k): v for k, v in data["rewards"].items()},
        winners=list(data.get("winners", [])),
        started_at=data.get("started_at"),
    )


class EventStore:
  """In-memory events with JSON persistence, so a restart never loses a live
  event (Render free tier restarts often)."""

  def __init__(self, path: Path):
    self.path = path
    self.events: dict[int, ChatRaceSession] = {}

  def load(self):
    if not self.path.exists():
      return
    try:
      cutoff = int(time.time()) - EVENT_TTL
      for item in json.loads(self.path.read_text()):
        session = ChatRaceSession.from_dict(item)
        if session.started_at >= cutoff and session.remaining > 0:
          self.events[session.channel_id] = session
      logger.info("Restored %s active event(s)", len(self.events))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
      logger.warning("Could not restore events: %s", exc)

  def save(self):
    try:
      tmp = self.path.with_suffix(".tmp")
      tmp.write_text(json.dumps([e.to_dict() for e in self.events.values()]))
      tmp.replace(self.path)
    except OSError as exc:
      logger.warning("Could not persist events: %s", exc)

  def get(self, channel_id: int) -> ChatRaceSession | None:
    return self.events.get(channel_id)

  def add(self, session: ChatRaceSession):
    self.events[session.channel_id] = session
    self.save()

  def remove(self, channel_id: int) -> ChatRaceSession | None:
    session = self.events.pop(channel_id, None)
    if session:
      session.is_active = False
      self.save()
    return session


store = EventStore(STATE_FILE)


# ---------------------------------------------------------
# HELPERS
# ---------------------------------------------------------


def is_host_or_admin(user: discord.abc.User, session: ChatRaceSession) -> bool:
  if user.id == session.host_id:
    return True
  perms = getattr(user, "guild_permissions", None)
  return bool(perms and (perms.administrator or perms.manage_guild))


def can_post_in(channel: discord.abc.GuildChannel) -> bool:
  me = channel.guild.me
  if me is None:
    return True
  perms = channel.permissions_for(me)
  return perms.send_messages and perms.embed_links and perms.view_channel


# ---------------------------------------------------------
# EMBEDS
# ---------------------------------------------------------


def announce_embed(session: ChatRaceSession, host: discord.abc.User):
  plural = "person" if session.winner_count == 1 else "people"
  embed = discord.Embed(
      title="🎁 Speed Message Event",
      description=(
          f"The first **{session.winner_count} {plural}** to send this exact"
          f" message in this channel win.\n\n```\n{session.phrase}\n```"
      ),
      color=COLOR_GOLD,
      timestamp=discord.utils.utcnow(),
  )
  for place in range(1, session.winner_count + 1):
    embed.add_field(
        name=f"{PLACE_LABELS[place]} place",
        value=f"**{session.reward_for(place)}**",
        inline=True,
    )
  embed.add_field(name="Host", value=host.mention, inline=False)
  embed.set_footer(
      text="Case and extra spaces are ignored • one win per person"
  )
  return embed


def status_embed(session: ChatRaceSession, guild: discord.Guild):
  embed = discord.Embed(
      title="📊 Event Status",
      description=f"```\n{session.phrase}\n```",
      color=COLOR_INFO,
      timestamp=discord.utils.utcnow(),
  )
  embed.add_field(
      name="Spots claimed",
      value=f"`{len(session.winners)}/{session.winner_count}`",
      inline=True,
  )
  embed.add_field(
      name="Started", value=f"<t:{session.started_at}:R>", inline=True
  )
  host = guild.get_member(session.host_id)
  embed.add_field(
      name="Host",
      value=host.mention if host else f"<@{session.host_id}>",
      inline=True,
  )
  if session.winners:
    lines = [
        f"{PLACE_LABELS.get(i, f'#{i}')} <@{uid}> — **{session.reward_for(i)}**"
        for i, uid in enumerate(session.winners, start=1)
    ]
    embed.add_field(name="Winners so far", value="\n".join(lines), inline=False)
  return embed


def final_embed(session: ChatRaceSession):
  embed = discord.Embed(
      title="🏁 Event complete",
      description=(
          f"All spots are claimed. The target message was:\n```\n"
          f"{session.phrase}\n```"
      ),
      color=COLOR_WIN,
      timestamp=discord.utils.utcnow(),
  )
  for place, user_id in enumerate(session.winners, start=1):
    embed.add_field(
        name=f"{PLACE_LABELS.get(place, f'#{place}')} place",
        value=f"<@{user_id}> — **{session.reward_for(place)}**",
        inline=False,
    )
  embed.set_footer(text="Host can now distribute the prizes")
  return embed


def cancel_embed(session: ChatRaceSession, actor: discord.abc.User):
  embed = discord.Embed(
      title="🛑 Event cancelled",
      description=f"Cancelled by {actor.mention}.",
      color=COLOR_RED,
      timestamp=discord.utils.utcnow(),
  )
  embed.add_field(
      name="Spots claimed",
      value=f"`{len(session.winners)}/{session.winner_count}`",
      inline=True,
  )
  if session.winners:
    embed.add_field(
        name="Winners",
        value="\n".join(
            f"{PLACE_LABELS.get(i, f'#{i}')} <@{uid}>"
            for i, uid in enumerate(session.winners, start=1)
        ),
        inline=False,
    )
  return embed


# ---------------------------------------------------------
# UI — persistent control panel
# ---------------------------------------------------------


class EventControlView(ui.View):
  """Stateless persistent view: buttons resolve the event by channel, so they
  keep working after a bot restart."""

  def __init__(self):
    super().__init__(timeout=None)

  async def _resolve(
      self, interaction: discord.Interaction
  ) -> ChatRaceSession | None:
    session = store.get(interaction.channel_id)
    if not session or not session.is_active:
      await interaction.response.send_message(
          "This event has already ended.", ephemeral=True
      )
      return None
    return session

  @ui.button(
      label="Status",
      style=discord.ButtonStyle.secondary,
      emoji="📊",
      custom_id="event:status",
  )
  async def status_button(
      self, interaction: discord.Interaction, button: ui.Button
  ):
    session = await self._resolve(interaction)
    if not session:
      return
    await interaction.response.send_message(
        embed=status_embed(session, interaction.guild), ephemeral=True
    )

  @ui.button(
      label="Cancel",
      style=discord.ButtonStyle.danger,
      emoji="🛑",
      custom_id="event:cancel",
  )
  async def cancel_button(
      self, interaction: discord.Interaction, button: ui.Button
  ):
    session = await self._resolve(interaction)
    if not session:
      return
    if not is_host_or_admin(interaction.user, session):
      await interaction.response.send_message(
          "⛔ Only the host or a server admin can cancel this event.",
          ephemeral=True,
      )
      return

    store.remove(session.channel_id)
    for child in self.children:
      child.disabled = True
    # One edit acknowledges the click and disables the panel — no
    # double-response bug.
    await interaction.response.edit_message(view=self)
    await interaction.followup.send(embed=cancel_embed(session, interaction.user))


class SetupEventModal(ui.Modal, title="Configure Speed Message Event"):

  phrase_input = ui.TextInput(
      label="Exact message members must send",
      placeholder="e.g. I love this server",
      required=True,
      max_length=120,
  )
  winner_count_input = ui.TextInput(
      label="Number of winners (1-3)",
      placeholder="1, 2 or 3",
      default="3",
      required=True,
      max_length=1,
  )
  reward_1st_input = ui.TextInput(
      label="1st place reward",
      placeholder="e.g. $10 gift card",
      required=True,
      max_length=100,
  )
  reward_2nd_input = ui.TextInput(
      label="2nd place reward (needed for 2+ winners)",
      placeholder="e.g. 500 credits",
      required=False,
      max_length=100,
  )
  reward_3rd_input = ui.TextInput(
      label="3rd place reward (needed for 3 winners)",
      placeholder="e.g. Supporter role",
      required=False,
      max_length=100,
  )

  def __init__(self, target_channel: discord.TextChannel):
    super().__init__()
    self.target_channel = target_channel

  async def on_submit(self, interaction: discord.Interaction):
    phrase = _WHITESPACE.sub(" ", self.phrase_input.value.strip())
    if len(phrase) < 2:
      await interaction.response.send_message(
          "❌ The target message must be at least 2 characters.", ephemeral=True
      )
      return

    raw_count = self.winner_count_input.value.strip()
    if raw_count not in ("1", "2", "3"):
      await interaction.response.send_message(
          "❌ Number of winners must be 1, 2 or 3.", ephemeral=True
      )
      return
    count = int(raw_count)

    rewards = {
        1: self.reward_1st_input.value.strip(),
        2: self.reward_2nd_input.value.strip(),
        3: self.reward_3rd_input.value.strip(),
    }
    # Blank or placeholder rewards would otherwise be announced as "None".
    missing = [
        place
        for place in range(1, count + 1)
        if not rewards[place] or rewards[place].lower() == "none"
    ]
    if missing:
      names = ", ".join(PLACE_LABELS[p] for p in missing)
      await interaction.response.send_message(
          f"❌ Missing a reward for: {names}. Fill one in or lower the winner"
          " count.",
          ephemeral=True,
      )
      return
    rewards = {p: rewards[p] for p in range(1, count + 1)}

    # Re-check: another host may have started an event while this modal was
    # open.
    if store.get(self.target_channel.id):
      await interaction.response.send_message(
          f"⚠️ An event just started in {self.target_channel.mention}. Cancel"
          " it first.",
          ephemeral=True,
      )
      return

    session = ChatRaceSession(
        channel_id=self.target_channel.id,
        guild_id=interaction.guild_id,
        host_id=interaction.user.id,
        phrase=phrase,
        winner_count=count,
        rewards=rewards,
    )

    try:
      await self.target_channel.send(
          embed=announce_embed(session, interaction.user),
          view=EventControlView(),
      )
    except discord.Forbidden:
      await interaction.response.send_message(
          f"❌ I can't post in {self.target_channel.mention}. Give me **Send"
          " Messages** and **Embed Links** there, then try again.",
          ephemeral=True,
      )
      return
    except discord.HTTPException as exc:
      logger.warning("Announce failed: %s", exc)
      await interaction.response.send_message(
          "❌ Discord rejected the announcement. Please try again.",
          ephemeral=True,
      )
      return

    # Only register the event once the announcement is actually live.
    store.add(session)
    await interaction.response.send_message(
        f"✅ **Event live** in {self.target_channel.mention} —"
        f" {count} winner(s).",
        ephemeral=True,
    )

  async def on_error(self, interaction: discord.Interaction, error: Exception):
    logger.exception("Modal failure", exc_info=error)
    if not interaction.response.is_done():
      await interaction.response.send_message(
          "⚠️ Setup failed. Please try again.", ephemeral=True
      )


# ---------------------------------------------------------
# BOT CORE
# ---------------------------------------------------------


class SpeedEventBot(commands.Bot):

  def __init__(self):
    intents = discord.Intents.default()
    intents.message_content = True
    super().__init__(command_prefix="!", intents=intents, help_command=None)
    self.web_runner: web.AppRunner | None = None

  async def setup_hook(self):
    store.load()
    # Register the persistent view so panel buttons survive restarts.
    self.add_view(EventControlView())
    self.web_runner = await start_webserver(PORT)

    if GUILD_ID:
      guild = discord.Object(id=int(GUILD_ID))
      self.tree.copy_global_to(guild=guild)
      await self.tree.sync(guild=guild)
      logger.info("Commands synced to guild %s", GUILD_ID)
    else:
      await self.tree.sync()
      logger.info("Commands synced globally")

  async def on_ready(self):
    logger.info("Online as %s (%s)", self.user, self.user.id)
    await self.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching, name="for the fastest typer"
        )
    )

  async def close(self):
    store.save()
    if self.web_runner:
      await self.web_runner.cleanup()
    await super().close()


bot = SpeedEventBot()


# ---------------------------------------------------------
# RENDER KEEP-ALIVE SERVER
# ---------------------------------------------------------


async def handle_ping(request: web.Request) -> web.Response:
  return web.json_response(
      {
          "status": "ok",
          "bot": str(bot.user) if bot.user else "starting",
          "active_events": len(store.events),
      }
  )


async def start_webserver(port: int) -> web.AppRunner:
  app = web.Application()
  app.router.add_get("/", handle_ping)
  app.router.add_get("/health", handle_ping)
  runner = web.AppRunner(app)
  await runner.setup()
  await web.TCPSite(runner, "0.0.0.0", port).start()
  logger.info("Keep-alive server listening on 0.0.0.0:%s", port)
  return runner


# ---------------------------------------------------------
# SLASH COMMANDS
# ---------------------------------------------------------


@bot.tree.command(
    name="event",
    description="Start a 'first to send this message wins' event",
)
@app_commands.describe(channel="Channel members must send the message in")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.guild_only()
async def event(
    interaction: discord.Interaction,
    channel: discord.TextChannel | None = None,
):
  target = channel or interaction.channel
  if not isinstance(target, discord.TextChannel):
    await interaction.response.send_message(
        "❌ Pick a normal text channel for the event.", ephemeral=True
    )
    return
  if store.get(target.id):
    await interaction.response.send_message(
        f"⚠️ An event is already running in {target.mention}. Finish or cancel"
        " it first.",
        ephemeral=True,
    )
    return
  if not can_post_in(target):
    await interaction.response.send_message(
        f"❌ I need **View Channel**, **Send Messages** and **Embed Links** in"
        f" {target.mention}.",
        ephemeral=True,
    )
    return
  await interaction.response.send_modal(SetupEventModal(target_channel=target))


@bot.tree.command(name="status", description="Show this channel's event status")
@app_commands.guild_only()
async def status_cmd(interaction: discord.Interaction):
  session = store.get(interaction.channel_id)
  if not session:
    await interaction.response.send_message(
        "No event is running in this channel.", ephemeral=True
    )
    return
  await interaction.response.send_message(
      embed=status_embed(session, interaction.guild), ephemeral=True
  )


@bot.tree.command(
    name="cancel", description="Cancel this channel's event (host or admin)"
)
@app_commands.guild_only()
async def cancel_cmd(interaction: discord.Interaction):
  session = store.get(interaction.channel_id)
  if not session:
    await interaction.response.send_message(
        "No event is running in this channel.", ephemeral=True
    )
    return
  if not is_host_or_admin(interaction.user, session):
    await interaction.response.send_message(
        "⛔ Only the host or a server admin can cancel this event.",
        ephemeral=True,
    )
    return
  store.remove(session.channel_id)
  await interaction.response.send_message(
      embed=cancel_embed(session, interaction.user)
  )


@bot.tree.command(name="help", description="How the event works")
async def help_cmd(interaction: discord.Interaction):
  embed = discord.Embed(
      title="⚡ Speed Message Event",
      description=(
          "A host sets a phrase and the first 1-3 members to send it exactly"
          " win the listed rewards."
      ),
      color=COLOR_INFO,
  )
  embed.add_field(
      name="Commands",
      value=(
          "`/event [channel]` — open the setup panel *(needs Manage"
          " Messages)*\n"
          "`/status` — spots claimed and winners so far\n"
          "`/cancel` — end the event early\n"
          "`/help` — this menu"
      ),
      inline=False,
  )
  embed.add_field(
      name="Rules",
      value=(
          "• One event per channel.\n"
          "• Matching ignores capitalisation and extra spaces.\n"
          "• One win per person; the host can't win their own event.\n"
          "• Live events survive a bot restart."
      ),
      inline=False,
  )
  await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------
# REAL-TIME MESSAGE MONITOR
# ---------------------------------------------------------


async def claim_spot(
    session: ChatRaceSession, user_id: int
) -> tuple[int | None, bool]:
  """Atomically claim the next place. Returns (place, event_finished)."""
  async with session.lock:
    if not session.is_active or session.remaining == 0:
      return None, False
    if user_id in session.winners:
      return None, False
    if user_id == session.host_id:
      return None, False
    session.winners.append(user_id)
    place = len(session.winners)
    finished = session.remaining == 0
    if finished:
      session.is_active = False
      store.remove(session.channel_id)
    else:
      store.save()
    return place, finished


@bot.event
async def on_message(message: discord.Message):
  if message.author.bot or not message.guild:
    return

  session = store.get(message.channel.id)
  if (
      session
      and session.is_active
      and normalize(message.content) == session.phrase_key
  ):
    place, finished = await claim_spot(session, message.author.id)
    if place is not None:
      try:
        await message.add_reaction(MEDALS.get(place, "✅"))
      except discord.HTTPException:
        pass
      try:
        await message.reply(
            f"⚡ **{PLACE_LABELS.get(place, f'#{place}')} place!**"
            f" {message.author.mention} wins"
            f" **{session.reward_for(place)}**.",
            mention_author=False,
        )
        if finished:
          await message.channel.send(
              content=(
                  f"🔔 <@{session.host_id}> — **all winners are in.** Time to"
                  " hand out the prizes."
              ),
              embed=final_embed(session),
          )
      except discord.HTTPException as exc:
        logger.warning("Could not announce winner: %s", exc)
      return

  await bot.process_commands(message)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
  if isinstance(error, app_commands.MissingPermissions):
    msg = "⛔ You need the **Manage Messages** permission to do that."
  elif isinstance(error, app_commands.NoPrivateMessage):
    msg = "❌ This bot only works inside a server."
  elif isinstance(error, app_commands.CommandOnCooldown):
    msg = f"⏳ Slow down — try again in {error.retry_after:.1f}s."
  else:
    logger.exception("Command error", exc_info=error)
    msg = "⚠️ Something went wrong. Please try again."
  try:
    if interaction.response.is_done():
      await interaction.followup.send(msg, ephemeral=True)
    else:
      await interaction.response.send_message(msg, ephemeral=True)
  except discord.HTTPException:
    pass


# ---------------------------------------------------------
# STARTUP
# ---------------------------------------------------------

if __name__ == "__main__":
  if not TOKEN:
    raise SystemExit(
        "Missing DISCORD_TOKEN environment variable. Set it in your Render"
        " environment settings."
    )
  bot.run(TOKEN, log_handler=None)
