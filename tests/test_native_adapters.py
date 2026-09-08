"""Optional native CLI checks against a local fake API, without real credentials.

Run: SANDBOX_NATIVE_TESTS=1 python3 -B -m unittest discover -s tests -p test_native_adapters.py -v
"""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('backend_config', REPO / 'backend-config.py')
backends = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backends)


@unittest.skipUnless(os.environ.get('SANDBOX_NATIVE_TESTS') == '1', 'opt-in native CLI checks')
class NativeAdapterTests(unittest.TestCase):
    def test_pi_and_omp_send_requests_to_the_selected_backend(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path, self.headers.get('Authorization'), body.get('model'), body.get('messages', [])))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                for delta, reason in [({'role': 'assistant', 'content': 'local adapter test passed'}, None), ({}, 'stop')]:
                    chunk = {'id': 'test', 'object': 'chat.completion.chunk',
                             'choices': [{'index': 0, 'delta': delta, 'finish_reason': reason}]}
                    self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')

        with HTTPServer(('127.0.0.1', 0), Handler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                for harness in ['pi', 'omp']:
                    with self.subTest(harness=harness), tempfile.TemporaryDirectory() as tmp:
                        self.assertIsNotNone(shutil.which(harness), f'{harness} must be installed')
                        requests.clear()
                        env = {'PATH': os.environ['PATH'], 'HOME': tmp, 'PI_CODING_AGENT_DIR': tmp + '/agent',
                               'PI_OFFLINE': '1', 'PI_TELEMETRY': '0'}
                        project = Path(tmp) / 'project'
                        project.mkdir()
                        for index, model in enumerate(['deepseek-v4-pro', 'deepseek-v4-flash']):
                            profile = {**backends.DEFAULTS['deepseek'], 'model': model,
                                       'base_url': f'http://127.0.0.1:{server.server_port}'}
                            with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'local-test-key'}):
                                records = backends.plan(harness, 'test', profile)
                            args = []
                            for kind, value in records:
                                if kind in ['env', 'secret']:
                                    key, value = value.split('=', 1)
                                    env[key] = value
                                elif kind == 'arg':
                                    args.append(value.replace('/opt/sandbox/backend-provider.mjs',
                                                              str(REPO / 'backend-provider.mjs')))
                            args += ['--no-tools', '--print', 'Say hello.' if index == 0 else 'Continue our conversation.']
                            if index:
                                args += ['--continue']
                            if harness == 'omp':
                                args += ['--no-lsp', '--no-extensions']
                            result = subprocess.run([harness, *args], env=env, cwd=project,
                                                    capture_output=True, text=True, timeout=35)
                            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                            self.assertIn('local adapter test passed', result.stdout)
                            self.assertEqual(requests[-1][:3], ('/chat/completions', 'Bearer local-test-key', model))
                        self.assertEqual(len(requests), 2)
                        # The second model receives the first model's assistant
                        # reply, proving that this resumed an existing conversation.
                        self.assertTrue(any(message.get('role') == 'assistant'
                                            and 'local adapter test passed' in str(message.get('content'))
                                            for message in requests[-1][3]))
            finally:
                server.shutdown()
                worker.join(timeout=2)
