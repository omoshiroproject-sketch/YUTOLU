#!/usr/bin/env python3
"""One safe autosave cycle; macOS launchd invokes this every 60 seconds."""

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from repository_guard import GuardError, check_history, check_tree, check_worktree, git, worktree_files


REMOTE = 'https://github.com/omoshiroproject-sketch/YUTOLU.git'
LABEL = 'jp.yutolu.autopush'


class AutoPush:
    def __init__(self, repo, expected_remote=REMOTE, settle_seconds=30):
        self.repo = Path(repo).resolve()
        self.expected_remote = expected_remote
        self.settle_seconds = settle_seconds
        self.env = os.environ.copy()
        for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR'):
            self.env.pop(key, None)
        self.env.update(GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0', GCM_INTERACTIVE='never')
        root = Path(self.g('rev-parse', '--show-toplevel').decode().strip()).resolve()
        if root != self.repo:
            raise GuardError('Unexpected repository root.')
        self.gitdir = Path(self.g('rev-parse', '--absolute-git-dir').decode().strip())
        self.state_dir = self.gitdir / 'yutolu-autopush'
        self.state_dir.mkdir(mode=0o700, exist_ok=True)

    def g(self, *args, data=None, env=None):
        return git(self.repo, *args, data=data, env=env or self.env)

    def is_quiet(self, *args):
        result = subprocess.run(['git', '-C', str(self.repo), *args], env=self.env,
                                capture_output=True, timeout=45)
        if result.returncode not in (0, 1):
            raise GuardError('Unable to inspect repository state.')
        return result.returncode == 0

    def record(self, status, message, commit=None):
        previous = {}
        state_path = self.state_dir / 'status.json'
        if state_path.exists():
            try:
                previous = json.loads(state_path.read_text())
            except (ValueError, OSError):
                pass
        timestamp = datetime.now(timezone.utc).isoformat()
        current = dict(previous, status=status, message=message, checked_at=timestamp)
        if commit:
            current.update(last_uploaded_commit=commit, last_success_at=timestamp)
        temporary = self.state_dir / 'status.tmp'
        temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(state_path)
        print(status + ': ' + message)
        return status

    def validate(self):
        if self.g('symbolic-ref', '--short', 'HEAD').decode().strip() != 'main':
            raise GuardError('Paused: checkout is not on main.')
        for operation in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'index.lock'):
            if (self.gitdir / operation).exists():
                raise GuardError('Paused: another Git operation is in progress.')
        for args in (('remote', 'get-url', '--all', 'origin'), ('remote', 'get-url', '--push', '--all', 'origin')):
            if self.g(*args).decode().splitlines() != [self.expected_remote]:
                raise GuardError('Paused: origin differs from the approved repository.')
        if not self.is_quiet('diff', '--cached', '--quiet'):
            raise GuardError('Paused: manually staged changes must be committed or unstaged first.')
        self.g('config', '--get', 'user.name')
        self.g('config', '--get', 'user.email')

    def commit_snapshot(self, head):
        # Use a separate index; keep manually staged changes intact. Holding the
        # real index lock prevents another Git writer from racing the snapshot.
        lock_path = self.gitdir / 'index.lock'
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise GuardError('Paused: another Git operation is in progress.')
        os.close(lock_fd)
        try:
            if not self.is_quiet('diff', '--cached', '--quiet'):
                raise GuardError('Paused: index changed during the autosave check.')
            if self.g('rev-parse', 'HEAD').decode().strip() != head:
                raise GuardError('Paused: HEAD changed during the autosave check.')
            with tempfile.TemporaryDirectory(prefix='snapshot-', dir=self.state_dir) as temp_dir:
                index = Path(temp_dir) / 'index'
                env = dict(self.env, GIT_INDEX_FILE=str(index))
                self.g('read-tree', head, env=env)
                self.g('add', '--all', '--', '.', env=env)
                tree = self.g('write-tree', env=env).decode().strip()
                check_tree(self.repo, tree, env)
                if tree == self.g('rev-parse', head + '^{tree}').decode().strip():
                    return head
                message = 'chore: autosave ' + datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC') + '\n'
                commit = self.g('commit-tree', tree, '-p', head, data=message.encode()).decode().strip()
                self.g('update-ref', '-m', 'YUTOLU autosave', 'refs/heads/main', commit, head)
                os.replace(index, self.gitdir / 'index')
                return commit
        finally:
            lock_path.unlink()

    def cycle(self):
        with (self.state_dir / 'cycle.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 'busy'
            try:
                return self._cycle()
            except (GuardError, OSError, UnicodeError, subprocess.TimeoutExpired) as error:
                message = str(error) if isinstance(error, GuardError) else 'Local I/O or timeout error; retry after checking the repository.'
                return self.record('blocked', message)

    def _cycle(self):
        if (self.state_dir / 'paused').exists():
            return self.record('paused', 'Autosave is paused by the user.')
        self.validate()
        self.g('fetch', '--quiet', 'origin', 'refs/heads/main:refs/remotes/origin/main')
        head = self.g('rev-parse', 'HEAD').decode().strip()
        remote_head = self.g('rev-parse', 'origin/main').decode().strip()
        if self.g('merge-base', head, remote_head).decode().strip() != remote_head:
            raise GuardError('Paused: GitHub has changes not in this checkout. Reconcile them manually; no force push was attempted.')
        dirty = bool(self.g('status', '--porcelain=v1', '--untracked-files=all'))
        if dirty:
            if self.settle_seconds:
                for path in worktree_files(self.repo, self.env):
                    full = self.repo / path
                    if full.exists() and time.time() - full.lstat().st_mtime < self.settle_seconds:
                        return self.record('waiting', 'Waiting for files to remain unchanged for 30 seconds.')
            check_worktree(self.repo, self.env)
            # Inspect local history too; a deleted secret must not be pushed in an earlier commit.
            check_history(self.repo, remote_head, head, self.env)
            head = self.commit_snapshot(head)
        if head == remote_head:
            return self.record('up_to_date', 'No changes to upload.')
        check_history(self.repo, remote_head, head, self.env)
        self.g('push', '--porcelain', 'origin', head + ':refs/heads/main')
        return self.record('uploaded', 'Changes saved to GitHub.', commit=head)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--now', action='store_true', help='Run once without the editing grace period')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--status', action='store_true')
    group.add_argument('--pause', action='store_true')
    group.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    try:
        app = AutoPush(Path(__file__).resolve().parents[1], settle_seconds=0 if args.now else 30)
        if args.status:
            state = app.state_dir / 'status.json'
            print(state.read_text() if state.exists() else 'Autosave has not run yet.')
            return 0
        if args.pause:
            (app.state_dir / 'paused').touch(mode=0o600)
            app.record('paused', 'Autosave is paused by the user.')
            return 0
        if args.resume:
            (app.state_dir / 'paused').unlink(missing_ok=True)
        return 1 if app.cycle() == 'blocked' else 0
    except (GuardError, OSError) as error:
        print(str(error) if isinstance(error, GuardError) else 'Unable to access the autosave configuration.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
