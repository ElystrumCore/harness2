"""Owned Linux namespace workers. No host networking or inherited credentials."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from .gateway import Refused

BRIDGE = Path(__file__).resolve().parent


def run_isolated(command, workspace, *, mounts=(), environment=None, gateway=None, token=None,
                 timeout=300, max_output=2_000_000):
    workspace = Path(workspace).resolve(strict=True)
    if not workspace.is_dir() or workspace == Path('/'):
        raise Refused('invalid workspace')
    argv = ['bwrap', '--unshare-all', '--die-with-parent', '--new-session', '--clearenv',
            '--ro-bind', '/usr', '/usr', '--symlink', 'usr/bin', '/bin',
            '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64',
            '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
            '--dir', '/etc', '--dir', '/home', '--dir', '/home/worker', '--dir', '/control',
            '--bind', str(workspace), '/work', '--dir', '/bridge']
    # The controller module contains hidden task cases. Mount only the two entry scripts.
    for helper in ('sandbox_proxy.py', 'chuck_entry.py'):
        argv += ['--ro-bind', str(BRIDGE / helper), '/bridge/' + helper]
    for path in mounts:
        target = Path(path).absolute()
        path = target.resolve(strict=True)
        if path == Path('/') or path in (Path('/home'), Path('/srv'), Path('/etc')):
            raise Refused('mount too broad')
        argv += ['--ro-bind', str(path), str(target)]
    env = {'PATH': '/usr/bin:/bin', 'HOME': '/home/worker', 'LANG': 'C.UTF-8',
           'TMPDIR': '/tmp', 'XDG_CONFIG_HOME': '/tmp/config', 'XDG_DATA_HOME': '/tmp/data',
           'XDG_STATE_HOME': '/tmp/state', 'XDG_CACHE_HOME': '/tmp/cache',
           'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1'}
    for key, value in (environment or {}).items():
        if key.startswith(('CHUCK_', 'CN_CLI_', 'OPENCODE_')) or key in {
            'PYTHONPATH', 'REDIS_URL', 'CYCLONE_URL', 'QDRANT_URL',
        }:
            env[key] = str(value)
    if gateway is not None:
        if not token:
            raise Refused('worker gateway token missing')
        argv += ['--ro-bind', str(gateway.socket_path), '/control/api.sock']
        env['ANTHROPIC_API_KEY'] = token
    for key, value in env.items():
        argv += ['--setenv', key, value]
    argv += ['--chdir', '/work', '--']
    if gateway is not None:
        argv += ['/usr/bin/python3', '/bridge/sandbox_proxy.py', '--socket', '/control/api.sock', '--']
    argv += [str(value) for value in command]
    started = time.monotonic()
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True, env={
                                   'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                               })
    chunks = [bytearray(), bytearray()]
    flooded = threading.Event()

    def read(pipe, index):
        while data := pipe.read(4096):
            room = max_output - len(chunks[index])
            chunks[index].extend(data[:room])
            if len(data) > room:
                flooded.set()
        pipe.close()

    readers = [threading.Thread(target=read, args=(pipe, index), daemon=True)
               for index, pipe in enumerate((process.stdout, process.stderr))]
    for thread in readers:
        thread.start()
    outcome = 'completed'
    try:
        while process.poll() is None:
            if flooded.is_set() or time.monotonic() - started > timeout:
                outcome = 'output_limit' if flooded.is_set() else 'timeout'
                os.killpg(process.pid, signal.SIGKILL)
                break
            time.sleep(.02)
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        for thread in readers:
            thread.join(timeout=5)
        if gateway is not None:
            gateway.revoke(token)
    if flooded.is_set():
        outcome = 'output_limit'
    if any(thread.is_alive() for thread in readers):
        raise Refused('worker pipe did not quiesce')
    values = [bytes(value).decode(errors='replace') for value in chunks]
    if token:
        values = [value.replace(token, '[attempt credential redacted]') for value in values]
    return {'exit_code': process.returncode, 'outcome': outcome, 'stdout': values[0],
            'stderr': values[1], 'wall_s': time.monotonic() - started}
