"""Test real launcher dispatch without starting a VM."""
import os
from pathlib import Path
import shlex
import subprocess
from test_runner import RunnerFixture, REPO


class QemuRunnerTests(RunnerFixture):
    def qemu(self, agent, *args, **env):
        return self.launch(agent, *args, SANDBOX_ISOLATION='qemu',
                           SANDBOX_QEMU_DISK='/images/agents.qcow2',
                           SANDBOX_DRY_RUN='1', **env)

    def test_same_agent_launches_share_state_but_select_no_common_disk(self):
        first = self.qemu('codex', '--resume', 'two words')
        second = self.qemu('codex', '--resume', 'two words')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        argv = shlex.split(first.stdout)
        self.assertEqual(argv, shlex.split(second.stdout))
        self.assertNotIn('--vm-dir', argv)
        self.assertIn(str(self.home/'.config/llm-sandbox/codex')+':/home/fedora/.config/codex', argv)
        self.assertEqual(argv[-4:], ['codex', '--dangerously-bypass-approvals-and-sandbox', '--resume', 'two words'])
        self.assertNotIn('podman', argv)
        self.assertFalse(self.capture.exists())

    def test_shell_and_check_keep_agent_state(self):
        for flag, expected in [('--shell', 'bash'), ('--check', 'true')]:
            result = self.qemu('claude', flag)
            self.assertEqual(result.returncode, 0, result.stderr)
            argv = shlex.split(result.stdout)
            self.assertIn(str(self.home/'.config/llm-sandbox/claude')+':/home/fedora/.claude', argv)
            self.assertEqual(argv[-1], expected)
            if flag == '--check':
                self.assertIn('--verify', argv)

    def test_repo_allowlist_override_and_literal_arguments(self):
        result = self.qemu('pi', '$(literal)', '', SANDBOX_ALLOW_FILE=str(REPO/'egress-allowlist.txt'))
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = shlex.split(result.stdout)
        self.assertEqual(argv[argv.index('--allow-file')+1], str(REPO/'egress-allowlist.txt'))
        self.assertEqual(argv[-2:], ['$(literal)', ''])

    def test_installed_entry_uses_installed_qemu_runtime(self):
        result = subprocess.run([str(self.home/'.local/bin/,omp-sandbox.sh')],
                                env={**self.env, 'SANDBOX_ISOLATION': 'qemu',
                                     'SANDBOX_QEMU_DISK': '/images/agents.qcow2', 'SANDBOX_DRY_RUN': '1'},
                                cwd=REPO, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(self.home/'.config/llm-sandbox/qemu/sandbox.py'), shlex.split(result.stdout))
        self.assertTrue((self.home/'.config/llm-sandbox/qemu/egress-proxy.py').is_file())

    def test_missing_image_setting_fails_without_podman(self):
        result = self.launch('codex', SANDBOX_ISOLATION='qemu', SANDBOX_DRY_RUN='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('SANDBOX_QEMU_DISK', result.stderr)
        self.assertFalse(self.capture.exists())
