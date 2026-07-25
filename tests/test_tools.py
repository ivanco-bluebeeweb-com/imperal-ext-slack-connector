"""Tool behaviour: connect, read, write.

The expensive mistakes this file guards against, in order of how much they
would cost the user:

1. Storing a token Slack rejects -- the app then reports "connected" and fails
   on every subsequent call.
2. Guessing which channel was meant and POSTING there. There is no undo for a
   message in the wrong channel.
3. A token reaching the store, a log line or an error string.
"""

import handlers_read as hr
import handlers_write as hw
from conftest import (FAKE_BOT_TOKEN, FAKE_BOT_TOKEN_TWO,
                      auth_test_payload, channel_payload, err,
                      message_payload, ok, user_payload)


# --- connect: verify BEFORE store --------------------------------------------

async def test_connect_verifies_the_token_before_storing_it(ctx, http):
    http.push(auth_test_payload(team="Acme", user="webbee"))
    result = await hw.connect_workspace(ctx, hw.ConnectWorkspaceParams(
        token=FAKE_BOT_TOKEN_TWO))

    assert result.status == "success", result.error
    # auth.test came first -- the proof that it was verified, not assumed.
    assert "auth.test" in http.urls()[0]
    stored = await ctx.secrets.get("slack_tokens")
    assert FAKE_BOT_TOKEN_TWO in stored


async def test_a_rejected_token_is_never_stored(ctx, http):
    """The whole point of verify-before-store."""
    http.push(err("invalid_auth"))
    result = await hw.connect_workspace(ctx, hw.ConnectWorkspaceParams(
        token=FAKE_BOT_TOKEN_TWO))

    assert result.status == "error"
    stored = await ctx.secrets.get("slack_tokens") or ""
    assert FAKE_BOT_TOKEN_TWO not in stored


async def test_a_rejected_token_never_appears_in_the_error(ctx, http):
    http.push(err("invalid_auth"))
    result = await hw.connect_workspace(ctx, hw.ConnectWorkspaceParams(
        token=FAKE_BOT_TOKEN_TWO))
    assert FAKE_BOT_TOKEN_TWO not in (result.error or "")


async def test_connecting_a_second_workspace_appends_rather_than_replaces(
        connected_ctx, http):
    """Losing an existing workspace to add a new one is data loss."""
    http.push(auth_test_payload(team="Second", team_id="T999"))
    result = await hw.connect_workspace(connected_ctx,
                                        hw.ConnectWorkspaceParams(
                                            token=FAKE_BOT_TOKEN_TWO))
    assert result.status == "success"
    stored = await connected_ctx.secrets.get("slack_tokens")
    # BOTH must survive: the pre-existing one and the newly added one.
    assert FAKE_BOT_TOKEN in stored
    assert FAKE_BOT_TOKEN_TWO in stored


async def test_an_empty_token_is_refused_without_calling_slack(ctx, http):
    result = await hw.connect_workspace(ctx, hw.ConnectWorkspaceParams(token=""))
    assert result.status == "error"
    assert http.calls == []


# --- no token configured -----------------------------------------------------

async def test_reading_without_a_token_says_what_to_do(ctx):
    result = await hr.list_channels(ctx, hr.ListChannelsParams())
    assert result.status == "error"
    assert result.error_code == "SLACK_TOKEN_MISSING"


# --- refusing to guess -------------------------------------------------------

async def test_an_ambiguous_channel_is_refused_not_guessed(connected_ctx, http):
    """Two plausible channels: ask, never pick one and post to it."""
    http.push(auth_test_payload())
    http.push(ok(channels=[
        channel_payload(channel_id="C1", name="standup-eng"),
        channel_payload(channel_id="C2", name="standup-design"),
    ]))
    result = await hw.send_message(connected_ctx, hw.SendMessageParams(
        channel="standup", text="morning"))

    assert result.status == "error"
    assert result.error_code == "SLACK_TARGET_AMBIGUOUS"
    # Crucially: nothing was posted.
    assert not any("chat.postMessage" in u for u in http.urls())


async def test_an_exact_name_match_wins_over_partial_matches(
        connected_ctx, http):
    """'standup' must resolve to #standup even when #standup-eng exists."""
    http.push(auth_test_payload())
    http.push(ok(channels=[
        channel_payload(channel_id="C1", name="standup-eng"),
        channel_payload(channel_id="C2", name="standup"),
    ]))
    http.push(ok(channel="C2", ts="1690000000.123456",
                 message=message_payload()))
    result = await hw.send_message(connected_ctx, hw.SendMessageParams(
        channel="standup", text="morning"))

    assert result.status == "success", result.error
    posted = [c for c in http.calls if "chat.postMessage" in c["url"]]
    assert posted[0]["json"]["channel"] == "C2"


