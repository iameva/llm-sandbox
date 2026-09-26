"""Exercise launchers with a temporary home and a stub QEMU runtime."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


class RunnerFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sandbox tests ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.capture = self.home / "argv.json"
        stub = self.home / "stub"
        stub.mkdir()
        # The launcher runs every helper through python3. Only the VM
        # runtime is replaced; backend selection and images.py stay real.
        python = stub / "python3"
        python.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            "*/qemu/sandbox.py)\n"
            f"    exec {sys.executable} -c 'import json, os, sys; "
            'open(os.environ["CAPTURE"], "w").write(json.dumps(sys.argv[1:])); '
            "sys.exit(int(os.environ.get(\"RUNNER_EXIT\", \"0\")))' \"$@\" ;;\n"
            "esac\n"
            f'exec {sys.executable} "$@"\n'
        )
        python.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("SANDBOX_")}
        self.env.update(HOME=str(self.home), PATH=f"{stub}:{os.environ['PATH']}",
                        CAPTURE=str(self.capture), OPENAI_API_KEY="", DEEPSEEK_API_KEY="",
                        SANDBOX_QEMU_DISK="/images/agents.qcow2")
        subprocess.run(["sh", "install.sh"], cwd=REPO, env=self.env,
                       check=True, capture_output=True)
        # Baseline launcher tests exercise native behavior without configured defaults.
        (self.home / ".config/llm-sandbox/backends.json").write_text("{}")
        (self.home / ".config/deepseek.api").write_text("test-key")

    def launch(self, agent, *args, **env):
        return subprocess.run(["bash", str(REPO / "sandbox-run.sh"), agent, *args],
                              cwd=REPO, env={**self.env, **env},
                              capture_output=True, text=True)

    def argv(self):
        return json.loads(self.capture.read_text())

    def values(self, option):
        argv = self.argv()
        return [argv[i + 1] for i, item in enumerate(argv) if item == option]

    def command(self):
        argv = self.argv()
        return argv[argv.index("--command") + 1:]


class RunnerTests(RunnerFixture):
    def test_agents_select_commands_and_preserve_argument_order(self):
        commands = {
            "claude": ["claude", "--dangerously-skip-permissions"],
            "codex": ["codex", "--dangerously-bypass-approvals-and-sandbox"],
            "deepseek-claude": ["claude", "--dangerously-skip-permissions", "--model", "deepseek-v4-pro"],
            "opencode": ["opencode"], "pi": ["pi"], "omp": ["omp"],
            "llm": ["zsh"],
        }
        for agent, command in commands.items():
            with self.subTest(agent=agent):
                result = self.launch(agent, "--example", "two words", "$(literal)", "")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.command(), [*command, "--example", "two words", "$(literal)", ""])
                argv = self.argv()
                self.assertTrue(argv[0].endswith("/qemu/sandbox.py"))
                self.assertEqual(argv[argv.index("--workspace") + 1], str(REPO))
                for mount in self.values("--mount"):
                    source, target = mount.split(":", 1)
                    self.assertTrue(source.startswith(str(self.home / ".config/llm-sandbox/")))
                    self.assertTrue(target.startswith("/home/fedora/"))

    def test_installed_entry_point(self):
        result = subprocess.run([str(self.home / ".local/bin/,codex-sandbox.sh"), "--resume"],
                                env=self.env, cwd=REPO, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.command(), ["codex", "--dangerously-bypass-approvals-and-sandbox", "--resume"])
        self.assertTrue(self.argv()[0].startswith(str(self.home / ".config/llm-sandbox/qemu/")))

    def test_removed_isolation_modes_fail_before_launch(self):
        for mode in ("container", "gvisor", "vm"):
            with self.subTest(mode=mode):
                result = self.launch("codex", SANDBOX_ISOLATION=mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("no longer supported", result.stderr)
                self.assertFalse(self.capture.exists())
        self.assertEqual(self.launch("codex", SANDBOX_ISOLATION="qemu").returncode, 0)

    def test_runtime_failure_propagates(self):
        self.assertEqual(self.launch("codex", RUNNER_EXIT="42").returncode, 42)

    def test_dry_run_does_not_create_state(self):
        result = self.launch("claude", SANDBOX_DRY_RUN="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sandbox.py", result.stdout)
        self.assertFalse(self.capture.exists())
        self.assertFalse((self.home / ".config/llm-sandbox/claude").exists())

    def test_claude_migration_preserves_existing_config(self):
        root = self.home / ".config/llm-sandbox"
        (root / ".claude.json").write_text('{"legacy": true}')
        self.assertEqual(self.launch("claude").returncode, 0)
        config = root / "claude/.claude.json"
        self.assertEqual(config.read_text(), '{"legacy": true}')
        self.assertEqual((root / ".claude.json").read_text(), '{"legacy": true}')
        config.write_text('{"current": true}')
        self.assertEqual(self.launch("claude").returncode, 0)
        self.assertEqual(config.read_text(), '{"current": true}')
        self.assertIn("CLAUDE_CONFIG_DIR=/home/fedora/.claude", self.values("--env"))

    def test_shell_keeps_agent_mount(self):
        self.assertEqual(self.launch("codex", "--shell", "-c", "echo hello").returncode, 0)
        self.assertEqual(self.command(), ["zsh", "-c", "echo hello"])
        self.assertIn(f"{self.home}/.config/llm-sandbox/codex:/home/fedora/.config/codex",
                      self.values("--mount"))

    def test_check_verifies_the_agent_configuration(self):
        self.assertEqual(self.launch("codex", "--check", "ignored").returncode, 0)
        self.assertIn("--verify", self.argv())
        self.assertEqual(self.command(), ["true"])
        self.assertIn(f"{self.home}/.config/llm-sandbox/codex:/home/fedora/.config/codex",
                      self.values("--mount"))

    def test_llm_shell_mounts_only_requested_state(self):
        self.assertEqual(self.launch("llm").returncode, 0)
        self.assertEqual(self.values("--mount"), [])
        self.assertEqual(self.launch("llm", SANDBOX_LLM_AGENTS="claude,codex").returncode, 0)
        targets = [mount.split(":", 1)[1] for mount in self.values("--mount")]
        self.assertEqual(targets, ["/home/fedora/.claude", "/home/fedora/.config/codex", "/home/fedora/.orca"])
        self.assertNotEqual(self.launch("llm", SANDBOX_LLM_AGENTS="aider").returncode, 0)

    def test_unknown_and_removed_agents_are_errors(self):
        for agent in ("missing", "aider"):
            with self.subTest(agent=agent):
                self.assertNotEqual(self.launch(agent).returncode, 0)
                self.assertFalse(self.capture.exists())


if __name__ == "__main__":
    unittest.main()
