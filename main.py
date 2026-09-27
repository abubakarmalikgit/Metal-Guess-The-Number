"""
=====================================================================
  GIVEAWAY EVENTS BOT — PREMIUM MINIMAL BUILD
  Python 3.11+ | discord.py 2.4+ | aiohttp

  Two independent game modes, each with its own command group:
    /number  — guess the secret number, first exact hit wins
    /race    — first 1-3 people to send an exact phrase win

  Deploy-ready for Render Web Services (keep-alive HTTP server).
=====================================================================
"""

import asyncio
import json
import logging
import os
import random
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
COLOR_BLURPLE = 0x5865F2
COLOR_WIN = 0x2ECC71
COLOR_RED = 0xE74C3C

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
PLACE_LABELS = {1: "🥇 1st", 2: "🥈 2nd", 3: "🥉 3rd"}

MAX_RANGE = 10_000_000
SESSION_TTL = 24 * 3600  # sessions older than this are dropped on restart
HINT_COOLDOWN = 1.5  # seconds between hint reactions, protects rate limits

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
logger = logging.getLogger("event_bot")

_WHITESPACE = re.compile(r"\s+")


# ---------------------------------------------------------
# HELPERS
# ---------------------------------------------------------


def normalize(text: str) -> str:
  """Case-insensitive, whitespace-tolerant comparison key."""
  return _WHITESPACE.sub(" ", text.strip()).lower()


def parse_int(raw: str) -> int | None:
  """Accept 1, -5, 1,000 and 1 000; reject everything else."""
  cleaned = raw.replace(",", "").replace("_", "").replace(" ", "")
  if not cleaned:
    return None
  body = cleaned[1:] if cleaned[0] in "+-" else cleaned
  if not body.isdigit() or len(body) > 12:
    return None
  return int(cleaned)


def fmt(n: int) -> str:
  return f"{n:,}"


def is_host_or_admin(user: discord.abc.User, host_id: int) -> bool:
  if user.id == host_id:
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
# SESSIONS
# ---------------------------------------------------------


class NumberSession:
  """Guess-the-number giveaway. First exact hit wins; optional live
  higher/lower hint reactions."""

  kind = "number"
  __slots__ = (
      "channel_id", "guild_id", "host_id", "target", "min_val", "max_val",
      "prize", "hints", "total_attempts", "players", "is_active",
      "started_at", "lock", "_last_hint",
  )

  def __init__(
      self,
      channel_id: int,
      guild_id: int,
      host_id: int,
      target: int,
      min_val: int,
      max_val: int,
      prize: str,
      hints: bool = False,
      total_attempts: int = 0,
      players: set[int] | None = None,
      started_at: int | None = None,
  ):
    self.channel_id = channel_id
    self.guild_id = guild_id
    self.host_id = host_id
    self.target = target
    self.min_val = min_val
    self.max_val = max_val
    self.prize = prize
    self.hints = hints
    self.total_attempts = total_attempts
    self.players: set[int] = players or set()
    self.is_active = True
    self.started_at = started_at or int(time.time())
    self.lock = asyncio.Lock()
    self._last_hint = 0.0

  def hint_ready(self) -> bool:
    now = time.monotonic()
    if now - self._last_hint < HINT_COOLDOWN:
      return False
    self._last_hint = now
    return True

  def to_dict(self) -> dict:
    return {
        "kind": self.kind,
        "channel_id": self.channel_id,
        "guild_id": self.guild_id,
        "host_id": self.host_id,
        "target": self.target,
        "min_val": self.min_val,
        "max_val": self.max_val,
        "prize": self.prize,
        "hints": self.hints,
        "total_attempts": self.total_attempts,
        "players": list(self.players),
        "started_at": self.started_at,
    }

  @classmethod
  def from_dict(cls, d: dict) -> "NumberSession":
    return cls(
        channel_id=d["channel_id"], guild_id=d["guild_id"],
        host_id=d["host_id"], target=d["target"], min_val=d["min_val"],
        max_val=d["max_val"], prize=d["prize"], hints=d.get("hints", False),
        total_attempts=d.get("total_attempts", 0),
        players=set(d.get("players", [])), started_at=d.get("started_at"),
    )


