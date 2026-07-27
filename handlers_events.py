"""Tools that support the inbound flow: read a message, a thread, or the setup.

Separate from `handlers_inbound.py` on purpose. That file is TRANSPORT: it is
called by Slack, has no user in context, and answers in milliseconds. These are
ordinary chat tools called by Webbee with a real user. They share a subject --
incoming messages -- but nothing else: different caller, different deadline,
different failure modes. Keeping them in one file made it the longest in the app
and mixed two audiences in one place.

`fetch_message` and `fetch_thread_context` exist because an event carries the
text as it was SENT. By the time a rule runs the message may have been edited or
deleted, and answering an edited question with a reply to the old one is worse
than not answering. These re-read the live state.
"""

from __future__ import annotations

from imperal_sdk import ActionResult

import accounts as acc
import inbound
import journal
import shared
import slack_client as sc
import slack_objects as so
from app import chat
from models import (
    ConnectEventsParams,
    FetchMessageParams,
    FetchThreadContextParams,
    InboundStatus,
    InboundStatusParams,
    MessageList,
    MessageRecord,
)

_error = shared.error
_from_envelope = shared.from_envelope
_resolve = shared.resolve
_resolve_channel = shared.resolve_channel_or_error


# --------------------------- inbound-support tools ---------------------------

@chat.function(
    "fetch_message",
    "Read one specific Slack message by its timestamp -- the message an event "
    "referred to.",
    action_type="read", chain_callable=True,
    data_model=MessageRecord,
)
async def fetch_message(ctx, params: FetchMessageParams) -> ActionResult:
    """Fetch a single message by ts.

    Exists because an event carries the text as it was SENT. By the time a rule
    runs, the message may have been edited or deleted -- and answering an
    edited message with a reply to its old text is worse than not answering.
    """
    ts = (params.ts or "").strip()
    if not ts:
        return _error(
            "Which message? Pass the ts from the Slack event.",
            sc.SLACK_VALIDATION_FAILED)

    token, _workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    channel, err = await _resolve_channel(ctx, token, params.channel)
    if err:
        return err

    # `latest=ts, inclusive=1, limit=1` is the documented way to ask for ONE
    # message: Slack has no conversations.getMessage endpoint.
    out = await sc.request(
        ctx, "GET", "conversations.history", token,
        params={"channel": channel["id"], "latest": ts, "oldest": ts,
                "inclusive": 1, "limit": 1})
    if not out.get("ok"):
        return _from_envelope(out)

    messages = (out.get("data") or {}).get("messages") or []
    if not messages:
        return _error(
            f"No message at {ts} in {shared.channel_label(channel)}. It may "
            "have been deleted, or the ts may belong to another channel.",
            sc.SLACK_NOT_FOUND)

    message = messages[0]
    users = await _user_map(ctx, token, [message])
    record = _message_record(message, users)
    return ActionResult.success(
        summary=(f"Message from {record.author} in "
                 f"{shared.channel_label(channel)}."),
        data=record)


@chat.function(
    "fetch_thread_context",
    "Read the whole thread around a message, so a reply answers the actual "
    "conversation rather than one line of it.",
    action_type="read", chain_callable=True,
    data_model=MessageList,
)
async def fetch_thread_context(ctx,
                               params: FetchThreadContextParams) -> ActionResult:
    """Fetch a thread in full, oldest first.

    Oldest-first deliberately: a conversation read bottom-up makes no sense to
    a model summarising it, and this output exists to be summarised.
    """
    thread_ts = (params.thread_ts or "").strip()
    if not thread_ts:
        return _error(
            "Which thread? Pass thread_ts (or reply_thread_ts) from the event.",
            sc.SLACK_VALIDATION_FAILED)

    token, _workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    channel, err = await _resolve_channel(ctx, token, params.channel)
    if err:
        return err

    out = await sc.paginate(
        ctx, "GET", "conversations.replies", token,
        params={"channel": channel["id"], "ts": thread_ts},
        results_key="messages", limit=max(params.limit, 1))
    if not out.get("ok"):
        return _from_envelope(out)

    raw = out.get("results") or []
    users = await _user_map(ctx, token, raw)
    records = [_message_record(m, users) for m in raw if isinstance(m, dict)]

    if not records:
        return _error(
            f"No thread at {thread_ts} in {shared.channel_label(channel)}.",
            sc.SLACK_NOT_FOUND)

    return ActionResult.success(
        summary=(f"{len(records)} message(s) in the thread in "
                 f"{shared.channel_label(channel)}."),
        data=MessageList(channel=channel["name"], channel_id=channel["id"],
                         messages=records, count=len(records),
                         thread_ts=thread_ts))


