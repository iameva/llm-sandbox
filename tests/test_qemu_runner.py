"""Test real launcher dispatch without starting a VM."""
import os
import json
from pathlib import Path
import shlex
import subprocess
from test_runner import RunnerFixture, REPO


class QemuRunnerTests(RunnerFixture):
    def qemu(self, agent, *args, **env):
        return self.launch(agent, *args, SANDBOX_DRY_RUN='1', **env)

    def assert_mcp_ports(self, argv, expected):
        runtime = argv[:argv.index('--command')]
        self.assertEqual([runtime[i + 1] for i, arg in enumerate(runtime) if arg == '--mcp-port'],
                         expected)
        self.assertFalse(any(arg.startswith('--mcp-port=') for arg in runtime))
        self.assertNotIn('--no-host-mcp', runtime)

    def test_mcp_ports_are_disabled_by_default(self):
        for env in ({}, {'SANDBOX_MCP_PORTS': ''}):
            with self.subTest(env=env):
                result = self.qemu('pi', **env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_mcp_ports(shlex.split(result.stdout), [])
                self.assertFalse(self.capture.exists())

    def test_mcp_port_options_are_repeatable_and_accept_equals_form(self):
        for args in [('--mcp-port', '1024', '--mcp-port', '65535'),
                     ('--mcp-port=1024', '--mcp-port=65535'),
                     ('--mcp-port', '1024', '--mcp-port=65535')]:
            with self.subTest(args=args):
                result = self.launch('codex', *args, '--resume', 'two words')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_mcp_ports(self.argv(), ['1024', '65535'])
                self.assertEqual(self.command(), ['codex', '--dangerously-bypass-approvals-and-sandbox',
                                                  '--resume', 'two words'])

    def test_mcp_environment_ports_combine_with_options_without_forwarding_host_secrets(self):
        secret = 'host-only-mcp-secret'
        for dry_run in ('0', '1'):
            for args, extra in [((), []),
                                (('--mcp-port', '8765', '--mcp-port=8766'), ['8765', '8766'])]:
                with self.subTest(dry_run=dry_run, args=args):
                    self.capture.unlink(missing_ok=True)
                    result = self.launch('pi', *args, '--resume', 'two words',
                                         SANDBOX_DRY_RUN=dry_run, SANDBOX_MCP_PORTS='1024,65535',
                                         CUSTOM_MCP_API_KEY=secret)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    argv = shlex.split(result.stdout) if dry_run == '1' else self.argv()
                    self.assert_mcp_ports(argv, ['1024', '65535', *extra])
                    self.assertEqual(argv[argv.index('--command') + 1:], ['pi', '--resume', 'two words'])
                    self.assertNotIn('CUSTOM_MCP_API_KEY', ' '.join(argv))
                    self.assertNotIn(secret, ' '.join(argv))
                    if dry_run == '1':
                        self.assertFalse(self.capture.exists())

    def test_no_host_mcp_disables_inherited_ports_without_emitting_port_options(self):
        for dry_run in ('0', '1'):
            for args in [('--no-host-mcp',), ('--no-host-mcp', '--no-host-mcp')]:
                with self.subTest(dry_run=dry_run, args=args):
                    self.capture.unlink(missing_ok=True)
                    result = self.launch('pi', *args, '--resume', 'two words',
                                         SANDBOX_DRY_RUN=dry_run, SANDBOX_MCP_PORTS='1024,65535')
                    self.assertEqual(result.returncode, 0, result.stderr)
                    argv = shlex.split(result.stdout) if dry_run == '1' else self.argv()
                    self.assert_mcp_ports(argv, [])
                    self.assertEqual(argv[argv.index('--command') + 1:], ['pi', '--resume', 'two words'])
                    if dry_run == '1':
                        self.assertFalse(self.capture.exists())

    def test_no_host_mcp_ignores_invalid_environment_without_validation(self):
        for value in ('', 'not-a-port', '8765,,8766', '8765, 8766', '8765\n8766',
                      '0', '1023', '65536', '99999999999999999999', '8765,65536'):
            with self.subTest(value=value):
                result = self.launch('pi', '--no-host-mcp', SANDBOX_MCP_PORTS=value)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_mcp_ports(self.argv(), [])
                self.assertEqual(self.command(), ['pi'])

    def test_no_host_mcp_conflicts_with_explicit_ports_in_either_order(self):
        for option in [('--mcp-port', '8765'), ('--mcp-port=8765',),
                       ('--mcp-port', 'not-a-port'), ('--mcp-port=',)]:
            for args in [('--no-host-mcp', *option), (*option, '--no-host-mcp')]:
                with self.subTest(args=args):
                    result = self.launch('pi', *args, SANDBOX_MCP_PORTS='not-a-port')
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('--no-host-mcp cannot be combined with --mcp-port', result.stderr)
                    self.assertFalse(self.capture.exists())

    def test_native_pi_no_mcp_passes_through_without_disabling_host_forwarding(self):
        for prefix, ports in [((), ['8764']), (('--mcp-port', '8765'), ['8764', '8765']),
                              (('--no-host-mcp',), [])]:
            with self.subTest(prefix=prefix):
                result = self.launch('pi', *prefix, '--no-mcp', '--resume', 'two words',
                                     SANDBOX_MCP_PORTS='8764')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_mcp_ports(self.argv(), ports)
                self.assertEqual(self.command(), ['pi', '--no-mcp', '--resume', 'two words'])

    def test_mcp_port_option_requires_a_value(self):
        for args in [('--mcp-port',), ('--mcp-port', ''), ('--mcp-port', '--resume')]:
            with self.subTest(args=args):
                self.capture.unlink(missing_ok=True)
                result = self.launch('pi', *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('--mcp-port requires a value', result.stderr)
                self.assertFalse(self.capture.exists())

    def test_mcp_port_options_reject_invalid_values(self):
        for value in ('', '0', '1023', '65536', '99999999999999999999', '-1', '+8765',
                      'not-a-port', '8765.0', '8765,8766', ' 8765', '8765 '):
            for args in [('--mcp-port', value), ('--mcp-port=' + value,)]:
                with self.subTest(args=args):
                    self.capture.unlink(missing_ok=True)
                    result = self.launch('pi', *args)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('mcp', result.stderr.lower())
                    self.assertFalse(self.capture.exists())

    def test_mcp_environment_rejects_invalid_ranges_and_list_syntax(self):
        for value in ('0', '1023', '65536', '99999999999999999999', '8765,1023', '8765,65536',
                      '-1', 'not-a-port', ',', ',8765', '8765,', '8765,,8766', '8765, 8766',
                      ' 8765', '8765 ', '8765 8766', '8765;8766', '8765\n8766'):
            with self.subTest(value=value):
                self.capture.unlink(missing_ok=True)
                result = self.launch('pi', '--mcp-port', '8767', SANDBOX_MCP_PORTS=value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('MCP', result.stderr)
                self.assertFalse(self.capture.exists())

    def test_mcp_option_parsing_stops_at_first_harness_argument(self):
        for args in [('--resume', '--no-host-mcp', '--mcp-port', '8766', '--mcp-port=8767'),
                     ('a prompt', '--mcp-port', 'not-a-port', '--no-host-mcp', '--mcp-port=65536'),
                     ('--', '--no-host-mcp', '--mcp-port'),
                     ('--no-mcp', '--no-host-mcp', '--mcp-port=8766')]:
            with self.subTest(args=args):
                result = self.launch('pi', '--mcp-port', '8765', *args, SANDBOX_MCP_PORTS='8764')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_mcp_ports(self.argv(), ['8764', '8765'])
                self.assertEqual(self.command(), ['pi', *args])

    def test_no_host_mcp_respects_harness_and_shell_argument_boundaries(self):
        for prefix, env_ports, ports in [((), '8764', ['8764']),
                                         (('--no-host-mcp',), 'not-a-port', [])]:
            for args in [('--resume', '--no-host-mcp', '--mcp-port=65536'),
                         ('--shell', '--no-host-mcp', '--mcp-port', 'not-a-port'),
                         ('--shell', '-c', 'printf "%s\\n" "$@"', 'shell',
                          '--no-host-mcp', '--mcp-port=65536')]:
                with self.subTest(prefix=prefix, args=args):
                    result = self.launch('pi', *prefix, *args, SANDBOX_MCP_PORTS=env_ports)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assert_mcp_ports(self.argv(), ports)
                    expected = ['zsh', *args[1:]] if args[0] == '--shell' else ['pi', *args]
                    self.assertEqual(self.command(), expected)

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
        self.assertFalse(self.capture.exists())

    def test_shell_and_check_keep_agent_state(self):
        for flag, expected in [('--shell', 'zsh'), ('--check', 'true')]:
            with self.subTest(flag=flag):
                result = self.qemu('claude', '--mcp-port', '8765', '--mcp-port=8766', flag,
                                   SANDBOX_MCP_PORTS='8764')
                self.assertEqual(result.returncode, 0, result.stderr)
                argv = shlex.split(result.stdout)
                self.assert_mcp_ports(argv, ['8764', '8765', '8766'])
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
        result = subprocess.run([str(self.home/'.local/bin/,omp-sandbox.sh'),
                                 '--mcp-port', '8765', '--mcp-port=8766', '--resume'],
                                env={**self.env, 'SANDBOX_DRY_RUN': '1', 'SANDBOX_MCP_PORTS': '8764'},
                                cwd=REPO, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = shlex.split(result.stdout)
        self.assertIn(str(self.home/'.config/llm-sandbox/qemu/sandbox.py'), argv)
        self.assert_mcp_ports(argv, ['8764', '8765', '8766'])
        self.assertEqual(argv[argv.index('--command') + 1:], ['omp', '--resume'])
        self.assertTrue((self.home/'.config/llm-sandbox/qemu/egress-proxy.py').is_file())
        self.assertTrue((self.home/'.config/llm-sandbox/qemu/relay_guard.py').is_file())

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

    def test_missing_image_setting_fails_before_launch(self):
        result = self.launch('codex', SANDBOX_QEMU_DISK='', SANDBOX_DRY_RUN='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('SANDBOX_QEMU_DISK', result.stderr)
        self.assertFalse(self.capture.exists())

    def test_configured_image_and_explicit_override(self):
        image = self.home/'agents.qcow2'
        image.write_bytes(b'test image; no VM is started')
        image.chmod(0o400)
        config = self.home/'.config/llm-sandbox/qemu.json'
        config.write_text(json.dumps({'version': 1, 'active_image': str(image)}))
        result = self.launch('codex', SANDBOX_QEMU_DISK='', SANDBOX_DRY_RUN='1')
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
