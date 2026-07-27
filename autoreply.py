"""Answering people in Slack when they address Webbee.

WHY THIS LIVES IN THE APP AND NOT IN AN AUTOMATION RULE
-------------------------------------------------------
The obvious shape was a scheduled automation rule: poll, find unanswered
mentions, reply. Two facts ruled it out.

* The platform does not offer Slack events as automation triggers, so a rule
  could not fire ON a mention -- it would poll anyway.
* Automation slots are a metered, capped resource. Spending one permanently on
  "read my own journal" is a bad trade when the app already runs its own
  schedule for the sweep, and the sweep is the exact moment new messages become
  known. Answering there costs nothing extra and cannot drift out of step with
  what was just collected.

So the reply pass hangs off the existing hourly sweep: collect, then answer
what is waiting.

THE THREE THINGS THAT MAKE THIS SAFE
------------------------------------
1. OFF BY DEFAULT. An app that starts writing to a human's Slack the moment it
   is deployed is an incident, not a feature. Nothing is sent until someone
   turns it on explicitly, per workspace.

2. IT ANSWERS ONCE. Every reply marks its message answered in the journal
   (see journal.mark_thread_replied). Without that, an hourly schedule
   re-answers the same person forever -- and the messages are already sent, so
   there is no undo. This is the failure mode the guard exists for.

3. IT NEVER ANSWERS ITSELF. The journal already drops the app's own messages
   (inbound.is_noise), which is what stops the obvious loop. The extra belt
   here: a per-run cap, so even a pathological journal state cannot produce a
   flood -- it produces at most a handful, and the log says why.

WHAT IT DOES NOT DECIDE
-----------------------
Money, deletion, publishing to a live site, promises about deadlines. The
prompt instructs the model to hand those to Vladislav rather than answer them.
An agent that commits its owner to something is worse than an agent that says
"I passed this on".
"""

from __future__ import annotations

import time

import journal
import slack_objects as so

#: Where the on/off switch lives. Its own collection rather than a field on the
#: workspace record: the switch is about BEHAVIOUR, and burying it in the
#: credential row would mean a token refresh could silently reset it.
SETTINGS_COLLECTION = "slack_autoreply_settings"

#: How many messages one pass will answer, at most.
#:
#: Not a performance knob -- a blast radius. If the journal ever ends up with a
#: hundred rows that look unanswered (a bad migration, a cleared `replied`
#: flag), the difference between a bounded pass and an unbounded one is the
#: difference between "three odd replies, investigate" and "a hundred messages
#: fired into a client's Slack".
MAX_REPLIES_PER_RUN = 5

#: Messages older than this are never auto-answered.
#:
#: A backlog answered in bulk reads as a malfunction: nobody wants a reply to
#: something they wrote two weeks ago, and a burst of them looks like the app
#: broke. New messages are the ones worth answering automatically; anything
#: older is for a human to look at.
MAX_AGE_SECONDS = 24 * 60 * 60


# WHY a pass did nothing, as NAMES rather than prose. The reason travels into a
# log line and into a test assertion; pinning either to a Russian sentence means
# rewording the message breaks the test, and the usual fix is to loosen the test
# until it checks nothing.
REASON_DISABLED = "disabled"
REASON_NOTHING_WAITING = "nothing_waiting"

#: Human wording for each reason, kept beside the names so the two cannot drift.
REASON_ALL_FAILED = "all_failed"

#: Human wording for each reason, kept beside the names so the two cannot drift.
REASON_TEXT = {
    REASON_DISABLED: "автоответы выключены",
    REASON_NOTHING_WAITING: "нет обращений без ответа",
    REASON_ALL_FAILED: "ни один ответ не удалось отправить",
}


async def _warn(ctx, message: str) -> None:
    """Log a warning without ever raising.

    Every caller here is on a path that has already done something real (or is
    about to). Letting a logging failure propagate would turn a bookkeeping
    hiccup into a failed reply, which is strictly worse than a missing log
    line.
    """
    try:
        await ctx.log(message, level="warn")
    except Exception:
        pass


async def _settings_doc(ctx):
    """The settings document, or None."""
    try:
        page = await ctx.store.query(SETTINGS_COLLECTION, limit=1)
    except Exception:
        return None
    rows = getattr(page, "data", None) or []
    return rows[0] if rows else None


async def is_enabled(ctx) -> bool:
    """Whether auto-reply is switched on.

    Fails towards OFF. A store blip must never be the reason the app starts
    writing to someone's Slack unasked: silence is recoverable, an unwanted
    message is not.
    """
    doc = await _settings_doc(ctx)
    if doc is None:
        return False
    data = getattr(doc, "data", None) or {}
    return bool(data.get("enabled"))


async def set_enabled(ctx, enabled: bool, *, note: str = "") -> bool:
    """Turn auto-reply on or off. Returns whether the WRITE SUCCEEDED.

    Success, deliberately -- not the resulting state. Returning the state looks
    natural and is a trap: switching OFF would return False, the caller cannot
    tell that apart from "the store refused", and reporting a failure for a
    successful switch-off means the user cannot turn Webbee's replies off with
    any confidence. A test caught exactly that.

    The state is not returned because the caller already knows it: it is the
    argument they just passed. What they cannot know is whether it stuck.
    """
    data = {
        "enabled": bool(enabled),
        "changed_at": time.time(),
        "note": note or "",
    }
    doc = await _settings_doc(ctx)
    try:
        if doc is not None and getattr(doc, "id", ""):
            await ctx.store.update(SETTINGS_COLLECTION, doc.id, data)
        else:
            await ctx.store.create(SETTINGS_COLLECTION, data)
    except Exception:
        await _warn(ctx, "Slack auto-reply switch could not be saved")
        return False
    return True


