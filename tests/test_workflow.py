"""Workflow is JSON-form YAML so structure checks need only stdlib."""
import json
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]

class WorkflowTests(unittest.TestCase):
    def test_workflow_security_and_partial_failure_structure(self):
        path = ROOT / '.github/workflows/aur.yml'
        self.assertTrue(path.exists(), 'workflow missing')
        workflow = json.loads(path.read_text())
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        self.assertEqual(len(workflow['on']['schedule']), 1)
        self.assertFalse(workflow['concurrency']['cancel-in-progress'])
        jobs = workflow['jobs']
        self.assertEqual(set(jobs), {'discover', 'build', 'publish'})
        self.assertIn('has_changes', jobs['build']['if'])
        self.assertFalse(jobs['build']['strategy']['fail-fast'])
        self.assertIn('always()', jobs['publish']['if'])
        self.assertIn("cancelled()", jobs['publish']['if'])
        self.assertIn('has_changes', jobs['publish']['if'])
        for name, job in jobs.items():
            if name != 'publish':
                self.assertNotIn('secrets.', json.dumps(job))
            for step in job['steps']:
                if 'uses' in step:
                    self.assertRegex(step['uses'], r'^actions/[a-z-]+@[0-9a-f]{40}$')
                if step.get('uses', '').startswith('actions/checkout@'):
                    self.assertFalse(step['with']['persist-credentials'])
        download = next(step for step in jobs['publish']['steps'] if step.get('uses', '').startswith('actions/download-artifact@'))
        self.assertTrue(download['with']['merge-multiple'])
        publish = json.dumps(jobs['publish'])
        for value in ('AUR_UPLOAD_KEY', 'AUR_UPLOAD_HOST', 'AUR_UPLOAD_PORT', 'AUR_UPLOAD_USER', 'AUR_SSH_KNOWN_HOSTS', '--require-all', 'verify'):
            self.assertIn(value, publish)

    def test_build_pins_commit_and_mounts_recipe_only(self):
        path = ROOT / 'scripts/build.sh'
        self.assertTrue(path.exists(), 'build script missing')
        text = path.read_text()
        self.assertRegex(text, r'git [^\n]*archive --format=tar "\$commit"')
        self.assertIn('"$commit"', text)
        self.assertIn('--no-checkout', text)
        self.assertIn('makepkg --syncdeps', text)
        self.assertIn('runuser -u builder', text)
        self.assertIn('COPILOT_AUTO_UPDATE=false', text)
        self.assertIn('TMPDIR=/build/.tmp', text)
        self.assertEqual(text.count('--mount'), 1)
        self.assertNotIn('/var/run/docker.sock', text)
        self.assertNotIn('GITHUB_TOKEN', text)
        subprocess.run(['bash', '-n', str(path)], check=True)

    def test_upload_host_key_enforcement(self):
        path = ROOT / 'scripts/upload.sh'
        self.assertTrue(path.exists(), 'upload script missing')
        text = path.read_text()
        for value in ('StrictHostKeyChecking=yes', 'IdentitiesOnly=yes', 'ssh-keygen -F', 'trap ', 'umask 077', '-F /dev/null'):
            self.assertIn(value, text)
        self.assertNotIn('ssh-keyscan', text)
        subprocess.run(['bash', '-n', str(path)], check=True)

    def test_upload_stream_and_cleanup_without_remote_ssh(self):
        import os
        import sys
        import tempfile
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            bindir = root / 'bin'
            bindir.mkdir()
            fake = bindir / 'ssh'
            fake.write_text('#!' + sys.executable + '\n' +
                            'import json,os,sys\n' +
                            'from pathlib import Path\n' +
                            'Path(os.environ["SSH_CAPTURE"]).write_text(json.dumps({"args":sys.argv[1:], "data":sys.stdin.read()}))\n')
            fake.chmod(0o755)
            key = root / 'host'
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
            known = '[2.nb.quq.me]:2222 ' + (root / 'host.pub').read_text()
            archive = root / 'transfer.tar'
            archive.write_text('test stream, not an actual remote upload')
            capture = root / 'capture.json'
            env = dict(os.environ, PATH=str(bindir) + ':' + os.environ['PATH'], RUNNER_TEMP=str(root),
                       SSH_CAPTURE=str(capture), AUR_UPLOAD_KEY='test-only-key',
                       AUR_UPLOAD_HOST='2.nb.quq.me', AUR_UPLOAD_USER='aurrepo',
                       AUR_UPLOAD_PORT='2222', AUR_SSH_KNOWN_HOSTS=known)
            result = subprocess.run(['bash', str(ROOT / 'scripts/upload.sh'), str(archive)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            received = json.loads(capture.read_text())
            self.assertEqual(received['data'], archive.read_text())
            self.assertIn('StrictHostKeyChecking=yes', received['args'])
            self.assertEqual(received['args'][-1], 'aurrepo@2.nb.quq.me')
            self.assertEqual(list(root.glob('aur-upload.*')), [])
            capture.unlink()
            env['AUR_SSH_KNOWN_HOSTS'] = known.replace('[2.nb.quq.me]:2222', '[wrong.example]:2222')
            result = subprocess.run(['bash', str(ROOT / 'scripts/upload.sh'), str(archive)], env=env, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(capture.exists(), 'must not call SSH when the host pin is missing')
            self.assertEqual(list(root.glob('aur-upload.*')), [])

if __name__ == '__main__':
    unittest.main()
