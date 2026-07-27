"""The message journal and the catch-up sweep.

WHAT THESE TESTS ARE REALLY PROTECTING. The user's requirement is "Webbee must
be aware of every message in the places she can reach". Awareness has exactly
two failure modes, and neither one announces itself:

  * a message is never recorded  -> silent loss, indistinguishable from "nobody
    wrote anything";
  * a message is recorded twice   -> the same message answered twice, which is
    the most visible way an integration looks broken.

Both are invisible in normal use, so each one gets a test that fails loudly.

Every test below was verified by SABOTAGE: the behaviour was deliberately
broken and the test was confirmed to fail. A test that has never been seen red
proves only that it runs.
"""

import handlers_journal as hj
import inbound
import journal
from conftest import (auth_test_payload, channel_payload, err,
                      message_payload, ok, user_payload)

DM_ID = "D0BKU1J252N"
CHANNEL_ID = "C024BE7LR"
SELF_USER = "U0BOTBOT"
SELF_BOT = "B0BOTBOT"


def _dm(channel_id=DM_ID, user="U024BE7LH", **extra) -> dict:
    """A DM as `conversations.list` really returns it.

    `is_member` is FALSE on purpose -- that is what Slack actually sends for a
    DM, and it is the exact field that made the old code call DMs unreachable.
    """
    payload = {"id": channel_id, "is_im": True, "is_member": False,
               "is_archived": False, "user": user}
    payload.update(extra)
    return payload


def _normalised(channel_id=CHANNEL_ID, ts="1690000000.000100", text="hi",
                thread_ts="", **extra) -> dict:
    """A normalised message, built by the REAL normalise().

    `thread_ts` goes into the event payload rather than being patched onto the
    result, so the connector's own thread logic derives reply_thread_ts. Patching
    it afterwards would test the patch, not the code that decides where a reply
    lands.
    """
    event = {"type": "message", "channel": channel_id, "ts": ts, "text": text,
             "user": "U024BE7LH"}
    if thread_ts:
        event["thread_ts"] = thread_ts
    row = inbound.normalise(
        event,
        {"event_id": "Ev1", "team_id": "T024BE7LH"},
        workspace={"workspace_id": "T024BE7LH", "workspace_name": "Acme"},
        self_user_id=SELF_USER)
    row.update(extra)
    return row


# --- the journal remembers, and remembers ONCE -------------------------------

async def test_a_message_is_recorded_and_can_be_read_back(ctx):
    """The core promise: what arrived is still there afterwards."""
    assert await journal.record(ctx, _normalised(text="ship it"),
                                source=journal.SOURCE_PUSH) is True

    rows = await journal.recent(ctx, limit=10)
    assert len(rows) == 1
    assert rows[0]["text"] == "ship it"


async def test_the_same_message_is_never_recorded_twice(ctx):
    """Push and sweep both see the same message; only one row may exist."""
    row = _normalised(ts="1690000000.000200")

    assert await journal.record(ctx, row, source=journal.SOURCE_PUSH) is True
    assert await journal.record(ctx, row, source=journal.SOURCE_SWEEP) is False

    assert len(await journal.recent(ctx, limit=10)) == 1


async def test_same_ts_in_two_channels_are_two_different_messages(ctx):
    """Keying on ts alone would silently drop one of them.

    Slack's ts is unique only within a conversation, so two channels can carry
    the same ts. Collapsing them loses a real message.
    """
    await journal.record(ctx, _normalised(channel_id="C111", ts="1690000000.5"),
                         source=journal.SOURCE_PUSH)
    await journal.record(ctx, _normalised(channel_id="C222", ts="1690000000.5"),
                         source=journal.SOURCE_PUSH)

    assert len(await journal.recent(ctx, limit=10)) == 2


async def test_newest_message_comes_first(ctx):
    """Ordering is done in Python because the store double ignores order_by.

    If this relied on the store, the test would pass while production returned
    an arbitrary order -- and "what came in recently" would be wrong.
    """
    await journal.record(ctx, _normalised(ts="1690000000.100", text="older"),
                         source=journal.SOURCE_PUSH)
    await journal.record(ctx, _normalised(ts="1690000900.100", text="newer"),
                         source=journal.SOURCE_PUSH)

    rows = await journal.recent(ctx, limit=10)
    assert [r["text"] for r in rows] == ["newer", "older"]


async def test_a_store_failure_never_propagates_out_of_record(ctx):
    """The webhook calls this on the delivery path.

    An exception here would become an HTTP 500, which makes Slack retry the
    whole delivery three more times -- turning "could not file it" into
    "delivered it repeatedly".
    """
    async def boom(*_a, **_k):
        raise RuntimeError("store down")

    ctx.store.create = boom
    assert await journal.record(ctx, _normalised(), source="push") is False


# --- the DM correction -------------------------------------------------------

