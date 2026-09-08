"""Exercise launchers with a temporary home and a Podman stub."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import shutil

REPO = Path(__file__).resolve().parents[1]


class RunnerFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sandbox tests ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.capture = self.home / "argv.json"
        stub = self.home / "stub"
        stub.mkdir()
        podman = stub / "podman"
        podman.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n"
            "sys.exit(int(os.environ.get('PODMAN_EXIT', '0')))\n"
        )
        podman.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("SANDBOX_")}
        self.env.update(HOME=str(self.home), PATH=f"{stub}:{os.environ['PATH']}",
                        CAPTURE=str(self.capture), OPENAI_API_KEY="", DEEPSEEK_API_KEY="")
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


class RunnerTests(RunnerFixture):
    def test_agents_create_sources_and_preserve_argument_order(self):
        commands = {
            "claude": ["claude", "--dangerously-skip-permissions"],
            "codex": ["codex", "--dangerously-bypass-approvals-and-sandbox"],
            "deepseek-claude": ["claude", "--dangerously-skip-permissions", "--model", "deepseek-v4-pro"],
            "opencode": ["opencode"], "pi": ["pi"], "omp": ["omp"],
            "aider": ["aider"], "llm": ["zsh"],
        }
        for agent, command in commands.items():
            with self.subTest(agent=agent):
                result = self.launch(agent, "--example", "two words", "$(literal)", "")
                self.assertEqual(result.returncode, 0, result.stderr)
                argv = self.argv()
                tail = ["llm-sandbox", *command, "--example", "two words", "$(literal)", ""]
                self.assertEqual(argv[-len(tail):], tail)
                self.assertEqual(argv[:6], ["run", "--rm", "-it", "--userns=keep-id",
                                           "--workdir", "/workspace"])
                self.assertIn("--network=pasta", argv)
                for i, arg in enumerate(argv):
                    if arg == "-v":
                        self.assertTrue(Path(argv[i + 1].split(":")[0]).exists())

    def test_installed_entry_point(self):
        result = subprocess.run([str(self.home / ".local/bin/,codex-sandbox.sh"), "--resume"],
                                env=self.env, cwd=REPO, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.argv()[-3:], ["codex", "--dangerously-bypass-approvals-and-sandbox", "--resume"])

    def test_confinement_fails_before_launch_even_with_ack(self):
        result = self.launch("codex", SANDBOX_CONFINE="1", SANDBOX_CONFINE_ACK="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported", result.stderr)
        self.assertFalse(self.capture.exists())
        self.assertFalse((self.home / ".config/llm-sandbox/codex").exists())

    def test_podman_failure_propagates(self):
        self.assertEqual(self.launch("codex", PODMAN_EXIT="42").returncode, 42)

    def test_dry_run_does_not_create_mounts(self):
        result = self.launch("codex", SANDBOX_DRY_RUN="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.capture.exists())
        self.assertFalse((self.home / ".config/llm-sandbox/codex").exists())

    def test_claude_migration_preserves_existing_config(self):
        root = self.home / ".config/llm-sandbox"
        (root / ".claude.json").write_text('{"legacy": true}')
        self.assertEqual(self.launch("claude").returncode, 0)
        config = root / "claude/.claude.json"
        self.assertEqual(config.read_text(), '{"legacy": true}')
        config.write_text('{"current": true}')
        self.assertEqual(self.launch("claude").returncode, 0)
        self.assertEqual(config.read_text(), '{"current": true}')

    def test_proxy_arguments(self):
        result = self.launch("codex", SANDBOX_PROXY="127.0.0.1:8080",
                             SANDBOX_PROXY_PREFLIGHT="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.argv()
        self.assertIn("--network=pasta:-T,8080", argv)
        self.assertIn("HTTPS_PROXY=http://127.0.0.1:8080", argv)
        self.assertIn("--dns=none", argv)

    def test_shell_keeps_agent_mount(self):
        self.assertEqual(self.launch("codex", "--shell", "-c", "echo hello").returncode, 0)
        argv = self.argv()
        self.assertEqual(argv[-4:], ["llm-sandbox", "zsh", "-c", "echo hello"])
        self.assertTrue(any(":/home/appuser/.config/codex:" in arg for arg in argv))

    def test_unknown_agent_is_an_error(self):
        self.assertNotEqual(self.launch("missing").returncode, 0)
        self.assertFalse(self.capture.exists())

    @unittest.skipUnless(shutil.which("zsh"), "zsh is needed to run sandbox checks")
    def test_proxy_check_requires_an_explicit_refusal(self):
        result = self.launch("llm", "--check", SANDBOX_PROXY="127.0.0.1:8080",
                             SANDBOX_PROXY_PREFLIGHT="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.argv()
        script = argv[argv.index("llm-sandbox") + 3]
        env = dict(self.env)
        for i, arg in enumerate(argv):
            if arg == "-e":
                key, value = argv[i + 1].split("=", 1)
                env[key] = value
        curl = self.home / "stub/curl"
        curl.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "print(os.environ['CONNECT_CODE'] if '%{http_connect}' in sys.argv else '405', end='')\n"
        )
        curl.chmod(0o755)
        # Ownership probes stay in this test's temporary directory.
        script = script.replace('own_check /workspace ', f'own_check "{self.home}" ')
        for code, expected in [("403", 0), ("000", 1), ("200", 1), ("502", 1)]:
            with self.subTest(code=code):
                result = subprocess.run(["zsh", "-c", script],
                                        env={**env, "CONNECT_CODE": code},
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertIn("ADVISORY", result.stdout)