@chat.function(
    "inbound_status",
    "Report whether the Slack events endpoint is ready: signing secret, "
    "endpoint URL, and what to paste into the Slack app settings.",
    action_type="read", chain_callable=True,
    data_model=InboundStatus,
)
async def inbound_status(ctx, params: InboundStatusParams) -> ActionResult:
    """Explain the inbound setup, including the exact URL to paste.

    Exists because the failure mode it prevents is invisible: an endpoint that
    is never called looks identical to one that is called and ignores
    everything. This names which half is missing.
    """
    try:
        secret = (await ctx.secrets.get(inbound.SIGNING_SECRET_NAME)) or ""
    except Exception:
        secret = ""

    records = []
    try:
        records = await acc.list_workspaces(ctx)
    except Exception:
        records = []
    usable = [r for r in records if r.get("status") == "ok"]

    seen = 0
    try:
        # query(), not list(): the ledger is keyed by a FIELD, and query is the
        # method that behaves the same against the real store and the test
        # double.
        page = await ctx.store.query(inbound.EVENTS_COLLECTION, limit=200)
        seen = len(getattr(page, "data", None) or [])
    except Exception:
        seen = 0

    # ctx.webhook_url is authoritative: it takes the host from the platform and
    # the app id from the KERNEL, not from this Python module. Hardcoding the
    # URL is what caused a past class of "the provider calls a URL nobody
    # listens on" bugs -- the SDK documents it as such.
    try:
        url = ctx.webhook_url("events")
    except Exception:
        url = ("https://panel.imperal.io/v1/ext/slack-connector/"
               "webhook/events")
    # How much has actually been recorded -- the difference between "configured"
    # and "working". A report that only described configuration is what let this
    # connector look broken while the sweep was quietly doing its job, and look
    # fine while nothing at all was arriving.
    stats: dict = {}
    try:
        stats = await journal.counts(ctx)
    except Exception:
        stats = {}
    recorded = int(stats.get("total") or 0)
    from_push = int(stats.get("from_push") or 0)
    from_sweep = int(stats.get("from_sweep") or 0)

    # Whether Slack ever KNOCKED, which the message counts cannot tell you: a
    # refused delivery records no message, so "0 pushed" covers both "Slack is
    # not calling" and "Slack is calling and being turned away".
    delivery = await inbound.delivery_report(ctx)
    attempts = int(delivery.get("attempts") or 0)
    refused = int(delivery.get("refused") or 0)
    refusal_code = str(delivery.get("last_refusal_code") or "")

    lines = [
        f"Endpoint (paste into Slack → Event Subscriptions): {url}",
        ("Signing secret: configured" if secret
         else "Signing secret: NOT SET — Slack deliveries are refused. "
              "Copy it from Slack → Basic Information → Signing Secret."),
        (f"Workspaces connected: {len(usable)}" if usable
         else "Workspaces connected: none — paste a token first."),
        "",
        f"Messages recorded so far: {recorded} "
        f"({from_push} pushed by Slack, {from_sweep} read by the sweep)",
        # The word "hourly" used to be baked in here. It became a lie the moment
        # the interval changed, and a status line that describes the schedule
        # wrongly is worse than one that says nothing -- so the cron string
        # speaks for itself.
        f"Scheduled sweep: on ({journal.SWEEP_CRON}) — reads every channel the "
        "app was added to, plus direct messages. Needs no signing secret.",
        f"Events remembered for de-duplication: {seen}",
        "",
        # The line that separates "Slack is not calling" from "Slack is calling
        # and being refused". Both look like "0 pushed" from the message counts
        # alone, and the fixes are completely different, so it is stated outright
        # instead of leaving the user (and me) to guess.
        (f"Delivery attempts seen at this endpoint: {attempts}"
         + (f" — {refused} refused" if refused else "")
         + (f" (last refusal: {refusal_code})" if refusal_code else "")),
        ("  → Slack has never called this endpoint: the Request URL or the bot "
         "event subscriptions are not saved in Slack yet."
         if attempts == 0
         else ("  → Slack IS calling, but deliveries are being refused. The "
               "signing secret stored here does not match the one in Slack — "
               "copy it again from Slack → Basic Information."
               if refused and not from_push
               else "  → Deliveries are arriving and being accepted.")),
        "",
        "Subscribe to these bot events in Slack:",
        "  app_mention, message.channels, message.groups, message.im",
        "",
        # Named honestly. These are DECLARED by this app, but the platform's
        # automations catalog does not list them, so a rule built on one fails
        # with "Event not found". Calling them "raised into automations" sent
        # the user off to build a trigger that cannot exist.
        "Declared inbound events (not yet selectable as automation triggers "
        "— the platform catalog does not list them):",
        f"  {inbound.EVENT_MESSAGE}",
        f"  {inbound.EVENT_MENTION}",
        f"  {inbound.EVENT_THREAD_REPLY}",
        f"  {inbound.EVENT_DM}",
    ]

    push_ready = bool(secret and usable)
    # Awareness does NOT require push: the sweep covers it. This is the whole
    # point of reporting the two separately.
    aware = bool(usable)

    if push_ready:
        summary = ("Slack inbound is ready: messages are pushed instantly and "
                   "the scheduled sweep backs it up.")
    elif aware:
        summary = (f"Webbee is seeing Slack messages via the scheduled sweep "
                   f"({recorded} recorded). Instant push is still off — add "
                   "the signing secret to enable it.")
    else:
        summary = "Slack is not connected yet — paste a bot token first."

    return ActionResult.success(
        summary=summary,
        data=InboundStatus(
            endpoint_url=url,
            signing_secret_set=bool(secret),
            workspaces_connected=len(usable),
            events_deduplicated=seen,
            ready=push_ready,
            aware=aware,
            messages_recorded=recorded,
            from_push=from_push,
            from_sweep=from_sweep,
            sweep_schedule=journal.SWEEP_CRON,
            delivery_attempts=attempts,
            deliveries_refused=refused,
            last_refusal_code=refusal_code,
            state=("ready" if push_ready
                   else "sweep only" if aware else "not connected"),
            detail="\n".join(lines)))


