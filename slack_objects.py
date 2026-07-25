"""Normalising Slack payloads into things a person can read.

Slack's wire format is machine-first in three ways that matter here:

1. TIMESTAMPS ARE IDENTITY. A message has no id -- it is identified by `ts`, an
   epoch-with-microseconds STRING like "1690000000.123456". It must stay a
   string: parsing it into a float and formatting it back loses precision and
   produces a ts Slack no longer recognises, so replies land in the wrong place
   or not at all. Every helper here passes `ts` through verbatim and derives a
   human date SEPARATELY.

2. TEXT IS MARKED UP. Message bodies contain `<@U024BE7LH>` for mentions,
   `<#C024BE7LR|general>` for channels, `<http://example.com|label>` for links
   and HTML entities (`&amp;`, `&lt;`, `&gt;`). Handing that to a user (or to a
   model summarising a channel) as-is is barely readable, so `render_text`
   resolves it.

3. NAMES LIVE ELSEWHERE. A mention carries only a user id, so rendering needs a
   lookup table. `render_text` accepts one and DEGRADES GRACEFULLY when a name
   is unknown -- it never blocks on fetching, and never invents a name.
"""

from __future__ import annotations

import datetime as _dt
import re

# <@U123>, <@U123|name>, <#C123>, <#C123|general>, <!here>, <!channel>,
# <!subteam^S123|@team>, <http://url>, <http://url|label>, <mailto:a@b|a@b>
_ENTITY_RE = re.compile(r"<([^<>]+)>")


