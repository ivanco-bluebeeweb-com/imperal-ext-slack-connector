"""slack_objects: making Slack's wire format readable.

Two invariants matter most here:

* `ts` is IDENTITY and must survive verbatim. Rounding it produces a timestamp
  Slack no longer recognises, so a reply lands in the wrong place or nowhere.
* mrkdwn must be resolved into names. `<@U024BE7LH>` is useless to a person and
  actively misleading to a model summarising a channel.
"""

import re

import slack_objects as so


# --- timestamps: display vs identity ----------------------------------------

def test_ts_is_humanized_for_display():
    assert so.humanize_ts("1690000000.123456") == "22 июл 2023, 04:26"


def test_a_rendered_date_is_not_mistaken_for_a_phone_number():
    """The displayed time must survive the platform's PII redaction.

    FOUND LIVE. The old format rendered "2026-07-27 18:28", and the leading run
    "2026-07-27 18" is ten digits with separators — the PII guard read it as a
    phone number and the whole string arrived in chat as "<PHONE>:28". Webbee
    could report WHAT was posted but not WHEN, which is precisely the question
    "what's the latest message?" asks.

    The invariant is not the exact wording: it is that the string never
    contains a long unbroken digit run, so letters must separate the parts.
    """
    rendered = so.humanize_ts("1785176897.496389")

    assert rendered == "27 июл 2026, 18:28"

    # No run of digits long enough to read as a phone number.
    longest = max((len(run) for run in re.findall(r"[\d\s.\-]+", rendered)),
                  default=0)
    assert longest < 10, (
        f"'{rendered}' contains a {longest}-char digit run — long enough to be "
        f"redacted as a phone number")
    assert any(ch.isalpha() for ch in rendered), (
        f"'{rendered}' is all digits and separators; it needs letters to be "
        f"distinguishable from a phone number")


def test_an_unparseable_ts_yields_empty_not_an_exception():
    """A message with an odd ts must still be readable."""
    assert so.humanize_ts("not-a-ts") == ""
    assert so.humanize_ts("") == ""
    assert so.humanize_ts(None) == ""


def test_thread_info_keeps_the_raw_ts_string_verbatim():
    """THE precision rule: a thread_ts that goes back to Slack is untouched.

    `ts` is a message's IDENTITY, not just a date. Parsing it into a float and
    formatting it back loses microsecond precision and yields a ts Slack no
    longer recognises -- replies then land in the wrong place or nowhere. So no
    helper here may ever round-trip it through a number.
    """
    raw = "1690000000.123456"
    thread_ts, replies = so.thread_info({"ts": raw, "thread_ts": raw,
                                         "reply_count": 3})
    assert thread_ts == raw
    assert isinstance(thread_ts, str)
    assert replies == 3


def test_a_ts_with_trailing_zeros_is_not_normalised_away():
    """1690000000.000100 must not become 1690000000.0001 or a float."""
    raw = "1690000000.000100"
    thread_ts, _ = so.thread_info({"ts": raw, "thread_ts": raw})
    assert thread_ts == raw


def test_message_text_renders_mentions_inside_a_real_message():
    out = so.message_text({"text": "hi <@U1>, see <#C1>"},
                          {"U1": "vlad"}, {"C1": "general"})
    assert "@vlad" in out and "#general" in out


def test_author_never_invents_a_name_for_an_unknown_user():
    out = so.author_of({"user": "U999"}, {"U1": "vlad"})
    assert "vlad" not in out


# --- mrkdwn rendering --------------------------------------------------------

def test_user_mentions_become_names():
    out = so.render_text("hey <@U1> and <@U2>", {"U1": "vlad", "U2": "ana"})
    assert out == "hey @vlad and @ana"


def test_an_unknown_mention_degrades_but_never_invents_a_name():
    out = so.render_text("hey <@U999>", {"U1": "vlad"})
    assert "U999" in out
    assert "vlad" not in out


def test_mention_with_inline_label_uses_the_label():
    out = so.render_text("hey <@U1|vlad>", {})
    assert out == "hey @vlad"


def test_channel_refs_become_names():
    out = so.render_text("see <#C1>", {}, {"C1": "general"})
    assert out == "see #general"


def test_channel_ref_with_inline_name_needs_no_lookup():
    assert so.render_text("see <#C1|random>", {}, {}) == "see #random"


def test_links_keep_both_label_and_url():
    out = so.render_text("read <http://ex.com|the doc>")
    assert "the doc" in out and "http://ex.com" in out


def test_a_bare_link_renders_as_the_url():
    assert so.render_text("see <http://ex.com>") == "see http://ex.com"


def test_special_mentions_are_readable():
    assert so.render_text("<!here> ping") == "@here ping"
    assert so.render_text("<!channel> ping") == "@channel ping"


def test_html_entities_are_unescaped():
    assert so.render_text("a &amp; b &lt;c&gt;") == "a & b <c>"


def test_rendering_empty_text_is_safe():
    assert so.render_text("") == ""
    assert so.render_text(None) == ""


# --- reference normalisation -------------------------------------------------

def test_channel_ref_strips_the_hash():
    assert so.normalize_channel_ref("#general") == "general"
    assert so.normalize_channel_ref("general") == "general"
    assert so.normalize_channel_ref("  #general  ") == "general"


def test_emoji_strips_the_colons():
    assert so.normalize_emoji(":thumbsup:") == "thumbsup"
    assert so.normalize_emoji("thumbsup") == "thumbsup"


def test_channel_ids_are_recognised_by_shape():
    assert so.looks_like_channel_id("C024BE7LR") is True
    assert so.looks_like_channel_id("D024BE7LR") is True
    assert so.looks_like_channel_id("general") is False
    assert so.looks_like_channel_id("") is False


def test_user_ids_are_recognised_by_shape():
    assert so.looks_like_user_id("U024BE7LH") is True
    assert so.looks_like_user_id("vlad") is False
