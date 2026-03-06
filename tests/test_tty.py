import unittest
from unittest import mock

from agent_hotline import tty


class TmuxDetectionTests(unittest.TestCase):
    def test_detect_tmux_session_returns_name_when_in_tmux(self):
        with mock.patch.dict("os.environ", {"TMUX": "/tmp/tmux-501/default,12345,0"}), \
             mock.patch("agent_hotline.tty.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="claude-myproject-99\n")
            session = tty.detect_tmux_session()

        self.assertEqual(session, "claude-myproject-99")

    def test_detect_tmux_session_returns_empty_when_not_in_tmux(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            session = tty.detect_tmux_session()

        self.assertEqual(session, "")

    def test_detect_tmux_session_returns_empty_on_command_failure(self):
        with mock.patch.dict("os.environ", {"TMUX": "/tmp/tmux-501/default,12345,0"}), \
             mock.patch("agent_hotline.tty.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=1, stdout="")
            session = tty.detect_tmux_session()

        self.assertEqual(session, "")


class ValidateTargetTests(unittest.TestCase):
    def test_validate_target_returns_none_when_session_exists(self):
        with mock.patch("agent_hotline.tty.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0)
            error = tty.validate_target("claude-proj-123")

        self.assertIsNone(error)

    def test_validate_target_returns_error_when_session_missing(self):
        with mock.patch("agent_hotline.tty.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=1)
            error = tty.validate_target("dead-session")

        self.assertIn("not found", error)

    def test_validate_target_returns_error_for_empty_name(self):
        error = tty.validate_target("")
        self.assertIn("No tmux session", error)

    def test_validate_target_returns_error_when_tmux_not_installed(self):
        with mock.patch("agent_hotline.tty.subprocess.run", side_effect=FileNotFoundError):
            error = tty.validate_target("some-session")

        self.assertIn("not installed", error)


class InjectTests(unittest.TestCase):
    def test_inject_sends_text_and_enter(self):
        with mock.patch("agent_hotline.tty.validate_target", return_value=None), \
             mock.patch("agent_hotline.tty.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0)
            result = tty.inject("claude-proj-123", "hello world")

        self.assertTrue(result)
        self.assertEqual(mock_run.call_count, 2)
        # First call: send-keys -l "hello world"
        args1 = mock_run.call_args_list[0][0][0]
        self.assertEqual(args1, ["tmux", "send-keys", "-t", "claude-proj-123", "-l", "hello world"])
        # Second call: send-keys Enter
        args2 = mock_run.call_args_list[1][0][0]
        self.assertEqual(args2, ["tmux", "send-keys", "-t", "claude-proj-123", "Enter"])

    def test_inject_single_key_no_enter(self):
        with mock.patch("agent_hotline.tty.validate_target", return_value=None), \
             mock.patch("agent_hotline.tty.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0)
            tty.inject("claude-proj-123", "y")

        # Only one call (no Enter)
        self.assertEqual(mock_run.call_count, 1)

    def test_inject_raises_on_invalid_target(self):
        with mock.patch("agent_hotline.tty.validate_target", return_value="session not found"):
            with self.assertRaises(RuntimeError):
                tty.inject("dead-session", "hello")

    def test_inject_raises_on_send_keys_failure(self):
        with mock.patch("agent_hotline.tty.validate_target", return_value=None), \
             mock.patch("agent_hotline.tty.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=1, stderr="session not found")
            with self.assertRaises(RuntimeError) as ctx:
                tty.inject("claude-proj-123", "hello")
            self.assertIn("send-keys failed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
