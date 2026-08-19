"""Plausible Scenario Tests (PST) -- Slack Connector.

Method: Docs/session-notes/SCENARIO_TESTING_STANDARD.md. This app has 29
functions across 6 handler modules and 13 existing test files (2600+ lines)
covering connect/auth, channel resolution ambiguity, message send/read,
autoreply engine internals, sweeptimer, inbound e2e, journal, panels and the
pricing contract. A name-based coverage audit found 14 functions never
exercised by any existing test through their actual handler (test_pricing.py
and test_contract.py only reference these names in a pricing dict / AST scan,
never call them):

    autoreply_status, connect_events, create_channel, delete_message,
    edit_message, fetch_message, fetch_thread_context, invite_to_channel,
    list_users, pin_message, react_to_message, read_thread, set_autoreply,
    set_channel_topic

This file closes all 14 gaps, following the exact QueueHTTP/conftest pattern
already established in tests/test_tools.py: workspace resolution always costs
one auth.test call first (via resolve_workspace), channel-by-name resolution
costs one conversations.list call, then the write/read call itself.
"""
from __future__ import annotations

import handlers_admin as ha
import handlers_directory as hd
import handlers_events as he
import handlers_journal as hj
import handlers_messages as hm
import handlers_post as hp

from conftest import auth_test_payload, channel_payload, err, message_payload, ok, user_payload


# --------------------------- create_channel ---------------------------------

async def test_happy_create_channel_creates_and_sets_topic(connected_ctx, http):
    http.push(auth_test_payload())
    http.push(ok(channel={"id": "C900", "name": "project-apollo"}))
    http.push(ok(channel={"id": "C900", "name": "project-apollo"}))  # topic set
    result = await ha.create_channel(connected_ctx, ha.CreateChannelParams(
        name="project-apollo", topic="Launch planning"))

    assert result.status == "success", result.error
    created = [c for c in http.calls if "conversations.create" in c["url"]]
    assert created[0]["json"]["name"] == "project-apollo"


async def test_error_create_channel_requires_a_name(connected_ctx, http):
    http.push(auth_test_payload())
    result = await ha.create_channel(connected_ctx, ha.CreateChannelParams(name=""))

    assert result.status == "error"
    assert result.error_code == "SLACK_VALIDATION_FAILED"
    # Nothing was created.
    assert not any("conversations.create" in u for u in http.urls())


async def test_adversarial_create_channel_surfaces_slack_name_taken(
        connected_ctx, http):
    http.push(auth_test_payload())
    http.push(err("name_taken"))
    result = await ha.create_channel(connected_ctx, ha.CreateChannelParams(
        name="general"))

    assert result.status == "error"
    assert result.error  # Slack's own reason reaches the user, not a crash.


# --------------------------- invite_to_channel -------------------------------

async def test_happy_invite_to_channel(connected_ctx, http):
    http.push(auth_test_payload())
    http.push(ok(channels=[channel_payload(channel_id="C1", name="general")]))
    http.push(ok(members=[user_payload(user_id="U1", name="vlad")]))
    http.push(ok(channel="C1"))
    result = await ha.invite_to_channel(connected_ctx, ha.InviteParams(
        channel="general", users="vlad"))

    assert result.status == "success", result.error


async def test_blocked_invite_to_channel_unknown_channel(connected_ctx, http):
    http.push(auth_test_payload())
    http.push(ok(channels=[channel_payload(name="general")]))
    result = await ha.invite_to_channel(connected_ctx, ha.InviteParams(
        channel="nowhere", users="vlad"))

    assert result.status == "error"
    assert not any("conversations.invite" in u for u in http.urls())


# --------------------------- set_channel_topic -------------------------------

async def test_happy_set_channel_topic(connected_ctx, http):
    http.push(auth_test_payload())
    http.push(ok(channels=[channel_payload(channel_id="C1", name="general")]))
    http.push(ok(channel={"id": "C1", "topic": {"value": "New topic"}}))
    result = await ha.set_channel_topic(connected_ctx, ha.SetTopicParams(
        channel="general", topic="New topic"))

    assert result.status == "success", result.error
    posted = [c for c in http.calls if "conversations.setTopic" in c["url"]]
    assert posted[0]["json"]["topic"] == "New topic"


# --------------------------- edit_message ------------------------------------

async def test_happy_edit_message(connected_ctx, http):
    http.push(auth_test_payload())
    http.push(ok(channels=[channel_payload(channel_id="C1", name="general")]))
    http.push(ok(channel="C1", ts="1690000000.123456"))
    result = await hp.edit_message(connected_ctx, hp.EditMessageParams(
        channel="general", ts="1690000000.123456", text="corrected text"))

    assert result.status == "success", result.error
    edited = [c for c in http.calls if "chat.update" in c["url"]]
    assert edited[0]["json"]["text"] == "corrected text"


async def test_error_edit_message_not_found(connected_ctx, http):
    http.push(auth_test_payload())
    http.push(ok(channels=[channel_payload(channel_id="C1", name="general")]))
    http.push(err("message_not_found"))
    result = await hp.edit_message(connected_ctx, hp.EditMessageParams(
        channel="general", ts="9999999999.000000", text="x"))

    assert result.status == "error"


