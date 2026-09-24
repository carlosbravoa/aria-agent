"""
aria/telegram_notify.py — legacy alias of aria.channels.telegram.notify.

The push-only Telegram sender moved into the built-in channel plugin package.
This module replaces itself in sys.modules with the real one, so imports,
monkeypatching and private names (_split, _md_to_html, _record_feed, …) all act
on the very same module object.
"""

import sys
from typing import TYPE_CHECKING

from aria.channels.telegram import notify as _m

if TYPE_CHECKING:   # let type checkers see the aliased module's public API
    from aria.channels.telegram.notify import *  # noqa: F403

if __name__ != "__main__":   # `python -m` of a library module stays a no-op
    sys.modules[__name__] = _m
