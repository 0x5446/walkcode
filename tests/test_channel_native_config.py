import unittest

from walkcode.channel_native import (
    ChannelConfigError,
    ChannelNativeConfig,
)


class ChannelNativeConfigTests(unittest.TestCase):
    def test_lark_channel_config_binds_agent_to_claude(self):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "lark",
                "LARK_APP_ID": "cli_x",
                "LARK_APP_SECRET": "s",
                "WALKCODE_AGENT": "claude",
                "LARK_ALLOWED_CHAT_IDS": "1,2",
                "WALKCODE_CWD": "/tmp/project",
                "WALKCODE_STATE_PATH": "/tmp/state.json",
            }
        )

        self.assertEqual(cfg.channel_kind, "lark")
        self.assertEqual(cfg.channel.credentials["app_id"], "cli_x")
        self.assertEqual(cfg.channel.options["allowed_chat_ids"], ("1", "2"))
        self.assertEqual(cfg.agent, "claude")
        self.assertEqual(cfg.agent_transport_kind, "claude_headless")
        self.assertEqual(cfg.cwd, "/tmp/project")
        self.assertEqual(cfg.state_path, "/tmp/state.json")
        self.assertEqual(cfg.handoff_continue, "auto")

    def test_handoff_continue_defaults_auto_and_rejects_garbage(self):
        # ADR 0051: auto is the shipped default (user decision 2026-07-13);
        # off stays as the explicit escape hatch.
        base = {
            "WALKCODE_CHANNEL": "lark",
            "LARK_APP_ID": "cli_x",
            "LARK_APP_SECRET": "s",
            "WALKCODE_AGENT": "claude",
            "LARK_ALLOWED_CHAT_IDS": "1",
            "WALKCODE_CWD": "/tmp/project",
            "WALKCODE_STATE_PATH": "/tmp/state.json",
        }
        cfg = ChannelNativeConfig.from_env({**base, "WALKCODE_HANDOFF_CONTINUE": "off"})
        self.assertEqual(cfg.handoff_continue, "off")
        with self.assertRaisesRegex(ChannelConfigError, "WALKCODE_HANDOFF_CONTINUE"):
            ChannelNativeConfig.from_env({**base, "WALKCODE_HANDOFF_CONTINUE": "on"})

    def test_e2e_lark_chat_id_restricts_v3_runtime_by_default(self):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "lark",
                "LARK_APP_ID": "cli_x",
                "LARK_APP_SECRET": "s",
                "WALKCODE_AGENT": "claude",
                "WALKCODE_E2E_LARK_CHAT_ID": "123",
            }
        )

        self.assertEqual(cfg.channel.options["allowed_chat_ids"], ("123",))

    def test_lark_only_config_is_valid_peer_channel(self):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "lark",
                "LARK_APP_ID": "app-id",
                "WALKCODE_AGENT": "claude",
                "LARK_APP_SECRET": "secret",
                "LARK_RECEIVE_ID": "chat-id",
                "LARK_RECEIVE_ID_TYPE": "chat_id",
                "LARK_OPENAPI_DOMAIN": "https://open.feishu.cn/",
            }
        )

        self.assertEqual(cfg.channel_kind, "lark")
        self.assertEqual(cfg.channel.credentials["app_id"], "app-id")
        self.assertEqual(cfg.channel.credentials["app_secret"], "secret")
        self.assertEqual(cfg.channel.options["receive_id"], "chat-id")
        self.assertEqual(cfg.channel.options["openapi_domain"], "https://open.feishu.cn")

    def test_removed_plural_channel_env_is_rejected(self):
        with self.assertRaisesRegex(ChannelConfigError, "WALKCODE_CHANNELS is not supported"):
            ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNELS": "lark",
                    "LARK_APP_ID": "cli_x",
                    "LARK_APP_SECRET": "s",
                    "WALKCODE_AGENT": "claude",
                }
            )

    def test_removed_plural_channel_env_guidance_names_lark(self):
        with self.assertRaisesRegex(ChannelConfigError, "use WALKCODE_CHANNEL=lark"):
            ChannelNativeConfig.from_env({"WALKCODE_CHANNELS": "lark", "WALKCODE_AGENT": "claude"})

    def test_retired_telegram_channel_is_rejected_with_adr_pointer(self):
        with self.assertRaisesRegex(ChannelConfigError, "ADR 0069"):
            ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "tg-token",
                    "WALKCODE_AGENT": "claude",
                }
            )

    def test_channel_env_rejects_multiple_channel_values(self):
        with self.assertRaisesRegex(ChannelConfigError, "exactly one channel: lark"):
            ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "lark,lark",
                    "WALKCODE_AGENT": "claude",
                    "LARK_APP_ID": "app-id",
                    "LARK_APP_SECRET": "secret",
                }
            )

    def test_bound_agent_accepts_product_names_not_transport_config(self):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "lark",
                "LARK_APP_ID": "cli_x",
                "LARK_APP_SECRET": "s",
                "WALKCODE_AGENT": "codex",
            }
        )

        self.assertEqual(cfg.agent, "codex")
        self.assertEqual(cfg.agent_transport_kind, "codex_app_server")
        self.assertTrue(cfg.state_path.endswith("/.walkcode/lark-codex-state.json"))

        with self.assertRaisesRegex(ChannelConfigError, "unknown agent"):
            ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "lark",
                    "LARK_APP_ID": "cli_x",
                    "LARK_APP_SECRET": "s",
                    "WALKCODE_AGENT": "unknown-agent",
                }
            )

    def test_agent_is_required(self):
        with self.assertRaisesRegex(ChannelConfigError, "missing WALKCODE_AGENT"):
            ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "lark",
                    "LARK_APP_ID": "cli_x",
                    "LARK_APP_SECRET": "s",
                }
            )

    def test_claude_settings_are_agent_options(self):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "lark",
                "LARK_APP_ID": "cli_x",
                "LARK_APP_SECRET": "s",
                "WALKCODE_AGENT": "claude",
                "WALKCODE_CLAUDE_SETTINGS": "~/profiles/vertex.json",
                "WALKCODE_CLAUDE_CLI_PATH": "~/bin/claude",
            }
        )

        self.assertEqual(cfg.agent_options["claude"]["settings"].split("/")[-2:], ["profiles", "vertex.json"])
        self.assertEqual(cfg.agent_options["claude"]["cli_path"].split("/")[-2:], ["bin", "claude"])
        self.assertEqual(cfg.agent_options["codex"], {})

    def test_profile_defaults_empty_and_keeps_legacy_state_path(self):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "lark",
                "LARK_APP_ID": "cli_x",
                "LARK_APP_SECRET": "s",
                "WALKCODE_AGENT": "claude",
            }
        )

        self.assertEqual(cfg.profile, "")
        self.assertTrue(cfg.state_path.endswith("/.walkcode/lark-claude-state.json"))

    def test_profile_names_state_path_by_profile_and_agent(self):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "lark",
                "LARK_APP_ID": "app-id",
                "LARK_APP_SECRET": "secret",
                "WALKCODE_AGENT": "claude",
                "WALKCODE_PROFILE": "work",
            }
        )

        self.assertEqual(cfg.profile, "work")
        self.assertTrue(cfg.state_path.endswith("/.walkcode/work-claude-state.json"))

    def test_explicit_state_path_beats_profile_derivation(self):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "lark",
                "LARK_APP_ID": "app-id",
                "LARK_APP_SECRET": "secret",
                "WALKCODE_AGENT": "codex",
                "WALKCODE_PROFILE": "personal",
                "WALKCODE_STATE_PATH": "/tmp/custom-state.json",
            }
        )

        self.assertEqual(cfg.state_path, "/tmp/custom-state.json")

    def test_invalid_profile_is_rejected(self):
        for bad in ("Work", "work profile", "work/one", "-work"):
            with self.assertRaisesRegex(ChannelConfigError, "invalid WALKCODE_PROFILE"):
                ChannelNativeConfig.from_env(
                    {
                        "WALKCODE_CHANNEL": "lark",
                        "LARK_APP_ID": "cli_x",
                        "LARK_APP_SECRET": "s",
                        "WALKCODE_AGENT": "claude",
                        "WALKCODE_PROFILE": bad,
                    }
                )

    def test_claude_config_dir_is_agent_option(self):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "lark",
                "LARK_APP_ID": "cli_x",
                "LARK_APP_SECRET": "s",
                "WALKCODE_AGENT": "claude",
                "WALKCODE_CLAUDE_CONFIG_DIR": "~/.claude-profiles/work",
            }
        )

        self.assertEqual(
            cfg.agent_options["claude"]["config_dir"].split("/")[-2:],
            [".claude-profiles", "work"],
        )

    def test_claude_anthropic_base_url_is_agent_option(self):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "lark",
                "LARK_APP_ID": "cli_x",
                "LARK_APP_SECRET": "s",
                "WALKCODE_AGENT": "claude",
                "WALKCODE_CLAUDE_ANTHROPIC_BASE_URL": "http://127.0.0.1:18899",
            }
        )

        self.assertEqual(
            cfg.agent_options["claude"]["anthropic_base_url"], "http://127.0.0.1:18899"
        )

    def test_claude_anthropic_base_url_absent_by_default(self):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "lark",
                "LARK_APP_ID": "cli_x",
                "LARK_APP_SECRET": "s",
                "WALKCODE_AGENT": "claude",
            }
        )

        self.assertNotIn("anthropic_base_url", cfg.agent_options["claude"])

    def test_invalid_claude_anthropic_base_url_is_rejected(self):
        with self.assertRaisesRegex(ChannelConfigError, "invalid WALKCODE_CLAUDE_ANTHROPIC_BASE_URL"):
            ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "lark",
                    "LARK_APP_ID": "cli_x",
                    "LARK_APP_SECRET": "s",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_CLAUDE_ANTHROPIC_BASE_URL": "127.0.0.1:18899",
                }
            )

    def test_claude_anthropic_base_url_without_host_is_rejected(self):
        with self.assertRaisesRegex(ChannelConfigError, "invalid WALKCODE_CLAUDE_ANTHROPIC_BASE_URL"):
            ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "lark",
                    "LARK_APP_ID": "cli_x",
                    "LARK_APP_SECRET": "s",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_CLAUDE_ANTHROPIC_BASE_URL": "http://",
                }
            )

    def test_claude_anthropic_base_url_empty_host_with_port_is_rejected(self):
        # urlsplit gives a non-empty netloc (":18899") but no hostname here —
        # checking netloc alone would wrongly accept this.
        with self.assertRaisesRegex(ChannelConfigError, "invalid WALKCODE_CLAUDE_ANTHROPIC_BASE_URL"):
            ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "lark",
                    "LARK_APP_ID": "cli_x",
                    "LARK_APP_SECRET": "s",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_CLAUDE_ANTHROPIC_BASE_URL": "http://:18899",
                }
            )

    def test_claude_anthropic_base_url_accepts_uppercase_scheme(self):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "lark",
                "LARK_APP_ID": "cli_x",
                "LARK_APP_SECRET": "s",
                "WALKCODE_AGENT": "claude",
                "WALKCODE_CLAUDE_ANTHROPIC_BASE_URL": "HTTP://127.0.0.1:18899",
            }
        )

        self.assertEqual(
            cfg.agent_options["claude"]["anthropic_base_url"], "HTTP://127.0.0.1:18899"
        )

    def test_claude_anthropic_base_url_rejects_combination_with_settings(self):
        with self.assertRaisesRegex(
            ChannelConfigError, "cannot be combined with WALKCODE_CLAUDE_SETTINGS"
        ):
            ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "lark",
                    "LARK_APP_ID": "cli_x",
                    "LARK_APP_SECRET": "s",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_CLAUDE_SETTINGS": "/tmp/vertex.json",
                    "WALKCODE_CLAUDE_ANTHROPIC_BASE_URL": "http://127.0.0.1:18899",
                }
            )

    def test_codex_home_is_agent_option(self):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "lark",
                "LARK_APP_ID": "cli_x",
                "LARK_APP_SECRET": "s",
                "WALKCODE_AGENT": "codex",
                "WALKCODE_CODEX_HOME": "~/.codex-profiles/personal",
            }
        )

        self.assertEqual(
            cfg.agent_options["codex"]["codex_home"].split("/")[-2:],
            [".codex-profiles", "personal"],
        )

    def test_removed_transport_env_is_rejected(self):
        with self.assertRaisesRegex(ChannelConfigError, "WALKCODE_DEFAULT_TRANSPORT is not supported"):
            ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "lark",
                    "LARK_APP_ID": "cli_x",
                    "LARK_APP_SECRET": "s",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_DEFAULT_TRANSPORT": "claude_headless",
                }
            )

    def test_legacy_feishu_env_does_not_configure_new_runtime(self):
        env = {
            "FEISHU_APP_ID": "old-app",
            "FEISHU_APP_SECRET": "old-secret",
            "FEISHU_RECEIVE_ID": "old-chat",
            "FEISHU_RECEIVE_ID_TYPE": "chat_id",
            "FEISHU_OPENAPI_DOMAIN": "https://open.feishu.cn",
        }

        with self.assertRaisesRegex(ChannelConfigError, "no channel"):
            ChannelNativeConfig.from_env(env)

    def test_channel_must_be_explicit_even_when_token_exists(self):
        with self.assertRaisesRegex(ChannelConfigError, "no channel"):
            ChannelNativeConfig.from_env(
                {
                    "LARK_APP_ID": "cli_x",
                    "LARK_APP_SECRET": "s",
                    "WALKCODE_AGENT": "claude",
                }
            )


if __name__ == "__main__":
    unittest.main()


class ClaudePermissionModeTests(unittest.TestCase):
    def test_valid_permission_mode_is_agent_option(self):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "lark",
                "LARK_APP_ID": "a",
                "LARK_APP_SECRET": "s",
                "WALKCODE_AGENT": "claude",
                "WALKCODE_CLAUDE_PERMISSION_MODE": "acceptEdits",
            }
        )
        self.assertEqual(cfg.agent_options["claude"]["permission_mode"], "acceptEdits")

    def test_invalid_permission_mode_is_rejected(self):
        with self.assertRaisesRegex(ChannelConfigError, "invalid WALKCODE_CLAUDE_PERMISSION_MODE"):
            ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "lark",
                    "LARK_APP_ID": "a",
                    "LARK_APP_SECRET": "s",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_CLAUDE_PERMISSION_MODE": "yolo",
                }
            )

    def test_permission_mode_absent_by_default(self):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "lark",
                "LARK_APP_ID": "a",
                "LARK_APP_SECRET": "s",
                "WALKCODE_AGENT": "claude",
            }
        )
        self.assertNotIn("permission_mode", cfg.agent_options["claude"])
