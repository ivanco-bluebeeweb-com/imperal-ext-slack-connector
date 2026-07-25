"""The Slack Events endpoint, plus the tools that read a specific message.

TRANSPORT SHAPE. The platform routes
``POST /v1/ext/slack-connector/webhook/events`` here. The handler receives the
raw body as a STRING, the request headers, and a context with NO user
(`user_id="__webhook__"`), because nobody is logged in when Slack pushes.

WHY IT ANSWERS FIRST AND THINKS SECOND. Slack gives an endpoint THREE SECONDS
to return 200; miss it and Slack retries, then eventually disables the
subscription. So this handler does the cheap, mandatory work inline --
verify, de-duplicate, normalise, emit -- and treats every enrichment (channel
name, display name) as OPTIONAL: each is wrapped so that a slow or failing
Slack lookup degrades the event to ids instead of blowing the deadline.

An event with ids and no names is still fully actionable: `channel_id` and
`reply_thread_ts` are what a reply needs. Names are for the human reading the
rule, so they are the right thing to sacrifice under time pressure.
"""

from __future__ import annotations

from imperal_sdk import ActionResult

import accounts as acc
import inbound
import shared
import slack_client as sc
import slack_objects as so
from app import chat, ext
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


# --------------------------- the events endpoint ---------------------------

@ext.emits(inbound.EVENT_MESSAGE)
@ext.emits(inbound.EVENT_MENTION)
@ext.emits(inbound.EVENT_THREAD_REPLY)
@ext.emits(inbound.EVENT_DM)
@ext.webhook("events", method="POST")
async def slack_events(ctx, headers: dict | None = None, body: str = "",
                       query_params: dict | None = None):
    """Receive one Slack event delivery.

    Returns a plain string body; the platform wraps it into the HTTP response.
    Slack only cares that the status is 200 -- it ignores the body except for
    the url_verification challenge, which MUST be echoed back verbatim.

    Every exit path returns 200 EXCEPT a failed signature check. That is
    deliberate: a 500 makes Slack retry, and retrying an event this app has
    decided to ignore (a bot message, a duplicate) would just repeat the same
    decision three more times. "Accepted and dropped" is 200.
    """
    headers = headers or {}
    payload = inbound.parse_body(body)

    # 1. URL VERIFICATION -- answered BEFORE the signature check, because Slack
    #    sends the challenge while the endpoint is first being saved, at which
    #    point the user may not have pasted the signing secret yet. The
    #    challenge carries no data and grants no access, so echoing it is safe.
    challenge = inbound.url_verification_response(payload)
    if challenge is not None:
        await ctx.log("Slack URL verification challenge answered", level="info")
        return challenge

    # 2. SIGNATURE -- the only path that refuses with a non-200. An unsigned or
    #    wrongly signed request is not a Slack delivery, and there is nothing to
    #    retry: refusing it is the correct final answer.
    secret = ""
    try:
        secret = (await ctx.secrets.get(inbound.SIGNING_SECRET_NAME)) or ""
    except Exception:
        secret = ""

    verdict = inbound.verify_signature(body, headers, secret)
    if not verdict["ok"]:
        # The reason is logged, never returned: telling an unauthenticated
        # caller WHY their signature failed helps them forge a better one.
        await ctx.log(
            f"Slack event rejected: {verdict['code']}", level="warn")
        return "unauthorised"

    envelope = payload if isinstance(payload, dict) else {}
    event = envelope.get("event") or {}
    if not isinstance(event, dict):
        return "ignored"

    event_id = str(envelope.get("event_id") or "")

    # 3. DEDUPE. The retry header is free; the ledger costs one store read and
    #    catches redelivery that arrives with no retry header at all.
    retry, retry_reason = inbound.is_retry(headers)
    if retry and event_id and await inbound.already_processed(ctx, event_id):
        await ctx.log(
            f"Slack retry ignored ({retry_reason}); already handled {event_id}",
            level="info")
        return "duplicate"
    if event_id and await inbound.already_processed(ctx, event_id):
        return "duplicate"

    # 4. WHICH WORKSPACE. The team_id in the envelope selects the token, so a
    #    user with several workspaces connected gets each event attributed to
    #    the right one instead of whichever token happens to be first.
    team_id = str(envelope.get("team_id") or "")
    workspace = await _workspace_for_team(ctx, team_id)
    self_user_id = str(workspace.get("identity_id") or "")

    # 5. NOISE. Checked before any enrichment: there is no point resolving the
    #    display name of a bot whose message is about to be dropped.
    #    is_noise returns (verdict, reason) -- unpacked, NOT truth-tested: a
    #    non-empty tuple is always truthy, so `if is_noise(...)` would discard
    #    every event and inbound would be silently 100% dead.
    noise, reason = inbound.is_noise(
        event, self_user_id=self_user_id,
        self_bot_id=str(workspace.get("bot_id") or ""))
    if noise:
        if event_id:
            await inbound.remember_event(ctx, event_id, kind="noise")
        await ctx.log(f"Slack event ignored: {reason}", level="info")
        return "ignored"

    normalised = inbound.normalise(event, envelope, workspace=workspace,
                                   self_user_id=self_user_id)

    # 6. ENRICHMENT -- best effort, never fatal.
    token = str(workspace.get("token") or "")
    if token:
        await _enrich(ctx, normalised, token)

    # 7. REMEMBER, then EMIT. The ledger is written BEFORE emitting: if the
    #    emit throws, a Slack retry finds the event already recorded and drops
    #    it, so the user is never answered twice. Losing one event is a far
    #    smaller failure than answering the same message three times.
    if event_id:
        await inbound.remember_event(ctx, event_id,
                                    kind=normalised.get("event_type", ""))

    await inbound.remember_reply_target(ctx, normalised)

    for event_name in inbound.classify(normalised):
        try:
            await ctx.extensions.emit(event_name, normalised)
        except Exception:
            await ctx.log(f"could not emit {event_name}", level="warn")

    # The store has no TTL, so the ledger is pruned here -- after a real
    # delivery, which is the only moment that creates rows. A timer would be
    # tidier but needs a schedule this app does not otherwise want.
    try:
        await inbound.prune_ledger(ctx)
    except Exception:
        pass

    await ctx.log(
        f"Slack {normalised['event_type']} in "
        f"{normalised.get('channel_name') or normalised['channel_id']} "
        f"→ {len(inbound.classify(normalised))} event(s) emitted",
        level="info")
    return "ok"


