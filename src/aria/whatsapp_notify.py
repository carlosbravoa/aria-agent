"""
aria/whatsapp_notify.py — Legacy alias for aria.channels.whatsapp.notify.

WhatsApp is now the built-in channel plugin in aria/channels/whatsapp/. This
module keeps `from aria import whatsapp_notify` and
`from aria.whatsapp_notify import send` working by replacing itself in
sys.modules with the real module — the SAME module object, so private names
and monkeypatching behave exactly as before.
"""

import sys
from typing import TYPE_CHECKING

from aria.channels.whatsapp import notify as _m

if TYPE_CHECKING:  # let type checkers see the aliased module's public names
    from aria.channels.whatsapp.notify import *  # noqa: F403

sys.modules[__name__] = _m
