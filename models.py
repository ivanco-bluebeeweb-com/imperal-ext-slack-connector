"""Pydantic parameter models and SDL return entities.

Every parameter that names a Slack object accepts a NAME, not just an id
(name-first, as with the Notion connector). Ids are still accepted -- pasting
one out of a Slack link must keep working -- but nothing here ever REQUIRES the
user to go find one.

Message identity deserves a note: Slack identifies a message by its `ts`
string, so every parameter that targets a message takes `ts` as a STRING and
never a number. A float loses microsecond precision and produces a ts Slack no
longer recognises, which means replies land in the wrong place or not at all.
"""

from typing import ClassVar

from pydantic import BaseModel, Field, model_validator
from imperal_sdk import sdl


# --------------------------- parameters ---------------------------

class WorkspaceScoped(BaseModel):
    """Base for every tool: which connected workspace to act in."""
    workspace: str = Field(
        "", description="Workspace name, e.g. 'Acme'. Omit when only one "
                        "Slack workspace is connected.")


class ListWorkspacesParams(BaseModel):
    refresh: bool = Field(
        False,
        description="Re-read workspace details from Slack instead of the cache")


class ConnectWorkspaceParams(BaseModel):
    """The token the user pastes on the Connect screen.

    Not WorkspaceScoped: this is the one action that runs BEFORE any workspace
    exists, so asking which workspace to act in would be circular. The
    workspace is discovered FROM the token.
    """
    token: str = Field(
        "", description="Slack app token: bot token starts with 'xoxb-', user "
                        "token with 'xoxp-'. Create one at api.slack.com/apps.")


class ListChannelsParams(WorkspaceScoped):
    query: str = Field(
        "", description="Filter channels by name fragment, e.g. 'standup'. "
                        "Empty returns all visible channels.")
    kind: str = Field(
        "", description="Limit to 'public', 'private', 'dm' or 'group_dm'. "
                        "Empty returns every kind the app can see.")
    include_archived: bool = Field(
        False, description="Include archived channels (excluded by default)")
    member_only: bool = Field(
        False, description="Only channels the app itself belongs to")
    limit: int = Field(
        50, ge=1, le=200, description="Maximum channels to return")


class ReadChannelParams(WorkspaceScoped):
    channel: str = Field(
        ..., description="Channel name or id, e.g. '#general', 'general' or "
                         "'C024BE7LR'")
    limit: int = Field(
        30, ge=1, le=200,
        description="How many recent messages to read (newest first)")
    include_thread_counts: bool = Field(
        True, description="Report how many replies each message has")


class ReadThreadParams(WorkspaceScoped):
    channel: str = Field(
        ..., description="Channel name or id the thread lives in")
    ts: str = Field(
        ..., description="Timestamp of the thread's parent message, e.g. "
                         "'1690000000.123456'. Pass it as text, not a number.")
    limit: int = Field(
        50, ge=1, le=200, description="Maximum replies to return")


class FetchMessageParams(WorkspaceScoped):
    """Read ONE message by ts -- the message a Slack event pointed at."""
    channel: str = Field(
        ..., description="Channel name or id the message is in. Pass the "
                         "channel_id straight from the Slack event.")
    ts: str = Field(
        ..., description="Timestamp of the message, as text — the message_ts "
                         "from the event. Never pass it as a number: a float "
                         "loses precision and Slack stops recognising it.")


class FetchThreadContextParams(WorkspaceScoped):
    """Read a whole thread, so a reply can answer the conversation."""
    channel: str = Field(
        ..., description="Channel name or id the thread lives in. Pass the "
                         "channel_id straight from the Slack event.")
    thread_ts: str = Field(
        ..., description="Thread timestamp as text — the thread_ts or "
                         "reply_thread_ts from the event.")
    limit: int = Field(
        50, ge=1, le=200, description="Maximum messages to return")


class InboundStatusParams(BaseModel):
    """No inputs: it reports on configuration, which is not parameterised."""


class ConnectEventsParams(BaseModel):
    """Save the Slack signing secret that authenticates inbound deliveries."""
    signing_secret: str = Field(
        ..., description="The app's Signing Secret from Slack → Basic "
                         "Information → App Credentials. A 32-character hex "
                         "string; NOT the bot token.")


class SearchMessagesParams(WorkspaceScoped):
    query: str = Field(
        ..., description="Search text. Slack modifiers work too, e.g. "
                         "'in:#general from:@vlad deploy'.")
    limit: int = Field(
        20, ge=1, le=100, description="Maximum matches to return")