async def test_a_dm_is_reachable_even_though_slack_says_not_a_member(ctx):
    """Proven live: the DM reported is_member=false and read fine.

    Trusting is_member for DMs skips the one conversation type the app
    unconditionally has access to -- and the old text told the user to
    /invite the app into a DM, which Slack offers no way to do.
    """
    reachable, why = journal.is_reachable(_dm())
    assert reachable is True, why


async def test_a_channel_without_membership_is_not_reachable(ctx):
    """The opposite case: for channels is_member is real."""
    reachable, why = journal.is_reachable(
        channel_payload(channel_id="C999", name="random", is_member=False))
    assert reachable is False
    assert "not in this channel" in why


async def test_an_archived_conversation_is_skipped(ctx):
    reachable, _ = journal.is_reachable(
        channel_payload(is_member=True, is_archived=True))
    assert reachable is False


# --- the sweep: awareness without the webhook --------------------------------

async def test_the_sweep_records_messages_from_a_dm_and_a_channel(
        connected_ctx, http):
    """End to end: this is the path that works with no signing secret at all."""
    ctx = connected_ctx
    http.push(auth_test_payload(user_id=SELF_USER, bot_id=SELF_BOT))
    http.push(ok(channels=[_dm(), channel_payload(is_member=True)]))
    http.push(ok(members=[user_payload()]))
    http.push(ok(channels=[channel_payload(is_member=True)]))
    # DMs are swept first, so the DM history is requested before the channel's.
    http.push(ok(messages=[message_payload(ts="1690000001.1", text="dm hello")]))
    http.push(ok(messages=[message_payload(ts="1690000002.1", text="chan hi")]))

    result = await hj.catch_up(ctx, hj.CatchUpParams())
    assert result.status == "success", result.error
    assert result.data.messages_new == 2

    texts = {r["text"] for r in await journal.recent(ctx, limit=10)}
    assert texts == {"dm hello", "chan hi"}


async def test_the_sweep_ignores_the_apps_own_messages(connected_ctx, http):
    """Without this the app becomes aware of itself and answers its own replies.

    In public, forever -- this is the loop that makes an integration infamous.
    """
    ctx = connected_ctx
    http.push(auth_test_payload(user_id=SELF_USER, bot_id=SELF_BOT))
    http.push(ok(channels=[channel_payload(is_member=True)]))
    http.push(ok(members=[user_payload()]))
    http.push(ok(channels=[channel_payload(is_member=True)]))
    http.push(ok(messages=[
        message_payload(ts="1690000003.1", text="mine", user=SELF_USER,
                        bot_id=SELF_BOT),
        message_payload(ts="1690000004.1", text="theirs"),
    ]))

    result = await hj.catch_up(ctx, hj.CatchUpParams())
    assert result.status == "success", result.error

    texts = [r["text"] for r in await journal.recent(ctx, limit=10)]
    assert texts == ["theirs"]
    assert result.data.messages_ignored == 1


async def test_a_second_sweep_records_nothing_new(connected_ctx, http):
    """Re-reading the same history must not duplicate awareness."""
    ctx = connected_ctx
    for _ in range(2):
        http.push(auth_test_payload(user_id=SELF_USER, bot_id=SELF_BOT))
        http.push(ok(channels=[channel_payload(is_member=True)]))
        http.push(ok(members=[user_payload()]))
        http.push(ok(channels=[channel_payload(is_member=True)]))
        http.push(ok(messages=[message_payload(ts="1690000005.1", text="once")]))

    first = await hj.catch_up(ctx, hj.CatchUpParams())
    second = await hj.catch_up(ctx, hj.CatchUpParams(full=True))

    assert first.data.messages_new == 1
    assert second.data.messages_new == 0
    assert len(await journal.recent(ctx, limit=10)) == 1


async def test_the_sweep_never_posts_to_slack(connected_ctx, http):
    """A sweep walks HISTORY. If it could reply it would answer a backlog in
    bulk, in public, to people who wrote days ago."""
    ctx = connected_ctx
    http.push(auth_test_payload(user_id=SELF_USER, bot_id=SELF_BOT))
    http.push(ok(channels=[channel_payload(is_member=True)]))
    http.push(ok(members=[user_payload()]))
    http.push(ok(channels=[channel_payload(is_member=True)]))
    http.push(ok(messages=[message_payload(ts="1690000006.1", text="hello?")]))

    await hj.catch_up(ctx, hj.CatchUpParams())

    posted = [u for u in http.urls()
              if "chat.postMessage" in u or "reactions.add" in u]
    assert posted == []


