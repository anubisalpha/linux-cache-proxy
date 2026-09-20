"""Entry point for one proxy worker process (used by the supervisor).

mitmproxy binds its listening socket with a bare asyncio.start_server(), with
no way to ask for SO_REUSEPORT. Several worker processes sharing one port need
it: the kernel then spreads incoming connections across them and each
connection still carries the real client address, so usage logging is
unaffected. Patching start_server before mitmproxy starts is the smallest
way to get that without forking mitmproxy.

All command-line arguments pass straight through to mitmdump.
"""
import asyncio
import functools
import sys


def _enable_reuse_port() -> None:
    original = asyncio.start_server
    asyncio.start_server = functools.partial(original, reuse_port=True)


def main() -> None:
    _enable_reuse_port()
    from mitmproxy.tools.main import mitmdump

    mitmdump(sys.argv[1:])


if __name__ == "__main__":
    main()
