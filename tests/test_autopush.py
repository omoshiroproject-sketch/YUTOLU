"""Use disposable local repositories: tests never contact GitHub."""

import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from autopush import AutoPush
from repository_guard import GuardError, check_bytes


def run_git(repo, *args):
    environment = os.environ.copy()
    environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR'):
        environment.pop(key, None)
    return subprocess.run(['git', '-C', str(repo), *args], check=True,
                          capture_output=True, env=environment).stdout.decode().strip()


class AutosaveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='yutolu-autopush-test-')
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.repo = root / 'checkout'
        self.remote = root / 'remote.git'
        self.repo.mkdir()
        self.remote.mkdir()
        run_git(self.remote, 'init', '--bare', '--initial-branch=main')
        run_git(self.repo, 'init', '--initial-branch=main')
        run_git(self.repo, 'config', 'user.name', 'Autosave test')
        run_git(self.repo, 'config', 'user.email', 'test@example.invalid')
        run_git(self.repo, 'config', 'commit.gpgsign', 'false')
        (self.repo / 'README.md').write_text('Example project\n')
        (self.repo / '.gitignore').write_text('.env\n.env.*\n!.env.example\n')
        run_git(self.repo, 'add', '--all')
        run_git(self.repo, 'commit', '-m', 'Initial test fixture')
        run_git(self.repo, 'remote', 'add', 'origin', str(self.remote))
        run_git(self.repo, 'push', '--set-upstream', 'origin', 'main')
        self.app = AutoPush(self.repo, expected_remote=str(self.remote), settle_seconds=0)

    def cycle(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.app.cycle()

    def test_saves_changes_once_and_keeps_index_clean(self):
        (self.repo / 'notes.md').write_text('New note\n')
        self.assertEqual(self.cycle(), 'uploaded')
        head = run_git(self.repo, 'rev-parse', 'HEAD')
        self.assertEqual(head, run_git(self.remote, 'rev-parse', 'main'))
        self.assertEqual(run_git(self.repo, 'status', '--porcelain'), '')
        self.assertEqual(self.cycle(), 'up_to_date')
        self.assertEqual(head, run_git(self.repo, 'rev-parse', 'HEAD'))

    def test_ignored_environment_file_is_not_uploaded(self):
        (self.repo / '.env').write_text('DEMO_PASSWORD=not-a-real-secret\n')
        (self.repo / 'notes.md').write_text('Safe note\n')
        self.assertEqual(self.cycle(), 'uploaded')
        self.assertNotIn('.env', run_git(self.remote, 'ls-tree', '-r', '--name-only', 'main').splitlines())

    def test_preserves_manual_staging(self):
        (self.repo / 'notes.md').write_text('Manually staged\n')
        run_git(self.repo, 'add', 'notes.md')
        index = run_git(self.repo, 'write-tree')
        head = run_git(self.repo, 'rev-parse', 'HEAD')
        self.assertEqual(self.cycle(), 'blocked')
        self.assertEqual(index, run_git(self.repo, 'write-tree'))
        self.assertEqual(head, run_git(self.repo, 'rev-parse', 'HEAD'))

    def test_secret_is_rejected_before_committing_without_echoing_value(self):
        fake_token = 'gh' + 'p_' + 'A' * 36
        (self.repo / 'unsafe.txt').write_text(fake_token)
        before = run_git(self.repo, 'rev-parse', 'HEAD')
        self.assertEqual(self.cycle(), 'blocked')
        self.assertEqual(before, run_git(self.repo, 'rev-parse', 'HEAD'))
        self.assertEqual(run_git(self.repo, 'diff', '--cached', '--name-only'), '')
        self.assertNotIn(fake_token, (self.app.state_dir / 'status.json').read_text())

    def test_removed_secret_in_outgoing_history_is_rejected(self):
        fake_token = 'gh' + 'p_' + 'B' * 36
        (self.repo / 'unsafe.txt').write_text(fake_token)
        run_git(self.repo, 'add', 'unsafe.txt')
        run_git(self.repo, 'commit', '-m', 'Synthetic unsafe fixture')
        run_git(self.repo, 'rm', 'unsafe.txt')
        run_git(self.repo, 'commit', '-m', 'Remove synthetic fixture')
        remote_head = run_git(self.remote, 'rev-parse', 'main')
        self.assertEqual(self.cycle(), 'blocked')
        self.assertEqual(remote_head, run_git(self.remote, 'rev-parse', 'main'))

    def test_remote_ahead_is_not_overwritten(self):
        other = Path(self.temporary.name) / 'other'
        run_git(Path(self.temporary.name), 'clone', str(self.remote), str(other))
        run_git(other, 'config', 'user.name', 'Other test')
        run_git(other, 'config', 'user.email', 'other@example.invalid')
        (other / 'remote.md').write_text('Remote change\n')
        run_git(other, 'add', '--all')
        run_git(other, 'commit', '-m', 'Remote fixture change')
        run_git(other, 'push')
        remote_head = run_git(self.remote, 'rev-parse', 'main')
        (self.repo / 'local.md').write_text('Local change\n')
        self.assertEqual(self.cycle(), 'blocked')
        self.assertEqual(remote_head, run_git(self.remote, 'rev-parse', 'main'))
        self.assertTrue((self.repo / 'local.md').exists())

    def test_other_branch_is_not_uploaded(self):
        run_git(self.repo, 'checkout', '-b', 'feature-test')
        (self.repo / 'notes.md').write_text('Feature branch\n')
        self.assertEqual(self.cycle(), 'blocked')

    def test_failed_push_retains_commit_for_retry(self):
        hook = self.remote / 'hooks' / 'pre-receive'
        hook.write_text('#!/bin/sh\nexit 1\n')
        hook.chmod(0o700)
        (self.repo / 'notes.md').write_text('Retry me\n')
        self.assertEqual(self.cycle(), 'blocked')
        pending = run_git(self.repo, 'rev-parse', 'HEAD')
        self.assertNotEqual(pending, run_git(self.remote, 'rev-parse', 'main'))
        hook.unlink()
        self.assertEqual(self.cycle(), 'uploaded')
        self.assertEqual(pending, run_git(self.remote, 'rev-parse', 'main'))

    def test_user_pause_keeps_changes_local(self):
        (self.app.state_dir / 'paused').touch()
        (self.repo / 'notes.md').write_text('Keep local\n')
        self.assertEqual(self.cycle(), 'paused')
        self.assertEqual(run_git(self.repo, 'rev-parse', 'HEAD'), run_git(self.remote, 'rev-parse', 'main'))

    def test_editing_grace_period(self):
        self.app.settle_seconds = 30
        (self.repo / 'notes.md').write_text('Still editing\n')
        self.assertEqual(self.cycle(), 'waiting')

    def test_symlink_is_not_followed(self):
        target = Path(self.temporary.name) / 'private.txt'
        target.write_text('Outside repository\n')
        (self.repo / 'shortcut').symlink_to(target)
        self.assertEqual(self.cycle(), 'blocked')

    def test_changed_destination_is_blocked(self):
        run_git(self.repo, 'remote', 'set-url', '--push', 'origin', str(self.remote) + '-wrong')
        (self.repo / 'notes.md').write_text('Do not send elsewhere\n')
        self.assertEqual(self.cycle(), 'blocked')


class GuardTests(unittest.TestCase):
    def test_private_key_marker_is_rejected(self):
        value = ('-----BEGIN ' + 'PRIVATE KEY-----').encode()
        with self.assertRaises(GuardError):
            check_bytes('bad.txt', value)

    def test_safe_environment_template_is_allowed(self):
        check_bytes('.env.example', b'PUBLIC_API_URL=\n')


if __name__ == '__main__':
    unittest.main()