async def test_an_unknown_channel_explains_the_membership_rule(
        connected_ctx, http):
    http.push(auth_test_payload())
    http.push(ok(channels=[channel_payload(name="general")]))
    result = await hw.send_message(connected_ctx, hw.SendMessageParams(
        channel="nowhere", text="hi"))

    assert result.status == "error"
    assert result.error_code in ("SLACK_TARGET_NOT_FOUND",
                                "SLACK_CHANNEL_NOT_FOUND")
    assert "invite" in (result.error or "").lower()


async def test_a_raw_channel_id_skips_the_name_lookup(connected_ctx, http):
    """Pasting an id out of a Slack link must keep working."""
    http.push(auth_test_payload())
    # An id is confirmed with conversations.info -- a cheap single lookup --
    # rather than by listing the whole workspace.
    http.push(ok(channel=channel_payload(channel_id="C024BE7LR", name="general")))
    http.push(ok(channel="C024BE7LR", ts="1690000000.123456",
                 message=message_payload()))
    result = await hw.send_message(connected_ctx, hw.SendMessageParams(
        channel="C024BE7LR", text="hi"))

    assert result.status == "success", result.error
    # THE POINT: no full-workspace listing was needed to resolve an id.
    assert not any("conversations.list" in u for u in http.urls())
    assert any("conversations.info" in u for u in http.urls())


# --- reading ------------------------------------------------------------------

async def test_read_channel_returns_readable_text_not_raw_mrkdwn(
        connected_ctx, http):
    """The value of the connector: names, not <@U024BE7LH>."""
    http.push(auth_test_payload())
    http.push(ok(channels=[channel_payload(channel_id="C1", name="general")]))
    # ORDER MATTERS: read_channel fetches history first, then resolves the
    # user ids it found into names.
    http.push(ok(messages=[message_payload(
        text="hi <@U1> see <http://ex.com|the doc>", user="U1")]))
    http.push(ok(members=[user_payload(user_id="U1", name="vlad")]))

    result = await hr.read_channel(connected_ctx, hr.ReadChannelParams(
        channel="general"))

    assert result.status == "success", result.error
    blob = str(result.data)
    assert "@vlad" in blob
    assert "<@U1>" not in blob


async def test_list_channels_filters_by_name_fragment(connected_ctx, http):
    http.push(auth_test_payload())
    http.push(ok(channels=[
        channel_payload(channel_id="C1", name="general"),
        channel_payload(channel_id="C2", name="standup-eng"),
    ]))
    result = await hr.list_channels(connected_ctx,
                                   hr.ListChannelsParams(query="standup"))
    assert result.status == "success", result.error
    blob = str(result.data)
    assert "standup-eng" in blob
    assert "general" not in blob


async def test_search_on_a_bot_token_explains_the_user_token_rule(
        connected_ctx, http):
    """Slack does not expose search to bots at all -- say so, don't just fail."""
    http.push(auth_test_payload())
    http.push(err("not_allowed_token_type"))
    result = await hr.search_messages(connected_ctx,
                                      hr.SearchMessagesParams(query="deploy"))
    assert result.status == "error"
    assert result.error_code == "SLACK_WRONG_TOKEN_TYPE"
    assert "xoxp" in (result.error or "").lower()


async def test_not_in_channel_is_a_membership_message_not_a_generic_error(
        connected_ctx, http):
    http.push(auth_test_payload())
    http.push(ok(channels=[channel_payload(channel_id="C1", name="private-ch")]))
    http.push(err("not_in_channel"))
    result = await hr.read_channel(connected_ctx,
                                   hr.ReadChannelParams(channel="private-ch"))
    assert result.status == "error"
    assert result.error_code == "SLACK_NOT_IN_CHANNEL"


# --- threads ------------------------------------------------------------------

async def test_replying_in_a_thread_sends_the_exact_ts(connected_ctx, http):
    """A rounded thread_ts silently breaks threading."""
    raw = "1690000000.000100"
    http.push(auth_test_payload())
    http.push(ok(channels=[channel_payload(channel_id="C1", name="general")]))
    http.push(ok(channel="C1", ts="1690000001.222", message=message_payload()))

    result = await hw.send_message(connected_ctx, hw.SendMessageParams(
        channel="general", text="reply", thread_ts=raw))

    assert result.status == "success", result.error
    posted = [c for c in http.calls if "chat.postMessage" in c["url"]]
    assert posted[0]["json"]["thread_ts"] == raw


# --- destructive --------------------------------------------------------------

def test_deleting_a_message_is_declared_destructive():
    """Slack deletion is final -- there is no trash to restore from.

    action_type drives the kernel's confirmation guard, so this classification
    IS the gate.
    """
    from app import chat
    fns = getattr(chat, "_functions", None) or getattr(chat, "functions", {})
    spec = fns["delete_message"]
    action_type = (spec.get("action_type") if isinstance(spec, dict)
                   else getattr(spec, "action_type", ""))
    assert action_type == "destructive"
