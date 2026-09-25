#!/usr/bin/env python3
"""Prepare or install this checkout's per-user macOS launch agent."""

import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys

from autopush import AutoPush, LABEL
from repository_guard import GuardError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true', help='Install and start after Git authentication succeeds')
    parser.add_argument('--stop', action='store_true', help='Pause and unload; leave the installation file for inspection')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        print('The background service installer is for macOS only.')
        return 1
    app = AutoPush(Path(__file__).resolve().parents[1])
    target = Path.home() / 'Library' / 'LaunchAgents' / (LABEL + '.plist')
    service = 'gui/%d/%s' % (os.getuid(), LABEL)
    if args.stop:
        (app.state_dir / 'paused').touch(mode=0o600)
        subprocess.run(['/bin/launchctl', 'bootout', service], capture_output=True)
        print('Autosave paused and unloaded.')
        return 0
    content = {
        'Label': LABEL,
        'ProgramArguments': ['/usr/bin/python3', str(app.repo / 'tools' / 'autopush.py')],
        'WorkingDirectory': str(app.repo),
        'StartInterval': 60,
        'RunAtLoad': True,
        'ProcessType': 'Background',
        'LowPriorityIO': True,
        'EnvironmentVariables': {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'},
        'StandardOutPath': str(app.state_dir / 'stdout.log'),
        'StandardErrorPath': str(app.state_dir / 'stderr.log'),
    }
    prepared = app.state_dir / (LABEL + '.plist')
    prepared.write_bytes(plistlib.dumps(content))
    if not args.install:
        print('Prepared launch agent; not installed or running.')
        return 0
    app.validate()
    app.g('push', '--dry-run', 'origin', 'HEAD:refs/heads/main')
    if target.exists():
        current = plistlib.loads(target.read_bytes())
        if current.get('Label') != LABEL or current.get('ProgramArguments') != content['ProgramArguments']:
            raise GuardError('An installation for another checkout already exists; left unchanged.')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(plistlib.dumps(content))
    target.chmod(0o600)
    subprocess.run(['/bin/launchctl', 'bootout', service], capture_output=True)
    (app.state_dir / 'paused').unlink(missing_ok=True)
    result = subprocess.run(['/bin/launchctl', 'bootstrap', 'gui/%d' % os.getuid(), str(target)], capture_output=True)
    if result.returncode:
        raise GuardError('Launch agent prepared, but macOS did not start it.')
    print('Installed: checks main every 60 seconds while this Mac is awake and you are logged in.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (GuardError, OSError) as error:
        print(str(error) if isinstance(error, GuardError) else 'Could not write or start the launch agent.')
        raise SystemExit(1)