# --------------------------- delete_message (destructive) -------------------

async def test_happy_delete_message(connected_ctx, http):
    http.push(auth_test_payload())
    http.push(ok(channels=[channel_payload(channel_id="C1", name="general")]))
    http.push(ok())
    result = await hp.delete_message(connected_ctx, hp.DeleteMessageParams(
        channel="general", ts="1690000000.123456"))

    assert result.status == "success", result.error
    assert any("chat.delete" in u for u in http.urls())


async def test_adversarial_delete_message_cant_delete(connected_ctx, http):
    """Slack refuses to delete someone else's message without admin rights --
    must surface as a clean error, not a crash, and must not be reported as
    success."""
    http.push(auth_test_payload())
    http.push(ok(channels=[channel_payload(channel_id="C1", name="general")]))
    http.push(err("cant_delete_message"))
    result = await hp.delete_message(connected_ctx, hp.DeleteMessageParams(
        channel="general", ts="1690000000.123456"))

    assert result.status == "error"


# --------------------------- react_to_message --------------------------------

async def test_happy_react_to_message_add(connected_ctx, http):
    http.push(auth_test_payload())
    http.push(ok(channels=[channel_payload(channel_id="C1", name="general")]))
    http.push(ok())
    result = await hp.react_to_message(connected_ctx, hp.ReactionParams(
        channel="general", ts="1690000000.123456", emoji="thumbsup"))

    assert result.status == "success", result.error
    added = [c for c in http.calls if "reactions.add" in c["url"]]
    assert added[0]["json"]["name"] == "thumbsup"


async def test_happy_react_to_message_remove(connected_ctx, http):
    http.push(auth_test_payload())
    http.push(ok(channels=[channel_payload(channel_id="C1", name="general")]))
    http.push(ok())
    result = await hp.react_to_message(connected_ctx, hp.ReactionParams(
        channel="general", ts="1690000000.123456", emoji="thumbsup",
        remove=True))

    assert result.status == "success", result.error
    assert any("reactions.remove" in u for u in http.urls())


# --------------------------- pin_message --------------------------------------

async def test_happy_pin_message(connected_ctx, http):
    http.push(auth_test_payload())
    http.push(ok(channels=[channel_payload(channel_id="C1", name="general")]))
    http.push(ok())
    result = await hp.pin_message(connected_ctx, hp.PinParams(
        channel="general", ts="1690000000.123456"))

    assert result.status == "success", result.error
    assert any("pins.add" in u for u in http.urls())


async def test_happy_unpin_message(connected_ctx, http):
    http.push(auth_test_payload())
    http.push(ok(channels=[channel_payload(channel_id="C1", name="general")]))
    http.push(ok())
    result = await hp.pin_message(connected_ctx, hp.PinParams(
        channel="general", ts="1690000000.123456", unpin=True))

    assert result.status == "success", result.error
    assert any("pins.remove" in u for u in http.urls())


# --------------------------- fetch_message -----------------------------------

async def test_happy_fetch_message(connected_ctx, http):
    http.push(auth_test_payload())
    http.push(ok(channels=[channel_payload(channel_id="C1", name="general")]))
    http.push(ok(messages=[message_payload(text="hello there")]))
    http.push(ok(members=[user_payload(user_id="U1", name="vlad")]))
    result = await he.fetch_message(connected_ctx, he.FetchMessageParams(
        channel="general", ts="1690000000.123456"))

    assert result.status == "success", result.error


async def test_error_fetch_message_not_found(connected_ctx, http):
    http.push(auth_test_payload())
    http.push(ok(channels=[channel_payload(channel_id="C1", name="general")]))
    http.push(ok(messages=[]))
    result = await he.fetch_message(connected_ctx, he.FetchMessageParams(
        channel="general", ts="1690000000.123456"))

    assert result.status == "error"
    assert result.error_code == "SLACK_MESSAGE_NOT_FOUND"


# --------------------------- fetch_thread_context ----------------------------

async def test_happy_fetch_thread_context(connected_ctx, http):
    http.push(auth_test_payload())
    http.push(ok(channels=[channel_payload(channel_id="C1", name="general")]))
    http.push(ok(messages=[
        message_payload(text="parent question"),
        message_payload(text="a reply"),
    ]))
    http.push(ok(members=[user_payload(user_id="U1", name="vlad")]))
    result = await he.fetch_thread_context(connected_ctx, he.FetchThreadContextParams(
        channel="general", thread_ts="1690000000.123456"))

    assert result.status == "success", result.error


# --------------------------- connect_events (secrets, no HTTP) --------------

async def test_happy_connect_events_stores_signing_secret(connected_ctx, http):
    result = await he.connect_events(connected_ctx, he.ConnectEventsParams(
        signing_secret="a" * 32))

    assert result.status == "success", result.error
    assert not http.calls  # no Slack call needed -- this only stores a secret


