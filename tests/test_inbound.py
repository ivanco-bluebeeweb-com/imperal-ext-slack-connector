"""Inbound Slack Events: signature, dedupe, noise, normalisation, emit.

These tests exist because every failure mode in this file is SILENT in
production. A broken signature check does not crash -- it accepts forged
traffic. A broken dedupe does not crash -- it answers the user three times. A
noise filter that drops everything does not crash -- inbound just goes quiet
and looks like Slack's fault. None of that shows up in a smoke test, so it has
to be pinned here.
"""

import hashlib
import hmac
import json
import time

import pytest

import inbound
from conftest import (
    FAKE_BOT_TOKEN,
    auth_test_payload,
    channel_payload,
    ok,
    user_payload,
)

SIGNING_SECRET = "s3cret-for-tests"


def sign(body: str, secret: str = SIGNING_SECRET, timestamp: str = "") -> dict:
    """Build the headers Slack would send for this body."""
    ts = timestamp or str(int(time.time()))
    digest = hmac.new(secret.encode(), f"v0:{ts}:{body}".encode(),
                      hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": f"v0={digest}"}


def envelope(event: dict, event_id: str = "Ev001", team_id: str = "T024BE7LH"):
    return {
        "type": "event_callback",
        "team_id": team_id,
        "event_id": event_id,
        "event": event,
    }


def message_event(text="hey <@U0BOTBOT> can you help",
                  channel="C024BE7LR", ts="1690000000.100000",
                  user="U024BE7LH", **extra) -> dict:
    event = {"type": "message", "channel": channel, "user": user,
             "text": text, "ts": ts, "channel_type": "channel"}
    event.update(extra)
    return event


# --- signature ---------------------------------------------------------------

def test_a_correctly_signed_request_is_accepted():
    body = json.dumps({"hello": "world"})
    verdict = inbound.verify_signature(body, sign(body), SIGNING_SECRET)
    assert verdict["ok"] is True


def test_a_tampered_body_fails_verification():
    """The signature covers the body; changing it after signing must fail."""
    body = json.dumps({"hello": "world"})
    headers = sign(body)
    verdict = inbound.verify_signature(
        json.dumps({"hello": "evil"}), headers, SIGNING_SECRET)
    assert verdict["ok"] is False
    assert verdict["code"] == "SLACK_SIGNATURE_INVALID"


def test_the_wrong_secret_fails_verification():
    body = json.dumps({"hello": "world"})
    headers = sign(body, secret="not-the-real-secret")
    assert inbound.verify_signature(body, headers, SIGNING_SECRET)["ok"] is False


def test_an_old_request_is_refused_even_with_a_valid_signature():
    """Replay protection: a captured request must not work forever."""
    old = str(int(time.time()) - (inbound.MAX_REQUEST_AGE_SECONDS + 60))
    body = json.dumps({"hello": "world"})
    verdict = inbound.verify_signature(body, sign(body, timestamp=old),
                                       SIGNING_SECRET)
    assert verdict["ok"] is False
    assert verdict["code"] == "SLACK_REQUEST_TOO_OLD"


def test_verification_fails_closed_when_no_secret_is_configured():
    """No secret must mean 'reject', never 'skip the check'."""
    body = json.dumps({"hello": "world"})
    verdict = inbound.verify_signature(body, sign(body), "")
    assert verdict["ok"] is False
    assert verdict["code"] == "SLACK_SIGNING_SECRET_MISSING"


def test_signature_uses_the_raw_body_not_reserialised_json():
    """Slack signs BYTES. Re-dumping the parsed dict changes them.

    This is the bug that makes every delivery fail with a correct secret, so
    it is pinned: a body with unusual spacing still verifies.
    """
    raw = '{"b":1,  "a":2}'          # deliberately not canonical JSON
    headers = sign(raw)
    assert inbound.verify_signature(raw, headers, SIGNING_SECRET)["ok"] is True
    requeued = json.dumps(json.loads(raw))
    assert inbound.verify_signature(
        requeued, headers, SIGNING_SECRET)["ok"] is False


# --- noise -------------------------------------------------------------------

def test_a_plain_human_message_is_not_noise():
    noise, _ = inbound.is_noise(message_event(), self_user_id="U0BOTBOT")
    assert noise is False


def test_the_apps_own_message_is_ignored():
    """Otherwise the app answers its own answer, in public, forever."""
    event = message_event(user="U0BOTBOT", text="I replied earlier")
    noise, reason = inbound.is_noise(event, self_user_id="U0BOTBOT")
    assert noise is True
    assert "own message" in reason


def test_a_message_from_any_bot_is_ignored():
    event = message_event(bot_id="B999OTHER", user="")
    noise, reason = inbound.is_noise(event, self_user_id="U0BOTBOT")
    assert noise is True
    assert "bot" in reason


def test_a_message_edit_is_ignored():
    event = message_event(subtype="message_changed")
    noise, _ = inbound.is_noise(event, self_user_id="U0BOTBOT")
    assert noise is True


def test_a_join_notice_is_ignored():
    event = message_event(subtype="channel_join", text="vlad joined")
    noise, _ = inbound.is_noise(event, self_user_id="U0BOTBOT")
    assert noise is True


def test_a_file_share_is_kept():
    """file_share is a real person saying something -- with an attachment."""
    event = message_event(subtype="file_share", text="see this")
    noise, _ = inbound.is_noise(event, self_user_id="U0BOTBOT")
    assert noise is False


def test_an_empty_message_is_ignored():
    noise, _ = inbound.is_noise(message_event(text=""),
                                self_user_id="U0BOTBOT")
    assert noise is True


def test_is_noise_returns_a_tuple_that_must_be_unpacked():
    """Regression: `if is_noise(...)` is ALWAYS true for a non-empty tuple.

    Truth-testing the return value instead of unpacking it silently discards
    every event and takes the whole inbound flow down without an error.
    """
    result = inbound.is_noise(message_event(), self_user_id="U0BOTBOT")
    assert isinstance(result, tuple) and len(result) == 2
    assert bool(result) is True and result[0] is False


# --- normalisation -----------------------------------------------------------

def test_normalise_produces_every_documented_field():
    event = message_event()
    shape = inbound.normalise(
        event, envelope(event),
        workspace={"workspace_id": "T024BE7LH", "workspace_name": "Acme"},
        self_user_id="U0BOTBOT")

    for field in ("workspace_id", "workspace_name", "channel_id",
                  "channel_name", "thread_ts", "message_ts", "user_id",
                  "user_display_name", "text", "event_type",
                  "is_thread_reply", "parent_message_ts", "mention_of_bot",
                  "reply_thread_ts"):
        assert field in shape, f"normalised payload is missing {field}"


def test_a_mention_is_flagged():
    event = message_event(text="hey <@U0BOTBOT> please look")
    shape = inbound.normalise(event, envelope(event), workspace={},
                              self_user_id="U0BOTBOT")
    assert shape["mention_of_bot"] is True


def test_a_message_naming_someone_else_is_not_a_mention():
    event = message_event(text="hey <@U024BE7LH> please look")
    shape = inbound.normalise(event, envelope(event), workspace={},
                              self_user_id="U0BOTBOT")
    assert shape["mention_of_bot"] is False


def test_a_thread_reply_is_recognised_and_points_at_its_parent():
    event = message_event(ts="1690000009.000000",
                          thread_ts="1690000000.100000")
    shape = inbound.normalise(event, envelope(event), workspace={},
                              self_user_id="U0BOTBOT")
    assert shape["is_thread_reply"] is True
    assert shape["parent_message_ts"] == "1690000000.100000"
    assert shape["reply_thread_ts"] == "1690000000.100000"


def test_a_thread_parent_is_not_itself_a_reply():
    """Slack sets thread_ts on the PARENT too; ts == thread_ts means parent.

    Getting this wrong reports every thread starter as a reply.
    """
    event = message_event(ts="1690000000.100000",
                          thread_ts="1690000000.100000")
    shape = inbound.normalise(event, envelope(event), workspace={},
                              self_user_id="U0BOTBOT")
    assert shape["is_thread_reply"] is False


def test_a_top_level_message_replies_into_a_new_thread_under_itself():
    """Answering a top-level mention should start a thread, not spray channel.

    reply_thread_ts falls back to the message's own ts, so a reply becomes the
    first response in a thread hanging off the message that asked.
    """
    event = message_event(ts="1690000000.100000")
    shape = inbound.normalise(event, envelope(event), workspace={},
                              self_user_id="U0BOTBOT")
    assert shape["reply_thread_ts"] == "1690000000.100000"


def test_a_dm_is_recognised():
    event = message_event(channel="D0PRIVATE", channel_type="im")
    shape = inbound.normalise(event, envelope(event), workspace={},
                              self_user_id="U0BOTBOT")
    assert shape["is_dm"] is True


def test_message_ts_is_never_coerced_to_a_number():
    """A Slack ts is a STRING. float() silently loses the last digits."""
    event = message_event(ts="1690000000.000100")
    shape = inbound.normalise(event, envelope(event), workspace={},
                              self_user_id="U0BOTBOT")
    assert shape["message_ts"] == "1690000000.000100"
    assert isinstance(shape["message_ts"], str)


# --- classification ----------------------------------------------------------

def test_a_plain_message_raises_only_the_catch_all():
    events = inbound.classify({"mention_of_bot": False,
                               "is_thread_reply": False, "is_dm": False})
    assert events == [inbound.EVENT_MESSAGE]


def test_a_mention_in_a_thread_raises_mention_and_thread_reply():
    """Both, deliberately: a rule author may bind to either."""
    events = inbound.classify({"mention_of_bot": True,
                               "is_thread_reply": True, "is_dm": False})
    assert inbound.EVENT_MESSAGE in events
    assert inbound.EVENT_MENTION in events
    assert inbound.EVENT_THREAD_REPLY in events


def test_every_emitted_event_name_is_app_id_prefixed():
    """Federal rule M7.3 -- an unprefixed name is rejected at deploy."""
    for name in (inbound.EVENT_MESSAGE, inbound.EVENT_MENTION,
                 inbound.EVENT_THREAD_REPLY, inbound.EVENT_DM):
        assert name.startswith("slack-connector.")


# --- dedupe ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_event_is_remembered_and_then_recognised(ctx):
    assert await inbound.already_processed(ctx, "Ev123") is False
    await inbound.remember_event(ctx, "Ev123", kind="message")
    assert await inbound.already_processed(ctx, "Ev123") is True


@pytest.mark.asyncio
async def test_an_unknown_event_is_not_treated_as_seen(ctx):
    await inbound.remember_event(ctx, "Ev123")
    assert await inbound.already_processed(ctx, "Ev999") is False


@pytest.mark.asyncio
async def test_dedupe_fails_open_when_the_store_is_unavailable(ctx):
    """Answering twice beats going silent on every message."""
    class BrokenStore:
        async def get(self, *a, **kw):
            raise RuntimeError("store down")

    ctx.store = BrokenStore()
    assert await inbound.already_processed(ctx, "Ev123") is False


def test_a_retry_delivery_is_detected_from_the_header():
    retry, reason = inbound.is_retry({"X-Slack-Retry-Num": "2",
                                      "X-Slack-Retry-Reason": "http_timeout"})
    assert retry is True
    assert reason == "http_timeout"


def test_a_first_delivery_is_not_a_retry():
    retry, _ = inbound.is_retry({})
    assert retry is False


def test_retry_headers_are_matched_case_insensitively():
    """Header case is not guaranteed across proxies."""
    retry, _ = inbound.is_retry({"x-slack-retry-num": "1"})
    assert retry is True


# --- url verification --------------------------------------------------------

def test_the_url_verification_challenge_is_echoed():
    payload = {"type": "url_verification", "challenge": "abc123"}
    assert inbound.url_verification_response(payload) == "abc123"


def test_a_normal_event_is_not_mistaken_for_a_challenge():
    assert inbound.url_verification_response(
        {"type": "event_callback"}) is None


# --- reply context -----------------------------------------------------------

@pytest.mark.asyncio
async def test_the_reply_target_is_remembered_per_channel(ctx):
    await inbound.remember_reply_target(ctx, {
        "channel_id": "C024BE7LR", "channel_name": "general",
        "reply_thread_ts": "1690000000.100000",
        "message_ts": "1690000000.100000", "user_id": "U024BE7LH",
        "user_display_name": "Vlad",
    })
    recalled = await inbound.recall_reply_target(ctx, "C024BE7LR")
    assert recalled["reply_thread_ts"] == "1690000000.100000"
    assert recalled["channel_name"] == "general"


@pytest.mark.asyncio
async def test_recalling_an_unknown_channel_returns_empty(ctx):
    assert await inbound.recall_reply_target(ctx, "C000NONE") == {}
