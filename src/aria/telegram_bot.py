"""
aria/telegram_bot.py — legacy alias of aria.channels.telegram.bot.

The Telegram bot moved into the built-in channel plugin package. This module
replaces itself in sys.modules with the real one, so `import aria.telegram_bot`,
`python -m aria.telegram_bot`, the `aria-telegram` entry point and
monkeypatching of private names all act on the very same module object.
"""

import sys
from typing import TYPE_CHECKING

from aria.channels.telegram import bot as _m

if TYPE_CHECKING:   # let type checkers see the aliased module's public API
    from aria.channels.telegram.bot import *  # noqa: F403

if __name__ == "__main__":
    _m.main()
else:
    sys.modules[__name__] = _m
