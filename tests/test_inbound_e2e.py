"""End-to-end inbound flow: a signed Slack delivery reaching the real endpoint.

`test_inbound.py` pins the PIECES in isolation. This file drives the actual
webhook handler with real signed payloads, because "every piece is individually
correct" is not the same claim as "the chain is wired together". The two worst
bugs in this feature were both invisible at unit level and only appeared when
the whole path ran:

  * `is_noise` returns a TUPLE, and a non-empty tuple is always truthy -- so a
    truth-tested call would have dropped 100% of events, silently.
  * the cached workspace record deliberately holds NO token, so enrichment
    received "" and every event would have arrived with bare ids.

The scenarios below are exactly the things a user can do in Slack.
"""

import json

import pytest

import inbound
from conftest import FAKE_BOT_TOKEN, auth_test_payload, channel_payload, ok
from test_inbound import SIGNING_SECRET, envelope, message_event, sign


@pytest.fixture
def inbound_ctx(ctx):
    """A ctx with a token AND a signing secret -- inbound needs both."""
    from imperal_sdk.testing import MockSecretStore

    ctx.secrets = MockSecretStore({
        "slack_tokens": FAKE_BOT_TOKEN,
        "slack_signing_secret": SIGNING_SECRET,
    })
    return ctx


@pytest.fixture
def endpoint():
    """The real webhook handler, as registered on the extension."""
    import main  # noqa: F401  (registers every module's decorators)
    import handlers_inbound

    return handlers_inbound.slack_events


def queue_delivery(http, channel_name="general", display="Vlad"):
    """Queue what ONE full delivery consumes, in order.

    auth.test (identity) -> conversations.info (channel name)
    -> users.info (display name). Queued explicitly so a change in call order
    fails loudly instead of drifting unnoticed.
    """
    http.push(auth_test_payload())
    http.push(ok(channel=channel_payload(name=channel_name)))
    http.push(ok(user={"id": "U024BE7LH", "name": "vlad",
                       "profile": {"display_name": display,
                                   "real_name": display}}))


async def deliver(endpoint, ctx, payload: dict, headers: dict | None = None):
    """POST one payload to the endpoint, signed unless told otherwise."""
    body = json.dumps(payload)
    return await endpoint(ctx, headers=headers or sign(body), body=body)


def emitted(ctx) -> list[dict]:
    return ctx.extensions._emitted


# --- the scenarios a user can actually perform ------------------------------

@pytest.mark.asyncio
async def test_a_mention_in_a_channel_becomes_an_imperal_event(
        inbound_ctx, http, endpoint):
    """User types "@webbee help" in #general."""
    queue_delivery(http)
    event = message_event(text="hey <@U0BOTBOT> can you help",
                          channel="C024BE7LR")
    event["type"] = "app_mention"

    result = await deliver(endpoint, inbound_ctx, envelope(event))

    assert result == "ok"
    names = [e["event_type"] for e in emitted(inbound_ctx)]
    assert inbound.EVENT_MENTION in names, (
        f"a mention must raise {inbound.EVENT_MENTION}, got {names}")

    data = emitted(inbound_ctx)[0]["data"]
    assert data["channel_id"] == "C024BE7LR"
    assert data["message_ts"] == "1690000000.100000"
    assert data["reply_thread_ts"] == "1690000000.100000", (
        "a top-level mention must be answerable as a THREAD under itself")
    assert data["mention_of_bot"] is True
    assert data["is_thread_reply"] is False


@pytest.mark.asyncio
async def test_enrichment_fills_in_channel_and_author_names(
        inbound_ctx, http, endpoint):
    """Names, not just ids -- this is what makes a rule prompt readable."""
    queue_delivery(http, channel_name="general", display="Vlad")
    event = message_event(text="hello <@U0BOTBOT>")
    event["type"] = "app_mention"

    await deliver(endpoint, inbound_ctx, envelope(event))

    data = emitted(inbound_ctx)[0]["data"]
    assert data["channel_name"] == "general", (
        "the token must reach enrichment -- the cached record carries none, so "
        "this asserts the token is resolved back out of the secret")
    assert data["user_display_name"] == "Vlad"
    assert data["workspace_name"] == "Acme"


