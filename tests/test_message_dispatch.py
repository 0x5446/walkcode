"""Regression tests for async message dispatch.

Background: the Lark SDK calls our message handler synchronously on its
WebSocket asyncio loop, then sends the ack frame only after the handler
returns. Before this change, `_on_message` ran tmux/HTTP work inline; a slow
handler delayed the ack, missed PING/PONG heartbeats, and caused Feishu to
redeliver the same message after reconnect (observed: a single image
delivered 5 times across 7 hours, intervals matching exponential backoff).

The fix offloads the work to a single-worker background executor so the SDK
callback returns immediately. These tests pin that behavior.
"""

import threading
import time
import unittest
from unittest.mock import patch

from walkcode import server


class OnMessageDispatchTests(unittest.TestCase):

    def test_on_message_returns_immediately_even_when_handler_blocks(self):
        """_on_message must not block the SDK callback thread."""
        started = threading.Event()
        release = threading.Event()

        def blocking_handler(_data):
            started.set()
            release.wait(timeout=5)

        with patch.object(server, "_handle_message_safe", blocking_handler):
            try:
                t0 = time.monotonic()
                server._on_message(object())
                elapsed = time.monotonic() - t0

                # SDK callback must return in well under a second even though
                # the background worker is still blocked.
                self.assertLess(elapsed, 0.5,
                                f"_on_message blocked for {elapsed:.3f}s")
                # And the work must actually have been scheduled.
                self.assertTrue(started.wait(timeout=2),
                                "_handle_message_safe was never invoked")
            finally:
                # Always release so the worker doesn't tie up the single-worker
                # executor for follow-up tests on assertion failure.
                release.set()

    def test_handle_message_safe_swallows_exceptions(self):
        """An exception in the worker must not kill the executor thread."""
        called = threading.Event()

        def raising_handler(_data):
            called.set()
            raise RuntimeError("boom")

        with patch.object(server, "_handle_message", raising_handler):
            # Must not raise.
            server._handle_message_safe(object())
            self.assertTrue(called.is_set())

        # Executor still alive: a follow-up submit completes normally.
        followup_done = threading.Event()
        with patch.object(server, "_handle_message_safe",
                          lambda _d: followup_done.set()):
            server._on_message(object())
            self.assertTrue(followup_done.wait(timeout=2),
                            "executor died after handler exception")


if __name__ == "__main__":
    unittest.main()
