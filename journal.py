"""The message journal: what Slack said, kept where Webbee can still see it.

WHY THIS EXISTS AT ALL.

The inbound endpoint does everything correctly -- verifies the signature,
de-duplicates, normalises, enriches -- and then hands the message to
`ctx.extensions.emit`. That call is the ONLY consumer. If nothing is listening,
the message is gone: no row, no log line with the text, nothing to look at
later. Awareness that lives exclusively inside an emit is awareness that
disappears the instant delivery stops working.

And delivery IS currently not working, for two independent reasons that were
both verified live:

  1. the signing secret is not set, so every delivery is refused; and
  2. the four inbound event names this app declares are NOT in the platform's
     automations catalog -- creating a rule on them fails with
     "Event 'slack-connector.app_mentioned' not found". Only OUTBOUND events
     ("the assistant sent a message") are registered.

Fixing (1) is a paste. (2) is not ours to fix. So an emit-only design cannot
answer "is Webbee aware of messages?" with yes under any amount of user effort.

The journal makes awareness a STORED FACT instead of a hoped-for side effect:

  * the webhook writes to it BEFORE emitting, so a push delivery is remembered
    even when nothing is subscribed and even if the emit throws;
  * `catch_up` fills it by POLLING the conversations the app can reach, which
    needs no signing secret, no automations slot and no platform fix -- it
    works today;
  * both paths write the SAME shape through the SAME function, so a journalled
    message is indistinguishable regardless of how it arrived. Two code paths
    producing two subtly different notions of "a message" is how the same
    question starts getting two different answers.

WHAT IT DELIBERATELY DOES NOT DO. It does not reply, react, or notify. A
journal that answers people would turn a read-only awareness feature into an
autonomous poster the user never asked for -- and the sweep runs over history,
so a bug there would answer old messages in bulk. Reading and acting stay
separate.
"""

from __future__ import annotations

import time

import inbound
import slack_objects as so

# One row per message. Separate from the dedupe ledger (`slack_seen_events`)
# because the two have opposite lifetimes: the ledger is a short-lived
# anti-replay cache that MUST expire, the journal is the record we want to keep.
JOURNAL_COLLECTION = "slack_message_journal"

# Where the last swept message stands, per conversation. Separate from
# `slack_channel_context` (which records where to REPLY) because a sweep cursor
# is bookkeeping about reading, not about answering: overloading one row would
# make a sweep silently move the reply target.
CURSOR_COLLECTION = "slack_sweep_cursor"

# The store has no TTL and no server-side count, so the journal is bounded
# here. 2000 is generous for "what happened recently" while keeping a full read
# cheap enough to sort in Python.
JOURNAL_MAX_ROWS = 2000

# How much to prune per pass. A bounded prune keeps the cost of one sweep
# bounded too; the next sweep continues trimming if there is still excess.
PRUNE_BATCH = 200

# Sweep limits. Both are hard stops: a workspace with hundreds of channels must
# not turn one tool call into an unbounded crawl of Slack.
MAX_CONVERSATIONS = 60
MAX_MESSAGES_PER_CONVERSATION = 50

SOURCE_PUSH = "push"
SOURCE_SWEEP = "sweep"

# The sweep interval, defined ONCE. The schedule decorator and the status report
# both read it from here: a report that hardcoded its own copy could claim an
# interval the schedule does not actually use, and that kind of lie survives
# every test because nothing ever compares the two strings.
#
# EVERY TEN MINUTES, not hourly. Hourly was right while this schedule only
# COLLECTED messages -- an archive an hour behind is still a usable archive. It
# stopped being right the moment replying rode along: someone writes to Webbee
# and waits up to 59 minutes for an answer, which reads as being ignored, and
# "she answers when you ask her" is not true at that latency.
#
# Ten and not one: this polls somebody else's API forever. Six passes an hour
# over the reachable conversations stays far inside Slack's limits (the cursor
# makes an idle pass nearly free -- it reads and stops), while a minute-by-minute
# crawl would spend sixty times the calls to shave off seconds nobody notices in
# a chat conversation.
#
# The offset is kept off the hour boundary on purpose: :00 is when every
# scheduled job everywhere fires at once.
SWEEP_CRON = "*/10 * * * *"


