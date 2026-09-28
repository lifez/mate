"""Explicit setup uses disposable homes and refuses overwrite/retargeting."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

SCRIPT = Path(__file__).resolve().parents[1] / 'bin/mate-remote-setup.py'


class RemoteSetupTests(unittest.TestCase):
    def test_exclusive_home_and_route(self):
        with tempfile.TemporaryDirectory(prefix='mate-setup-test-') as folder:
            root = Path(folder)
            env = dict(os.environ, MATE_HOME=str(root / 'primary'))
            primary = str(uuid.uuid4())
            def run(*args):
                return subprocess.run([sys.executable, str(SCRIPT), *args], env=env,
                                      capture_output=True, text=True, timeout=10)
            args = ('init-home', str(root / 'remote'), '--primary', primary,
                    '--provider', 'fixture', '--model', 'fixture', '--effort', 'off')
            created = run(*args)
            self.assertEqual(created.returncode, 0, created.stderr)
            config = json.loads(created.stdout)['config']
            binding = root / 'remote/remote.json'
            original = binding.read_bytes()
            self.assertEqual(binding.stat().st_mode & 0o777, 0o600)
            self.assertNotEqual(run(*args).returncode, 0)
            self.assertEqual(binding.read_bytes(), original)
            route_args = ('init-route', 'test', '--host', 'fixture', '--home', config['home'], '--primary', primary)
            created = run(*route_args)
            self.assertEqual(created.returncode, 0, created.stderr)
            route = Path(json.loads(created.stdout)['route'])
            original = route.read_bytes()
            self.assertEqual(route.stat().st_mode & 0o777, 0o600)
            self.assertNotEqual(run(*route_args).returncode, 0)
            self.assertEqual(route.read_bytes(), original)
            # The receiver must accept what setup writes, for either supervisor harness.
            claude = run('init-home', str(root / 'claude'), '--primary', primary, '--harness', 'claude', '--model', 'claude-test')
            self.assertEqual(claude.returncode, 0, claude.stderr)
            for name in ('remote', 'claude'):
                check = subprocess.run([sys.executable, '-c', 'import sys; sys.path.insert(0, sys.argv[1]); import mate_remote_transport as t; '
                                        't.settings(sys.argv[2], True)', str(SCRIPT.parent), str(root / name / 'remote.json')],
                                       capture_output=True, text=True, timeout=10)
                self.assertEqual(check.returncode, 0, check.stderr)
            supervisor = json.loads((root / 'claude/remote.json').read_text())['supervisor']
            self.assertEqual(supervisor, dict(harness='claude', provider='anthropic', model='claude-test', effort='high'))


if __name__ == '__main__':
    unittest.main()
