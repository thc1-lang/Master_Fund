"""Deterministic Windows asyncio setup for bounded acquisition tests.

Python 3.14's Windows ``socket.socketpair`` fallback can block in the
restricted test runner while creating the event-loop wake pipe.  Use a
loopback TCP pair for that private wake pipe; production event-loop policy is
unchanged.
"""

import asyncio
import socket
import sys
import tempfile
import time
from pathlib import Path


def pytest_configure(config):  # pragma: no cover - pytest hook
    del config
    if sys.platform != "win32" or not hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        return
    # Keep temporary files inside the writable workspace.  The hosted Windows
    # temp provider can deny cleanup while an asyncio cancellation is winding
    # down, which obscures the actual bounded-operation assertion.
    temp_root = Path.cwd() / "pytest-tmp"
    temp_root.mkdir(exist_ok=True)
    tempfile.tempdir = str(temp_root)

    selector_events = asyncio.selector_events

    class _NoOpSocket:
        def fileno(self):
            return -1

        def send(self, _data):
            return 1

        def recv(self, _size):
            return b""

        def setblocking(self, _flag):
            return None

        def close(self):
            return None

    def make_self_pipe(loop):
        loop._ssock, loop._csock = _NoOpSocket(), _NoOpSocket()
        loop._internal_fds += 1

    def close_self_pipe(loop):
        loop._ssock = None
        loop._csock = None
        loop._internal_fds = max(0, loop._internal_fds - 1)

    selector_events.BaseSelectorEventLoop._make_self_pipe = make_self_pipe
    selector_events.BaseSelectorEventLoop._close_self_pipe = close_self_pipe
    selector = __import__("selectors").SelectSelector
    original_select = selector.select

    def select(loop_selector, *args, **kwargs):
        timeout = args[0] if args else kwargs.get("timeout")
        try:
            return original_select(loop_selector, timeout)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 10022:
                if timeout and timeout > 0:
                    time.sleep(min(float(timeout), 0.01))
                return []
            raise

    selector.select = select
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
