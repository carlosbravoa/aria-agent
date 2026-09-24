"""
aria/whatsapp_deploy.py — Legacy alias for aria.channels.whatsapp.deploy.

WhatsApp is now the built-in channel plugin in aria/channels/whatsapp/. This
module keeps `from aria import whatsapp_deploy` (installer,
self-update) working by replacing itself in
sys.modules with the real module — the SAME module object, so private names
and monkeypatching behave exactly as before.
"""

import sys
from typing import TYPE_CHECKING

from aria.channels.whatsapp import deploy as _m

if TYPE_CHECKING:  # let type checkers see the aliased module's public names
    from aria.channels.whatsapp.deploy import *  # noqa: F403

sys.modules[__name__] = _m
