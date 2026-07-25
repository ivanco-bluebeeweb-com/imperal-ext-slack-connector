"""Inbound Slack Events: verify, de-duplicate, normalise.

This is the transport layer that turns Slack's push delivery into an Imperal
event. It is deliberately separate from the tools: a webhook runs with NO user
in context (`user_id="__webhook__"`), it must answer in milliseconds, and it is
reachable by anyone on the internet who knows the URL. None of that is true of
a chat tool, so none of that logic belongs next to one.

THE THREE HARD PARTS, and why each is done this way.

1. SIGNATURE VERIFICATION over the RAW body.
   Slack signs the bytes it sent. Re-serialising the parsed JSON and hashing
   that produces a different string -- key order and whitespace differ -- so
   every request would be rejected. The handler therefore hashes the body
   string EXACTLY as received, before json.loads touches it.

   The comparison uses hmac.compare_digest, not `==`. A plain comparison
   short-circuits on the first wrong byte, which leaks how much of a forged
   signature was correct and makes the secret guessable one byte at a time.

   Requests older than five minutes are refused even with a valid signature:
   without that window a captured request stays replayable forever.

2. DE-DUPLICATION by event_id.
   Slack retries an event if the endpoint does not answer 200 within three
   seconds, and marks the retry with X-Slack-Retry-Num. Retries are NOT rare --
   a slow cold start is enough. Without dedupe the same mention answers the
   user two or three times, which is the single most visible way an integration
   like this looks broken.

   Two layers, because they fail differently:
     * the retry HEADER is free and catches the common case instantly;
     * the event_id LEDGER in the store catches redelivery that arrives without
       a retry header, and survives a process restart.

3. NOISE SUPPRESSION.
   A bot that reacts to its own messages talks to itself forever -- in public,
   in the user's channel. So: no bot_id, no app_id-of-self, no message_changed
   or message_deleted subtypes, no join/leave chatter. The rule is allowlist,
   not denylist: an unrecognised subtype is IGNORED rather than forwarded,
   because a new Slack subtype should be silent by default, not a surprise
   broadcast.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import slack_objects as so

# --- constants ---------------------------------------------------------------

SIGNING_SECRET_NAME = "slack_signing_secret"

#: Slack's own replay window. Its docs specify five minutes; matching that
#: exactly means a legitimate request is never refused for being "too old"
#: while the endpoint stays closed to captured-and-replayed traffic.
MAX_REQUEST_AGE_SECONDS = 60 * 5

#: How long a processed event_id is remembered. Slack retries for roughly 30
#: minutes; an hour of memory covers that with room to spare, and the ledger is
#: pruned so it cannot grow without bound.
EVENT_LEDGER_TTL_SECONDS = 60 * 60

EVENTS_COLLECTION = "slack_seen_events"
CHANNELS_COLLECTION = "slack_channel_context"

#: Subtypes that are still a HUMAN MESSAGE. Slack sends `file_share` and
#: `thread_broadcast` as subtyped messages, and both carry real user text --
#: dropping every subtype would silently lose a user who attached a file.
HUMAN_SUBTYPES = {"", "file_share", "thread_broadcast"}

#: Emitted event types. MUST be app_id-prefixed: the SDK enforces a federal
#: cross-namespace block (M7.3), so a bare "slack.message_received" raises at
#: import. These are the names that appear in the automation rule builder.
EVENT_MESSAGE = "slack-connector.message_received"
EVENT_MENTION = "slack-connector.app_mentioned"
EVENT_THREAD_REPLY = "slack-connector.thread_reply_received"
EVENT_DM = "slack-connector.dm_received"


# --- signature verification --------------------------------------------------

def verify_signature(body: str, headers: dict, signing_secret: str,
                     now: float | None = None) -> dict:
    """Check Slack's request signature over the RAW body.

    Returns {"ok": True} or {"ok": False, "reason": ..., "code": ...}. Reasons
    are for the audit log, never for the caller: a forged request must not be
    told which check it failed.
    """
    if not signing_secret:
        return {"ok": False, "code": "SLACK_SIGNING_SECRET_MISSING",
                "reason": "no signing secret configured"}

    lower = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    timestamp = lower.get("x-slack-request-timestamp", "")
    signature = lower.get("x-slack-signature", "")
    if not timestamp or not signature:
        return {"ok": False, "code": "SLACK_SIGNATURE_MISSING",
                "reason": "signature headers absent"}

    try:
        sent_at = float(timestamp)
    except (TypeError, ValueError):
        return {"ok": False, "code": "SLACK_SIGNATURE_INVALID",
                "reason": "timestamp not a number"}

    current = time.time() if now is None else now
    if abs(current - sent_at) > MAX_REQUEST_AGE_SECONDS:
        return {"ok": False, "code": "SLACK_REQUEST_TOO_OLD",
                "reason": "outside the replay window"}

    # v0 is Slack's scheme version; the basestring shape is fixed by them.
    basestring = f"v0:{timestamp}:{body}".encode()
    digest = hmac.new(signing_secret.encode(), basestring,
                      hashlib.sha256).hexdigest()
    expected = f"v0={digest}"

    # compare_digest, not == : a short-circuiting comparison leaks how many
    # leading bytes of a forged signature were right.
    if not hmac.compare_digest(expected, signature):
        return {"ok": False, "code": "SLACK_SIGNATURE_INVALID",
                "reason": "signature mismatch"}
    return {"ok": True}


# --- de-duplication ----------------------------------------------------------

def is_retry(headers: dict) -> tuple[bool, str]:
    """Whether Slack flagged this delivery as a retry, and why it says so."""
    lower = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    num = lower.get("x-slack-retry-num", "")
    reason = lower.get("x-slack-retry-reason", "")
    return bool(num and num not in ("", "0")), reason


# --- store access ------------------------------------------------------------
#
# The key is stored as a FIELD and looked up with query(where=...), rather than
# used as the document id. `store.set("collection/key", ...)` looks like it
# gives key-addressed storage, but on a NEW key it falls through to create(),
# which assigns a RANDOM id -- so a later get(collection, key) never finds the
# row and dedupe silently never matches. Querying on a field behaves the same
# way against the real store and the in-memory test double.

async def _find(ctx, collection: str, field: str, value: str):
    """First document whose `field` equals `value`, or None."""
    try:
        page = await ctx.store.query(collection, where={field: value}, limit=1)
    except Exception:
        return None
    rows = getattr(page, "data", None) or []
    return rows[0] if rows else None


async def _upsert(ctx, collection: str, field: str, value: str,
                  data: dict) -> None:
    """Update the row identified by field==value, or create it."""
    existing = await _find(ctx, collection, field, value)
    if existing is not None and getattr(existing, "id", ""):
        await ctx.store.update(collection, existing.id, data)
        return
    await ctx.store.create(collection, data)


async def already_processed(ctx, event_id: str) -> bool:
    """True when this event_id was handled before.

    A store failure returns False -- fail OPEN. Answering a user twice because
    the ledger was briefly unreadable is a far smaller harm than going silent
    on every message while it is down.
    """
    if not event_id:
        return False
    doc = await _find(ctx, EVENTS_COLLECTION, "event_id", event_id)
    if doc is None:
        return False
    data = getattr(doc, "data", None) or {}
    stamped = float(data.get("at") or 0)
    if stamped and (time.time() - stamped) > EVENT_LEDGER_TTL_SECONDS:
        return False
    return True


async def remember_event(ctx, event_id: str, kind: str = "") -> None:
    """Record an event_id as processed. Never raises: dedupe is best-effort."""
    if not event_id:
        return
    try:
        await _upsert(ctx, EVENTS_COLLECTION, "event_id", event_id,
                      {"event_id": event_id, "at": time.time(), "kind": kind})
    except Exception:
        await ctx.log("event ledger could not be updated", "warn")


async def prune_ledger(ctx, limit: int = 200) -> int:
    """Drop ledger rows past their TTL.

    The store has no TTL of its own, so without this the collection grows for
    the life of the install. Called opportunistically after a successful
    delivery rather than on a timer -- traffic is what creates the rows, so
    traffic is the honest moment to clean them up.
    """
    removed = 0
    try:
        page = await ctx.store.query(EVENTS_COLLECTION, limit=limit)
        rows = getattr(page, "data", None) or []
    except Exception:
        return 0
    cutoff = time.time() - EVENT_LEDGER_TTL_SECONDS
    for doc in (rows or [])[:limit]:
        data = getattr(doc, "data", None) or {}
        stamped = float(data.get("at") or 0)
        if stamped and stamped < cutoff:
            try:
                await ctx.store.delete(EVENTS_COLLECTION, getattr(doc, "id", ""))
                removed += 1
            except Exception:
                break
    return removed


# --- noise suppression -------------------------------------------------------

def is_noise(event: dict, self_user_id: str = "",
             self_bot_id: str = "") -> tuple[bool, str]:
    """Whether this event must NOT become an Imperal event, and why.

    The reason string is returned for the audit log: "ignored" with no
    explanation is indistinguishable from "lost", and the difference matters
    the moment someone asks why their message went unanswered.
    """
    if not isinstance(event, dict):
        return True, "event payload is not an object"

    kind = str(event.get("type") or "")

    # A message the app itself posted. Without this the app answers its own
    # answer -- forever, in public.
    if event.get("bot_id"):
        if self_bot_id and str(event.get("bot_id")) == self_bot_id:
            return True, "own message (bot_id matches this app)"
        return True, "message from a bot"
    if event.get("bot_profile"):
        return True, "message from a bot"
    if self_user_id and str(event.get("user") or "") == self_user_id:
        return True, "own message (user id matches this app)"

    # Edits, deletions, joins, topic changes: real Slack traffic, but not
    # someone talking TO the app. Allowlist, so a future subtype is silent by
    # default instead of surprising the user with a broadcast.
    subtype = str(event.get("subtype") or "")
    if kind == "message" and subtype not in HUMAN_SUBTYPES:
        return True, f"message subtype '{subtype}' is not a human message"

    # A message with no text and no files carries nothing to act on.
    text = str(event.get("text") or "").strip()
    if not text and not event.get("files"):
        return True, "message has no text"

    if event.get("hidden"):
        return True, "Slack marked the message hidden"

    return False, ""


# --- normalisation -----------------------------------------------------------

def mentions_bot(text: str, self_user_id: str) -> bool:
    """Whether the raw text contains an @-mention of this app."""
    if not self_user_id:
        return False
    return f"<@{self_user_id}>" in (text or "")


def normalise(event: dict, envelope: dict, *, workspace: dict,
              self_user_id: str = "") -> dict:
    """Turn a Slack event into ONE internal shape.

    Slack's payloads differ per event type -- app_mention has no `channel_type`,
    a DM has no channel name, a thread reply carries thread_ts while its parent
    does not. Every consumer would otherwise reimplement those quirks, and each
    would get a different subset right. So the shape below is the same for all
    of them, and the fields a given event genuinely lacks are empty rather than
    absent: a missing key is a KeyError in a rule, an empty string is a
    condition that simply does not match.
    """
    kind = str(event.get("type") or "")
    channel_id = str(event.get("channel") or "")
    message_ts = str(event.get("ts") or "")
    thread_ts = str(event.get("thread_ts") or "")
    text = str(event.get("text") or "")

    # THE CRITICAL DISTINCTION for replying in the right place. Slack sets
    # thread_ts on every message IN a thread, including the parent -- so
    # thread_ts == ts means "this is the parent", not "this is a reply".
    is_reply = bool(thread_ts and thread_ts != message_ts)

    channel_type = str(event.get("channel_type") or "")
    is_dm = channel_type == "im" or channel_id.startswith("D")

    return {
        "event_type": kind,
        "event_id": str(envelope.get("event_id") or ""),
        "workspace_id": str(workspace.get("workspace_id") or
                            envelope.get("team_id") or ""),
        "workspace_name": str(workspace.get("workspace_name") or ""),
        "channel_id": channel_id,
        "channel_name": "",          # filled in by the handler if resolvable
        "channel_type": channel_type,
        "is_dm": is_dm,
        "message_ts": message_ts,
        "thread_ts": thread_ts,
        # Where a reply MUST go. For a thread reply that is the thread; for a
        # top-level message it is the message itself, so answering opens a
        # thread under it rather than adding noise to the channel.
        "reply_thread_ts": thread_ts or message_ts,
        "parent_message_ts": thread_ts if is_reply else "",
        "is_thread_reply": is_reply,
        "user_id": str(event.get("user") or ""),
        "user_display_name": "",     # filled in by the handler if resolvable
        "user_handle": "",
        "text": text,
        "text_readable": text,       # mentions resolved later, when names exist
        "mention_of_bot": (kind == "app_mention"
                           or mentions_bot(text, self_user_id)),
        "has_files": bool(event.get("files")),
        "permalink": "",
        "received_at": so.humanize_ts(message_ts),
    }


def classify(normalised: dict) -> list[str]:
    """Which Imperal events this message should raise.

    A mention inside a thread is BOTH a mention and a thread reply, and a rule
    author may reasonably subscribe to either -- so the list is deliberately
    not mutually exclusive. `message_received` is always included as the
    catch-all a broad rule can bind to without enumerating the specific kinds.
    """
    events = [EVENT_MESSAGE]
    if normalised.get("mention_of_bot"):
        events.append(EVENT_MENTION)
    if normalised.get("is_thread_reply"):
        events.append(EVENT_THREAD_REPLY)
    if normalised.get("is_dm"):
        events.append(EVENT_DM)
    return events


# --- reply context -----------------------------------------------------------

async def remember_reply_target(ctx, normalised: dict) -> None:
    """Store where the last inbound message in this channel came from.

    This is what lets a follow-up reply land in the RIGHT thread without the
    thread_ts being carried by hand through an automation prompt. An LLM asked
    to remember a 16-digit timestamp across a multi-step flow will eventually
    paraphrase it, and a paraphrased ts is one Slack does not recognise -- the
    reply then either vanishes or lands in the channel instead of the thread.
    Keyed by channel, because "answer where they wrote" is per-conversation.
    """
    channel_id = normalised.get("channel_id") or ""
    if not channel_id:
        return
    try:
        await _upsert(ctx, CHANNELS_COLLECTION, "channel_id", channel_id, {
            "channel_id": channel_id,
            "channel_name": normalised.get("channel_name") or "",
            "workspace_id": normalised.get("workspace_id") or "",
            "workspace_name": normalised.get("workspace_name") or "",
            "reply_thread_ts": normalised.get("reply_thread_ts") or "",
            "last_message_ts": normalised.get("message_ts") or "",
            "last_user_id": normalised.get("user_id") or "",
            "last_user_name": normalised.get("user_display_name") or "",
            "at": time.time(),
        })
    except Exception:
        await ctx.log("reply context could not be stored", "warn")


async def recall_reply_target(ctx, channel_id: str) -> dict:
    """The last inbound thread for a channel, or {} when nothing is known."""
    if not channel_id:
        return {}
    doc = await _find(ctx, CHANNELS_COLLECTION, "channel_id", channel_id)
    if doc is None:
        return {}
    return getattr(doc, "data", None) or {}


# --- URL verification --------------------------------------------------------

def url_verification_response(payload: dict) -> str | None:
    """Slack's one-time endpoint handshake.

    Slack POSTs {"type": "url_verification", "challenge": ...} when the Event
    Subscriptions URL is saved and expects the challenge echoed back verbatim.
    This is answered BEFORE the signature check has a secret to check against
    -- during setup the secret may not be stored yet -- and it is safe to
    answer: echoing an opaque string Slack itself just sent reveals nothing.
    """
    if not isinstance(payload, dict):
        return None
    if str(payload.get("type") or "") != "url_verification":
        return None
    challenge = payload.get("challenge")
    return str(challenge) if challenge else None


def parse_body(body: str) -> dict:
    """Parse the request body, tolerating anything that is not JSON."""
    try:
        parsed = json.loads(body or "{}")
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
