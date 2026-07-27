"""Answering people in Slack by herself -- and the guards that make it safe.

The capability under test WRITES TO OTHER PEOPLE'S SLACK on a schedule, with
nobody watching. Every test here exists because the corresponding mistake would
be visible to someone else and impossible to take back.
"""

import autoreply
import journal


async def _mention(ctx, *, ts: str, channel: str = "C0000000001", text: str = "@Imperal помоги",
                   is_dm: bool = False, now: float = 1_000_000.0):
    """Journal one message addressed to Webbee, fresh as of `now`."""
    await journal.record(ctx, {
        "channel_id": channel,
        "channel_name": "general",
        "message_ts": ts,
        "text": text,
        "text_readable": text,
        "user_id": "U9",
        "user_display_name": "Vlad",
        "mention_of_bot": not is_dm,
        "is_dm": is_dm,
        "reply_thread_ts": ts,
    }, source=journal.SOURCE_SWEEP)


# --- off by default ----------------------------------------------------------

async def test_nothing_is_answered_until_someone_turns_it_on(ctx):
    """THE DEFAULT IS SILENCE.

    An app that starts writing to a human's Slack the moment it is deployed is
    an incident, not a feature. The switch has to be flipped by a person, and
    "was it deployed?" must never be the same question as "does it reply?".
    """
    await _mention(ctx, ts="999999.1")

    assert await autoreply.is_enabled(ctx) is False, "по умолчанию должно быть выключено"

    report = await autoreply.run_once(ctx, now=999999.5)

    assert report["replied"] == 0
    assert report["reason"] == autoreply.REASON_DISABLED
    # The message is still waiting: refusing to answer must not lose it.
    assert len(await autoreply.pending(ctx, now=999999.5)) == 1


async def test_turning_it_on_is_remembered(ctx):
    assert await autoreply.set_enabled(ctx, True) is True
    assert await autoreply.is_enabled(ctx) is True

    assert await autoreply.set_enabled(ctx, False) is True
    assert await autoreply.is_enabled(ctx) is False


# --- what counts as waiting --------------------------------------------------

async def test_mentions_and_dms_are_both_collected_and_never_doubled(ctx):
    """Two queries, one list.

    The journal's filters are AND-ed, so mentions and DMs need separate reads.
    A DM that also mentions the bot appears in BOTH -- answering it twice would
    send the same person two replies to one message.
    """
    await _mention(ctx, ts="1000.1", text="@Imperal в канале")
    await _mention(ctx, ts="1000.2", channel="D1", text="@Imperal в личке",
                   is_dm=True)
    # This one mentions the bot AND is a DM: the overlap case.
    await journal.record(ctx, {
        "channel_id": "D1", "message_ts": "1000.3", "text": "@Imperal и то и то",
        "text_readable": "@Imperal и то и то", "user_id": "U9",
        "mention_of_bot": True, "is_dm": True, "reply_thread_ts": "1000.3",
    }, source=journal.SOURCE_SWEEP)

    rows = await autoreply.pending(ctx, now=1000.9)
    keys = [r["message_key"] for r in rows]

    assert len(keys) == len(set(keys)), f"дубликаты в очереди: {keys}"
    assert len(keys) == 3


async def test_an_old_message_is_not_answered_out_of_the_blue(ctx):
    """A day-old question does not deserve a surprise answer.

    Found by thinking about the first switch-on: the journal holds history, so
    enabling auto-reply would otherwise dump answers onto conversations that
    moved on days ago -- confusing for everyone in the channel and impossible
    to recall.
    """
    now = 1_000_000.0
    await _mention(ctx, ts="999000.1")            # ~28 minutes old: fresh
    await _mention(ctx, ts="900000.1", channel="C0000000002")  # ~28 hours old: stale

    rows = await autoreply.pending(ctx, now=now)

    assert [r["message_ts"] for r in rows] == ["999000.1"]


async def test_an_answered_message_drops_out_of_the_queue(ctx):
    """The loop guard, from the auto-reply side."""
    await _mention(ctx, ts="1000.1")
    assert len(await autoreply.pending(ctx, now=1000.9)) == 1

    await journal.mark_replied(ctx, "C0000000001", "1000.1")

    assert await autoreply.pending(ctx, now=1000.9) == []