@pytest.mark.asyncio
async def test_a_thread_reply_is_reported_as_a_thread_reply(
        inbound_ctx, http, endpoint):
    """User replies inside an existing thread."""
    queue_delivery(http)
    event = message_event(
        text="yes please",
        ts="1690000500.200000",           # the reply
        thread_ts="1690000000.100000",    # the parent
    )

    result = await deliver(endpoint, inbound_ctx, envelope(event))

    assert result == "ok"
    names = [e["event_type"] for e in emitted(inbound_ctx)]
    assert inbound.EVENT_THREAD_REPLY in names

    data = emitted(inbound_ctx)[0]["data"]
    assert data["is_thread_reply"] is True
    assert data["parent_message_ts"] == "1690000000.100000"
    assert data["reply_thread_ts"] == "1690000000.100000", (
        "a reply must be answered in ITS OWN thread, not under itself")


@pytest.mark.asyncio
async def test_the_app_ignores_its_own_message(inbound_ctx, http, endpoint):
    """The loop-prevention test: the app must not answer itself."""
    http.push(auth_test_payload())   # identity only; it stops before enrichment
    event = message_event(text="I have done that for you")
    event["bot_id"] = "B0BOTBOT"     # matches auth_test_payload's bot_id

    result = await deliver(endpoint, inbound_ctx, envelope(event))

    assert result == "ignored"
    assert emitted(inbound_ctx) == [], (
        "an app answering its own message is an infinite public loop")


@pytest.mark.asyncio
async def test_a_redelivered_event_is_processed_exactly_once(
        inbound_ctx, http, endpoint):
    """Slack retries; the user must be answered once, not twice."""
    queue_delivery(http)
    event = message_event(text="hello <@U0BOTBOT>")
    event["type"] = "app_mention"
    payload = envelope(event, event_id="Ev-SAME")

    first = await deliver(endpoint, inbound_ctx, payload)
    count_after_first = len(emitted(inbound_ctx))

    # Slack's retry: same event_id, retry headers attached.
    body = json.dumps(payload)
    headers = sign(body)
    headers["X-Slack-Retry-Num"] = "1"
    headers["X-Slack-Retry-Reason"] = "http_timeout"
    second = await endpoint(inbound_ctx, headers=headers, body=body)

    assert first == "ok"
    assert second == "duplicate"
    assert len(emitted(inbound_ctx)) == count_after_first, (
        "a retry must not emit again -- that is how a bot answers three times")


# --- security ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_unsigned_delivery_is_refused_and_emits_nothing(
        inbound_ctx, http, endpoint):
    """Anyone can POST to a public URL; only Slack can sign."""
    payload = envelope(message_event())
    body = json.dumps(payload)

    result = await endpoint(inbound_ctx, headers={}, body=body)

    assert result != "ok"
    assert emitted(inbound_ctx) == [], (
        "an unsigned request must never reach the automation engine")


@pytest.mark.asyncio
async def test_a_forged_signature_is_refused(inbound_ctx, http, endpoint):
    payload = envelope(message_event())
    body = json.dumps(payload)
    headers = sign(body, secret="not-the-real-secret")

    result = await endpoint(inbound_ctx, headers=headers, body=body)

    assert result != "ok"
    assert emitted(inbound_ctx) == []


@pytest.mark.asyncio
async def test_the_url_verification_challenge_is_echoed_verbatim(
        inbound_ctx, http, endpoint):
    """Slack sends this while SAVING the endpoint, before a secret exists.

    Answered before the signature check on purpose: at that moment the user may
    not have pasted the signing secret yet, and refusing the challenge would
    make the endpoint impossible to save at all.
    """
    payload = {"type": "url_verification", "challenge": "abc123xyz"}
    body = json.dumps(payload)

    result = await endpoint(inbound_ctx, headers={}, body=body)

    assert result == "abc123xyz"
    assert emitted(inbound_ctx) == []


# --- what a rule receives ---------------------------------------------------

@pytest.mark.asyncio
async def test_the_emitted_payload_carries_every_field_a_rule_needs(
        inbound_ctx, http, endpoint):
    """The contract the automation engine sees.

    Pinned as a whole because a rule referencing a field that quietly stopped
    being emitted fails at RUNTIME, in the user's channel, not here.
    """
    queue_delivery(http)
    event = message_event(text="hey <@U0BOTBOT>")
    event["type"] = "app_mention"

    await deliver(endpoint, inbound_ctx, envelope(event))

    data = emitted(inbound_ctx)[0]["data"]
    for field in ("event_type", "event_id", "workspace_id", "workspace_name",
                  "channel_id", "channel_name", "channel_type", "is_dm",
                  "message_ts", "thread_ts", "reply_thread_ts",
                  "parent_message_ts", "is_thread_reply", "user_id",
                  "user_display_name", "user_handle", "text", "text_readable",
                  "mention_of_bot", "permalink"):
        assert field in data, f"rules would break without {field!r}"