def humanize_ts(ts: str) -> str:
    """Turn a Slack ts into 'YYYY-MM-DD HH:MM' UTC, or '' if unparseable.

    Purely for DISPLAY -- the original string is what gets sent back to Slack.
    A malformed ts yields an empty string rather than an exception: a message
    with an odd timestamp should still be readable.
    """
    raw = (ts or "").strip()
    if not raw:
        return ""
    try:
        seconds = float(raw.split(".")[0])
    except (ValueError, IndexError):
        return ""
    try:
        moment = _dt.datetime.fromtimestamp(seconds, tz=_dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    return moment.strftime("%Y-%m-%d %H:%M")


def _unescape(text: str) -> str:
    """Reverse Slack's three HTML escapes.

    Order matters: &amp; must come LAST, otherwise "&amp;lt;" would first
    become "&lt;" and then be wrongly decoded to "<".
    """
    return (text.replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&amp;", "&"))


def render_text(text: str, users: dict | None = None,
                channels: dict | None = None) -> str:
    """Resolve Slack markup into readable prose.

    `users` and `channels` map id -> display name. Both are optional: an
    unknown id renders as a readable placeholder rather than a raw token, and
    nothing here ever fetches or guesses a name.
    """
    if not text:
        return ""

    user_map = users or {}
    channel_map = channels or {}

    def _replace(match: re.Match) -> str:
        inner = match.group(1)

        # Broadcast pings and special commands: <!here>, <!channel>,
        # <!subteam^S12345|@designers>
        if inner.startswith("!"):
            body = inner[1:]
            if "|" in body:
                return "@" + body.split("|", 1)[1].lstrip("@")
            return "@" + body.split("^")[0]

        # User mention: <@U123> or <@U123|legacy_name>
        if inner.startswith("@"):
            body = inner[1:]
            if "|" in body:
                uid, label = body.split("|", 1)
                return "@" + (label or user_map.get(uid, uid))
            return "@" + user_map.get(body, body)

        # Channel link: <#C123> or <#C123|general>
        if inner.startswith("#"):
            body = inner[1:]
            if "|" in body:
                cid, label = body.split("|", 1)
                return "#" + (label or channel_map.get(cid, cid))
            return "#" + channel_map.get(body, body)

        # Link with a label: <http://example.com|see this> -> "see this
        # (http://example.com)". The URL is KEPT because a summary that drops
        # the link loses the only actionable part of many Slack messages.
        if "|" in inner:
            target, label = inner.split("|", 1)
            if target.startswith("mailto:"):
                return label or target[len("mailto:"):]
            return f"{label} ({target})" if label else target

        if inner.startswith("mailto:"):
            return inner[len("mailto:"):]
        return inner

    return _unescape(_ENTITY_RE.sub(_replace, text))


def message_text(message: dict, users: dict | None = None,
                 channels: dict | None = None) -> str:
    """Best-effort readable text for one message.

    Falls back through Slack's layers: plain `text`, then attachment
    fallbacks, then block-kit text. A bot post built entirely from blocks has
    an EMPTY `text` field, so reading only `text` would render whole channels
    of app notifications as blank lines.
    """
    direct = render_text(str(message.get("text") or ""), users, channels)
    if direct.strip():
        return direct

    for attachment in message.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        for key in ("fallback", "text", "title", "pretext"):
            value = str(attachment.get(key) or "")
            if value.strip():
                return render_text(value, users, channels)

    collected: list[str] = []
    for block in message.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        text_node = block.get("text")
        if isinstance(text_node, dict):
            value = str(text_node.get("text") or "")
            if value.strip():
                collected.append(value)
        for field in block.get("fields") or []:
            if isinstance(field, dict):
                value = str(field.get("text") or "")
                if value.strip():
                    collected.append(value)
    if collected:
        return render_text("\n".join(collected), users, channels)

    if message.get("files"):
        names = [str(f.get("name") or "file")
                 for f in message["files"] if isinstance(f, dict)]
        if names:
            return f"[shared {', '.join(names)}]"

    return ""


def author_of(message: dict, users: dict | None = None) -> str:
    """Who wrote a message: a person, an app, or an honest blank.

    Bot posts often have NO `user` field at all -- only `bot_id` plus an
    optional `username`. Reading only `user` would attribute half of a busy
    channel to nobody.
    """
    user_map = users or {}
    uid = str(message.get("user") or "")
    if uid:
        return user_map.get(uid, uid)
    username = str(message.get("username") or "")
    if username:
        return username
    bot_id = str(message.get("bot_id") or "")
    if bot_id:
        return f"app ({bot_id})"
    return ""


def channel_name(channel: dict) -> str:
    """Human label for a conversation, including DMs.

    A DM has no `name` -- it is `im: true` with a `user` id, so it needs a
    different label or it renders as an empty row.
    """
    name = str(channel.get("name") or "")
    if name:
        return name
    if channel.get("is_im"):
        peer = str(channel.get("user") or "")
        return f"DM with {peer}" if peer else "DM"
    if channel.get("is_mpim"):
        return "group DM"
    return str(channel.get("id") or "")


def channel_kind(channel: dict) -> str:
    """Classify a conversation the way a user would describe it."""
    if channel.get("is_im"):
        return "dm"
    if channel.get("is_mpim"):
        return "group_dm"
    if channel.get("is_private"):
        return "private_channel"
    return "public_channel"


def user_display_name(user: dict) -> str:
    """Pick the name a human would recognise.

    Slack has four competing name fields; `display_name` is what the person
    chose to be called, so it wins, with documented fallbacks.
    """
    profile = user.get("profile")
    if isinstance(profile, dict):
        for key in ("display_name_normalized", "display_name", "real_name"):
            value = str(profile.get(key) or "")
            if value.strip():
                return value
    for key in ("real_name", "name"):
        value = str(user.get(key) or "")
        if value.strip():
            return value
    return str(user.get("id") or "")


def user_name_map(members: list) -> dict:
    """id -> display name, for resolving mentions in message text."""
    mapping: dict = {}
    for member in members or []:
        if isinstance(member, dict) and member.get("id"):
            mapping[str(member["id"])] = user_display_name(member)
    return mapping


def channel_name_map(channels: list) -> dict:
    """id -> channel name, for resolving channel links in message text."""
    mapping: dict = {}
    for channel in channels or []:
        if isinstance(channel, dict) and channel.get("id"):
            mapping[str(channel["id"])] = channel_name(channel)
    return mapping


def permalink_of(message: dict) -> str:
    """Slack's permalink when the endpoint supplied one, else ''."""
    return str(message.get("permalink") or "")


def thread_info(message: dict) -> tuple[str, int]:
    """(thread parent ts, reply count) -- ('' , 0) for a standalone message.

    Slack marks a thread PARENT with `thread_ts == ts`, and a REPLY with a
    `thread_ts` pointing at the parent. Callers need that distinction to avoid
    reporting every reply as its own thread.
    """
    thread_ts = str(message.get("thread_ts") or "")
    replies = int(message.get("reply_count") or 0)
    return thread_ts, replies


def reactions_of(message: dict) -> str:
    """Compact ':thumbsup: 3, :eyes: 1' summary, or '' when there are none."""
    parts: list[str] = []
    for reaction in message.get("reactions") or []:
        if not isinstance(reaction, dict):
            continue
        name = str(reaction.get("name") or "")
        count = int(reaction.get("count") or 0)
        if name:
            parts.append(f":{name}: {count}" if count else f":{name}:")
    return ", ".join(parts)


def normalize_channel_ref(value: str) -> str:
    """Accept '#general', 'general', 'C123' or a Slack link; return a lookup key.

    Users paste all four shapes. Stripping the decoration here means every
    tool accepts every shape without repeating the logic.
    """
    raw = (value or "").strip()
    if raw.startswith("<#") and raw.endswith(">"):
        inner = raw[2:-1]
        raw = inner.split("|")[0]
    return raw.lstrip("#").strip()


def looks_like_channel_id(value: str) -> bool:
    """True for a Slack conversation id (C…/G…/D… + uppercase alphanumerics).

    Guard, not validation: it decides whether to skip a name lookup, so it is
    deliberately strict about shape and never invents an id.
    """
    raw = (value or "").strip()
    if len(raw) < 9 or raw[0] not in "CGD":
        return False
    return all(ch.isdigit() or (ch.isalpha() and ch.isupper()) for ch in raw)


def looks_like_user_id(value: str) -> bool:
    """True for a Slack user id (U… or W… + uppercase alphanumerics)."""
    raw = (value or "").strip()
    if len(raw) < 9 or raw[0] not in "UW":
        return False
    return all(ch.isdigit() or (ch.isalpha() and ch.isupper()) for ch in raw)


def normalize_user_ref(value: str) -> str:
    """Accept '@vlad', 'vlad', 'U123' or '<@U123>'; return a lookup key."""
    raw = (value or "").strip()
    if raw.startswith("<@") and raw.endswith(">"):
        raw = raw[2:-1].split("|")[0]
    return raw.lstrip("@").strip()


def normalize_emoji(value: str) -> str:
    """Slack wants a reaction name WITHOUT colons: ':eyes:' -> 'eyes'."""
    return (value or "").strip().strip(":").strip()