class ListUsersParams(WorkspaceScoped):
    query: str = Field(
        "", description="Filter by name or email fragment. Empty lists everyone.")
    include_bots: bool = Field(
        False, description="Include bot and app users (excluded by default)")
    limit: int = Field(
        50, ge=1, le=200, description="Maximum people to return")


class CheckAccessParams(WorkspaceScoped):
    pass


class CatchUpParams(WorkspaceScoped):
    """Poll every reachable conversation and journal what is new.

    This is the path that makes awareness work WITHOUT the inbound endpoint:
    no signing secret, no automation slot, no platform fix. Slack is polled for
    the conversations the app can read, and anything not already journalled is
    recorded.
    """
    limit_per_channel: int = Field(
        25, ge=1, le=200,
        description="How many recent messages to examine per conversation")
    max_channels: int = Field(
        40, ge=1, le=60,
        description="How many conversations to sweep, at most")
    include_channels: bool = Field(
        True, description="Sweep channels the app has been added to")
    include_dms: bool = Field(
        True, description="Sweep direct messages with the app")
    full: bool = Field(
        False,
        description="Ignore the saved position and re-examine recent history. "
                    "Use after a gap; the journal still refuses duplicates.")


class ListInboundParams(BaseModel):
    """Read what the journal remembers. Not WorkspaceScoped: the journal is
    keyed by conversation, and a caller asking 'what came in' rarely knows or
    cares which workspace a message belongs to."""
    limit: int = Field(
        30, ge=1, le=200, description="How many messages to return, newest first")
    channel: str = Field(
        "", description="Only this conversation, by name or id. Empty = all.")
    dms_only: bool = Field(
        False, description="Only direct messages")
    mentions_only: bool = Field(
        False, description="Only messages that @-mention the app")
    unanswered_only: bool = Field(
        False, description="Only messages with no reply from the app yet")


class SendMessageParams(WorkspaceScoped):
    channel: str = Field(
        ..., description="Channel name or id to post to, e.g. '#general'. A "
                         "person's name or @handle sends a direct message.")
    text: str = Field(
        ..., description="Message text. Slack mrkdwn works: *bold*, _italic_, "
                         "`code`, <http://url|label>.")
    thread_ts: str = Field(
        "", description="Reply inside an existing thread instead of posting to "
                        "the channel. Timestamp of the parent message, as text.")
    reply_broadcast: bool = Field(
        False,
        description="When replying in a thread, also show the reply in the channel")
    reply_to_last_thread: bool = Field(
        False,
        description="Answer in the thread where this channel's most recent "
                    "inbound Slack message arrived. Use this when replying to "
                    "a Slack event (a mention or a thread reply) and you do "
                    "not have the thread_ts to hand — the connector remembers "
                    "it. Ignored when thread_ts is given explicitly.")
    unfurl_links: bool = Field(
        True, description="Let Slack expand link previews")


class EditMessageParams(WorkspaceScoped):
    channel: str = Field(..., description="Channel name or id the message is in")
    ts: str = Field(
        ..., description="Timestamp of the message to edit, as text")
    text: str = Field(..., description="Replacement text for the message")


class DeleteMessageParams(WorkspaceScoped):
    channel: str = Field(..., description="Channel name or id the message is in")
    ts: str = Field(
        ..., description="Timestamp of the message to delete, as text")


class ReactionParams(WorkspaceScoped):
    channel: str = Field(..., description="Channel name or id the message is in")
    ts: str = Field(
        ..., description="Timestamp of the message to react to, as text")
    emoji: str = Field(
        ..., description="Emoji name without colons, e.g. 'thumbsup' or 'eyes'")
    remove: bool = Field(
        False, description="Set true to remove the reaction instead of adding it")


class PinParams(WorkspaceScoped):
    channel: str = Field(..., description="Channel name or id the message is in")
    ts: str = Field(
        ..., description="Timestamp of the message to pin, as text")
    unpin: bool = Field(
        False, description="Set true to unpin instead of pin")


class CreateChannelParams(WorkspaceScoped):
    name: str = Field(
        ..., description="Channel name, e.g. 'project-apollo'. Slack lowercases "
                         "it and replaces spaces with hyphens.")
    private: bool = Field(
        False, description="Create a private channel instead of a public one")
    topic: str = Field("", description="Optional channel topic to set")
    invite: str = Field(
        "", description="Optional comma-separated names to invite, e.g. "
                        "'@vlad, @maria'")