@pytest.mark.asyncio
async def test_a_dm_raises_the_dm_event(inbound_ctx, http, endpoint):
    queue_delivery(http, channel_name="")
    event = message_event(text="hi there", channel="D024BE7LR")
    event["channel_type"] = "im"

    await deliver(endpoint, inbound_ctx, envelope(event))

    names = [e["event_type"] for e in emitted(inbound_ctx)]
    assert inbound.EVENT_DM in names
    assert emitted(inbound_ctx)[0]["data"]["is_dm"] is True


# --- the journal: awareness that survives a dead emit -------------------------
# These exist because a SABOTAGE run proved the suite could not see the journal
# write being deleted from the endpoint: every test stayed green while a signed,
# verified Slack message left no trace at all. That is the exact failure this
# whole feature is meant to prevent, so it now has tests that fail loudly.

async def test_a_delivered_message_is_journalled(inbound_ctx, http, endpoint):
    """A push delivery must be REMEMBERED, not only emitted.

    `emit` is fire-and-forget into the platform's automations catalog. With no
    subscriber the message is gone -- and the four inbound Slack events are
    currently not even in that catalog. Storing is what makes awareness real.
    """
    import journal

    queue_delivery(http)
    body = json.dumps(envelope(message_event(text="hello there")))
    await endpoint(inbound_ctx, headers=sign(body), body=body)

    rows = await journal.recent(inbound_ctx, limit=5)
    assert len(rows) == 1, "a verified Slack delivery left no journal row"
    assert rows[0]["text"] == "hello there"
    assert rows[0]["source"] == journal.SOURCE_PUSH


async def test_the_message_is_journalled_even_when_the_emit_fails(
        inbound_ctx, http, endpoint):
    """A broken emit must not cost the record.

    Two mechanisms hold this up: the emit loop guards its own exceptions, and
    the journal write is ordered ahead of it. Sabotage showed the guard alone is
    currently sufficient -- moving the write after the emit kept this green --
    so the ordering is defence in depth against future code landing between the
    two, and this test pins the PROPERTY (a failing emit costs nothing) rather
    than the ordering that happens to implement it.
    """
    import journal

    async def exploding_emit(*_a, **_kw):
        raise RuntimeError("no subscriber for this event")

    inbound_ctx.extensions.emit = exploding_emit

    queue_delivery(http)
    body = json.dumps(envelope(message_event(text="survives the emit")))
    result = await endpoint(inbound_ctx, headers=sign(body), body=body)

    assert result is not None, "the endpoint must still answer Slack"
    rows = await journal.recent(inbound_ctx, limit=5)
    assert len(rows) == 1, "the message was lost when the emit failed"
    assert rows[0]["text"] == "survives the emit"


async def test_an_ignored_message_is_not_journalled(inbound_ctx, http, endpoint):
    """Noise stays out of the journal.

    The app's own messages must never be recorded: a journal that remembers
    Webbee's own posts is a journal that will eventually have her answer
    herself.
    """
    import journal

    http.push(auth_test_payload())
    own = message_event(text="my own words")
    own["user"] = "U0BOTBOT"
    own["bot_id"] = "B0BOTBOT"
    body = json.dumps(envelope(own))
    await endpoint(inbound_ctx, headers=sign(body), body=body)

    assert await journal.recent(inbound_ctx, limit=5) == []


@pytest.mark.asyncio
async def test_a_forged_delivery_is_not_written_to_the_journal(
        inbound_ctx, http, endpoint):
    """The journal is what awareness READS -- so it is what forgery must not reach.

    The existing forgery test asserts no emit. That was the right guarantee when
    emit was the mechanism, but emit turned out to be inert: the journal is what
    Webbee and the panel actually read. A signature check that stopped the emit
    while still recording the message would pass that test and put attacker text
    in front of the user as a genuine Slack message.

    Verified live against the deployed endpoint: an unsigned and a wrongly-signed
    delivery both come back "unauthorised".
    """
    import journal

    before = await journal.counts(inbound_ctx)

    payload = envelope(message_event(text="forged: wire me the money"))
    body = json.dumps(payload)
    headers = sign(body, secret="not-the-real-secret")

    result = await endpoint(inbound_ctx, headers=headers, body=body)

    assert result != "ok"
    after = await journal.counts(inbound_ctx)
    assert after["total"] == before["total"], \
        "a forged delivery was recorded in the message log"

    rows = await journal.recent(inbound_ctx, limit=50)
    assert not any("wire me the money" in str(r.get("text") or "") for r in rows), \
        "forged text reached the journal"


