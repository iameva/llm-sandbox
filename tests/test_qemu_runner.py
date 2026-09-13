"""Test real launcher dispatch without starting a VM."""
import os
import json
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

    def test_qemu_resource_settings_reach_installed_runtime(self):
        result = self.qemu('codex', SANDBOX_QEMU_MEMORY_MIB='4096', SANDBOX_QEMU_CPUS='4',
                           SANDBOX_QEMU_BOOT_TIMEOUT='300', SANDBOX_QEMU_BATCH_TIMEOUT='0',
                           SANDBOX_QEMU_IDLE_TIMEOUT='0', SANDBOX_QEMU_CACHE_DIR='/disk/cache',
                           SANDBOX_QEMU_KEEP_ARTIFACTS='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = shlex.split(result.stdout)
        for option, value in [('memory-mib', '4096'), ('cpus', '4'), ('boot-timeout', '300'),
                              ('batch-timeout', '0'), ('idle-timeout', '0'), ('cache-dir', '/disk/cache')]:
            self.assertEqual(argv[argv.index('--'+option)+1], value)
        self.assertIn('--keep-artifacts', argv)

    def test_missing_image_setting_fails_without_podman(self):
        result = self.launch('codex', SANDBOX_ISOLATION='qemu', SANDBOX_DRY_RUN='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('SANDBOX_QEMU_DISK', result.stderr)
        self.assertFalse(self.capture.exists())

    def test_configured_image_and_explicit_override(self):
        image = self.home/'agents.qcow2'
        image.write_bytes(b'test image; no VM is started')
        image.chmod(0o400)
        config = self.home/'.config/llm-sandbox/qemu.json'
        config.write_text(json.dumps({'version': 1, 'active_image': str(image)}))
        result = self.launch('codex', SANDBOX_ISOLATION='qemu', SANDBOX_DRY_RUN='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = shlex.split(result.stdout)
        self.assertEqual(argv[argv.index('--disk')+1], str(image))
        override = self.qemu('codex')
        argv = shlex.split(override.stdout)
        self.assertEqual(argv[argv.index('--disk')+1], '/images/agents.qcow2')

    def test_installed_manager_and_builder_and_config_survive_reinstall(self):
        config = self.home/'.config/llm-sandbox/qemu.json'
        config.write_text('{"version": 1}')
        subprocess.run(['sh', 'install.sh'], cwd=REPO, env=self.env, check=True, capture_output=True)
        self.assertEqual(config.read_text(), '{"version": 1}')
        manager = self.home/'.local/bin/,sandbox-image'
        for command in ([str(manager), 'status'],
                        ['python3', str(self.home/'.config/llm-sandbox/qemu/build_image.py'), '--help']):
            result = subprocess.run(command, cwd=self.home, env=self.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