class InviteParams(WorkspaceScoped):
    channel: str = Field(..., description="Channel name or id to invite into")
    users: str = Field(
        ..., description="Comma-separated names, @handles or ids to invite")


class SetTopicParams(WorkspaceScoped):
    channel: str = Field(..., description="Channel name or id to update")
    topic: str = Field(
        "", description="New channel topic. Empty clears it.")
    purpose: str = Field(
        "", description="New channel purpose/description. Empty leaves it as is.")


# --------------------------- returned entities ---------------------------

# `sdl.Entity` REQUIRES `id` and `title`: they are what the narrator and the
# audit ledger use to name a result ("sent to #general") rather than printing a
# dict. Passing them by hand at every construction site is how one gets
# forgotten -- and a forgotten one is a ValidationError at runtime, on the
# SUCCESS path, after the Slack write already happened. That is the worst
# possible place to fail: the side effect is done and the user still sees an
# error.
#
# So each entity below derives them from its own domain fields instead. A
# subclass states WHICH of its fields carry identity and name, and the
# validator fills the contract.
class _Named(sdl.Entity):
    """Base that satisfies the required id/title from domain fields.

    Subclasses set `_id_field` / `_title_field`. Both default to empty rather
    than to a placeholder like "unknown": an entity with no natural name should
    read as blank, never as invented text.

    `_subtitle_parts` names the fields that make up the one-line context under
    the title. It exists because `subtitle` is what a chat client shows next to
    an entity, and leaving it empty is how a message ends up displayed as
    "author · author · kind" with the actual words nowhere in sight.
    """
    id: str | int = ""
    title: str = ""

    _id_field: ClassVar[str] = ""
    _title_field: ClassVar[str] = ""
    _subtitle_parts: ClassVar[tuple[str, ...]] = ()
    #: Prefix for ids built from a Slack timestamp. A bare "1785176897.496389"
    #: is a long digit string, and the platform's PII guard reads it as a phone
    #: number and replaces it with "<PHONE>" -- so the id arrived in chat as
    #: redacted noise. The prefix keeps the id stable and still parseable
    #: (split on ":") while no longer looking like a phone number.
    _TS_ID_PREFIX: ClassVar[str] = "slack:"

    @model_validator(mode="after")
    def _fill_identity(self):
        if not self.id and self._id_field:
            raw = str(getattr(self, self._id_field, "") or "")
            # Only timestamps get the prefix; channel ids and the like are
            # already non-numeric and must stay exactly as Slack returns them.
            if raw and self._id_field == "ts":
                raw = f"{self._TS_ID_PREFIX}{raw}"
            self.id = raw
        if not self.title and self._title_field:
            self.title = str(getattr(self, self._title_field, "") or "")
        # A message can legitimately have no text: an attachment, an image, a
        # bare file drop. Titling it "" produces a nameless row that reads as a
        # rendering bug, so say what it actually is.
        if not self.title and self._title_field == "text":
            self.title = ("[вложение]" if getattr(self, "has_files", False)
                          else "[без текста]")
        if self.subtitle is None and self._subtitle_parts:
            parts = [str(getattr(self, f, "") or "").strip()
                     for f in self._subtitle_parts]
            joined = " · ".join(p for p in parts if p)
            self.subtitle = joined or None
        return self


class InboundStatus(_Named):
    """Whether Slack messages are reaching Webbee, and by which route.

    Two separate facts, deliberately not collapsed into one. `ready` is about
    PUSH (endpoint + signing secret); `aware` is about whether messages are
    actually being recorded at all -- which the hourly sweep achieves without
    push. Reporting only the first said "not ready" while awareness worked fine,
    which is how a working feature gets debugged as a broken one.
    """
    endpoint_url: str = ""
    signing_secret_set: bool = False
    workspaces_connected: int = 0
    events_deduplicated: int = 0
    ready: bool = False
    aware: bool = False
    messages_recorded: int = 0
    from_push: int = 0
    from_sweep: int = 0
    sweep_schedule: str = ""
    #: Delivery attempts, as observed at the endpoint itself.
    #:
    #: from_push == 0 has two completely different causes -- Slack never knocked
    #: (Request URL missing, events not subscribed) or Slack knocked and was
    #: refused (signature mismatch) -- and the fix differs entirely. Without
    #: these the two are indistinguishable, and the evidence that separates them
    #: went only to a log the user cannot read.
    delivery_attempts: int = 0
    deliveries_refused: int = 0
    last_refusal_code: str = ""
    detail: str = ""
    state: str = ""

    _id_field: ClassVar[str] = "endpoint_url"
    _title_field: ClassVar[str] = "state"