@pytest.mark.asyncio
async def test_an_unsigned_delivery_is_not_written_to_the_journal(
        inbound_ctx, http, endpoint):
    """No signature headers at all -- the plainest forgery attempt."""
    import journal

    before = await journal.counts(inbound_ctx)
    body = json.dumps(envelope(message_event(text="unsigned intruder")))

    result = await endpoint(inbound_ctx, headers={}, body=body)

    assert result != "ok"
    after = await journal.counts(inbound_ctx)
    assert after["total"] == before["total"], \
        "an unsigned delivery was recorded in the message log"


@pytest.mark.asyncio
async def test_a_refused_delivery_is_recorded_as_an_attempt(
        inbound_ctx, http, endpoint):
    """"Slack never knocked" and "Slack was refused" must not look identical.

    Both produce zero recorded messages, so the message counts alone cannot tell
    them apart -- and the fixes are opposites: one means finishing the setup in
    the Slack console, the other means the stored secret is wrong. The evidence
    that separated them used to go only to ctx.log, which the user cannot read.

    This is not hypothetical. It is exactly the wall the live investigation hit:
    push showed 0 delivered while every visible setting was correct, and there
    was no way to tell which of the two situations it was.
    """
    import inbound as ib

    before = await ib.delivery_report(inbound_ctx)
    assert before["attempts"] == 0

    body = json.dumps(envelope(message_event()))
    headers = sign(body, secret="not-the-real-secret")

    result = await endpoint(inbound_ctx, headers=headers, body=body)
    assert result != "ok"

    after = await ib.delivery_report(inbound_ctx)
    assert after["attempts"] == 1, "the knock itself was not recorded"
    assert after["refused"] == 1
    assert after["last_refusal_code"], "the refusal code was not kept"


@pytest.mark.asyncio
async def test_an_accepted_delivery_is_recorded_as_accepted(
        inbound_ctx, http, endpoint):
    """The other side of the same fact, so 'accepted' is not assumed."""
    import inbound as ib

    body = json.dumps(envelope(message_event()))
    result = await endpoint(inbound_ctx, headers=sign(body), body=body)
    assert result == "ok"

    report = await ib.delivery_report(inbound_ctx)
    assert report["attempts"] == 1
    assert report["accepted"] == 1
    assert report["refused"] == 0


@pytest.mark.asyncio
async def test_the_delivery_probe_never_stores_a_secret_or_message_text(
        inbound_ctx, http, endpoint):
    """A diagnostic must not become a data leak.

    It records counters and a refusal CODE. If it ever held the signature, the
    body, or the message text, then anyone who can read the app's store would
    get the contents of private Slack conversations from a debug aid.
    """
    import inbound as ib

    secret_text = "wire the money to account 12345"
    body = json.dumps(envelope(message_event(text=secret_text)))
    await endpoint(inbound_ctx, headers=sign(body), body=body)

    report = await ib.delivery_report(inbound_ctx)
    blob = str(report)
    assert secret_text not in blob, "message text leaked into the delivery probe"
    assert "v0=" not in blob, "a signature leaked into the delivery probe"


@pytest.mark.asyncio
async def test_delivery_attempts_accumulate_across_calls(
        inbound_ctx, http, endpoint):
    """Two knocks must count as two, not overwrite each other.

    Sabotage exposed this gap. The single-delivery tests above passed even with
    the original bug re-introduced, because reading the PRIOR counters only
    happens on the second delivery -- so a counter that resets to 1 every time
    looked perfectly correct with one call.

    That bug was real and it was mine: stored fields live under Document.data,
    and reading them straight off the Document silently returns the default. The
    counter would have sat at 1 forever no matter how often Slack called, which
    is worthless for the exact question it exists to answer -- "is Slack
    knocking repeatedly and being turned away every time?"
    """
    import inbound as ib

    body_one = json.dumps(envelope(message_event(ts="111.1")))
    await endpoint(inbound_ctx, headers=sign(body_one, secret="wrong"),
                   body=body_one)

    body_two = json.dumps(envelope(message_event(ts="222.2")))
    await endpoint(inbound_ctx, headers=sign(body_two, secret="wrong"),
                   body=body_two)

    report = await ib.delivery_report(inbound_ctx)
    assert report["attempts"] == 2, \
        f"two deliveries counted as {report['attempts']}"
    assert report["refused"] == 2
