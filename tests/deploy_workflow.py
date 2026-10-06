#!/usr/bin/env python3
"""Exercise deployment orchestration without Docker, root, network or credentials."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = load("hook", "deploy-dealcontext.py")
installer = load("installer", "install.py")


class DeploymentTests(unittest.TestCase):
    def test_cd_pause_blocks_only_deployment_and_preserves_profile(self):
        workflow = (ROOT / ".github/workflows/image.yml").read_text()
        publication, deployment = workflow.split("  deploy:\n", 1)
        self.assertEqual(deployment.splitlines()[0].strip(),
                         "if: vars.CONTEXT_DEPLOY_PAUSED != 'true' && vars.COLORS_PROFILE != ''")
        self.assertNotIn("CONTEXT_DEPLOY_PAUSED", publication)
        self.assertIn("name: ${{ vars.COLORS_PROFILE }}", deployment)

    def run_hook(self, fail=None, killed=False, count=1, recovery_count=None):
        calls = []
        inspections = 0
        def command(*args, capture=False):
            nonlocal inspections
            calls.append(args)
            if args[:2] == fail:
                raise RuntimeError("stub failure")
            if args[:2] == ("docker", "ps"):
                return "old"
            if args[:2] == ("docker", "inspect"):
                if "--format" in args:
                    return json.dumps({"Running": False, "ExitCode": 137 if killed else 0})
                inspections += 1
                size = recovery_count if inspections > 1 and recovery_count is not None else count
                return json.dumps([{"Id": "old", "Config": {"Image": hook.IMAGE, "Labels": {
                    "once": json.dumps({"host": hook.HOST, "env": {"SECRET": "never print"}})}}}] * size)
            return ""
        with patch.object(hook, "run", side_effect=command):
            if fail or killed or count != 1:
                with self.assertRaises(RuntimeError):
                    hook.deploy()
            else:
                hook.deploy()
        return calls

    def test_order(self):
        calls = self.run_hook()
        self.assertEqual([c[:2] for c in calls], [
            ("docker", "ps"), ("docker", "inspect"), ("docker", "pull"),
            ("docker", "stop"), ("docker", "inspect"), ("once", "update")])
        self.assertEqual(calls[3], ("docker", "stop", "--time", "60", "old"))
        self.assertEqual(calls[-1], ("once", "update", hook.HOST, "--auto-update=false"))

    def test_pull_failure_does_not_stop(self):
        calls = self.run_hook(fail=("docker", "pull"))
        self.assertNotIn(("docker", "stop"), [c[:2] for c in calls])
        self.assertNotIn(("once", "start"), [c[:2] for c in calls])

    def test_update_failure_recovers_and_fails(self):
        self.assertEqual(self.run_hook(fail=("once", "update"))[-1], ("docker", "start", "old"))

    def test_stop_failure_recovers_without_update(self):
        calls = self.run_hook(fail=("docker", "stop"))
        self.assertEqual(calls[-1], ("docker", "start", "old"))
        self.assertNotIn(("once", "update"), [c[:2] for c in calls])

    def test_forced_stop_does_not_update(self):
        calls = self.run_hook(killed=True)
        self.assertEqual(calls[-1], ("docker", "start", "old"))
        self.assertNotIn(("once", "update"), [c[:2] for c in calls])

    def test_ambiguous_containers_never_mutate(self):
        self.assertEqual(len(self.run_hook(count=2)), 2)

    def test_ambiguous_recovery_never_starts_another_writer(self):
        calls = self.run_hook(fail=("once", "update"), recovery_count=2)
        self.assertNotIn(("docker", "start"), [c[:2] for c in calls])

    def test_missing_old_container_never_starts_another_writer(self):
        calls = self.run_hook(fail=("once", "update"), recovery_count=0)
        self.assertNotIn(("docker", "start"), [c[:2] for c in calls])

    def test_arguments_rejected_before_commands(self):
        with patch.object(sys, "argv", ["deploy-dealcontext", "other-host"]), patch.object(hook, "run") as run:
            with self.assertRaises(RuntimeError):
                hook.main()
            run.assert_not_called()

    def test_installer_preserves_other_keys_and_restrictions(self):
        website = 'restrict,command="/usr/local/bin/deploy www.pocketcontext.com" ssh-ed25519 AAA website\n'
        crm = f'no-port-forwarding,no-pty,{installer.OLD} ssh-ed25519 BBB crm\n'
        result = installer.rewrite_keys(website + crm + crm)
        self.assertEqual(result, website + (crm.replace(installer.OLD, installer.NEW) * 2))
        self.assertEqual(installer.rewrite_keys(result), result)
        with self.assertRaises(RuntimeError):
            installer.rewrite_keys(website)


if __name__ == "__main__":
    unittest.main()
