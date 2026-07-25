"""slack_client: the request funnel, error classification, pagination.

The heavyweight test in here is `ok: false` on HTTP 200. Slack reports
application failures that way, so a status-first classifier reads every Slack
failure as a SUCCESS with an empty body. These tests pin the body-first
behaviour so that can never regress.
"""

import pytest

import slack_client as sc
from conftest import auth_test_payload, err, ok


# --- the ok:false trap -------------------------------------------------------

async def test_ok_false_on_http_200_is_a_failure(ctx, http):
    """HTTP 200 + ok:false must NOT be read as success."""
    http.push(err("channel_not_found"))
    out = await sc.request(ctx, "GET", "conversations.info", "xoxb-t")
    assert out["ok"] is False
    assert out["code"] == sc.SLACK_CHANNEL_NOT_FOUND


async def test_ok_true_on_http_200_is_a_success(ctx, http):
    http.push(ok(channel={"id": "C1"}))
    out = await sc.request(ctx, "GET", "conversations.info", "xoxb-t")
    assert out["ok"] is True
    assert out["data"]["channel"]["id"] == "C1"


async def test_a_body_without_ok_at_all_is_not_trusted(ctx, http):
    """Slack always sends `ok`. A body without it is not a Slack response."""
    http.push({"channel": {"id": "C1"}})
    out = await sc.request(ctx, "GET", "conversations.info", "xoxb-t")
    assert out["ok"] is False
    assert out["code"] == sc.SLACK_RESPONSE_UNEXPECTED


# --- error mapping -----------------------------------------------------------

@pytest.mark.parametrize("slack_error,expected", [
    ("invalid_auth", sc.SLACK_TOKEN_REJECTED),
    ("token_revoked", sc.SLACK_TOKEN_REJECTED),
    ("account_inactive", sc.SLACK_TOKEN_REJECTED),
    ("missing_scope", sc.SLACK_SCOPE_MISSING),
    ("not_allowed_token_type", sc.SLACK_WRONG_TOKEN_TYPE),
    ("not_in_channel", sc.SLACK_NOT_IN_CHANNEL),
    ("channel_not_found", sc.SLACK_CHANNEL_NOT_FOUND),
    ("user_not_found", sc.SLACK_USER_NOT_FOUND),
    ("message_not_found", sc.SLACK_MESSAGE_NOT_FOUND),
    ("is_archived", sc.SLACK_ARCHIVED),
    ("ratelimited", "RATE_LIMITED"),
])
def test_slack_error_codes_map_to_structured_codes(slack_error, expected):
    code, message = sc.classify(200, {"ok": False, "error": slack_error})
    assert code == expected
    assert message, "every mapped code must carry a human message"


def test_an_unmapped_slack_error_still_gets_a_code():
    code, message = sc.classify(200, {"ok": False, "error": "some_new_thing"})
    assert code == sc.SLACK_HTTP_ERROR
    assert message


def test_status_codes_still_classify_when_body_is_useless():
    assert sc.classify(429, None)[0] == "RATE_LIMITED"
    assert sc.classify(503, None)[0] == "BACKEND_5XX"
    assert sc.classify(500, None)[0] == "BACKEND_5XX"


def test_scope_message_says_to_reinstall():
    """A new scope does nothing until the app is reinstalled -- say so."""
    _, message = sc.classify(200, {"ok": False, "error": "missing_scope"})
    assert "reinstall" in message.lower()


def test_wrong_token_type_message_names_the_user_token():
    """The only way to search Slack is a user token; the message must say it."""
    _, message = sc.classify(200, {"ok": False, "error": "not_allowed_token_type"})
    assert "xoxp" in message or "user token" in message.lower()


# --- token hygiene -----------------------------------------------------------

async def test_a_missing_token_never_reaches_the_network(ctx, http):
    out = await sc.request(ctx, "GET", "auth.test", "")
    assert out["code"] == sc.SLACK_TOKEN_MISSING
    assert http.calls == [], "no request may be made without a token"


