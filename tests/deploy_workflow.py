#!/usr/bin/env python3
"""Validate deployment gating and exercise SSH/health scripts with synthetic tools."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / '.github/workflows/image.yml').read_text()
APP = ROOT.name
RETIRED = APP in {'accountcontext', 'chatcontext', 'observecontext', 'peoplecontext', 'raisecontext'}
HOST = {'dealcontext': 'crm', 'taskcontext': 'tasks', 'accountcontext': 'accounts'}.get(APP, APP.removesuffix('context'))
PUBLICATION, DEPLOYMENT = WORKFLOW.split('  deploy:\n', 1)


def script(step):
    section = DEPLOYMENT.split('      - name: ' + step + '\n', 1)[1]
    section = section.split('      - name:', 1)[0]
    body = section.split('        run: |\n', 1)[1]
    return '\n'.join(line[10:] for line in body.splitlines() if line.startswith('          '))


class DeploymentTests(unittest.TestCase):
    def test_publication_and_deployment_gates_are_independent(self):
        self.assertEqual(DEPLOYMENT.splitlines()[0].strip(),
                         "if: " + ("false && " if RETIRED else "") + "github.ref == 'refs/heads/main' && github.event_name != 'pull_request' && vars.CONTEXT_DEPLOY_PAUSED != 'true' && vars.COLORS_PROFILE != ''")
        self.assertNotIn('CONTEXT_DEPLOY_PAUSED', PUBLICATION)
        if APP == "vaultcontext":
            self.assertIn("vars.VAULTCONTEXT_PUBLISH == 'true'", PUBLICATION)
        if APP == "notifycontext":
            self.assertIn("vars.NOTIFYCONTEXT_PUBLISH_ENABLED == 'true'", PUBLICATION)
        self.assertIn('      - manifest\n', DEPLOYMENT)
        self.assertNotIn('always()', DEPLOYMENT)
        self.assertIn('    permissions:\n      contents: read', DEPLOYMENT)

    def test_environment_and_serialization_preserve_running_deployments(self):
        self.assertIn('    environment:\n      name: ${{ vars.COLORS_PROFILE }}', DEPLOYMENT)
        self.assertIn('      group: deploy-${{ vars.COLORS_PROFILE }}\n      cancel-in-progress: false', DEPLOYMENT)
        self.assertIn("cancel-in-progress: ${{ github.ref != 'refs/heads/main' }}", PUBLICATION)
        for name in ('SERVER_IP', 'SERVER_USER', 'SSH_KNOWN_HOSTS'):
            self.assertIn(name + ': ${{ vars.' + name + ' }}', DEPLOYMENT)
        self.assertIn('SSH_PRIVATE_KEY: ${{ secrets.SSH_PRIVATE_KEY }}', DEPLOYMENT)

    def run_step(self, name, known_hosts='synthetic-pinned-host-key', failing_tool=''):
        with tempfile.TemporaryDirectory(prefix='vault-deploy-test-') as directory:
            root = Path(directory)
            calls = root / 'calls'
            for tool in ('ssh-agent', 'ssh-add', 'ssh', 'curl'):
                executable = root / tool
                executable.write_text('#!' + sys.executable + '\n'
                    'import json, os, sys\n'
                    'from pathlib import Path\n'
                    'name = Path(sys.argv[0]).name\n'
                    'with open(os.environ["SYNTHETIC_CALLS"], "a") as stream:\n'
                    '    stream.write(json.dumps([name, *sys.argv[1:]]) + "\\n")\n'
                    'if name == "ssh-add": sys.stdin.read()\n'
                    'if name == os.environ.get("FAILING_TOOL"): sys.exit(17)\n')
                executable.chmod(0o700)
            env = dict(os.environ, HOME=str(root), PATH=str(root) + ':' + os.environ['PATH'],
                       SYNTHETIC_CALLS=str(calls), FAILING_TOOL=failing_tool, SSH_PRIVATE_KEY='synthetic-private-key',
                       SSH_KNOWN_HOSTS=known_hosts, SERVER_USER='deploy',
                       SERVER_IP='192.0.2.1')
            result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script(name)],
                                    env=env, text=True, capture_output=True, timeout=10)
            events = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
            hosts = root / '.ssh/known_hosts'
            return result, events, hosts.read_text() if hosts.exists() else None

    def test_missing_pinned_host_key_never_connects(self):
        result, calls, hosts = self.run_step('Deploy via SSH', '')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('ssh', [event[0] for event in calls])
        self.assertIsNone(hosts)
        self.assertNotIn('synthetic-private-key', result.stdout + result.stderr)

    def test_ssh_uses_pinned_identity_without_remote_command(self):
        result, calls, hosts = self.run_step('Deploy via SSH')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(hosts, 'synthetic-pinned-host-key\n')
        self.assertEqual([event for event in calls if event[0] == 'ssh'], [
            ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', 'deploy@192.0.2.1']])
        self.assertNotIn('synthetic-private-key', result.stdout + result.stderr)
        self.assertNotIn('ssh-keyscan', script('Deploy via SSH'))

    def test_health_check_targets_vault_with_bounded_retries(self):
        result, calls, _ = self.run_step('Verify public health')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 1)
        args = calls[0]
        self.assertEqual(args[0], 'curl')
        self.assertEqual(args[-1], f'https://{HOST}.pocketcontext.com/up')
        self.assertIn('--fail', args)
        self.assertEqual(args[args.index('--retry-max-time') + 1], '180')
        self.assertEqual(args[args.index('--max-time') + 1], '20')

    def test_transport_and_health_failures_propagate(self):
        for step, tool in [('Deploy via SSH', 'ssh'), ('Verify public health', 'curl')]:
            with self.subTest(step=step):
                result, _, _ = self.run_step(step, failing_tool=tool)
                self.assertEqual(result.returncode, 17)

    def test_retired_commands_fail_closed_with_guidance(self):
        for filename in ('install.py', f'deploy-{APP}.py'):
            for extra in ([], ['unexpected-target']):
                with self.subTest(filename=filename, extra=extra):
                    result = subprocess.run([sys.executable, '-I', str(ROOT / 'deploy' / filename), *extra],
                                            env={'PATH': '/nonexistent'}, capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('once-pocketcontext-v2', result.stderr)
                    self.assertIn('retired', result.stderr)
                    self.assertEqual(result.stdout, '')


    def test_application_and_container_checks_cover_both_architectures(self):
        application = (ROOT / '.github/workflows/test.yml').read_text()
        triggers = application.split('permissions:', 1)[0]
        self.assertIn('  workflow_call:', triggers)
        self.assertNotIn('  push:', triggers)
        self.assertNotIn('  pull_request:', triggers)
        self.assertIn('ubuntu-24.04-arm', application)
        check = PUBLICATION.split('  check:', 1)[1].split('  tests:', 1)[0]
        self.assertIn('runner: ubuntu-24.04-arm', check)
        self.assertIn('runner: ubuntu-24.04', check)
        for mode in (' config --image ', ' smoke --image '):
            self.assertIn(mode, check)
        self.assertTrue(' restore --image ' in check or 'docker/object_storage_smoke.py' in check)
        tests = PUBLICATION.split('  tests:', 1)[1].split('  build:', 1)[0]
        self.assertNotIn('    if:', tests)
        build = PUBLICATION.split('  build:', 1)[1].split('  manifest:', 1)[0]
        self.assertIn('      - check', build)
        self.assertIn('      - tests', build)
        if APP == 'vaultcontext':
            self.assertIn('macos-15', application)

    def test_current_main_guard_precedes_promotion_and_deployment(self):
        guard = '      - name: Require current main revision'
        manifest = PUBLICATION.split('  manifest:', 1)[1]
        self.assertLess(manifest.index(guard), manifest.index('--tag "$IMAGE:latest"'))
        self.assertLess(DEPLOYMENT.index(guard), DEPLOYMENT.index('      - name: Deploy via SSH'))
        self.assertIn('--tag "$IMAGE:sha-$GITHUB_SHA"', manifest)
        self.assertNotIn('continue-on-error', manifest + DEPLOYMENT)

    def test_current_main_guard_rejects_stale_malformed_and_failed_lookup(self):
        with tempfile.TemporaryDirectory(prefix='main-guard-') as directory:
            root = Path(directory)
            gh = root / 'gh'
            gh.write_text('#!/bin/sh\nprintf "%s\\n" "$SYNTHETIC_HEAD"\nexit "$SYNTHETIC_EXIT"\n')
            gh.chmod(0o700)
            expected = 'a' * 40
            for head, status, success in [(expected, '0', True), ('b'*40, '0', False),
                                           ('', '0', False), ('malformed', '0', False),
                                           (expected, '1', False)]:
                with self.subTest(head=head, status=status):
                    env = dict(os.environ, PATH=str(root)+':'+os.environ['PATH'],
                               GITHUB_SHA=expected, GITHUB_REPOSITORY='example/app',
                               SYNTHETIC_HEAD=head, SYNTHETIC_EXIT=status)
                    result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c',
                                             script('Require current main revision')],
                                            env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode == 0, success, result.stderr)


if __name__ == '__main__':
    unittest.main()