# --------------------------- small shared helpers ---------------------------

async def _user_map(ctx, token: str, messages: list) -> dict:
    """Resolve the user ids appearing in `messages` to display names."""
    ids = {str(m.get("user") or "") for m in messages if isinstance(m, dict)}
    ids.discard("")
    if not ids:
        return {}
    try:
        out = await sc.paginate(ctx, "GET", "users.list", token,
                                results_key="members", limit=400)
        if out.get("ok"):
            return so.user_name_map(out.get("results") or [])
    except Exception:
        pass
    return {}


def _message_record(message: dict, users: dict) -> MessageRecord:
    thread_ts, reply_count = so.thread_info(message)
    ts = str(message.get("ts") or "")
    return MessageRecord(
        id=ts,
        title=so.author_of(message, users),
        author=so.author_of(message, users),
        text=so.message_text(message, users=users),
        ts=ts,
        posted_at=so.humanize_ts(ts),
        author_id=str(message.get("user") or ""),
        thread_ts=thread_ts,
        is_thread_parent=bool(thread_ts and thread_ts == ts),
        reply_count=reply_count,
        reactions=so.reactions_of(message),
        permalink=so.permalink_of(message),
    )


@chat.function(
    "connect_events",
    "Save the Slack signing secret so inbound Slack events can be verified "
    "and accepted.",
    action_type="write", chain_callable=True,
    effects=["slack.events.configured"],
    event="slack-connector.connect_events",
    data_model=InboundStatus,
)
async def connect_events(ctx, params: ConnectEventsParams) -> ActionResult:
    """Store the signing secret that makes the inbound endpoint usable.

    A SEPARATE function from connect_workspace because the two credentials do
    opposite jobs: the token lets this app talk TO Slack, the signing secret
    proves a request came FROM Slack. Folding them into one form would suggest
    an app is only usable with both, when sending works with just the token.

    Validated by SHAPE before storing. The mistake this catches is real and
    otherwise invisible: pasting the bot token into the signing-secret field
    stores a value that is syntactically fine, and the only symptom is that
    every Slack delivery is refused with no clue why.
    """
    secret = (params.signing_secret or "").strip()
    if not secret:
        return _error(
            "Paste the signing secret first. It is in Slack → your app → "
            "Basic Information → App Credentials → Signing Secret.",
            sc.SLACK_VALIDATION_FAILED)

    if secret.startswith(("xoxb-", "xoxp-", "xapp-")):
        return _error(
            "That is a Slack TOKEN, not the signing secret. The signing secret "
            "is on the Basic Information page under App Credentials, and it "
            "has no 'xox' prefix.",
            sc.SLACK_VALIDATION_FAILED)

    # Slack's signing secrets are 32 hex characters. Checked loosely: a length
    # floor catches a truncated paste, while not hard-failing if Slack ever
    # changes the format.
    if len(secret) < 16:
        return _error(
            "That signing secret looks incomplete — Slack's is 32 characters. "
            "Copy the whole value (use the Show button in Slack first).",
            sc.SLACK_VALIDATION_FAILED)

    try:
        await ctx.secrets.set(inbound.SIGNING_SECRET_NAME, secret)
    except Exception:
        await ctx.log("slack signing secret could not be written", "error")
        return _error(
            "The signing secret could not be saved. Try again, or paste it in "
            "the app's Secrets tab.",
            sc.SLACK_SECRET_WRITE_FAILED)

    try:
        url = ctx.webhook_url("events")
    except Exception:
        url = "https://panel.imperal.io/v1/ext/slack-connector/webhook/events"

    return ActionResult.success(
        summary=("Signing secret saved. Now paste the Request URL into Slack → "
                 "Event Subscriptions and subscribe to app_mention, "
                 "message.channels, message.groups and message.im."),
        data=InboundStatus(
            # NOT ready=True. Saving the secret is one step of four, and the
            # other three happen in the Slack console where this app cannot see
            # them. Claiming "ready" here is the same over-claim the events
            # screen made: it reported success while zero messages had ever
            # arrived by push, which is precisely the silence being debugged.
            endpoint_url=url, signing_secret_set=True, ready=False,
            state="secret saved",
            detail=(f"Request URL: {url}\n\n"
                    "Slack will call it once to verify — that challenge is "
                    "answered automatically.\n\n"
                    "This is step 2 of 4. Still to do in Slack: save the "
                    "Request URL, subscribe to the four bot events, then "
                    "Reinstall the app.")),
        refresh_panels=["slack", "slack_nav"],
    )