class WorkspaceRecord(_Named):
    """One connected Slack workspace and whether its token still works."""
    workspace_name: str = ""
    workspace_id: str = ""
    identity: str = ""
    token_kind: str = ""
    detail: str = ""

    _id_field: ClassVar[str] = "workspace_id"
    _title_field: ClassVar[str] = "workspace_name"


class WorkspaceList(_Named):
    workspaces: list[WorkspaceRecord] = []
    count: int = 0
    note: str = ""

    title: str = "Connected Slack workspaces"


class ChannelRecord(_Named):
    """A conversation: channel, private channel, DM or group DM."""
    name: str = ""
    channel_id: str = ""
    topic: str = ""
    purpose: str = ""
    member_count: int = 0
    is_member: bool = False
    is_archived: bool = False

    _id_field: ClassVar[str] = "channel_id"
    _title_field: ClassVar[str] = "name"


class ChannelList(_Named):
    channels: list[ChannelRecord] = []
    count: int = 0
    has_more: bool = False
    note: str = ""

    title: str = "Slack conversations"


class MessageRecord(_Named):
    """One message, with mentions and links already rendered readable.

    The title is the TEXT, not the author. A reader asking "what was said?"
    gets an answer from the title alone; titling by author produced rows like
    "Vladislav Ivanco · Vladislav Ivanco" with the words nowhere in sight, so
    the message was in the result and still unreadable.
    """
    text: str = ""
    author: str = ""
    author_id: str = ""
    ts: str = ""
    posted_at: str = ""
    thread_ts: str = ""
    reply_count: int = 0
    reactions: str = ""
    is_thread_parent: bool = False
    permalink: str = ""

    _id_field: ClassVar[str] = "ts"
    _title_field: ClassVar[str] = "text"
    _subtitle_parts: ClassVar[tuple[str, ...]] = ("author", "posted_at")


class MessageList(_Named):
    channel: str = ""
    channel_id: str = ""
    messages: list[MessageRecord] = []
    count: int = 0
    has_more: bool = False
    note: str = ""
    # Set when the list IS a thread, so a consumer can reply into it without
    # having to dig the ts back out of the first message.
    thread_ts: str = ""

    _id_field: ClassVar[str] = "channel_id"
    _title_field: ClassVar[str] = "channel"


class SearchHit(_Named):
    """A search match -- carries its channel, since results span the workspace.

    Titled by TEXT: a page of hits all titled by channel name reads as the same
    row repeated, which defeats the point of searching.
    """
    text: str = ""
    author: str = ""
    channel: str = ""
    ts: str = ""
    posted_at: str = ""
    permalink: str = ""

    _id_field: ClassVar[str] = "ts"
    _title_field: ClassVar[str] = "text"
    _subtitle_parts: ClassVar[tuple[str, ...]] = ("author", "where", "posted_at")

    @property
    def where(self) -> str:
        """Channel with its #, matching how Slack itself writes it."""
        return f"#{self.channel}" if self.channel else ""


class SearchResults(_Named):
    query: str = ""
    hits: list[SearchHit] = []
    count: int = 0
    total_available: int = 0
    note: str = ""

    _title_field: ClassVar[str] = "query"


class UserRecord(_Named):
    display_name: str = ""
    real_name: str = ""
    user_id: str = ""
    email: str = ""
    # NOT `title`: that is the required entity title on sdl.Entity, and Slack's
    # "title" means job title. Shadowing it silently replaced the entity's name
    # with a job description -- so the domain field is renamed instead.
    job_title: str = ""
    timezone: str = ""
    is_bot: bool = False
    is_admin: bool = False
    is_deactivated: bool = False

    _id_field: ClassVar[str] = "user_id"
    _title_field: ClassVar[str] = "display_name"


class UserList(_Named):
    users: list[UserRecord] = []
    count: int = 0
    has_more: bool = False

    title: str = "Slack workspace members"


class AccessReport(_Named):
    """What the token can currently reach, and why anything missing is missing."""
    workspace_name: str = ""
    identity: str = ""
    token_kind: str = ""
    channels_visible: int = 0
    channels_joined: int = 0
    conversations_readable: int = 0
    dms_readable: int = 0
    can_search: bool = False
    granted_scopes: str = ""
    missing_for_common_tasks: str = ""
    explanation: str = ""

    _title_field: ClassVar[str] = "workspace_name"