def message_key(channel_id: str, message_ts: str) -> str:
    """Stable identity of one message.

    Slack's `ts` is unique only WITHIN a conversation, so the channel has to be
    part of the key. Keying on ts alone would make two messages posted in the
    same microsecond in different channels collide -- rare, but the failure is
    a silently dropped message, which is exactly what this module exists to
    prevent.

    `event_id` is NOT used: it exists only on push deliveries, so a sweep and a
    push of the SAME message would produce two different keys and the message
    would be journalled twice.
    """
    return f"{channel_id}:{message_ts}"


async def _find(ctx, collection: str, field: str, value: str):
    """First document whose `field` equals `value`, or None."""
    try:
        page = await ctx.store.query(collection, where={field: value}, limit=1)
    except Exception:
        return None
    rows = getattr(page, "data", None) or []
    return rows[0] if rows else None


async def _all(ctx, collection: str, limit: int = JOURNAL_MAX_ROWS,
               where: dict | None = None) -> list:
    """Every document in a collection, up to `limit`.

    Ordering is done by the CALLER in Python, not with `order_by`. The
    in-memory store double accepts `order_by` and ignores it, so a test would
    pass while production returned a different order -- the worst kind of green
    test. Sorting here behaves identically against both.
    """
    try:
        page = await ctx.store.query(collection, where=where or {}, limit=limit)
    except Exception:
        return []
    return list(getattr(page, "data", None) or [])


def _row(doc) -> dict:
    """The data dict of a store document, tolerating a bare dict."""
    if isinstance(doc, dict):
        return doc
    return getattr(doc, "data", None) or {}


def _sort_key(row: dict) -> float:
    """Newest-first ordering key.

    Slack's ts IS the epoch seconds of the message, so it sorts correctly as a
    float and needs no separate clock. `at` (when WE stored it) is the
    fallback, because a swept backlog is all recorded at the same instant and
    would otherwise collapse into arbitrary order.
    """
    try:
        return float(row.get("message_ts") or 0) or float(row.get("at") or 0)
    except (TypeError, ValueError):
        try:
            return float(row.get("at") or 0)
        except (TypeError, ValueError):
            return 0.0


async def already_journalled(ctx, channel_id: str, message_ts: str) -> bool:
    """True when this exact message is already recorded.

    A store failure returns False -- fail towards RECORDING. A duplicate row is
    visible and harmless; a message dropped because the dedupe lookup blipped
    is invisible, and invisible loss is the failure this module exists to
    prevent.
    """
    key = message_key(channel_id, message_ts)
    if not channel_id or not message_ts:
        return False
    doc = await _find(ctx, JOURNAL_COLLECTION, "message_key", key)
    return doc is not None


