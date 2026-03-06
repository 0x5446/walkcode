import unittest
from unittest import mock

from agent_hotline import tty


class TTYProcessTests(unittest.TestCase):
    def test_friendly_applescript_error_for_accessibility_denial(self):
        message = tty._friendly_applescript_error(
            "369:383: execution error: System Events got an error: "
            "osascript is not allowed to send keystrokes. (1002)"
        )

        self.assertIn("辅助功能", message)
        self.assertIn("agent-hotline serve", message)

    def test_inspect_tty_owner_returns_live_tty(self):
        with mock.patch("agent_hotline.tty._ps_field", side_effect=[
            "Fri  6 Mar 13:00:10 2026",
            "ttys009",
        ]):
            status, live_tty = tty.inspect_tty_owner(1234, "Fri Mar  6 13:00:10 2026")

        self.assertEqual(status, "ok")
        self.assertEqual(live_tty, "/dev/ttys009")

    def test_inspect_tty_owner_detects_pid_reuse(self):
        with mock.patch("agent_hotline.tty._ps_field", return_value="Fri Mar  6 13:05:00 2026"):
            status, live_tty = tty.inspect_tty_owner(1234, "Fri Mar  6 13:00:10 2026")

        self.assertEqual(status, "process_reused")
        self.assertIsNone(live_tty)

    def test_detect_terminal_binding_walks_parent_chain(self):
        with mock.patch("agent_hotline.tty._ps_field", side_effect=[
            "??",
            "200",
            "ttys004",
            "Fri Mar  6 13:00:10 2026",
            "300",
            "ttys004",
            "Fri Mar  6 13:00:10 2026",
            "",
        ]):
            tty_path, pid, started_at = tty.detect_terminal_binding(start_pid=100, max_depth=3)

        self.assertEqual(tty_path, "/dev/ttys004")
        self.assertEqual(pid, 300)
        self.assertEqual(started_at, "2026-03-06 13:00:10")

    def test_normalize_lstart_accepts_both_orders(self):
        self.assertEqual(
            tty._normalize_lstart("Fri Mar  6 13:00:10 2026"),
            "2026-03-06 13:00:10",
        )
        self.assertEqual(
            tty._normalize_lstart("Fri  6 Mar 13:00:10 2026"),
            "2026-03-06 13:00:10",
        )

    def test_title_escape_sequence_sanitizes_control_chars(self):
        sequence = tty._title_escape_sequence("plaudclaw\x1b\x07 ttys001")

        self.assertEqual(
            sequence,
            "\033]0;plaudclaw ttys001\007\033]1;plaudclaw ttys001\007\033]2;plaudclaw ttys001\007",
        )

    def test_set_terminal_title_writes_osc_sequences(self):
        mock_file = mock.mock_open()

        with (
            mock.patch("agent_hotline.tty.validate_tty", return_value=None),
            mock.patch("builtins.open", mock_file),
        ):
            tty.set_terminal_title("/dev/ttys001", "plaudclaw ttys001 9079ba57")

        handle = mock_file()
        handle.write.assert_called_once_with(
            "\033]0;plaudclaw ttys001 9079ba57\007"
            "\033]1;plaudclaw ttys001 9079ba57\007"
            "\033]2;plaudclaw ttys001 9079ba57\007"
        )
        handle.flush.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