async def test_the_token_never_appears_in_an_error(ctx, http):
    secret = "xoxb-super-secret-value"
    http.push(err("invalid_auth"))
    out = await sc.request(ctx, "GET", "auth.test", secret)
    blob = repr(out)
    assert secret not in blob
    assert "super-secret" not in blob


async def test_the_token_travels_in_the_authorization_header(ctx, http):
    http.push(auth_test_payload())
    await sc.request(ctx, "GET", "auth.test", "xoxb-abc")
    headers = http.calls[-1]["headers"]
    assert headers.get("Authorization") == "Bearer xoxb-abc"


def test_token_kind_is_detected_from_the_prefix():
    assert sc.token_kind("xoxb-1-2") == "bot"
    assert sc.token_kind("xoxp-1-2") == "user"
    assert sc.token_kind("garbage") == "unknown"


# --- transport ---------------------------------------------------------------

async def test_a_timeout_is_reported_as_a_timeout(ctx, http):
    http.push(TimeoutError("timed out"))
    out = await sc.request(ctx, "GET", "auth.test", "xoxb-t")
    assert out["code"] == "BACKEND_TIMEOUT"
    assert out["retryable"] is True


async def test_an_unreachable_host_is_distinct_from_a_timeout(ctx, http):
    http.push(ConnectionError("nodename nor servname provided"))
    out = await sc.request(ctx, "GET", "auth.test", "xoxb-t")
    assert out["code"] == sc.SLACK_UNREACHABLE


async def test_a_non_json_body_is_reported_as_such(ctx, http):
    http.push("<html>maintenance</html>")
    out = await sc.request(ctx, "GET", "auth.test", "xoxb-t")
    assert out["code"] == sc.SLACK_RESPONSE_NOT_JSON


async def test_every_request_carries_an_explicit_timeout(ctx, http):
    """A hanging call must fail diagnosably, not wait to be cancelled."""
    http.push(auth_test_payload())
    await sc.request(ctx, "GET", "auth.test", "xoxb-t")
    assert http.calls[-1]["timeout"], "no timeout was passed"


# --- rate limiting -----------------------------------------------------------

async def test_retry_after_is_surfaced_when_slack_rate_limits(ctx, http):
    http.push(err("ratelimited"), status=429, headers={"Retry-After": "30"})
    out = await sc.request(ctx, "GET", "conversations.list", "xoxb-t")
    assert out["code"] == "RATE_LIMITED"
    assert out["retryable"] is True
    assert out.get("retry_after") == 30


# --- pagination --------------------------------------------------------------

async def test_pagination_follows_the_cursor(ctx, http):
    http.push(ok(channels=[{"id": "C1"}],
                 response_metadata={"next_cursor": "abc"}))
    http.push(ok(channels=[{"id": "C2"}],
                 response_metadata={"next_cursor": ""}))
    out = await sc.paginate(ctx, "GET", "conversations.list", "xoxb-t",
                            results_key="channels")
    assert out["ok"] is True
    assert [c["id"] for c in out["results"]] == ["C1", "C2"]
    assert http.calls[1]["params"].get("cursor") == "abc"


async def test_pagination_stops_at_the_limit(ctx, http):
    http.push(ok(channels=[{"id": "C1"}, {"id": "C2"}, {"id": "C3"}],
                 response_metadata={"next_cursor": "more"}))
    out = await sc.paginate(ctx, "GET", "conversations.list", "xoxb-t",
                            results_key="channels", limit=2)
    assert len(out["results"]) == 2
    assert len(http.calls) == 1, "must not fetch another page once satisfied"


async def test_pagination_reports_an_error_mid_stream(ctx, http):
    http.push(ok(channels=[{"id": "C1"}],
                 response_metadata={"next_cursor": "abc"}))
    http.push(err("ratelimited"))
    out = await sc.paginate(ctx, "GET", "conversations.list", "xoxb-t",
                            results_key="channels")
    assert out["ok"] is False
    assert out["code"] == "RATE_LIMITED"