async def record(ctx, normalised: dict, *, source: str) -> bool:
    """Write one normalised message into the journal.

    Returns True when a row was created, False when it was a duplicate or
    could not be stored. NEVER raises: the webhook calls this on the delivery
    path, where an exception would turn "we could not file this message" into
    "Slack got a 500 and will retry the whole thing three more times".
    """
    channel_id = str(normalised.get("channel_id") or "")
    message_ts = str(normalised.get("message_ts") or "")
    if not channel_id or not message_ts:
        return False

    if await already_journalled(ctx, channel_id, message_ts):
        return False

    row = {
        "message_key": message_key(channel_id, message_ts),
        "source": source,
        "at": time.time(),
        # Identity of the conversation and the speaker.
        "workspace_id": str(normalised.get("workspace_id") or ""),
        "workspace_name": str(normalised.get("workspace_name") or ""),
        "channel_id": channel_id,
        "channel_name": str(normalised.get("channel_name") or ""),
        "channel_type": str(normalised.get("channel_type") or ""),
        "is_dm": bool(normalised.get("is_dm")),
        "user_id": str(normalised.get("user_id") or ""),
        "user_display_name": str(normalised.get("user_display_name") or ""),
        # What was said. Both forms are kept: `text` is what Slack sent (raw
        # <@U…> mentions and all), `text_readable` is the rendered version.
        # Keeping only the rendered one would lose the ability to test a rule
        # against the literal payload.
        "text": str(normalised.get("text") or ""),
        "text_readable": str(normalised.get("text_readable")
                             or normalised.get("text") or ""),
        # Everything needed to REPLY without re-deriving Slack's thread quirks.
        "message_ts": message_ts,
        "thread_ts": str(normalised.get("thread_ts") or ""),
        "reply_thread_ts": str(normalised.get("reply_thread_ts") or message_ts),
        "is_thread_reply": bool(normalised.get("is_thread_reply")),
        "mention_of_bot": bool(normalised.get("mention_of_bot")),
        "has_files": bool(normalised.get("has_files")),
        "permalink": str(normalised.get("permalink") or ""),
        "posted_at": str(normalised.get("received_at")
                         or so.humanize_ts(message_ts)),
        # Which Imperal events this message WOULD raise. Stored so the journal
        # answers "would a mention rule have fired?" even though the catalog
        # currently has nowhere for that rule to live.
        "event_names": ",".join(inbound.classify(normalised)),
    }

    try:
        await ctx.store.create(JOURNAL_COLLECTION, row)
    except Exception:
        try:
            await ctx.log("Slack message could not be journalled", level="warn")
        except Exception:
            pass
        return False
    return True


async def prune(ctx, keep: int = JOURNAL_MAX_ROWS) -> int:
    """Drop the oldest rows above `keep`. Returns how many were removed."""
    rows = await _all(ctx, JOURNAL_COLLECTION, limit=keep + PRUNE_BATCH + 1)
    if len(rows) <= keep:
        return 0

    ordered = sorted(rows, key=lambda d: _sort_key(_row(d)))
    excess = ordered[:len(rows) - keep][:PRUNE_BATCH]

    removed = 0
    for doc in excess:
        doc_id = getattr(doc, "id", "")
        if not doc_id:
            continue
        try:
            await ctx.store.delete(JOURNAL_COLLECTION, doc_id)
            removed += 1
        except Exception:
            continue
    return removed


async def recent(ctx, *, limit: int = 30, channel_id: str = "",
                 dms_only: bool = False, mentions_only: bool = False,
                 unresolved_only: bool = False) -> list[dict]:
    """Journalled messages, newest first, filtered in Python.

    Filtering happens here rather than in `where` because the store double
    supports equality only, and combining several equality filters server-side
    would still not express "mentions OR dms". Reading a bounded collection and
    filtering in memory is the honest version and behaves the same everywhere.
    """
    rows = [_row(d) for d in await _all(ctx, JOURNAL_COLLECTION)]

    if channel_id:
        rows = [r for r in rows if str(r.get("channel_id") or "") == channel_id]
    if dms_only:
        rows = [r for r in rows if r.get("is_dm")]
    if mentions_only:
        rows = [r for r in rows if r.get("mention_of_bot")]
    if unresolved_only:
        rows = [r for r in rows if not r.get("replied")]

    rows.sort(key=_sort_key, reverse=True)
    return rows[:max(1, limit)]


async def counts(ctx) -> dict:
    """Totals for the status panel: how much is remembered, and of what kind."""
    rows = [_row(d) for d in await _all(ctx, JOURNAL_COLLECTION)]
    return {
        "total": len(rows),
        "dms": sum(1 for r in rows if r.get("is_dm")),
        "mentions": sum(1 for r in rows if r.get("mention_of_bot")),
        "threads": sum(1 for r in rows if r.get("is_thread_reply")),
        "from_push": sum(1 for r in rows if r.get("source") == SOURCE_PUSH),
        "from_sweep": sum(1 for r in rows if r.get("source") == SOURCE_SWEEP),
        "channels": len({str(r.get("channel_id") or "") for r in rows
                         if r.get("channel_id")}),
        "newest_at": max((_sort_key(r) for r in rows), default=0.0),
    }