def _is_fresh(row: dict, *, now: float | None = None) -> bool:
    """True when the message is recent enough to auto-answer."""
    moment = now if now is not None else time.time()
    try:
        posted = float(str(row.get("message_ts") or "0").split(".")[0])
    except (TypeError, ValueError):
        return False
    if posted <= 0:
        return False
    return (moment - posted) <= MAX_AGE_SECONDS


async def pending(ctx, *, now: float | None = None) -> list[dict]:
    """Messages addressed to Webbee that are still waiting for an answer.

    TWO queries, not one: the journal filters are AND-ed, so mentions and DMs
    cannot be expressed in a single call. Merged here and de-duplicated on the
    message key -- a DM that also mentions the bot must not be answered twice.
    """
    mentions = await journal.recent(ctx, limit=50, mentions_only=True,
                                    unresolved_only=True)
    dms = await journal.recent(ctx, limit=50, dms_only=True,
                               unresolved_only=True)

    merged: dict[str, dict] = {}
    for row in list(mentions) + list(dms):
        key = str(row.get("message_key") or "")
        if not key or key in merged:
            continue
        if not _is_fresh(row, now=now):
            continue
        merged[key] = row

    rows = sorted(merged.values(),
                  key=lambda r: journal._sort_key(r), reverse=True)
    return rows[:MAX_REPLIES_PER_RUN]


def build_prompt(row: dict) -> str:
    """The instruction that turns one Slack message into one reply.

    Written as a brief, not a template. A canned answer is worse than no
    answer: it teaches people the app is a wall, and they stop writing.
    """
    who = str(row.get("user_display_name") or "коллега")
    where = ("личном сообщении" if row.get("is_dm")
             else f"канале #{row.get('channel_name') or ''}".strip())
    text = str(row.get("text_readable") or row.get("text") or "")
    when = so.humanize_ts(str(row.get("message_ts") or ""))

    return (
        "Ты — Webbee, ИИ-агент Imperal Cloud. К тебе обратились в Slack, в "
        f"{where}. Автор: {who}. Время: {when}.\n\n"
        f"Сообщение:\n{text}\n\n"
        "Напиши ОДИН ответ для Slack. Правила:\n"
        "• По существу и коротко — это чат, а не письмо. 2–5 предложений.\n"
        "• Если нужны факты, которых у тебя нет, честно скажи, что проверишь, "
        "и назови, что именно проверишь. Не выдумывай данные.\n"
        "• Если вопрос требует решения про деньги, удаление данных, "
        "публикацию на живой сайт или сроки — не решай сам. Скажи, что "
        "передал вопрос Владиславу.\n"
        "• Не обещай того, чего не можешь сделать.\n"
        "• Без приветствий-шаблонов и без подписи. Сразу по делу.\n"
        "• На том языке, на котором написано сообщение."
    )


async def _compose(ctx, row: dict) -> str:
    """Ask the model for a reply. Empty string means "do not send"."""
    ai = getattr(ctx, "ai", None)
    if ai is None:
        return ""
    try:
        out = await ai.complete(build_prompt(row))
    except Exception:
        await _warn(ctx, "Slack auto-reply could not be composed")
        return ""

    text = str(getattr(out, "text", "") or getattr(out, "content", "") or "")
    return text.strip()


async def run_once(ctx, *, now: float | None = None) -> dict:
    """One pass: answer what is waiting. Returns a report of what happened.

    Reports rather than logs-and-forgets, because the caller is a schedule and
    the only way anyone learns what this did is the value it hands back.
    """
    if not await is_enabled(ctx):
        return {"enabled": False, "considered": 0, "replied": 0,
                "skipped": 0, "reason": REASON_DISABLED,
                "detail": REASON_TEXT[REASON_DISABLED]}

    rows = await pending(ctx, now=now)
    if not rows:
        return {"enabled": True, "considered": 0, "replied": 0,
                "skipped": 0, "reason": REASON_NOTHING_WAITING,
                "detail": REASON_TEXT[REASON_NOTHING_WAITING]}

    replied = 0
    skipped = 0
    for row in rows:
        text = await _compose(ctx, row)
        if not text:
            skipped += 1
            continue

        channel_id = str(row.get("channel_id") or "")
        thread_ts = str(row.get("reply_thread_ts")
                        or row.get("message_ts") or "")
        if not channel_id or not thread_ts:
            skipped += 1
            continue

        # THROUGH THE TOOL, not a raw HTTP call. The tool is what threads the
        # reply, marks the message answered and emits the event; a shortcut
        # here would answer without closing the loop -- and then answer the
        # same person again next hour.
        #
        # Imported here rather than at module scope: handlers_post imports the
        # journal, and a top-level import in both directions is how a circular
        # import turns into a dead app at startup.
        try:
            import handlers_post as hp

            out = await hp.send_message(ctx, hp.SendMessageParams(
                channel=channel_id, text=text, thread_ts=thread_ts))
        except Exception:
            out = None
            await _warn(ctx, "Slack auto-reply could not be sent")

        # A failed send is NOT counted as a reply. Counting it would make the
        # report claim someone was answered when their message is still
        # waiting -- and a wrong report is worse than a missing one, because
        # nobody goes looking.
        if out is not None and getattr(out, "status", "") == "success":
            replied += 1
        else:
            skipped += 1

    reason = "" if replied else REASON_ALL_FAILED
    return {"enabled": True, "considered": len(rows), "replied": replied,
            "skipped": skipped, "reason": reason,
            "detail": REASON_TEXT.get(reason, "")}