class RaceSession:
  """Speed-message event. First 1-3 people to send an exact phrase win."""

  kind = "race"
  __slots__ = (
      "channel_id", "guild_id", "host_id", "phrase", "phrase_key",
      "winner_count", "rewards", "winners", "is_active", "started_at", "lock",
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
    self.lock = asyncio.Lock()

  def reward_for(self, place: int) -> str:
    return self.rewards.get(place, "Prize")

  @property
  def remaining(self) -> int:
    return max(self.winner_count - len(self.winners), 0)

  def to_dict(self) -> dict:
    return {
        "kind": self.kind,
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
  def from_dict(cls, d: dict) -> "RaceSession":
    return cls(
        channel_id=d["channel_id"], guild_id=d["guild_id"],
        host_id=d["host_id"], phrase=d["phrase"],
        winner_count=d["winner_count"],
        rewards={int(k): v for k, v in d["rewards"].items()},
        winners=list(d.get("winners", [])), started_at=d.get("started_at"),
    )


Session = NumberSession | RaceSession
_CLASSES = {"number": NumberSession, "race": RaceSession}


class SessionStore:
  """Sessions keyed by (kind, channel_id) with JSON persistence, so a restart
  never loses a live event. One event of each kind can run per channel."""

  def __init__(self, path: Path):
    self.path = path
    self.sessions: dict[tuple[str, int], Session] = {}

  def load(self):
    if not self.path.exists():
      return
    try:
      cutoff = int(time.time()) - SESSION_TTL
      for item in json.loads(self.path.read_text()):
        cls = _CLASSES.get(item.get("kind"))
        if not cls:
          continue
        session = cls.from_dict(item)
        if session.started_at >= cutoff:
          self.sessions[(session.kind, session.channel_id)] = session
      logger.info("Restored %s live session(s)", len(self.sessions))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
      logger.warning("Could not restore sessions: %s", exc)

  def save(self):
    try:
      tmp = self.path.with_suffix(".tmp")
      tmp.write_text(json.dumps([s.to_dict() for s in self.sessions.values()]))
      tmp.replace(self.path)
    except OSError as exc:
      logger.warning("Could not persist sessions: %s", exc)

  def get(self, kind: str, channel_id: int) -> Session | None:
    return self.sessions.get((kind, channel_id))

  def add(self, session: Session):
    self.sessions[(session.kind, session.channel_id)] = session
    self.save()

  def remove(self, kind: str, channel_id: int) -> Session | None:
    session = self.sessions.pop((kind, channel_id), None)
    if session:
      session.is_active = False
      self.save()
    return session

  def count(self, kind: str) -> int:
    return sum(1 for k in self.sessions if k[0] == kind)


store = SessionStore(STATE_FILE)


# ---------------------------------------------------------
# EMBEDS — NUMBER MODE
# ---------------------------------------------------------


def number_announce_embed(s: NumberSession, host: discord.abc.User):
  embed = discord.Embed(
      title="🎯 Guess the Number",
      description=(
          "A secret number is locked in. Type numbers in this channel to"
          " win.\n\n⚡ **Spam allowed** — guess as often and as fast as you"
          " like. First exact hit takes the prize."
      ),
      color=COLOR_GOLD,
      timestamp=discord.utils.utcnow(),
  )
  embed.add_field(
      name="Range",
      value=f"`{fmt(s.min_val)}` — `{fmt(s.max_val)}`",
      inline=True,
  )
  embed.add_field(name="Prize", value=s.prize, inline=True)
  embed.add_field(name="Host", value=host.mention, inline=True)
  if s.hints:
    embed.add_field(
        name="Hints",
        value="I react 🔼 if the target is higher, 🔽 if it's lower.",
        inline=False,
    )
  embed.set_footer(text="Out-of-range numbers are ignored silently")
  return embed


def number_status_embed(s: NumberSession, guild: discord.Guild):
  span = s.max_val - s.min_val + 1
  host = guild.get_member(s.host_id)
  embed = discord.Embed(
      title="📊 Number Event Status",
      color=COLOR_BLURPLE,
      timestamp=discord.utils.utcnow(),
  )
  embed.add_field(
      name="Range",
      value=f"`{fmt(s.min_val)}` — `{fmt(s.max_val)}`",
      inline=True,
  )
  embed.add_field(name="Possible numbers", value=f"`{fmt(span)}`", inline=True)
  embed.add_field(
      name="Guesses made", value=f"`{fmt(s.total_attempts)}`", inline=True
  )
  embed.add_field(name="Players", value=f"`{fmt(len(s.players))}`", inline=True)
  embed.add_field(
      name="Hints", value="On" if s.hints else "Off", inline=True
  )
  embed.add_field(
      name="Started", value=f"<t:{s.started_at}:R>", inline=True
  )
  embed.add_field(name="Prize", value=s.prize, inline=False)
  embed.set_footer(
      text=f"Hosted by {host.display_name if host else 'unknown'}"
  )
  return embed


def number_win_embed(s: NumberSession, winner: discord.abc.User, guess: int):
  embed = discord.Embed(
      title="🎉 We have a winner!",
      description=f"{winner.mention} hit the exact number.",
      color=COLOR_WIN,
      timestamp=discord.utils.utcnow(),
  )
  embed.add_field(name="Winning number", value=f"`{fmt(guess)}`", inline=True)
  embed.add_field(
      name="Total guesses", value=f"`{fmt(s.total_attempts)}`", inline=True
  )
  embed.add_field(name="Players", value=f"`{fmt(len(s.players))}`", inline=True)
  embed.add_field(name="Prize", value=s.prize, inline=False)
  embed.set_thumbnail(url=winner.display_avatar.url)
  embed.set_footer(text="Event completed")
  return embed


def number_cancel_embed(s: NumberSession, actor: discord.abc.User):
  embed = discord.Embed(
      title="🛑 Number event cancelled",
      description=(
          f"Cancelled by {actor.mention}.\nThe secret number was"
          f" **{fmt(s.target)}**."
      ),
      color=COLOR_RED,
      timestamp=discord.utils.utcnow(),
  )
  embed.add_field(
      name="Guesses made", value=f"`{fmt(s.total_attempts)}`", inline=True
  )
  return embed


# ---------------------------------------------------------
# EMBEDS — RACE MODE
# ---------------------------------------------------------


def race_announce_embed(s: RaceSession, host: discord.abc.User):
  plural = "person" if s.winner_count == 1 else "people"
  embed = discord.Embed(
      title="⚡ Speed Message Event",
      description=(
          f"The first **{s.winner_count} {plural}** to send this exact message"
          f" in this channel win.\n\n```\n{s.phrase}\n```"
      ),
      color=COLOR_GOLD,
      timestamp=discord.utils.utcnow(),
  )
  for place in range(1, s.winner_count + 1):
    embed.add_field(
        name=f"{PLACE_LABELS[place]} place",
        value=f"**{s.reward_for(place)}**",
        inline=True,
    )
  embed.add_field(name="Host", value=host.mention, inline=False)
  embed.set_footer(text="Case and extra spaces ignored • one win per person")
  return embed


def race_status_embed(s: RaceSession, guild: discord.Guild):
  host = guild.get_member(s.host_id)
  embed = discord.Embed(
      title="📊 Speed Event Status",
      description=f"```\n{s.phrase}\n```",
      color=COLOR_BLURPLE,
      timestamp=discord.utils.utcnow(),
  )
  embed.add_field(
      name="Spots claimed",
      value=f"`{len(s.winners)}/{s.winner_count}`",
      inline=True,
  )
  embed.add_field(name="Started", value=f"<t:{s.started_at}:R>", inline=True)
  embed.add_field(
      name="Host",
      value=host.mention if host else f"<@{s.host_id}>",
      inline=True,
  )
  if s.winners:
    embed.add_field(
        name="Winners so far",
        value="\n".join(
            f"{PLACE_LABELS.get(i, f'#{i}')} <@{uid}> —"
            f" **{s.reward_for(i)}**"
            for i, uid in enumerate(s.winners, start=1)
        ),
        inline=False,
    )
  return embed


def race_final_embed(s: RaceSession):
  embed = discord.Embed(
      title="🏁 Event complete",
      description=(
          f"All spots are claimed. The target message was:\n```\n{s.phrase}\n```"
      ),
      color=COLOR_WIN,
      timestamp=discord.utils.utcnow(),
  )
  for place, user_id in enumerate(s.winners, start=1):
    embed.add_field(
        name=f"{PLACE_LABELS.get(place, f'#{place}')} place",
        value=f"<@{user_id}> — **{s.reward_for(place)}**",
        inline=False,
    )
  embed.set_footer(text="Host can now distribute the prizes")
  return embed


def race_cancel_embed(s: RaceSession, actor: discord.abc.User):
  embed = discord.Embed(
      title="🛑 Speed event cancelled",
      description=f"Cancelled by {actor.mention}.",
      color=COLOR_RED,
      timestamp=discord.utils.utcnow(),
  )
  embed.add_field(
      name="Spots claimed",
      value=f"`{len(s.winners)}/{s.winner_count}`",
      inline=True,
  )
  if s.winners:
    embed.add_field(
        name="Winners",
        value="\n".join(
            f"{PLACE_LABELS.get(i, f'#{i}')} <@{uid}>"
            for i, uid in enumerate(s.winners, start=1)
        ),
        inline=False,
    )
  return embed


# ---------------------------------------------------------
# UI — persistent control panels
# ---------------------------------------------------------


class BaseControlView(ui.View):
  """Stateless persistent view: buttons resolve the session by channel, so
  they keep working after a restart."""

  kind: str = ""

  def __init__(self):
    super().__init__(timeout=None)

  async def _resolve(self, interaction: discord.Interaction):
    session = store.get(self.kind, interaction.channel_id)
    if not session or not session.is_active:
      await interaction.response.send_message(
          "This event has already ended.", ephemeral=True
      )
      return None
    return session

  async def _guard_cancel(self, interaction: discord.Interaction, session):
    if is_host_or_admin(interaction.user, session.host_id):
      return True
    await interaction.response.send_message(
        "⛔ Only the host or a server admin can cancel this event.",
        ephemeral=True,
    )
    return False

  async def _finish_cancel(self, interaction: discord.Interaction, embed):
    for child in self.children:
      child.disabled = True
    # One edit acknowledges the click and disables the panel.
    await interaction.response.edit_message(view=self)
    await interaction.followup.send(embed=embed)


class NumberControlView(BaseControlView):
  kind = "number"

  @ui.button(
      label="Status", style=discord.ButtonStyle.secondary, emoji="📊",
      custom_id="number:status",
  )
  async def status_button(
      self, interaction: discord.Interaction, button: ui.Button
  ):
    session = await self._resolve(interaction)
    if session:
      await interaction.response.send_message(
          embed=number_status_embed(session, interaction.guild), ephemeral=True
      )

  @ui.button(
      label="Cancel", style=discord.ButtonStyle.danger, emoji="🛑",
      custom_id="number:cancel",
  )
  async def cancel_button(
      self, interaction: discord.Interaction, button: ui.Button
  ):
    session = await self._resolve(interaction)
    if not session or not await self._guard_cancel(interaction, session):
      return
    store.remove(self.kind, session.channel_id)
    await self._finish_cancel(
        interaction, number_cancel_embed(session, interaction.user)
    )


class RaceControlView(BaseControlView):
  kind = "race"

  @ui.button(
      label="Status", style=discord.ButtonStyle.secondary, emoji="📊",
      custom_id="race:status",
  )
  async def status_button(
      self, interaction: discord.Interaction, button: ui.Button
  ):
    session = await self._resolve(interaction)
    if session:
      await interaction.response.send_message(
          embed=race_status_embed(session, interaction.guild), ephemeral=True
      )

  @ui.button(
      label="Cancel", style=discord.ButtonStyle.danger, emoji="🛑",
      custom_id="race:cancel",
  )
  async def cancel_button(
      self, interaction: discord.Interaction, button: ui.Button
  ):
    session = await self._resolve(interaction)
    if not session or not await self._guard_cancel(interaction, session):
      return
    store.remove(self.kind, session.channel_id)
    await self._finish_cancel(
        interaction, race_cancel_embed(session, interaction.user)
    )


# ---------------------------------------------------------
# MODALS
# ---------------------------------------------------------


class NumberSetupModal(ui.Modal, title="Create Number Guessing Event"):

  min_input = ui.TextInput(
      label="Minimum number", placeholder="e.g. 1", default="1", max_length=12
  )
  max_input = ui.TextInput(
      label="Maximum number", placeholder="e.g. 500", default="100",
      max_length=12,
  )
  secret_input = ui.TextInput(
      label="Secret number (blank = random)",
      placeholder="Leave empty and I'll pick one secretly",
      required=False, max_length=12,
  )
  prize_input = ui.TextInput(
      label="Prize",
      placeholder="e.g. Nitro Basic, 1,000 credits, Steam key",
      required=False, default="Host will provide the reward", max_length=150,
  )
  hints_input = ui.TextInput(
      label="Higher/lower hints? (yes / no)",
      placeholder="yes = I react 🔼 / 🔽 to guesses",
      default="no", required=False, max_length=3,
  )

  def __init__(self, target_channel: discord.TextChannel):
    super().__init__()
    self.target_channel = target_channel

  async def on_submit(self, interaction: discord.Interaction):
    min_val = parse_int(self.min_input.value)
    max_val = parse_int(self.max_input.value)

    if min_val is None or max_val is None:
      await interaction.response.send_message(
          "❌ Minimum and maximum must be whole numbers.", ephemeral=True
      )
      return
    if min_val >= max_val:
      await interaction.response.send_message(
          f"❌ Minimum (`{fmt(min_val)}`) must be smaller than maximum"
          f" (`{fmt(max_val)}`).",
          ephemeral=True,
      )
      return
    if max_val - min_val + 1 > MAX_RANGE:
      await interaction.response.send_message(
          f"❌ That range is too wide (max {fmt(MAX_RANGE)} numbers).",
          ephemeral=True,
      )
      return

    raw_secret = self.secret_input.value.strip()
    if raw_secret:
      secret = parse_int(raw_secret)
      if secret is None:
        await interaction.response.send_message(
            "❌ The secret number must be a whole number.", ephemeral=True
        )
        return
      if not min_val <= secret <= max_val:
        await interaction.response.send_message(
            f"❌ The secret number must be between `{fmt(min_val)}` and"
            f" `{fmt(max_val)}`.",
            ephemeral=True,
        )
        return
    else:
      secret = random.SystemRandom().randint(min_val, max_val)

    hints = normalize(self.hints_input.value) in ("y", "yes", "true", "on", "1")

    # Re-check: someone may have started one while this modal was open.
    if store.get("number", self.target_channel.id):
      await interaction.response.send_message(
          f"⚠️ A number event just started in {self.target_channel.mention}."
          " Cancel it first.",
          ephemeral=True,
      )
      return

    session = NumberSession(
        channel_id=self.target_channel.id,
        guild_id=interaction.guild_id,
        host_id=interaction.user.id,
        target=secret,
        min_val=min_val,
        max_val=max_val,
        prize=self.prize_input.value.strip() or "Host will provide the reward",
        hints=hints,
    )

    if not await announce(
        interaction,
        self.target_channel,
        number_announce_embed(session, interaction.user),
        NumberControlView(),
    ):
      return

    store.add(session)
    await interaction.response.send_message(
        f"✅ **Number event live** in {self.target_channel.mention}. Secret"
        f" number `{fmt(secret)}` is locked in — only you can see this.",
        ephemeral=True,
    )

  async def on_error(self, interaction: discord.Interaction, error: Exception):
    await modal_error(interaction, error)


class RaceSetupModal(ui.Modal, title="Create Speed Message Event"):

  phrase_input = ui.TextInput(
      label="Exact message members must send",
      placeholder="e.g. I love this server",
      required=True, max_length=120,
  )
  winner_count_input = ui.TextInput(
      label="Number of winners (1-3)", placeholder="1, 2 or 3", default="3",
      required=True, max_length=1,
  )
  reward_1st_input = ui.TextInput(
      label="1st place reward", placeholder="e.g. $10 gift card",
      required=True, max_length=100,
  )
  reward_2nd_input = ui.TextInput(
      label="2nd place reward (needed for 2+ winners)",
      placeholder="e.g. 500 credits", required=False, max_length=100,
  )
  reward_3rd_input = ui.TextInput(
      label="3rd place reward (needed for 3 winners)",
      placeholder="e.g. Supporter role", required=False, max_length=100,
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
        p for p in range(1, count + 1)
        if not rewards[p] or rewards[p].lower() == "none"
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

    if store.get("race", self.target_channel.id):
      await interaction.response.send_message(
          f"⚠️ A speed event just started in {self.target_channel.mention}."
          " Cancel it first.",
          ephemeral=True,
      )
      return

    session = RaceSession(
        channel_id=self.target_channel.id,
        guild_id=interaction.guild_id,
        host_id=interaction.user.id,
        phrase=phrase,
        winner_count=count,
        rewards=rewards,
    )

    if not await announce(
        interaction,
        self.target_channel,
        race_announce_embed(session, interaction.user),
        RaceControlView(),
    ):
      return

    store.add(session)
    await interaction.response.send_message(
        f"✅ **Speed event live** in {self.target_channel.mention} —"
        f" {count} winner(s).",
        ephemeral=True,
    )

  async def on_error(self, interaction: discord.Interaction, error: Exception):
    await modal_error(interaction, error)


async def announce(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    embed: discord.Embed,
    view: ui.View,
) -> bool:
  """Post the public announcement before registering the session, so a failed
  send never leaves a ghost event behind."""
  try:
    await channel.send(embed=embed, view=view)
    return True
  except discord.Forbidden:
    await interaction.response.send_message(
        f"❌ I can't post in {channel.mention}. Give me **Send Messages** and"
        " **Embed Links** there, then try again.",
        ephemeral=True,
    )
  except discord.HTTPException as exc:
    logger.warning("Announce failed: %s", exc)
    await interaction.response.send_message(
        "❌ Discord rejected the announcement. Please try again.",
        ephemeral=True,
    )
  return False


async def modal_error(interaction: discord.Interaction, error: Exception):
  logger.exception("Modal failure", exc_info=error)
  if not interaction.response.is_done():
    await interaction.response.send_message(
        "⚠️ Setup failed. Please try again.", ephemeral=True
    )


# ---------------------------------------------------------
# BOT CORE
# ---------------------------------------------------------


class EventBot(commands.Bot):

  def __init__(self):
    intents = discord.Intents.default()
    intents.message_content = True
    super().__init__(command_prefix="!", intents=intents, help_command=None)
    self.web_runner: web.AppRunner | None = None

  async def setup_hook(self):
    store.load()
    # Register persistent views so panel buttons survive restarts.
    self.add_view(NumberControlView())
    self.add_view(RaceControlView())
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
            type=discord.ActivityType.playing,
            name="/number • /race",
        )
    )

  async def close(self):
    store.save()
    if self.web_runner:
      await self.web_runner.cleanup()
    await super().close()


bot = EventBot()


# ---------------------------------------------------------
# RENDER KEEP-ALIVE SERVER
# ---------------------------------------------------------


async def handle_ping(request: web.Request) -> web.Response:
  return web.json_response({
      "status": "ok",
      "bot": str(bot.user) if bot.user else "starting",
      "number_events": store.count("number"),
      "race_events": store.count("race"),
  })


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
# COMMAND GROUP — /number
# ---------------------------------------------------------

number_group = app_commands.Group(
    name="number",
    description="Guess-the-number giveaway",
    guild_only=True,
)


def resolve_target(interaction: discord.Interaction, channel):
  return channel or interaction.channel


@number_group.command(name="start", description="Start a number guessing event")
@app_commands.describe(channel="Channel to run the event in")
@app_commands.checks.has_permissions(manage_messages=True)
async def number_start(
    interaction: discord.Interaction,
    channel: discord.TextChannel | None = None,
):
  target = resolve_target(interaction, channel)
  if not isinstance(target, discord.TextChannel):
    await interaction.response.send_message(
        "❌ Pick a normal text channel.", ephemeral=True
    )
    return
  if store.get("number", target.id):
    await interaction.response.send_message(
        f"⚠️ A number event is already running in {target.mention}.",
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
  await interaction.response.send_modal(NumberSetupModal(target))


@number_group.command(name="status", description="Show live number event stats")
async def number_status(interaction: discord.Interaction):
  session = store.get("number", interaction.channel_id)
  if not session:
    await interaction.response.send_message(
        "No number event is running in this channel.", ephemeral=True
    )
    return
  await interaction.response.send_message(
      embed=number_status_embed(session, interaction.guild), ephemeral=True
  )


@number_group.command(
    name="cancel", description="Cancel the number event and reveal the answer"
)
async def number_cancel(interaction: discord.Interaction):
  session = store.get("number", interaction.channel_id)
  if not session:
    await interaction.response.send_message(
        "No number event is running in this channel.", ephemeral=True
    )
    return
  if not is_host_or_admin(interaction.user, session.host_id):
    await interaction.response.send_message(
        "⛔ Only the host or a server admin can cancel this event.",
        ephemeral=True,
    )
    return
  store.remove("number", session.channel_id)
  await interaction.response.send_message(
      embed=number_cancel_embed(session, interaction.user)
  )


@number_group.command(
    name="reveal", description="Privately re-check the secret number (host)"
)
async def number_reveal(interaction: discord.Interaction):
  session = store.get("number", interaction.channel_id)
  if not session:
    await interaction.response.send_message(
        "No number event is running in this channel.", ephemeral=True
    )
    return
  if not is_host_or_admin(interaction.user, session.host_id):
    await interaction.response.send_message(
        "⛔ Only the host or a server admin can do that.", ephemeral=True
    )
    return
  await interaction.response.send_message(
      f"🔒 The secret number is **{fmt(session.target)}**. Only you can see"
      " this.",
      ephemeral=True,
  )


# ---------------------------------------------------------
# COMMAND GROUP — /race
# ---------------------------------------------------------

race_group = app_commands.Group(
    name="race",
    description="First person to send an exact message wins",
    guild_only=True,
)


@race_group.command(name="start", description="Start a speed message event")
@app_commands.describe(channel="Channel to run the event in")
@app_commands.checks.has_permissions(manage_messages=True)
async def race_start(
    interaction: discord.Interaction,
    channel: discord.TextChannel | None = None,
):
  target = resolve_target(interaction, channel)
  if not isinstance(target, discord.TextChannel):
    await interaction.response.send_message(
        "❌ Pick a normal text channel.", ephemeral=True
    )
    return
  if store.get("race", target.id):
    await interaction.response.send_message(
        f"⚠️ A speed event is already running in {target.mention}.",
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
  await interaction.response.send_modal(RaceSetupModal(target))


@race_group.command(name="status", description="Show claimed spots and winners")
async def race_status(interaction: discord.Interaction):
  session = store.get("race", interaction.channel_id)
  if not session:
    await interaction.response.send_message(
        "No speed event is running in this channel.", ephemeral=True
    )
    return
  await interaction.response.send_message(
      embed=race_status_embed(session, interaction.guild), ephemeral=True
  )


@race_group.command(name="cancel", description="Cancel the speed event")
async def race_cancel(interaction: discord.Interaction):
  session = store.get("race", interaction.channel_id)
  if not session:
    await interaction.response.send_message(
        "No speed event is running in this channel.", ephemeral=True
    )
    return
  if not is_host_or_admin(interaction.user, session.host_id):
    await interaction.response.send_message(
        "⛔ Only the host or a server admin can cancel this event.",
        ephemeral=True,
    )
    return
  store.remove("race", session.channel_id)
  await interaction.response.send_message(
      embed=race_cancel_embed(session, interaction.user)
  )


bot.tree.add_command(number_group)
bot.tree.add_command(race_group)


@bot.tree.command(name="help", description="How both event modes work")
async def help_cmd(interaction: discord.Interaction):
  embed = discord.Embed(
      title="🎮 Giveaway Events",
      description="Two independent modes. Both can run in the same channel.",
      color=COLOR_BLURPLE,
  )
  embed.add_field(
      name="🎯 /number — Guess the Number",
      value=(
          "`/number start [channel]` — setup panel *(Manage Messages)*\n"
          "`/number status` — range, guess count, players\n"
          "`/number reveal` — re-check the secret privately *(host)*\n"
          "`/number cancel` — end and reveal the answer\n"
          "*Unlimited guesses, first exact hit wins. Random or manual secret,"
          " optional 🔼/🔽 hint reactions.*"
      ),
      inline=False,
  )
  embed.add_field(
      name="⚡ /race — Speed Message",
      value=(
          "`/race start [channel]` — setup panel *(Manage Messages)*\n"
          "`/race status` — claimed spots and winners\n"
          "`/race cancel` — end early\n"
          "*First 1-3 people to send an exact phrase win, each with its own"
          " reward. Case and spacing tolerant, one win per person.*"
      ),
      inline=False,
  )
  embed.set_footer(text="Live events survive a bot restart")
  await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------
# GAME LOGIC — race-safe claims
# ---------------------------------------------------------


async def try_number_guess(
    session: NumberSession, user_id: int, guess: int
) -> bool:
  """Count the guess and report whether it won. Locked so simultaneous
  messages can never produce two winners."""
  async with session.lock:
    if not session.is_active:
      return False
    session.total_attempts += 1
    session.players.add(user_id)
    if guess == session.target:
      session.is_active = False
      store.remove("number", session.channel_id)
      return True
    if session.total_attempts % 25 == 0:
      # Periodic persistence instead of a disk write per guess.
      store.save()
    return False


async def claim_race_spot(
    session: RaceSession, user_id: int
) -> tuple[int | None, bool]:
  """Atomically claim the next place. Returns (place, event_finished)."""
  async with session.lock:
    if not session.is_active or session.remaining == 0:
      return None, False
    if user_id in session.winners or user_id == session.host_id:
      return None, False
    session.winners.append(user_id)
    place = len(session.winners)
    finished = session.remaining == 0
    if finished:
      session.is_active = False
      store.remove("race", session.channel_id)
    else:
      store.save()
    return place, finished


# ---------------------------------------------------------
# MESSAGE MONITOR
# ---------------------------------------------------------


@bot.event
async def on_message(message: discord.Message):
  if message.author.bot or not message.guild:
    return

  handled = await handle_race(message) or await handle_number(message)
  if not handled:
    await bot.process_commands(message)


async def handle_race(message: discord.Message) -> bool:
  session = store.get("race", message.channel.id)
  if not session or not session.is_active:
    return False
  if normalize(message.content) != session.phrase_key:
    return False

  place, finished = await claim_race_spot(session, message.author.id)
  if place is None:
    return False

  try:
    await message.add_reaction(MEDALS.get(place, "✅"))
  except discord.HTTPException:
    pass
  try:
    await message.reply(
        f"⚡ **{PLACE_LABELS.get(place, f'#{place}')} place!**"
        f" {message.author.mention} wins **{session.reward_for(place)}**.",
        mention_author=False,
    )
    if finished:
      await message.channel.send(
          content=(
              f"🔔 <@{session.host_id}> — **all winners are in.** Time to hand"
              " out the prizes."
          ),
          embed=race_final_embed(session),
      )
  except discord.HTTPException as exc:
    logger.warning("Could not announce race winner: %s", exc)
  return True


async def handle_number(message: discord.Message) -> bool:
  session = store.get("number", message.channel.id)
  if not session or not session.is_active:
    return False

  guess = parse_int(message.content.strip())
  if guess is None or not session.min_val <= guess <= session.max_val:
    return False

  won = await try_number_guess(session, message.author.id, guess)
  if won:
    try:
      await message.add_reaction("🎉")
    except discord.HTTPException:
      pass
    try:
      await message.channel.send(
          content=f"🔔 <@{session.host_id}> — **we have a winner!**",
          embed=number_win_embed(session, message.author, guess),
      )
    except discord.HTTPException as exc:
      logger.warning("Could not announce number winner: %s", exc)
    return True

  if session.hints and session.hint_ready():
    try:
      await message.add_reaction("🔼" if guess < session.target else "🔽")
    except discord.HTTPException:
      pass
  return True


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