# --- sweep bookkeeping -------------------------------------------------------

async def cursor_for(ctx, channel_id: str) -> str:
    """The ts of the newest message already swept from this conversation."""
    if not channel_id:
        return ""
    doc = await _find(ctx, CURSOR_COLLECTION, "channel_id", channel_id)
    if doc is None:
        return ""
    return str(_row(doc).get("last_ts") or "")


async def set_cursor(ctx, channel_id: str, last_ts: str) -> None:
    """Advance the sweep cursor. Never raises: a lost cursor costs re-reading.

    Deliberately only ever moves FORWARD. If a later sweep saw an older ts
    (Slack returning an unexpected order, a clock oddity), rewinding the cursor
    would re-journal a backlog; the dedupe key would absorb it, but the sweep
    would do that work on every single call from then on.
    """
    if not channel_id or not last_ts:
        return
    try:
        current = await cursor_for(ctx, channel_id)
        if current and float(current) >= float(last_ts):
            return
    except (TypeError, ValueError):
        pass

    data = {"channel_id": channel_id, "last_ts": last_ts, "at": time.time()}
    try:
        doc = await _find(ctx, CURSOR_COLLECTION, "channel_id", channel_id)
        if doc is not None and getattr(doc, "id", ""):
            await ctx.store.update(CURSOR_COLLECTION, doc.id, data)
        else:
            await ctx.store.create(CURSOR_COLLECTION, data)
    except Exception:
        try:
            await ctx.log("Slack sweep cursor could not be saved", level="warn")
        except Exception:
            pass


def is_reachable(conversation: dict) -> tuple[bool, str]:
    """Whether the app can read this conversation's history, and why not.

    THE DM CORRECTION. `conversations.list` reports `is_member: false` for
    DMs -- proven live: the DM with the account owner came back as a non-member
    and its history read fine, before and after posting into it. Slack simply
    does not model "membership" for a DM with a bot: if the conversation exists
    in the app's list, the app is one of its two participants by definition.

    Treating `is_member` as authoritative would therefore skip every DM and
    report "no access" for the one place the app unconditionally HAS access --
    and the old guidance even told the user to `/invite` the app into a DM,
    which Slack offers no way to do. Channels are the opposite: for those
    `is_member` is real, and reading without it fails with not_in_channel.
    """
    if not isinstance(conversation, dict):
        return False, "not a conversation object"

    channel_id = str(conversation.get("id") or "")
    kind = so.channel_kind(conversation)

    if conversation.get("is_archived"):
        return False, "archived"
    if kind in ("dm", "group_dm") or channel_id.startswith(("D", "G")):
        return True, ""
    if conversation.get("is_member"):
        return True, ""
    return False, "the app is not in this channel"


def history_to_normalised(message: dict, *, conversation: dict,
                          workspace: dict, self_user_id: str,
                          users: dict | None = None,
                          channels: dict | None = None) -> dict:
    """Turn a `conversations.history` row into the SAME shape as a push event.

    A history row is nearly an event payload but misses the two fields the
    envelope would have carried: `channel` and `channel_type`. Rather than
    duplicating `normalise` (and drifting from it), those are filled in and the
    real function does the work -- so thread detection, DM detection and
    mention detection are literally the same code for both paths.
    """
    channel_id = str(conversation.get("id") or "")
    kind = so.channel_kind(conversation)
    channel_type = {
        "dm": "im", "group_dm": "mpim",
        "private": "group", "public": "channel",
    }.get(kind, "channel")

    synthetic = dict(message)
    synthetic["type"] = "message"
    synthetic["channel"] = channel_id
    synthetic["channel_type"] = channel_type

    normalised = inbound.normalise(
        synthetic, {"event_id": "", "team_id": workspace.get("workspace_id", "")},
        workspace=workspace, self_user_id=self_user_id)

    # Enrichment that the push path does over the network; here the maps are
    # already in hand from the sweep, so it costs nothing.
    normalised["channel_name"] = (
        so.channel_name(conversation)
        or (channels or {}).get(channel_id, "")
        or channel_id)
    normalised["user_display_name"] = so.author_of(message, users or {})
    normalised["text_readable"] = so.message_text(message, users or {},
                                                  channels or {})
    normalised["permalink"] = so.permalink_of(message)
    return normalised


