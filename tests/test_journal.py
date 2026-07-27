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


# --- the hourly sweep --------------------------------------------------------

def test_the_sweep_is_actually_scheduled():
    """Awareness must not depend on somebody remembering to ask.

    Without a schedule the journal is an archive you consult, not awareness that
    keeps itself current -- and the difference is invisible until the moment
    someone expects Webbee to already know.
    """
    import main  # noqa: F401  (registers decorators)
    from app import ext

    assert "slack_catch_up" in ext.schedules, "the sweep does not run on its own"
    cron = ext.schedules["slack_catch_up"].cron
    assert len(cron.split()) == 5, f"not a cron expression: {cron!r}"
    # Not every minute: this polls somebody else's API forever.
    assert not cron.startswith("*"), f"sweeping too often: {cron!r}"


async def test_the_scheduled_sweep_records_messages(connected_ctx, http):
    """The schedule must reach the same journal the tool writes to."""
    http.push(auth_test_payload())                 # resolve workspace
    http.push(ok(channels=[_dm(channel_id="D1")]))  # conversations.list
    http.push(ok(members=[user_payload(user_id="U024BE7LH", name="vlad")]))
    http.push(ok(channels=[]))                     # name_maps channels
    http.push(ok(messages=[
        message_payload(ts="1690000500.1", text="are you there?",
                        user="U024BE7LH"),
    ]))

    await hj.scheduled_catch_up(connected_ctx)

    rows = await journal.recent(connected_ctx, limit=5)
    assert len(rows) == 1, "the scheduled sweep recorded nothing"
    assert rows[0]["text"] == "are you there?"
    assert rows[0]["source"] == journal.SOURCE_SWEEP


async def test_the_scheduled_sweep_never_raises(connected_ctx, http, monkeypatch):
    """A throwing scheduled task gets retried or disabled -- neither helps.

    The next hourly pass recovers anything missed, because the cursor only
    advances on success. So swallowing costs nothing and keeps the schedule
    alive, while propagating could switch awareness off entirely.

    The exception is forced by making the sweep itself explode. An earlier
    version of this test just starved the HTTP queue and passed even with the
    guard deleted -- sabotage exposed that: catch_up returns an error RESULT
    rather than raising, so nothing was ever thrown and the test proved nothing.
    """
    async def exploding_sweep(*_a, **_kw):
        raise RuntimeError("slack unreachable")

    monkeypatch.setattr(hj, "catch_up", exploding_sweep)

    await hj.scheduled_catch_up(connected_ctx)  # must simply return


async def test_the_scheduled_sweep_tolerates_a_dead_slack(connected_ctx, http):
    """No usable Slack response must not become a crashing background task."""
    # Nothing queued: every Slack call fails.
    await hj.scheduled_catch_up(connected_ctx)


# --- the status report must not call a working setup "not ready" --------------

async def test_status_separates_push_readiness_from_awareness(connected_ctx, http):
    """"Push is off" and "Webbee is unaware" are different facts.

    Reporting only push readiness produced "not ready" while the sweep was
    recording messages perfectly well -- which invites debugging a feature that
    already works, and hides the one thing actually missing (the secret).
    """
    import handlers_events as he

    await journal.record(ctx=connected_ctx,
                         normalised=_normalised(ts="1690003000.1"),
                         source=journal.SOURCE_SWEEP)

    http.push(auth_test_payload(team="Acme"))
    result = await he.inbound_status(connected_ctx, he.InboundStatusParams())

    assert result.status == "success", result.error
    data = result.data
    # No signing secret -> push is genuinely not ready...
    assert data.ready is False
    # ...but she IS aware, and the report has to say so.
    assert data.aware is True
    assert data.messages_recorded == 1
    assert data.from_sweep == 1
    assert data.state == "sweep only"
    assert data.sweep_schedule == journal.SWEEP_CRON
    # And it must not promise automation triggers that cannot be created.
    assert "not yet selectable" in data.detail


async def test_status_does_not_claim_a_sweep_interval_it_does_not_use():
    """The reported interval and the real schedule must be the same string.

    They used to be two literals, and nothing compared them -- so the report
    could advertise an interval the schedule had long since changed away from.
    """
    import main  # noqa: F401
    from app import ext

    assert ext.schedules["slack_catch_up"].cron == journal.SWEEP_CRON


# --- joining channels --------------------------------------------------------

async def test_joining_public_channels_the_app_is_not_in(connected_ctx, http):
    """One call instead of one /invite per channel, forever.

    This is the point of the tool: a bot token can self-join PUBLIC channels, so
    the manual invite was never required for the common case.
    """
    http.push(auth_test_payload())
    http.push(ok(channels=[
        channel_payload(channel_id="C1", name="general", is_member=True),
        channel_payload(channel_id="C2", name="random", is_member=False),
        channel_payload(channel_id="C3", name="lol-kek", is_member=False),
    ]))
    http.push(ok())   # join C2
    http.push(ok())   # join C3

    result = await hj.join_channels(connected_ctx, hj.JoinChannelsParams())

    assert result.status == "success", result.error
    assert result.data.joined_count == 2
    assert "#random" in result.data.joined and "#lol-kek" in result.data.joined
    # It must not try to join the channel it is already in.
    assert sum(1 for u in http.urls() if "conversations.join" in u) == 2


