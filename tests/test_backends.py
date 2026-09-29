"""Check backend adapters, session sharing, and credential handling."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_runner import RunnerFixture, REPO

spec = importlib.util.spec_from_file_location('backends', REPO / 'backend-config.py')
backends = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backends)


class ProfileTests(unittest.TestCase):
    def test_unknown_backend_and_invalid_config_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'backends.json'
            with self.assertRaisesRegex(ValueError, 'unknown backend'):
                backends.load_profile(path, 'pi', 'missing', '')
            path.write_text('{"profiles": []}')
            with self.assertRaises(ValueError):
                backends.load_profile(path, 'pi', 'openai', '')

    def test_override_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'backends.json'
            path.write_text(json.dumps({'defaults': {'claude': 'deepseek'}, 'profiles': {
                'deepseek': {'model': 'global-model', 'harnesses': {'claude': {'model': 'claude-model'}}}
            }}))
            name, profile = backends.load_profile(path, 'claude', '', '')
            self.assertEqual((name, profile['model']), ('deepseek', 'claude-model'))
            _, profile = backends.load_profile(path, 'claude', '', 'cli-model')
            self.assertEqual(profile['model'], 'cli-model')

    def test_credentials_are_not_read_in_dry_run(self):
        with patch.object(Path, 'read_text', side_effect=AssertionError('read secret')):
            self.assertEqual(backends.credential({'key_file': '/secret'}, True), 'DRY_RUN_KEY_NOT_LOADED')

    def test_unsupported_combinations_fail(self):
        for harness, profile in [
            ('pi', {'provider': 'anthropic', 'auth': 'login', 'model': 'opus'}),
            ('claude', {'provider': 'openai', 'auth': 'login', 'model': 'gpt-test'}),
            ('claude', {'provider': 'openai', 'auth': 'api_key', 'model': 'gpt-test'}),
            ('codex', {'provider': 'deepseek', 'auth': 'login', 'model': 'deepseek-v4-pro'}),
            ('codex', {'provider': 'openai', 'auth': 'login', 'base_url': 'https://example.org'}),
        ]:
            with self.subTest(harness=harness, profile=profile), self.assertRaises(ValueError):
                backends.plan(harness, 'test', profile, True)

    def test_gpt_gateway_is_explicit(self):
        profile = {'provider': 'openai', 'auth': 'api_key', 'model': 'gateway-gpt',
                   'anthropic_base_url': 'http://127.0.0.1:4000'}
        records = backends.plan('claude', 'gateway', profile, True)
        self.assertIn(('env', 'ANTHROPIC_BASE_URL=http://127.0.0.1:4000'), records)
        self.assertIn(('secret', 'ANTHROPIC_AUTH_TOKEN=DRY_RUN_KEY_NOT_LOADED'), records)

    def test_anthropic_profiles_preserve_native_alias_resolution(self):
        for auth in ('login', 'api_key'):
            for model in ('opus', 'sonnet', 'haiku', 'claude-opus-4-6'):
                with self.subTest(auth=auth, model=model):
                    profile = {**backends.DEFAULTS['claude'], 'auth': auth, 'model': model}
                    records = backends.plan('claude', 'claude', profile, True)
                    self.assertEqual([value for kind, value in records if kind == 'arg'],
                                     ['--model', model])
                    self.assertIn(('env', 'CLAUDE_CODE_SUBAGENT_MODEL=haiku'), records)
                    self.assertFalse(any(value.startswith('ANTHROPIC_DEFAULT_')
                                         for kind, value in records if kind == 'env'))

    def test_third_party_claude_profiles_remap_all_model_families(self):
        for profile in (backends.DEFAULTS['deepseek'],
                        {'provider': 'openai', 'auth': 'api_key', 'model': 'gateway-main',
                         'fast_model': 'gateway-fast', 'anthropic_base_url': 'https://example.org'}):
            with self.subTest(provider=profile['provider']):
                records = backends.plan('claude', 'gateway', profile, True)
                for family in ('OPUS', 'SONNET', 'HAIKU'):
                    model = profile['fast_model' if family == 'HAIKU' else 'model']
                    self.assertIn(('env', f'ANTHROPIC_DEFAULT_{family}_MODEL={model}'), records)

    def test_gpt_profiles_need_a_model_even_on_native_harnesses(self):
        for harness in ['codex', 'omp', 'opencode']:
            with self.subTest(harness=harness), self.assertRaisesRegex(ValueError, 'set a model'):
                backends.plan(harness, 'chatgpt', backends.DEFAULTS['chatgpt'], True)

    def test_pi_keeps_its_own_model_selection_without_an_override(self):
        for name, profile in [('chatgpt', {**backends.DEFAULTS['chatgpt'], 'model': 'gpt-5.6-sol'}),
                              ('openai', {**backends.DEFAULTS['openai'], 'model': 'gpt-5.6-sol'})]:
            with self.subTest(backend=name):
                args = lambda records: [value for kind, value in records if kind == 'arg']
                provider = 'openai-codex' if name == 'chatgpt' else 'openai'
                records = backends.plan('pi', name, profile, True)
                self.assertEqual(args(records), ['--provider', provider])
                explicit = backends.plan('pi', name, profile, True, model_explicit=True)
                self.assertEqual(args(explicit), ['--provider', provider, '--model', 'gpt-5.6-sol'])

    def test_pi_custom_api_backends_still_require_a_model(self):
        with self.assertRaisesRegex(ValueError, 'set a model'):
            backends.plan('pi', 'deepseek', {**backends.DEFAULTS['deepseek'], 'model': ''}, True)

    def test_login_and_api_key_profiles_stay_distinct(self):
        for harness in ['codex', 'pi', 'omp', 'opencode']:
            with self.subTest(harness=harness):
                login = {**backends.DEFAULTS['chatgpt'], 'model': 'gpt-test'}
                api = {**backends.DEFAULTS['openai'], 'model': 'gpt-test'}
                login_records = backends.plan(harness, 'chatgpt', login, True)
                api_records = backends.plan(harness, 'openai', api, True)
                self.assertFalse(any(kind == 'secret' for kind, _ in login_records))
                self.assertTrue(any(kind == 'secret' for kind, _ in api_records))
                self.assertNotEqual(login_records, api_records)

    def test_omp_uses_model_selector_for_extension_providers(self):
        records = backends.plan('omp', 'deepseek', backends.DEFAULTS['deepseek'], True)
        args = [value for kind, value in records if kind == 'arg']
        self.assertNotIn('--provider', args)
        self.assertIn('sandbox_backend/deepseek-v4-pro', args)

    def test_legacy_import_never_overwrites_sessions_or_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / 'deepseek-claude/projects/-workspace'
            shared = root / 'claude/projects/-workspace'
            old.mkdir(parents=True)
            shared.mkdir(parents=True)
            (old / 'new.jsonl').write_text('legacy session')
            (old / 'same.jsonl').write_text('old branch')
            (shared / 'same.jsonl').write_text('shared branch')
            (root / 'deepseek-claude/.credentials.json').write_text('private')
            backends.migrate_claude_sessions(root)
            self.assertEqual((shared / 'new.jsonl').read_text(), 'legacy session')
            self.assertEqual((shared / 'same.jsonl').read_text(), 'shared branch')
            self.assertTrue((old / 'new.jsonl').exists())
            self.assertFalse((root / 'claude/.credentials.json').exists())
            (shared / 'new.jsonl').write_text('resumed')
            backends.migrate_claude_sessions(root)
            self.assertEqual((shared / 'new.jsonl').read_text(), 'resumed')


class BackendRunnerTests(RunnerFixture):
    # Reuse the isolated runner fixture, without duplicating its inherited tests.
    def test_each_harness_keeps_its_mounts_when_backend_changes(self):
        for harness in ['claude', 'codex', 'pi', 'omp', 'opencode']:
            with self.subTest(harness=harness):
                self.assertEqual(self.launch(harness).returncode, 0)
                mounts = self.values('--mount')
                result = self.launch(harness, '--backend', 'deepseek', '--resume', 'session-id')
                self.assertEqual(result.returncode, 0, result.stderr)
                changed = self.argv()
                self.assertEqual(changed[-2:], ['--resume', 'session-id'])
                for mount in mounts:
                    self.assertIn(mount, changed)
                self.assertFalse(any('/deepseek-claude:' in arg for arg in changed))

    def test_alias_and_backend_have_identical_arguments(self):
        first = self.launch('deepseek-claude', '--resume', 'session-id')
        self.assertEqual(first.returncode, 0, first.stderr)
        alias = self.argv()
        second = self.launch('claude', '--backend', 'deepseek', '--resume', 'session-id')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(alias, self.argv())

    def test_key_does_not_appear_in_arguments_or_dry_run(self):
        result = self.launch('claude', '--backend', 'deepseek')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ANTHROPIC_AUTH_TOKEN', self.argv())
        self.assertNotIn('test-key', json.dumps(self.argv()))
        dry = self.launch('claude', '--backend', 'deepseek', SANDBOX_DRY_RUN='1')
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertNotIn('test-key', dry.stdout + dry.stderr)
        self.assertNotIn('DRY_RUN_KEY_NOT_LOADED', dry.stdout + dry.stderr)

    def test_installed_alias_uses_shared_sessions(self):
        result = subprocess.run([str(self.home / '.local/bin/,deepseek-claude-code.sh'), '--resume'],
                                env=self.env, cwd=REPO, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        root = self.home / '.config/llm-sandbox'
        self.assertIn(f'{root}/claude:/home/fedora/.claude', self.values('--mount'))
        self.assertFalse((root / 'deepseek-claude').exists())

    def test_model_override_and_invalid_backend(self):
        result = self.launch('claude', '--backend=deepseek', '--model', 'deepseek-v4-flash', '--resume')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ANTHROPIC_MODEL=deepseek-v4-flash', self.argv())
        self.capture.unlink()
        self.assertNotEqual(self.launch('pi', '--backend', 'missing').returncode, 0)
        self.assertFalse(self.capture.exists())

    def test_first_install_selects_subscription_models_without_openai_key(self):
        path = self.home / '.config/llm-sandbox/backends.json'
        path.unlink()
        subprocess.run(['sh', 'install.sh'], cwd=REPO, env=self.env, check=True, capture_output=True)
        self.assertTrue(path.exists())
        for harness in ('codex', 'pi', 'omp', 'opencode'):
            with self.subTest(harness=harness):
                result = self.launch(harness)
                self.assertEqual(result.returncode, 0, result.stderr)
                args = self.argv()
                self.assertFalse(any('API_KEY' in arg for arg in args))
                if harness == 'pi':
                    # Pi keeps its own model state; only the provider is selected.
                    self.assertIn('--provider', args)
                    self.assertNotIn('--model', args)
                    continue
                expected_model = 'gpt-6-astra'
                self.assertTrue(any(expected_model in arg for arg in args))
                if harness in ('omp', 'opencode'):
                    self.assertTrue(any('gpt-5.6-luna' in arg for arg in args))
        self.assertEqual(self.launch('claude').returncode, 0)
        self.assertIn('opus', self.argv())
        self.assertFalse(any(arg.startswith('ANTHROPIC_DEFAULT_') for arg in self.argv()))

    def test_pi_forwards_only_an_explicit_model(self):
        self.assertEqual(self.launch('pi', '--backend', 'chatgpt').returncode, 0)
        args = self.argv()
        self.assertIn('--provider', args)
        self.assertNotIn('--model', args)
        self.assertEqual(self.launch('pi', '--backend', 'chatgpt', '--model', 'gpt-5.6-luna').returncode, 0)
        args = self.argv()
        self.assertIn('--provider', args)
        self.assertIn('--model', args)
        self.assertIn('gpt-5.6-luna', args)

    def test_reinstall_keeps_user_profiles(self):
        path = self.home / '.config/llm-sandbox/backends.json'
        value = '{"defaults":{"pi":"deepseek"}}'
        path.write_text(value)
        subprocess.run(['sh', 'install.sh'], cwd=REPO, env=self.env, check=True, capture_output=True)
        self.assertEqual(path.read_text(), value)
        self.assertEqual(self.launch('pi').returncode, 0)
        self.assertIn('deepseek-v4-pro', self.argv())

    def test_retired_aider_default_does_not_block_launches(self):
        path = self.home / '.config/llm-sandbox/backends.json'
        path.write_text('{"defaults":{"aider":"deepseek","pi":"deepseek"}}')
        self.assertEqual(self.launch('pi').returncode, 0)
        self.assertIn('deepseek-v4-pro', self.argv())
        self.assertNotEqual(self.launch('aider').returncode, 0)

    def test_reinstall_removes_aider_entry_point(self):
        stale = self.home / '.local/bin/,aider-sandbox.sh'
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text('#!/bin/sh\n')
        subprocess.run(['sh', 'install.sh'], cwd=REPO, env=self.env, check=True, capture_output=True)
        self.assertFalse(stale.exists())

    def test_missing_secret_fails_before_creating_mounts(self):
        (self.home / '.config/deepseek.api').unlink()
        result = self.launch('codex', '--backend', 'deepseek', DEEPSEEK_API_KEY='')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.capture.exists())
        self.assertFalse((self.home / '.config/llm-sandbox/codex').exists())
