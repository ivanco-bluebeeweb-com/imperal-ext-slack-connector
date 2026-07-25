"""Entrypoint for the web-kernel and CLI tools (imperal validate/build).
Sets up sys.path, purges stale module cache, then imports ext/chat and all
handler modules so their decorators register on the same Extension instance.
"""

import os
import sys

_EXT_DIR = os.path.dirname(os.path.abspath(__file__))
if _EXT_DIR not in sys.path:
    sys.path.insert(0, _EXT_DIR)

# Purge stale cached modules so a fresh load always registers decorators
# (the validator may run multiple extensions in the same process).
_LOCAL = ("app", "models", "slack_client", "slack_objects", "accounts",
          "shared", "inbound", "handlers_directory", "handlers_messages",
          "handlers_post", "handlers_admin", "handlers_inbound", "panels")
for _mod in _LOCAL:
    sys.modules.pop(_mod, None)

# Handlers are split by DOMAIN, not by read/write: "what is there" (directory),
# "what was said" (messages), "say something" (post), "run the channel" (admin),
# "what just arrived" (inbound -- the Slack Events endpoint).
# Each stays small enough to read in one sitting.
from app import ext, chat  # noqa: E402,F401
import handlers_directory  # noqa: E402,F401
import handlers_messages  # noqa: E402,F401
import handlers_post  # noqa: E402,F401
import handlers_admin  # noqa: E402,F401
import handlers_inbound  # noqa: E402,F401
import panels  # noqa: E402,F401