# --- reply bookkeeping -------------------------------------------------------

async def mark_replied(ctx, channel_id: str, message_ts: str, *,
                       reply_ts: str = "") -> bool:
    """Record that this message has been answered. True if a row was updated.

    THIS IS THE LOOP GUARD, not a nicety. `recent(unresolved_only=True)` has
    always filtered on `replied`, but nothing ever WROTE the field -- so the
    filter passed everything through and "messages with no reply yet" meant
    "every message". A scheduled rule built on that would answer the same
    person on every single run: an hourly stranger repeating itself in someone
    else's Slack. The failure is not a wrong answer, it is harassment.

    Keyed on (channel_id, message_ts) -- the same identity the dedupe key uses.
    A ts alone is not unique across conversations.

    Never raises. If the mark cannot be saved the caller has already sent its
    reply, and crashing afterwards would turn a bookkeeping problem into a
    visible failure. It is logged instead, and the worst case is one repeated
    answer rather than a lost one.
    """
    if not channel_id or not message_ts:
        return False

    key = message_key(channel_id, message_ts)
    try:
        doc = await _find(ctx, JOURNAL_COLLECTION, "message_key", key)
        if doc is None or not getattr(doc, "id", ""):
            return False
        data = dict(_row(doc))
        data["replied"] = True
        data["replied_at"] = time.time()
        if reply_ts:
            data["reply_ts"] = reply_ts
        await ctx.store.update(JOURNAL_COLLECTION, doc.id, data)
        return True
    except Exception:
        try:
            await ctx.log("Slack reply mark could not be saved", level="warn")
        except Exception:
            pass
        return False


async def mark_thread_replied(ctx, channel_id: str, thread_ts: str, *,
                              reply_ts: str = "") -> int:
    """Mark every journalled message of one thread as answered. Returns count.

    A reply is addressed to a THREAD, not to a row: Slack's `thread_ts` names
    the parent, while the journal stores each message under its own ts. Marking
    only the row whose ts equals thread_ts would leave a top-level message that
    was answered in a thread still looking unanswered -- and the loop guard
    would answer it again on the next run.

    Bounded scan in Python, like `recent`: the store double supports equality
    only and cannot express "ts OR reply_thread_ts".
    """
    if not channel_id or not thread_ts:
        return 0

    marked = 0
    try:
        docs = await _all(ctx, JOURNAL_COLLECTION)
    except Exception:
        return 0

    for doc in docs:
        row = _row(doc)
        if str(row.get("channel_id") or "") != channel_id:
            continue
        if row.get("replied"):
            continue
        # Either the parent itself, or any message whose reply belongs in this
        # same thread.
        own_ts = str(row.get("message_ts") or "")
        target = str(row.get("reply_thread_ts") or own_ts)
        if thread_ts not in (own_ts, target):
            continue
        try:
            data = dict(row)
            data["replied"] = True
            data["replied_at"] = time.time()
            if reply_ts:
                data["reply_ts"] = reply_ts
            await ctx.store.update(JOURNAL_COLLECTION, doc.id, data)
            marked += 1
        except Exception:
            continue

    if not marked:
        return 0
    try:
        await ctx.log(f"Slack: marked {marked} message(s) as answered",
                      level="info")
    except Exception:
        pass
    return marked