async def _workspace_for_team(ctx, team_id: str) -> dict:
    """Find the connected workspace matching Slack's team_id, WITH its token.

    Falls back to the single connected workspace when the id does not match --
    the common case is exactly one workspace, and refusing to handle an event
    over an id mismatch would break the whole flow for a cosmetic reason.

    The returned dict carries `token`, which the cached record deliberately
    does NOT: `list_workspaces` never stores a token, so enrichment would
    silently get "" and every event would arrive with bare ids instead of
    names. The token is read back from the secret and matched by line index --
    the same mapping `resolve_workspace` uses.
    """
    try:
        records = await acc.list_workspaces(ctx)
    except Exception:
        return {}

    usable = [r for r in records if r.get("status") == "ok"]
    match = {}
    for record in usable:
        if team_id and record.get("workspace_id") == team_id:
            match = dict(record)
            break
    if not match and len(usable) == 1:
        match = dict(usable[0])
    if not match:
        return {}

    try:
        creds = await acc.read_tokens(ctx)
        tokens = creds.get("tokens") or []
        line = int(match.get("line", 0))
        if 0 <= line < len(tokens):
            match["token"] = tokens[line]
    except Exception:
        pass
    return match


async def _enrich(ctx, normalised: dict, token: str) -> None:
    """Add channel name, author name and readable text -- if Slack answers.

    Each lookup is independent and individually guarded: a missing
    `users:read` scope should cost the display name, not the channel name too.
    """
    channel_id = normalised.get("channel_id") or ""
    user_id = normalised.get("user_id") or ""

    if channel_id:
        try:
            out = await sc.request(ctx, "GET", "conversations.info", token,
                                   params={"channel": channel_id})
            if out.get("ok"):
                channel = (out.get("data") or {}).get("channel") or {}
                normalised["channel_name"] = so.channel_name(channel)
        except Exception:
            pass

    users: dict = {}
    if user_id:
        try:
            out = await sc.request(ctx, "GET", "users.info", token,
                                   params={"user": user_id})
            if out.get("ok"):
                person = (out.get("data") or {}).get("user") or {}
                display = so.user_display_name(person)
                normalised["user_display_name"] = display
                normalised["user_handle"] = str(person.get("name") or "")
                users[user_id] = display
        except Exception:
            pass

    # Resolve <@U…> into names now that at least the author is known, so a rule
    # prompt reads "hey @vlad" instead of "hey <@U024BE7LH>".
    try:
        normalised["text_readable"] = so.render_text(
            normalised.get("text") or "", users=users)
    except Exception:
        normalised["text_readable"] = normalised.get("text") or ""


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
    lines = [
        f"Endpoint (paste into Slack → Event Subscriptions): {url}",
        ("Signing secret: configured" if secret
         else "Signing secret: NOT SET — every delivery will be refused. "
              "Copy it from Slack → Basic Information → Signing Secret."),
        (f"Workspaces connected: {len(usable)}" if usable
         else "Workspaces connected: none — paste a token first."),
        f"Events remembered for de-duplication: {seen}",
        "",
        "Subscribe to these bot events in Slack:",
        "  app_mention, message.channels, message.groups, message.im",
        "",
        "Events raised into Imperal automations:",
        f"  {inbound.EVENT_MESSAGE}",
        f"  {inbound.EVENT_MENTION}",
        f"  {inbound.EVENT_THREAD_REPLY}",
        f"  {inbound.EVENT_DM}",
    ]

    ready = bool(secret and usable)
    return ActionResult.success(
        summary=("Slack inbound is ready." if ready
                 else "Slack inbound is NOT ready yet — see the detail."),
        data=InboundStatus(
            endpoint_url=url,
            signing_secret_set=bool(secret),
            workspaces_connected=len(usable),
            events_deduplicated=seen,
            ready=ready,
            state="ready" if ready else "not ready",
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
            endpoint_url=url, signing_secret_set=True, ready=True,
            state="secret saved",
            detail=(f"Request URL: {url}\n\n"
                    "Slack will call it once to verify — that challenge is "
                    "answered automatically.")),
        refresh_panels=["slack", "slack_nav"],
    )
