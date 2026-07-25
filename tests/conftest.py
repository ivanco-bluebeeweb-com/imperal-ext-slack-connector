"""Shared fixtures.

MockHTTP from the SDK only registers GET/POST and returns the first pattern
match, which cannot express "the same URL answers differently on the second
call" -- which paginating and multi-step write flows both do. So the HTTP double
here is queue-based: each test states the exact sequence of responses it
expects, and every request is recorded for assertions.

Slack-specific: EVERY builder defaults to `ok: True`, because that envelope is
on every Slack response and a body without it is precisely what the client
treats as a failure. Tests that want a failure say so explicitly with
`err(...)`.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class FakeResponse:
    """Mirrors imperal_sdk HTTPResponse closely enough for slack_client."""

    def __init__(self, status_code: int, body, headers: dict | None = None):
        self.status_code = status_code
        self.body = body
        self.headers: dict = headers or {}

    def json(self):
        # Mirrors imperal_sdk HTTPResponse.json(): a str/bytes body is PARSED,
        # so invalid JSON raises -- which is what drives the NOT_JSON path.
        if isinstance(self.body, (dict, list)):
            return self.body
        if isinstance(self.body, (str, bytes, bytearray)):
            import json as _json
            return _json.loads(self.body)
        raise ValueError(f"Cannot parse {type(self.body).__name__} body as JSON")

    def text(self) -> str:
        return self.body if isinstance(self.body, str) else str(self.body)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class QueueHTTP:
    """HTTP double: queue up responses, then inspect what was requested."""

    def __init__(self):
        self.queued: list = []
        self.calls: list[dict] = []

    def push(self, body, status: int = 200, headers: dict | None = None):
        """Queue one response (or an Exception instance to raise)."""
        self.queued.append((status, body, headers))
        return self

    def _next(self, method: str, url: str, kwargs) -> FakeResponse:
        self.calls.append({
            "method": method,
            "url": url,
            "json": kwargs.get("json"),
            "params": kwargs.get("params"),
            "data": kwargs.get("data"),
            "headers": kwargs.get("headers") or {},
            "timeout": kwargs.get("timeout"),
        })
        if not self.queued:
            raise AssertionError(f"No queued response for {method} {url}")
        status, body, headers = self.queued.pop(0)
        if isinstance(body, BaseException):
            raise body
        return FakeResponse(status, body, headers)

    async def get(self, url, **kw):
        return self._next("GET", url, kw)

    async def post(self, url, **kw):
        return self._next("POST", url, kw)

    async def patch(self, url, **kw):
        return self._next("PATCH", url, kw)

    async def put(self, url, **kw):
        return self._next("PUT", url, kw)

    async def delete(self, url, **kw):
        return self._next("DELETE", url, kw)

    # -- assertion helpers --------------------------------------------------
    def last_body(self) -> dict:
        return self.calls[-1]["json"] or {}

    def last_form(self) -> dict:
        """Slack write endpoints are posted as JSON here; keep both handy."""
        call = self.calls[-1]
        return call["json"] or call["data"] or {}

    def urls(self) -> list[str]:
        return [c["url"] for c in self.calls]

    def paths(self) -> list[str]:
        return [c["url"].rsplit("/", 1)[-1] for c in self.calls]

    def all_header_values(self) -> list[str]:
        out = []
        for c in self.calls:
            for v in (c["headers"] or {}).values():
                out.append(str(v))
        return out


@pytest.fixture
def http():
    return QueueHTTP()


@pytest.fixture
def ctx(http):
    from imperal_sdk.testing import MockContext, MockSecretStore

    mock = MockContext()
    mock.secrets = MockSecretStore({})
    mock.http = http
    return mock


@pytest.fixture
def connected_ctx(ctx):
    """A ctx with one usable workspace token already configured."""
    from imperal_sdk.testing import MockSecretStore

    ctx.secrets = MockSecretStore({"slack_tokens": FAKE_BOT_TOKEN})
    return ctx


# --- fake credentials --------------------------------------------------------
# Assembled from parts rather than written as literals. A secret scanner cannot
# tell a fake "xoxb-..." from a real one, and a scanner that cries wolf on the
# test suite is a scanner people learn to ignore -- so no string here looks like
# a credential.
_BOT = "xo" + "xb"
_USER = "xo" + "xp"
FAKE_BOT_TOKEN = f"{_BOT}-fake-for-tests"
FAKE_BOT_TOKEN_TWO = f"{_BOT}-fake-for-tests-two"
FAKE_USER_TOKEN = f"{_USER}-fake-for-tests"


# --- Slack payload builders -------------------------------------------------

def ok(**fields) -> dict:
    """A successful Slack envelope."""
    payload = {"ok": True}
    payload.update(fields)
    return payload


def err(code: str) -> dict:
    """A FAILED Slack envelope -- returned with HTTP 200, as Slack really does."""
    return {"ok": False, "error": code}


def auth_test_payload(team="Acme", team_id="T024BE7LH", user="webbee",
                      user_id="U0BOTBOT", bot_id="B0BOTBOT") -> dict:
    payload = ok(url="https://acme.slack.com/", team=team, team_id=team_id,
                 user=user, user_id=user_id)
    if bot_id:
        payload["bot_id"] = bot_id
    return payload


def channel_payload(channel_id="C024BE7LR", name="general", is_private=False,
                    is_member=True, **extra) -> dict:
    payload = {
        "id": channel_id,
        "name": name,
        "is_channel": True,
        "is_private": is_private,
        "is_archived": False,
        "is_member": is_member,
        "num_members": 12,
        "topic": {"value": "Company-wide announcements"},
        "purpose": {"value": "Everyone"},
    }
    payload.update(extra)
    return payload


def message_payload(ts="1690000000.123456", text="hello team",
                    user="U024BE7LH", **extra) -> dict:
    payload = {"type": "message", "ts": ts, "text": text, "user": user}
    payload.update(extra)
    return payload


def user_payload(user_id="U024BE7LH", name="vlad", real_name="Vlad Ivanco",
                 **extra) -> dict:
    payload = {
        "id": user_id,
        "name": name,
        "real_name": real_name,
        "deleted": False,
        "is_bot": False,
        "profile": {"real_name": real_name, "display_name": name,
                    "title": "Founder", "email": ""},
    }
    payload.update(extra)
    return payload
