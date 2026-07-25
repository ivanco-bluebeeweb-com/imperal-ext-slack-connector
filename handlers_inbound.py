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
from app import ext





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