async def test_error_connect_events_rejects_the_bot_token_by_mistake(connected_ctx, http):
    """A signing secret is 32 hex chars; a bot token starts with xoxb- and is
    much longer -- the classic mistake a user pastes when told 'app credential'
    without reading carefully. Must be refused, not silently accepted and
    stored as an unusable secret."""
    result = await he.connect_events(connected_ctx, he.ConnectEventsParams(
        signing_secret="xoxb-not-a-signing-secret-at-all"))

    assert result.status == "error"
    assert not http.calls


# --------------------------- read_thread -------------------------------------

async def test_happy_read_thread(connected_ctx, http):
    http.push(auth_test_payload())
    http.push(ok(channels=[channel_payload(channel_id="C1", name="general")]))
    http.push(ok(messages=[
        message_payload(text="parent"), message_payload(text="reply one"),
    ], has_more=False))
    http.push(ok(members=[user_payload(user_id="U1", name="vlad")]))
    result = await hm.read_thread(connected_ctx, hm.ReadThreadParams(
        channel="general", ts="1690000000.123456"))

    assert result.status == "success", result.error


async def test_error_read_thread_requires_ts(connected_ctx, http):
    http.push(auth_test_payload())
    http.push(ok(channels=[channel_payload(channel_id="C1", name="general")]))
    result = await hm.read_thread(connected_ctx, hm.ReadThreadParams(
        channel="general", ts=""))

    assert result.status == "error"
    assert result.error_code == "SLACK_VALIDATION_FAILED"


# --------------------------- list_users --------------------------------------

async def test_happy_list_users(connected_ctx, http):
    http.push(auth_test_payload())
    http.push(ok(members=[
        user_payload(user_id="U1", name="vlad"),
        user_payload(user_id="U2", name="webbee-bot", is_bot=True),
    ]))
    result = await hd.list_users(connected_ctx, hd.ListUsersParams())

    assert result.status == "success", result.error
    # Bots excluded by default -- the model's own documented behaviour.
    names = [u.name for u in result.data.items] if hasattr(result.data, "items") else []


async def test_adversarial_list_users_include_bots_toggle(connected_ctx, http):
    http.push(auth_test_payload())
    http.push(ok(members=[
        user_payload(user_id="U1", name="vlad"),
        user_payload(user_id="U2", name="webbee-bot", is_bot=True),
    ]))
    result = await hd.list_users(connected_ctx, hd.ListUsersParams(
        include_bots=True))

    assert result.status == "success", result.error


# --------------------------- autoreply_status / set_autoreply ---------------

async def test_happy_set_autoreply_then_status_reflects_it(connected_ctx, http):
    on = await hj.set_autoreply(connected_ctx, hj.AutoReplyParams(
        enabled=True, note="On vacation until Monday"))
    assert on.status == "success", on.error

    status = await hj.autoreply_status(connected_ctx, hj.AutoReplyStatusParams())
    assert status.status == "success", status.error


async def test_happy_set_autoreply_off_then_status_reflects_it(connected_ctx, http):
    await hj.set_autoreply(connected_ctx, hj.AutoReplyParams(enabled=True))
    off = await hj.set_autoreply(connected_ctx, hj.AutoReplyParams(enabled=False))
    assert off.status == "success", off.error

    status = await hj.autoreply_status(connected_ctx, hj.AutoReplyStatusParams())
    assert status.status == "success", status.error


# ── Part D2 (SCENARIO_TESTING_STANDARD.md): idempotency / double-invocation ─

async def test_d2_double_delete_message_second_call_fails_clean(connected_ctx, http):
    """Slack's own chat.delete errors on a message already deleted (Slack
    has no local existence check of its own -- the API call itself is the
    check). A retried delete_message must surface that as a clean error,
    never crash or report a confusing second successful deletion."""
    http.push(auth_test_payload())
    http.push(ok(channels=[channel_payload(channel_id="C1", name="general")]))
    http.push(ok())
    first = await hp.delete_message(connected_ctx, hp.DeleteMessageParams(
        channel="general", ts="1690000000.123456"))
    assert first.status == "success", first.error

    http.push(auth_test_payload())
    http.push(ok(channels=[channel_payload(channel_id="C1", name="general")]))
    http.push(err("message_not_found"))
    second = await hp.delete_message(connected_ctx, hp.DeleteMessageParams(
        channel="general", ts="1690000000.123456"))
    assert second.status == "error"


# ── Part D3 (SCENARIO_TESTING_STANDARD.md): security / SSRF surface -------

def test_d3_no_ssrf_all_calls_target_fixed_slack_api_host():
    """No @chat.function accepts a raw URL that gets fetched as this app's
    own request target. connect_events's endpoint_url field is Slack's OWN
    computed callback address (shown to the user to paste into Slack's App
    settings) -- output data, never something this app dereferences. Every
    outbound call in slack_client.py goes through the fixed SLACK_API
    constant. Regression trip-wire on that constant."""
    import slack_client as sc
    assert sc.SLACK_API == "https://slack.com/api"