async def test_one_unreadable_channel_does_not_abort_the_sweep(
        connected_ctx, http):
    """A partial sweep that reports itself beats a total failure."""
    ctx = connected_ctx
    http.push(auth_test_payload(user_id=SELF_USER, bot_id=SELF_BOT))
    http.push(ok(channels=[
        channel_payload(channel_id="C111", name="broken", is_member=True),
        channel_payload(channel_id="C222", name="fine", is_member=True),
    ]))
    http.push(ok(members=[user_payload()]))
    http.push(ok(channels=[channel_payload(is_member=True)]))
    http.push(err("not_in_channel"))
    http.push(ok(messages=[message_payload(ts="1690000007.1", text="got it")]))

    result = await hj.catch_up(ctx, hj.CatchUpParams())
    assert result.status == "success", result.error
    assert result.data.messages_new == 1
    assert result.data.conversations_skipped >= 1


async def test_the_cursor_only_moves_forward(ctx):
    """Rewinding would re-read a backlog on every sweep from then on."""
    await journal.set_cursor(ctx, CHANNEL_ID, "1690000900.000")
    await journal.set_cursor(ctx, CHANNEL_ID, "1690000100.000")
    assert await journal.cursor_for(ctx, CHANNEL_ID) == "1690000900.000"


async def test_cursor_comparison_is_numeric_not_lexical(ctx):
    """'999999999.9' > '1785168241.7' as STRINGS but not as times.

    A string comparison parks the cursor in the future and every later message
    is skipped forever -- silent, total loss of awareness for that channel.
    """
    assert hj._greater("1785168241.7", "999999999.9") is True
    assert hj._greater("999999999.9", "1785168241.7") is False


# --- reading the journal back ------------------------------------------------

async def test_list_inbound_can_narrow_to_dms_and_mentions(connected_ctx):
    ctx = connected_ctx
    await journal.record(ctx, _normalised(channel_id=DM_ID, ts="1690001000.1",
                                          text="private word", is_dm=True),
                         source=journal.SOURCE_PUSH)
    await journal.record(ctx, _normalised(ts="1690001001.1", text="hey <@U0BOTBOT>",
                                          mention_of_bot=True),
                         source=journal.SOURCE_PUSH)
    await journal.record(ctx, _normalised(ts="1690001002.1", text="chatter"),
                         source=journal.SOURCE_PUSH)

    everything = await hj.list_inbound(ctx, hj.ListInboundParams())
    assert everything.data.count == 3

    dms = await hj.list_inbound(ctx, hj.ListInboundParams(dms_only=True))
    assert [m.text for m in dms.data.messages] == ["private word"]

    mentions = await hj.list_inbound(ctx, hj.ListInboundParams(mentions_only=True))
    assert [m.text for m in mentions.data.messages] == ["hey <@U0BOTBOT>"]


async def test_an_empty_journal_explains_itself_instead_of_looking_broken(
        connected_ctx):
    """"No messages" must not be mistaken for "awareness is broken"."""
    result = await hj.list_inbound(connected_ctx, hj.ListInboundParams())
    assert result.status == "success", result.error
    assert result.data.count == 0
    assert "catch_up" in (result.data.note + result.data.detail)


async def test_the_journal_keeps_what_a_reply_needs(ctx):
    """Awareness is only useful if the message can still be answered.

    reply_thread_ts is stored rather than re-derived, because Slack sets
    thread_ts on the PARENT too -- so "has thread_ts" does not mean "is a
    reply", and getting that wrong puts answers in the wrong place.
    """
    await journal.record(
        ctx, _normalised(ts="1690002000.1", thread_ts="1690001999.1"),
        source=journal.SOURCE_PUSH)

    row = (await journal.recent(ctx, limit=1))[0]
    assert row["channel_id"] == CHANNEL_ID
    assert row["reply_thread_ts"] == "1690001999.1"
    assert row["is_thread_reply"] is True


# --- check_access must not repeat the mistake that caused all this ------------

async def test_check_access_counts_dms_as_readable(connected_ctx, http):
    """`is_member` is FALSE for DMs, and check_access must not be fooled by it.

    This is the precise signal that produced a wrong answer to the user's
    question: a workspace where the app reads DMs fine was described as
    "member of 1", which reads as "DMs are not reachable". The report now counts
    what is actually READABLE, so the count matches reality.
    """
    import handlers_directory as hd

    http.push(auth_test_payload())                    # resolve workspace
    http.push(auth_test_payload())                    # identify
    http.push(ok(channels=[
        channel_payload(channel_id="C1", name="general", is_member=True),
        channel_payload(channel_id="C2", name="random", is_member=False),
        _dm(channel_id="D1"),
    ]))

    result = await hd.check_access(connected_ctx, hd.CheckAccessParams())

    assert result.status == "success", result.error
    report = result.data
    # joined counts only the channel; readable counts the channel AND the DM.
    assert report.channels_joined == 1
    assert report.dms_readable == 1
    assert report.conversations_readable == 2
    # The old text told people to invite the app into a DM. There is no such
    # thing in Slack, so that instruction could only waste their time.
    assert "DMs are invisible" not in report.explanation
    assert "no invite" in report.explanation.lower()
    # A reachable DM means history/posting is NOT listed as a gap.
    assert "no channel" not in report.missing_for_common_tasks