async def test_a_refused_join_is_reported_not_swallowed(connected_ctx, http):
    """A scope refusal must not hide behind a cheerful summary.

    "Nothing happened" has two meanings -- already a member, or Slack said no --
    and they need opposite reactions. Collapsing them is how a missing
    channels:join scope goes unnoticed.
    """
    http.push(auth_test_payload())
    http.push(ok(channels=[
        channel_payload(channel_id="C2", name="random", is_member=False),
    ]))
    http.push(err("missing_scope"))

    result = await hj.join_channels(connected_ctx, hj.JoinChannelsParams())

    assert result.status == "success", result.error
    assert result.data.joined_count == 0
    assert result.data.failed_count == 1
    assert "random" in result.data.failed
    assert "could not join" in result.data.detail.lower()


async def test_a_private_channel_is_named_as_needing_a_human(
        connected_ctx, http):
    """Slack has no self-join for a private channel -- say so, don't fail vaguely."""
    http.push(auth_test_payload())
    http.push(ok(channels=[
        channel_payload(channel_id="C1", name="general", is_member=True),
    ]))

    result = await hj.join_channels(
        connected_ctx, hj.JoinChannelsParams(channels="#secret-plans"))

    assert result.status == "success", result.error
    assert "secret-plans" in result.data.needs_a_human
    assert "invite" in result.data.detail.lower()


async def test_dry_run_joins_nothing(connected_ctx, http):
    """A write tool needs a way to be asked what it WOULD do."""
    http.push(auth_test_payload())
    http.push(ok(channels=[
        channel_payload(channel_id="C2", name="random", is_member=False),
    ]))

    result = await hj.join_channels(
        connected_ctx, hj.JoinChannelsParams(dry_run=True))

    assert result.status == "success", result.error
    assert result.data.joined_count == 0
    assert not any("conversations.join" in u for u in http.urls()), \
        "dry run actually joined a channel"


def test_the_schedule_never_joins_channels_on_its_own():
    """Joining is visible in Slack -- it must stay a deliberate act.

    The hourly sweep reads; it must never quietly add the app to channels,
    because a background task changing channel membership is not something a
    user can anticipate or consent to.
    """
    import inspect
    src = inspect.getsource(hj.scheduled_catch_up)
    assert "join_channels" not in src


async def test_naming_a_channel_it_is_already_in_does_not_rejoin(
        connected_ctx, http):
    """Re-joining is not harmless: Slack posts a visible "added" line.

    Named channels take a different path from the join-everything case, where
    the list is already filtered -- so this path needs its own guard. Sabotage
    proved it: breaking the membership filter kept every other test green.
    """
    http.push(auth_test_payload())
    http.push(ok(channels=[
        channel_payload(channel_id="C1", name="general", is_member=True),
        channel_payload(channel_id="C2", name="random", is_member=False),
    ]))
    http.push(ok())   # the single legitimate join of #random

    result = await hj.join_channels(connected_ctx, hj.JoinChannelsParams(
        channels="#general, #random"))

    assert result.status == "success", result.error
    assert result.data.joined_count == 1
    assert result.data.already_count == 1
    # Exactly ONE join call -- #general must not be touched again.
    assert sum(1 for u in http.urls() if "conversations.join" in u) == 1


async def test_a_dry_run_changes_nothing(connected_ctx, http):
    """A write tool needs a way to be asked "what would you do?" first."""
    http.push(auth_test_payload())
    http.push(ok(channels=[
        channel_payload(channel_id="C2", name="random", is_member=False),
    ]))

    result = await hj.join_channels(connected_ctx, hj.JoinChannelsParams(
        dry_run=True))

    assert result.status == "success", result.error
    assert result.data.joined_count == 0
    assert not any("conversations.join" in u for u in http.urls()), \
        "a dry run contacted Slack to join"


async def test_a_skipped_dm_is_not_blamed_on_a_missing_invite(
        connected_ctx, http):
    """Advice must match the CAUSE, not just the fact of a skip.

    A blanket "type /invite @imperal" was appended to every skip, including a
    DM -- which has no invite at all. That is the same class of confidently
    wrong guidance this connector already had to correct once.
    """
    http.push(auth_test_payload())
    http.push(ok(channels=[_dm(channel_id="D9", user="USLACKBOT")]))
    http.push(ok(members=[]))
    http.push(ok(channels=[]))
    # The DM read fails, so it lands in the skip list.
    http.push(err("channel_not_found"))

    result = await hj.catch_up(connected_ctx, hj.CatchUpParams())

    assert result.status == "success", result.error
    detail = result.data.detail
    assert "/invite" not in detail, \
        f"told the user to invite the app into a DM: {detail!r}"


async def test_a_skipped_channel_points_at_the_join_tool(connected_ctx, http):
    """When membership IS the cause, say so -- and name the tool that fixes it."""
    http.push(auth_test_payload())
    http.push(ok(channels=[
        channel_payload(channel_id="C7", name="random", is_member=False),
    ]))
    http.push(ok(members=[]))
    http.push(ok(channels=[]))

    result = await hj.catch_up(connected_ctx, hj.CatchUpParams())

    assert result.status == "success", result.error
    assert "join_channels" in result.data.detail


def test_the_membership_note_offers_the_self_join_before_the_chore():
    """Guidance must lead with what the app can do unaided.

    The note used to prescribe a manual /invite for public channels, which is
    a chore the app can now do itself -- and repeating it forever, for every new
    channel, was exactly the cost join_channels removes.
    """
    import shared

    note = shared.MEMBERSHIP_NOTE
    assert "join_channels" in note
    # Private channels genuinely need a human; that must survive.
    assert "/invite" in note and "rivate" in note
    # And the DM correction must not regress.
    assert "NO invite" in note
