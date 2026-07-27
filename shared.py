"""Helpers shared by the read and write tool layers.

These live here rather than in `handlers_read.py` so that `handlers_write.py`
never imports private names from a sibling layer -- a dependency that would say
"write is built on read" when the two are really peers. (The Notion connector
had to be refactored for exactly this; starting that way here.)
"""

from __future__ import annotations

from imperal_sdk import ActionResult

import accounts as acc
import slack_client as sc

# The one sentence that explains Slack's access model. Reused verbatim wherever
# emptiness might otherwise read as a bug -- an empty result here usually means
# "the app was never invited", not "nothing exists".
# The DM sentence was WRONG and is corrected here. It used to say DMs are
# "invisible until the app is invited", which sent people looking for an invite
# that does not exist: Slack has no /invite for a DM. Verified live against the
# workspace -- the DM with the account owner is reported by conversations.list
# with `is_member: false`, and both reading its history and posting into it
# succeed. Only CHANNELS need the app added.
MEMBERSHIP_NOTE = (
    "A Slack app only reaches conversations it belongs to. Public channels can "
    "be listed without joining, but reading history or posting needs the app in "
    "the channel -- open it in Slack and type /invite @your-app. Private "
    "channels need the same invite. Direct messages with the app need NO "
    "invite: anyone in the workspace can DM it and it can read and reply "
    "there."
)


def error(message: str, code: str, retryable: bool = False) -> ActionResult:
    """Error result carrying a structured code.

    `code` is mandatory on purpose. The kernel stamps EXT_UNSTRUCTURED_ERROR on
    any error emitted without one (I-EXT-ERROR-CODE-NORMALIZED), which turns a
    precise failure into un-actionable prose -- the WP Publisher incident.
    Validator rule V32 only flags literal `ActionResult.error(` call sites, so
    routing every error through a helper would hide this app from the rule --
    hence the positional argument: a code-less error here is a TypeError at
    authoring time, not a silent downgrade in production.
    """
    return ActionResult.error(message, retryable, code=code)


def from_envelope(out: dict) -> ActionResult:
    """Convert a slack_client error envelope into an ActionResult."""
    return error(out.get("error") or sc.message_for(out.get("code", "")),
                 out.get("code") or sc.SLACK_HTTP_ERROR,
                 bool(out.get("retryable")))


async def resolve(ctx, workspace: str) -> tuple[str, dict, ActionResult | None]:
    """Resolve the workspace token, or hand back a ready-made error."""
    picked = await acc.resolve_workspace(ctx, workspace)
    if not picked.get("ok"):
        return "", {}, from_envelope(picked)
    return picked["token"], picked.get("workspace", {}), None


def channel_label(channel: dict) -> str:
    """How a resolved conversation is named back to the user.

    A channel reads as "#general", but a DM has no name at all -- prefixing an
    empty string with "#" produces a bare "#", which looks like a bug in the
    confirmation message right after a message was sent. So DMs and group DMs
    describe themselves instead.
    """
    name = (channel.get("name") or "").strip()
    kind = (channel.get("kind") or "").strip()
    if kind == "dm":
        return f"your DM with @{name}" if name else "that DM"
    if kind == "group_dm":
        return "that group DM"
    return f"#{name}" if name else "that conversation"


async def resolve_channel_or_error(ctx, token: str, reference: str):
    """(channel dict, None) or (None, ActionResult) -- name-first, never guessed."""
    found = await acc.resolve_channel(ctx, token, reference)
    if not found.get("ok"):
        return None, from_envelope(found)
    return found, None


async def resolve_user_or_error(ctx, token: str, reference: str):
    """(user dict, None) or (None, ActionResult)."""
    found = await acc.resolve_user(ctx, token, reference)
    if not found.get("ok"):
        return None, from_envelope(found)
    return found, None
