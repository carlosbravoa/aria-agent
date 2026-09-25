"""
aria/channels/cli.py — `aria-channel`: run or inspect channel plugins.

  aria-channel <name>     run that channel (what its systemd unit executes)
  aria-channel --list     show every discovered channel and whether it's enabled
"""

from __future__ import annotations

import argparse
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aria-channel",
                                     description="Run an Aria channel plugin.")
    parser.add_argument("name", nargs="?", help="channel to run")
    parser.add_argument("--list", action="store_true", help="list channels and exit")
    args = parser.parse_args(argv)

    from aria.setup import is_first_run, run as setup_run
    if is_first_run():
        setup_run()
    from aria import config
    config.load()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from aria import channels
    if args.list or not args.name:
        on = {p.name for p in channels.enabled()}
        push = channels.push_channel()
        for name, p in sorted(channels.discover().items()):
            flags = []
            if name in on:
                flags.append("enabled")
                if p.runs_attached:
                    flags.append(p.mode)
            if push is not None and push.name == name:
                flags.append("push default")
            if not p.builtin:
                flags.append(f"user: {p.source}")
            print(f"{name:12} {p.description}" + (f"  [{', '.join(flags)}]" if flags else ""))
        return 0 if args.list else 2

    plugin = channels.get(args.name)
    if plugin is None:
        print(f"aria-channel: unknown channel '{args.name}' "
              f"(available: {', '.join(sorted(channels.discover()))})", file=sys.stderr)
        return 2
    if plugin.runs_attached:
        print(f"aria-channel: note — {plugin.name} is configured as attached "
              f"(ARIA_CHANNEL_MODE); running it as a service anyway", file=sys.stderr)
    # Built-ins with their own entry points take the run lock themselves;
    # for everything else the lock is held here, around run().
    from aria.channels.runlock import hold_for_service
    lock = None if plugin.builtin else hold_for_service(plugin.name, logging.getLogger(__name__))
    try:
        plugin.run()
    finally:
        if lock is not None:
            lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