class MessageAck(_Named):
    """Confirmation of a write against a message.

    `marked_answered` reports how many journalled messages this reply closed
    out. It is surfaced rather than kept internal because "did my reply
    actually mark the thread as handled?" is the difference between answering
    someone once and answering them every hour.
    """
    channel: str = ""
    channel_id: str = ""
    ts: str = ""
    action: str = ""
    permalink: str = ""
    detail: str = ""
    marked_answered: int = 0

    _id_field: ClassVar[str] = "ts"
    _title_field: ClassVar[str] = "channel"


class ChannelAck(_Named):
    """Confirmation of a write against a channel."""
    name: str = ""
    channel_id: str = ""
    action: str = ""
    invited: str = ""
    detail: str = ""

    _id_field: ClassVar[str] = "channel_id"
    _title_field: ClassVar[str] = "name"


class InboundMessage(_Named):
    """One journalled inbound message -- what was said, where, and by whom.

    Carries `reply_thread_ts` because the whole point of remembering a message
    is being able to answer it in the right place later, and re-deriving
    Slack's thread rules at reply time is how replies end up in the wrong
    thread.

    Titled by TEXT for a concrete reason: the journal is how Webbee learns what
    was said in Slack, and titling by author meant every row arrived in chat as
    "Vladislav Ivanco" with the message body absent. The panel showed the words
    (it renders the full record), chat did not -- so the same message was
    visible in one surface and invisible in the other, and Webbee could not
    mention what she could not read.
    """
    text: str = ""
    author: str = ""
    author_id: str = ""
    channel: str = ""
    channel_id: str = ""
    is_dm: bool = False
    ts: str = ""
    posted_at: str = ""
    thread_ts: str = ""
    reply_thread_ts: str = ""
    is_thread_reply: bool = False
    mention_of_bot: bool = False
    has_files: bool = False
    source: str = ""
    would_raise: str = ""
    permalink: str = ""

    _id_field: ClassVar[str] = "ts"
    _title_field: ClassVar[str] = "text"
    _subtitle_parts: ClassVar[tuple[str, ...]] = ("author", "where", "posted_at")

    @property
    def where(self) -> str:
        """Human location: a DM says so, a channel gets its #name."""
        if self.is_dm:
            return "личное сообщение"
        return f"#{self.channel}" if self.channel else ""


class InboundLog(_Named):
    """A page of journalled messages, plus what the journal holds overall."""
    messages: list[InboundMessage] = []
    count: int = 0
    total_remembered: int = 0
    dms: int = 0
    mentions: int = 0
    from_push: int = 0
    from_sweep: int = 0
    conversations: int = 0
    note: str = ""
    detail: str = ""

    _id_field: ClassVar[str] = "note"
    _title_field: ClassVar[str] = "note"


class SweepReport(_Named):
    """What one catch-up sweep looked at and what it newly learned."""
    conversations_seen: int = 0
    conversations_swept: int = 0
    conversations_skipped: int = 0
    messages_examined: int = 0
    messages_new: int = 0
    messages_duplicate: int = 0
    messages_ignored: int = 0
    skipped_detail: str = ""
    swept_detail: str = ""
    detail: str = ""
    state: str = ""

    _id_field: ClassVar[str] = "state"
    _title_field: ClassVar[str] = "state"


class JoinChannelsParams(WorkspaceScoped):
    """Have the app add ITSELF to public channels.

    Exists because the alternative was a permanent chore: a human typing
    /invite @app in every channel, including every channel created from now on.
    A bot token with channels:join can join any PUBLIC channel unaided, so the
    manual step was never actually required for the common case.

    Private channels and DMs are untouched by this: Slack genuinely has no
    self-join for a private channel (someone inside must invite), and a DM needs
    no joining at all.
    """
    channels: str = Field(
        "", description="Channel names or ids, comma-separated, e.g. "
                        "'#random, #lol-kek'. EMPTY joins every public channel "
                        "the app can see but is not yet in.")
    dry_run: bool = Field(
        False,
        description="List what would be joined without joining anything")


class JoinReport(_Named):
    """What the app joined, what it skipped, and what it could not do."""
    joined: str = ""
    joined_count: int = 0
    already_in: str = ""
    already_count: int = 0
    failed: str = ""
    failed_count: int = 0
    needs_a_human: str = ""
    detail: str = ""
    state: str = ""

    _id_field: ClassVar[str] = "state"
    _title_field: ClassVar[str] = "state"