async def test_a_run_answers_at_most_a_handful(ctx):
    """A cap, so no journal state can turn into a flood.

    Not a performance concern: a burst of replies in someone else's channel
    reads as a malfunctioning bot, and the messages cannot be unsent.
    """
    for i in range(12):
        await _mention(ctx, ts=f"1000.{i}")

    rows = await autoreply.pending(ctx, now=1000.9)

    assert len(rows) == autoreply.MAX_REPLIES_PER_RUN


# --- the prompt --------------------------------------------------------------

def test_the_prompt_carries_the_actual_question():
    """A brief, not a template.

    If the message text did not reach the prompt, the reply would be generic
    filler -- which teaches people the app is a wall and they stop writing.
    """
    prompt = autoreply.build_prompt({
        "text_readable": "@Imperal почему упал деплой?",
        "user_display_name": "Vlad",
        "channel_name": "general",
    })

    assert "почему упал деплой?" in prompt
    assert "Vlad" in prompt


def test_the_prompt_forbids_deciding_things_that_are_not_its_call():
    """Money, deletion, publishing, deadlines: hand those to Vladislav.

    An agent that commits its owner to something in front of colleagues is a
    problem no error message can undo.
    """
    prompt = autoreply.build_prompt({"text_readable": "заплатим?",
                                     "user_display_name": "Vlad"})

    lowered = prompt.lower()
    assert "владислав" in lowered


# --- end to end: the pass that must not repeat itself ------------------------

async def test_a_pass_answers_once_and_never_again(connected_ctx, http):
    """THE WHOLE POINT, and the most expensive failure available.

    A schedule that answers without closing the loop writes to the same person
    on every run, forever, and the messages are already delivered -- there is no
    undo and no apology that makes an hourly repeat acceptable. So this asserts
    both halves in one flow: the reply goes out, AND the second pass is silent.
    """
    from conftest import auth_test_payload, channel_payload, message_payload, ok

    await _mention(connected_ctx, ts="1000.1", channel="C0000000002")
    await autoreply.set_enabled(connected_ctx, True)

    # A composed answer, then the Slack calls the send path makes.
    connected_ctx.ai = _FakeAI("Посмотрела — вот что нашла по заголовкам.")
    http.push(auth_test_payload())
    http.push(ok(channel=channel_payload(
        channel_id="C0000000002", name="general")))
    http.push(ok(channel="C0000000002", ts="2000.5", message=message_payload()))

    first = await autoreply.run_once(connected_ctx, now=1000.9)

    assert first["replied"] == 1, first
    posted = [c for c in http.calls if "chat.postMessage" in c["url"]]
    assert len(posted) == 1, "ответ не отправлен"
    # Threaded, not dumped at the top of the channel.
    assert posted[0]["json"]["thread_ts"] == "1000.1"

    # SECOND PASS: nothing left to answer, and crucially no second postMessage.
    second = await autoreply.run_once(connected_ctx, now=1001.0)

    assert second["replied"] == 0
    assert second["reason"] == autoreply.REASON_NOTHING_WAITING
    posted_after = [c for c in http.calls if "chat.postMessage" in c["url"]]
    assert len(posted_after) == 1, (
        "тот же человек получил второй ответ — это ежечасный спам")


async def test_a_message_is_left_waiting_when_no_answer_could_be_composed(
        connected_ctx, http):
    """No text, no send -- and the message stays in the queue.

    Sending an empty or placeholder reply would be worse than silence: it closes
    the message as \"answered\" while the person got nothing of value.
    """
    await _mention(connected_ctx, ts="1000.2", channel="C0000000002")
    await autoreply.set_enabled(connected_ctx, True)
    connected_ctx.ai = _FakeAI("")          # composition yields nothing

    report = await autoreply.run_once(connected_ctx, now=1000.9)

    assert report["replied"] == 0
    assert report["skipped"] == 1
    assert not any("chat.postMessage" in c["url"] for c in http.calls)
    # Still waiting: a failed composition must not silently swallow a question.
    assert len(await autoreply.pending(connected_ctx, now=1000.9)) == 1


class _FakeAI:
    """Stands in for the model: returns one canned completion.

    Deliberately minimal. The tests here are about the GUARDS -- threading,
    marking, caps, silence -- and a fake that tried to be clever would make
    failures ambiguous between \"the guard broke\" and \"the fake broke\".
    """

    def __init__(self, text: str):
        self._text = text
        self.prompts: list[str] = []

    async def complete(self, prompt: str, model: str = "", **kwargs):
        self.prompts.append(prompt)

        class _Result:
            text = self._text
            content = self._text
        return _Result()
