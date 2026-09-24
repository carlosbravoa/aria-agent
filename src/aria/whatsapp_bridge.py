"""
aria/whatsapp_bridge.py — Legacy alias for aria.channels.whatsapp.bridge.

WhatsApp is now the built-in channel plugin in aria/channels/whatsapp/. This
module keeps the `aria-whatsapp` console script (aria.whatsapp_bridge:main)
and `python -m aria.whatsapp_bridge` working by replacing itself in
sys.modules with the real module — the SAME module object, so private names
and monkeypatching behave exactly as before.
"""

import sys
from typing import TYPE_CHECKING

from aria.channels.whatsapp import bridge as _m

if TYPE_CHECKING:  # let type checkers see the aliased module's public names
    from aria.channels.whatsapp.bridge import *  # noqa: F403

if __name__ == "__main__":      # python -m aria.whatsapp_bridge
    _m.main()
else:
    sys.modules[__name__] = _m
