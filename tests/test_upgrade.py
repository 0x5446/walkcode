"""Regression test for `walkcode upgrade` re-installing hooks with the NEW code.

Background: cmd_upgrade replaced the on-disk package with `uv tool install`, then
called cmd_install_hooks() *in-process*. But the running interpreter is still the
OLD version, so the in-process call wrote the previous version's hook set —
silently dropping any hook the new version added. This is exactly how v0.10.17's
UserPromptSubmit hook went missing after `walkcode upgrade` until install-hooks
was re-run by hand.

The fix invokes install-hooks through the freshly installed `walkcode` shim
(a subprocess running the NEW code). This test pins that contract.
"""

import argparse
import unittest
from unittest.mock import patch

from walkcode import __main__ as m


class UpgradeReinstallHooksTests(unittest.TestCase):
    def test_install_hooks_runs_via_new_binary_not_in_process(self):
        calls = []
        with patch.object(m, "_run", lambda cmd, **kw: calls.append(cmd)), \
             patch.object(m, "_get_latest_tag", lambda: "v9.9.9"), \
             patch.object(m, "_current_version", lambda: "0.0.0"), \
             patch.object(m, "_read_pid", lambda: None), \
             patch.object(m, "cmd_install_hooks") as in_proc:
            m.cmd_upgrade(argparse.Namespace())

        # install-hooks must run through the freshly installed shim (subprocess)…
        self.assertTrue(
            any("install-hooks" in c for c in calls),
            f"expected a subprocess `walkcode install-hooks`, got {calls}",
        )
        # …and NOT via the old in-process function (which would write stale hooks)
        in_proc.assert_not_called()
        # …after the package itself was reinstalled
        self.assertTrue(any("uv tool install" in c for c in calls))
        # ordering: uv install happens before install-hooks
        uv_idx = next(i for i, c in enumerate(calls) if "uv tool install" in c)
        hooks_idx = next(i for i, c in enumerate(calls) if "install-hooks" in c)
        self.assertLess(uv_idx, hooks_idx)


if __name__ == "__main__":
    unittest.main()
