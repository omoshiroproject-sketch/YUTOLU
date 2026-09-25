#!/usr/bin/env python3
"""Conservative pre-push checks. Findings never include matched secret values."""

import argparse
import fnmatch
from pathlib import Path
import re
import subprocess


MAX_BYTES = 10 * 1024 * 1024
SAFE_ENV = {'.env.example', '.env.sample', '.env.template'}
SECRET_PATTERNS = (
    rb'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----',
    rb'\bgh[pousr]_[A-Za-z0-9_]{30,}\b',
    rb'\bgithub_pat_[A-Za-z0-9_]{40,}\b',
    rb'\bAKIA[0-9A-Z]{16}\b',
    rb'\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b',
    rb'\bxox[baprs]-[A-Za-z0-9-]{20,}\b',
    rb'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b',
)


class GuardError(Exception):
    pass


def git(repo, *args, env=None, data=None):
    try:
        result = subprocess.run(
            ['git', '-C', str(repo), *args], input=data, capture_output=True,
            timeout=45, env=env,
        )
    except subprocess.TimeoutExpired:
        raise GuardError('Git operation timed out; no force push was attempted.')
    if result.returncode:
        # Do not echo stderr: it may contain credential-bearing URLs or file data.
        raise GuardError('Git operation failed: ' + args[0])
    return result.stdout


def check_bytes(path, data, mode='100644'):
    name = Path(path).name.lower()
    parts = Path(path).parts
    denied = (
        (name == '.env' or name.startswith('.env.')) and name not in SAFE_ENV
    ) or any(fnmatch.fnmatch(name, pattern) for pattern in (
        '*.pem', '*.key', '*.p12', '*.pfx', '*.p8', '*.jks', '*.keystore',
        '*.sqlite', '*.sqlite3', '*.db', '*.dump', '*.zip', '*.tar', '*.tar.gz',
        'credentials.json', 'service-account*.json', 'serviceaccount*.json',
        '*-firebase-adminsdk-*.json', 'id_rsa', 'id_ed25519', '.netrc', '.npmrc',
    )) or any(part in {'.ssh', '.aws', '.config', 'node_modules'} for part in parts)
    if denied:
        raise GuardError('Excluded or sensitive file: ' + path)
    if mode not in {'100644', '100755'}:
        raise GuardError('Symlink/submodule requires manual review: ' + path)
    if len(data) > MAX_BYTES:
        raise GuardError('File exceeds 10 MiB; manual review required: ' + path)
    if any(re.search(pattern, data) for pattern in SECRET_PATTERNS):
        raise GuardError('Possible credential detected; value omitted: ' + path)
    if re.search(rb'/Users/[A-Za-z0-9._-]+/', data):
        raise GuardError('Local home-directory path requires review: ' + path)


def worktree_files(repo, env=None):
    listing = git(repo, 'ls-files', '-z', '--cached', '--others', '--exclude-standard', env=env)
    return sorted(set(p.decode('utf-8') for p in listing.split(b'\0') if p))


def check_worktree(repo, env=None):
    paths = worktree_files(repo, env)
    for path in paths:
        full = Path(repo) / path
        if full.is_symlink():
            raise GuardError('Symlink requires manual review: ' + path)
        if not full.exists():
            continue  # Tracked deletion.
        if not full.is_file():
            raise GuardError('Non-file requires manual review: ' + path)
        if full.stat().st_size > MAX_BYTES:
            raise GuardError('File exceeds 10 MiB; manual review required: ' + path)
        check_bytes(path, full.read_bytes())
    return len(paths)


def check_tree(repo, revision='HEAD', env=None):
    listing = git(repo, 'ls-tree', '-r', '-z', revision, env=env)
    count = 0
    for item in listing.split(b'\0'):
        if not item:
            continue
        header, raw_path = item.split(b'\t', 1)
        mode, kind, oid = header.decode().split()
        path = raw_path.decode('utf-8')
        if kind != 'blob':
            raise GuardError('Submodule requires manual review: ' + path)
        size = int(git(repo, 'cat-file', '-s', oid, env=env))
        if size > MAX_BYTES:
            raise GuardError('File exceeds 10 MiB; manual review required: ' + path)
        check_bytes(path, git(repo, 'cat-file', 'blob', oid, env=env), mode)
        count += 1
    return count


def check_history(repo, base, head='HEAD', env=None):
    commits = git(repo, 'rev-list', '--reverse', base + '..' + head, env=env).decode().split()
    for commit in commits:
        check_tree(repo, commit, env)
    return len(commits)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worktree', action='store_true')
    parser.add_argument('--rev', default='HEAD')
    parser.add_argument('--base', help='Also inspect every outgoing commit since this ref')
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    try:
        count = check_worktree(repo) if args.worktree else check_tree(repo, args.rev)
        if args.base:
            check_history(repo, args.base, args.rev)
        print('Repository checks passed: %d files. This is not a complete secret/PII audit.' % count)
    except (GuardError, UnicodeError, OSError) as error:
        print(str(error) if isinstance(error, GuardError) else 'Unable to safely inspect a file.')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
