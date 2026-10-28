#!/usr/bin/env python3
"""Versioned, hash-verified Codex capacity patch. No App bundle files are edited."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time

PROJECT_NAME = 'Codex-AtCapacity-Retry'
VERSION = '1.1.1'
# Keep the installed patch identifier and paths stable for existing users.
PATCH_ID = 'codex-capacity-immediate-retry'
LABEL = 'com.local.codex-capacity-patch'
FILES = ('wrapper.py', 'test_wrapper.py', 'test_real_backend.py', 'patch_manager.py', 'test_patch_manager.py',
         'README.md', 'README.zh-CN.md', 'LICENSE')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def copy_atomic(source, target, mode):
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + target.name + '-', dir=target.parent)
    os.close(fd)
    try:
        shutil.copyfile(source, name)
        os.chmod(name, mode)
        with open(name, 'rb') as stream:
            os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        if os.path.lexists(name):
            os.unlink(name)


def atomic(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and not path.is_symlink() and path.stat().st_size == len(data) and path.read_bytes() == data:
        return False
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.lexists(name):
            os.unlink(name)
    return True


def json_write(path, data):
    return atomic(path, (json.dumps(data, indent=2, sort_keys=True) + '\n').encode())


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def run(args, timeout=12, input_data=None):
    """Bound only the command's own process group, including SSH grandchildren."""
    p = subprocess.Popen(args, stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        out, err = p.communicate(input_data, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            out, err = p.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = p.communicate(timeout=1)
        return 124, out.decode(errors='replace'), err.decode(errors='replace')
    return p.returncode, out.decode(errors='replace'), err.decode(errors='replace')


def validate(bundle):
    manifest = read_json(bundle / 'manifest.json')
    if not manifest or manifest.get('patch_id') != PATCH_ID:
        raise ValueError('Wrong patch manifest')
    if not re.fullmatch(r'\d+\.\d+\.\d+', manifest['version']):
        raise ValueError('Invalid patch version')
    if set(manifest['files']) != set(FILES):
        raise ValueError('Unexpected patch payload')
    for name, expected in manifest['files'].items():
        if digest((bundle / name).read_bytes()) != expected:
            raise ValueError('Patch checksum failed: ' + name)
    if manifest.get('retry_delay_ms') != 0 or manifest.get('retry_limit') is not None:
        raise ValueError('Patch must preserve immediate unlimited retry')
    return manifest


class Patch:
    def __init__(self, home=None, role=None, bundle=None, runner=run, app=None, codex=None):
        self.home = Path(home or Path.home())
        self.root = self.home / '.local/lib/codex-capacity-retry'
        self.role = role or ('mac' if sys.platform == 'darwin' else 'remote')
        self.bundle = Path(bundle or Path(__file__).resolve().parent)
        self.run = runner
        config = read_json(self.root / 'config.json', {})
        default_app = '/Applications/Codex.app' if Path('/Applications/Codex.app').exists() else '/Applications/ChatGPT.app'
        self.app = Path(app or config.get('app_path') or default_app).expanduser()
        self.explicit_app = app is not None
        self.real_cli = Path(codex).expanduser().absolute() if codex else None
        self.entry = self.home / '.local/bin' / ('codex-capacity-retry' if self.role == 'mac' else 'codex')
        self.control = self.home / '.local/bin/codex-capacity-patch'
        self.state_path = self.root / 'patch-state.json'

    def candidates(self):
        if self.role == 'mac':
            resources = self.app / 'Contents/Resources'
            return [resources / 'codex-cli/CodexCLI.app/Contents/MacOS/codex',
                    resources / 'codex-cli/bin/codex', resources / 'codex', resources / 'codex-cli']
        h = self.home
        result = [h / '.codex/packages/standalone/current/bin/codex',
                h / '.npm-global/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex',
                h / '.npm-global/bin/codex']
        found = shutil.which('codex')
        if found and Path(found) != self.entry:
            result.append(Path(found))
        return result

    def launcher(self):
        return ('#!/bin/sh\n# ' + PATCH_ID + '\nexec ' + shlex.quote(sys.executable) + ' ' +
                shlex.quote(str(self.root / 'current/wrapper.py')) + ' "$@"\n').encode()

    def owns_entry(self):
        if not self.entry.is_file() or self.entry.is_symlink():
            return False
        with self.entry.open('rb') as stream:
            data = stream.read(2048)
        return PATCH_ID.encode() in data or b'codex-capacity-retry/wrapper.py' in data

    def snapshot_entry(self):
        if self.entry.is_symlink():
            return {'kind': 'symlink', 'target': os.readlink(self.entry)}
        if self.entry.exists():
            checksum = file_digest(self.entry)
            backup = self.root / 'backups' / (checksum + '.entry')
            if not backup.exists():
                copy_atomic(self.entry, backup, self.entry.stat().st_mode & 0o777)
            return {'kind': 'file', 'backup': str(backup), 'sha256': checksum,
                    'mode': self.entry.stat().st_mode & 0o777}
        return {'kind': 'missing'}

    def initial_restore(self):
        old = read_json(self.root / 'entry-before.json') if self.role == 'remote' else None
        if old:
            return {'kind': 'symlink', 'target': old['symlink']} if old.get('symlink') else {'kind': 'missing'}
        return {'kind': 'missing'} if self.owns_entry() else self.snapshot_entry()

    def resolve_real(self, config, restore):
        changed_external = self.role == 'remote' and os.path.lexists(self.entry) and not self.owns_entry()
        extra = []
        latest = None
        if changed_external:
            if self.entry.is_symlink():
                target = Path(os.readlink(self.entry))
                extra.append(target if target.is_absolute() else self.entry.parent / target)
            else:
                with self.entry.open('rb') as stream:
                    magic = stream.read(4)
                if magic != b'\x7fELF':
                    raise ValueError('Unknown external codex launcher; refusing to replace it')
                latest = self.snapshot_entry()
                extra.append(Path(latest['backup']))
        previous = [Path(config['real_bin'])] if config.get('real_bin') else []
        candidates = extra + self.candidates() + previous
        if not changed_external:
            candidates = previous + candidates
        if self.explicit_app:
            candidates = self.candidates() + candidates
        if self.real_cli is not None:
            candidates = [self.real_cli] + candidates
        valid = [p for p in candidates if p.is_file() and os.access(p, os.X_OK) and p != self.entry]
        if not valid:
            raise ValueError('No executable Codex CLI found; existing configuration preserved')
        real = valid[0]
        stamp = [str(real), str(real.resolve()), real.stat().st_mtime_ns, real.stat().st_size]
        cached = config.get('real_stamp') == stamp and config.get('real_version')
        if cached:
            version = config['real_version']
        else:
            code, out, err = self.run([str(real), '--version'], timeout=8)
            if code or not re.search(r'codex-cli\s+\S+', out):
                raise ValueError('Codex CLI validation failed; existing entry preserved')
            version = out.strip()
            self.verify_protocol(real)
        if changed_external:
            restore['entry'] = latest or self.snapshot_entry()
        return str(real), stamp, version

    def verify_protocol(self, real):
        """Check changed CLI versions without starting a model or touching sessions."""
        with tempfile.TemporaryDirectory(prefix='codex-capacity-schema-') as folder:
            code, _, _ = self.run([str(real), 'app-server', 'generate-json-schema',
                                   '--experimental', '--out', folder], timeout=12)
            if code:
                raise ValueError('New CLI protocol could not be verified; no entry replaced')
            root = Path(folder) / 'v2'
            start = read_json(root / 'TurnStartParams.json', {})
            completed = read_json(root / 'TurnCompletedNotification.json', {})
            if not {'input', 'threadId'} <= set(start.get('required', [])) or not {
                    'turn', 'threadId'} <= set(completed.get('required', [])) or (
                    'serverOverloaded' not in json.dumps(completed)):
                raise ValueError('New CLI capacity protocol is incompatible; no entry replaced')

    def ensure(self, activate=False):
        state = read_json(self.state_path, {})
        if not activate and not state.get('enabled'):
            return {'enabled': False, 'changed': False}
        manifest = validate(self.bundle)
        version = manifest['version']
        restore = read_json(self.root / 'patch-rollback.json')
        if restore is None:
            restore = {'entry': self.initial_restore()}
            if self.role == 'mac':
                old = read_json(self.root / 'launch-env-before.json')
                restore['environment'] = old.get('CODEX_CLI_PATH', '') if old is not None else self.run(
                    ['/bin/launchctl', 'getenv', 'CODEX_CLI_PATH'])[1].strip()
        config = read_json(self.root / 'config.json', {})
        real, stamp, cli_version = self.resolve_real(config, restore)
        release = self.root / 'releases' / version
        changed = False
        for name in FILES + ('manifest.json',):
            changed |= atomic(release / name, (self.bundle / name).read_bytes(),
                              0o700 if name.endswith('.py') else 0o600)
        link = release / 'config.json'
        if not link.is_symlink():
            if link.exists():
                raise ValueError('Unexpected release config file')
            link.symlink_to('../../config.json')
        current = self.root / 'current'
        desired = 'releases/' + version
        if not current.is_symlink() or os.readlink(current) != desired:
            temp = self.root / ('.current-' + str(os.getpid()))
            temp.symlink_to(desired)
            os.replace(temp, current)
            changed = True
        config.update(real_bin=real, real_candidates=[str(p) for p in self.candidates()],
                      real_stamp=stamp, real_version=cli_version, patch_version=version)
        if self.role == 'mac':
            config['app_path'] = str(self.app)
        config.setdefault('log_path', str(self.home / ('Library/Logs/codex-capacity-retry/events.jsonl'
                           if self.role == 'mac' else '.codex/log/capacity-immediate-retry.jsonl')))
        changed |= json_write(self.root / 'config.json', config)
        json_write(self.root / 'patch-rollback.json', restore)
        changed |= atomic(self.entry, self.launcher(), 0o700)
        control = ('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' ' +
                   shlex.quote(str(self.root / 'current/patch_manager.py')) + ' "$@"\n').encode()
        changed |= atomic(self.control, control, 0o700)
        if self.role == 'mac':
            changed |= self.setup_launch_agent()
        state.update(enabled=True, version=version, checked_at=time.time(), cli_version=cli_version,
                     entry=str(self.entry), real_bin=real)
        if changed:
            state['last_repair_at'] = time.time()
        json_write(self.state_path, state)
        return {'enabled': True, 'version': version, 'changed': changed,
                'project': PROJECT_NAME, 'cli_version': cli_version, 'delay_ms': 0, 'retry_limit': None}

    def setup_launch_agent(self):
        code, value, _ = self.run(['/bin/launchctl', 'getenv', 'CODEX_CLI_PATH'])
        changed = value.strip() != str(self.entry)
        if changed:
            code, _, err = self.run(['/bin/launchctl', 'setenv', 'CODEX_CLI_PATH', str(self.entry)])
            if code:
                raise RuntimeError('Could not restore App CLI override')
        plist = self.home / 'Library/LaunchAgents' / (LABEL + '.plist')
        logs = self.home / 'Library/Logs/codex-capacity-retry'
        logs.mkdir(parents=True, exist_ok=True)
        spec = {'Label': LABEL, 'ProgramArguments': [sys.executable,
                str(self.root / 'current/patch_manager.py'), 'ensure', '--all'],
                'RunAtLoad': True, 'StartInterval': 60,
                'WatchPaths': [str(self.app / 'Contents/Info.plist'), str(self.entry)],
                'StandardOutPath': str(logs / 'patch-maintenance.jsonl'),
                'StandardErrorPath': str(logs / 'patch-maintenance.stderr.log')}
        plist_changed = atomic(plist, plistlib.dumps(spec))
        domain = f'gui/{os.getuid()}'
        loaded = self.run(['/bin/launchctl', 'print', domain + '/' + LABEL])[0] == 0
        if plist_changed and loaded:
            self.run(['/bin/launchctl', 'bootout', domain + '/' + LABEL])
            loaded = False
        if not loaded:
            code, _, err = self.run(['/bin/launchctl', 'bootstrap', domain, str(plist)])
            if code:
                raise RuntimeError('Could not enable patch upgrade check')
        old_label = 'com.local.codex-capacity-retry-env'
        old = self.home / 'Library/LaunchAgents' / (old_label + '.plist')
        if old.exists():
            self.run(['/bin/launchctl', 'bootout', domain + '/' + old_label])
            old.unlink()
        return changed or plist_changed

    def status(self):
        state = read_json(self.state_path, {})
        current = self.root / 'current'
        errors = []
        try:
            manifest = validate(current)
            if manifest['version'] != state.get('version'):
                errors.append('release_version_mismatch')
        except (ValueError, OSError, KeyError) as exc:
            errors.append(str(exc))
        expected = self.launcher()
        if (not self.entry.is_file() or self.entry.is_symlink() or
                self.entry.stat().st_size != len(expected) or self.entry.read_bytes() != expected):
            errors.append('entry_overwritten')
        config = read_json(self.root / 'config.json', {})
        real = config.get('real_bin')
        if not real or not Path(real).is_file():
            errors.append('real_cli_missing')
        elif config.get('real_stamp') != [str(Path(real)), str(Path(real).resolve()),
                                          Path(real).stat().st_mtime_ns, Path(real).stat().st_size]:
            errors.append('cli_changed_unverified')
        if self.role == 'mac' and self.run(['/bin/launchctl', 'getenv', 'CODEX_CLI_PATH'])[1].strip() != str(self.entry):
            errors.append('app_override_missing')
        if self.role == 'mac':
            agent = self.home / 'Library/LaunchAgents' / (LABEL + '.plist')
            if not agent.exists() or self.run(['/bin/launchctl', 'print', f'gui/{os.getuid()}/' + LABEL])[0]:
                errors.append('upgrade_maintenance_missing')
        return {'enabled': bool(state.get('enabled')), 'version': state.get('version'),
                'healthy': not errors and bool(state.get('enabled')), 'errors': errors,
                'cli_version': config.get('real_version'), 'delay_ms': 0, 'retry_limit': None}

    def rollback(self):
        state = read_json(self.state_path, {})
        state.update(enabled=False, rolled_back_at=time.time())
        json_write(self.state_path, state)
        restore = read_json(self.root / 'patch-rollback.json', {})
        changed = False
        if self.owns_entry():
            entry = restore.get('entry', {'kind': 'missing'})
            if entry['kind'] == 'symlink':
                temp = self.entry.with_name('.rollback-' + str(os.getpid()))
                temp.symlink_to(entry['target'])
                os.replace(temp, self.entry)
            elif entry['kind'] == 'file':
                backup = Path(entry['backup'])
                if file_digest(backup) != entry['sha256']:
                    raise ValueError('Rollback backup checksum failed')
                copy_atomic(backup, self.entry, entry['mode'])
            else:
                self.entry.unlink()
            changed = True
        if self.role == 'mac':
            plist = self.home / 'Library/LaunchAgents' / (LABEL + '.plist')
            if plist.exists():
                self.run(['/bin/launchctl', 'bootout', f'gui/{os.getuid()}/' + LABEL])
                plist.unlink()
            previous = restore.get('environment', '')
            self.run(['/bin/launchctl', 'setenv', 'CODEX_CLI_PATH', previous] if previous else
                     ['/bin/launchctl', 'unsetenv', 'CODEX_CLI_PATH'])
        return {'enabled': False, 'changed': changed, 'running_connections_restarted': False}


def archive(bundle):
    validate(bundle)
    data = io.BytesIO()
    import gzip
    with gzip.GzipFile(fileobj=data, mode='wb', mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w') as tar:
            for name in FILES + ('manifest.json',):
                raw = (bundle / name).read_bytes()
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(raw), 0o755 if name.endswith('.py') else 0o644
                tar.addfile(info, io.BytesIO(raw))
    return data.getvalue()


def remote(host, action, bundle):
    if not valid_host(host):
        raise ValueError('Invalid SSH host alias')
    if action == 'apply':
        encoded = base64.b64encode(archive(bundle)).decode()
        script = '''import base64,io,tarfile,tempfile,subprocess,sys
from pathlib import Path
with tempfile.TemporaryDirectory(prefix='codex-capacity-patch-') as folder:
 p=Path(folder)
 with tarfile.open(fileobj=io.BytesIO(base64.b64decode(PAYLOAD)),mode='r:gz') as t:
  for m in t.getmembers():
   if '/' in m.name or m.name.startswith('.') or not m.isfile():raise ValueError('Unsafe patch archive')
   (p/m.name).write_bytes(t.extractfile(m).read())
 r=subprocess.run([sys.executable,str(p/'patch_manager.py'),'apply'],timeout=20)
 sys.exit(r.returncode)
'''.replace('PAYLOAD', repr(encoded))
        command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', host, 'python3', '-']
        code, out, err = run(command, timeout=35, input_data=script.encode())
    else:
        command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', host,
                   'python3', '"$HOME/.local/lib/codex-capacity-retry/current/patch_manager.py"', action]
        code, out, err = run(command, timeout=20)
    if code:
        return {'host': host, 'ok': False, 'exit_code': code}
    try:
        result = json.loads(out.strip().splitlines()[-1])
        return {'host': host, 'ok': result.get('healthy', result.get('enabled', False)) if action != 'rollback' else not result.get('enabled'), **result}
    except (ValueError, IndexError):
        return {'host': host, 'ok': False, 'error': 'invalid_patch_response'}


def valid_host(host):
    return bool(host and not host.startswith('-') and re.fullmatch(r'[A-Za-z0-9_.@:\[\]-]+', host))


def managed_hosts(root, requested=None):
    if requested is not None:
        if not all(valid_host(host) for host in requested):
            raise ValueError('Invalid SSH host alias')
        hosts = list(dict.fromkeys(requested))
        json_write(root / 'patch-targets.json', {'hosts': hosts})
        return hosts
    hosts = read_json(root / 'patch-targets.json', {}).get('hosts', [])
    if not isinstance(hosts, list) or not all(isinstance(h, str) and valid_host(h) for h in hosts):
        raise ValueError('Invalid saved SSH hosts')
    return hosts


def main():
    parser = argparse.ArgumentParser(description=PROJECT_NAME + ': ' + __doc__)
    parser.add_argument('action', choices=['apply', 'ensure', 'status', 'rollback'])
    parser.add_argument('--all', action='store_true', help='Include previously configured SSH hosts')
    parser.add_argument('--host', action='append', default=None, metavar='SSH_ALIAS',
                        help='SSH host to include; repeat as needed. Saved only by apply.')
    parser.add_argument('--app', type=Path, help='macOS App bundle path')
    parser.add_argument('--codex', type=Path, help='Explicit original Codex CLI executable')
    args = parser.parse_args()
    patch = Patch(app=args.app, codex=args.codex)
    patch.root.mkdir(parents=True, exist_ok=True)
    with (patch.root / 'patch.lock').open('a') as lock:
        try:
            hosts = managed_hosts(patch.root, args.host) if args.action == 'apply' and args.host is not None else (
                args.host if args.host is not None else managed_hosts(patch.root))
            if not all(valid_host(h) for h in hosts):
                raise ValueError('Invalid SSH host alias')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'busy': True}))
            return 0
        if patch.role == 'mac' and args.action == 'ensure':
            # Keep launchd's already-open append file intact while bounding its size.
            for filename in ['patch-maintenance.jsonl', 'patch-maintenance.stderr.log']:
                log = patch.home / 'Library/Logs/codex-capacity-retry' / filename
                if log.exists() and log.stat().st_size > 4 * 1024 * 1024:
                    with log.open('r+b') as stream:
                        stream.seek(-2 * 1024 * 1024, os.SEEK_END)
                        tail = stream.read().split(b'\n', 1)[-1]
                        stream.seek(0)
                        stream.write(tail)
                        stream.truncate()
        try:
            if args.action == 'apply':
                result = patch.ensure(activate=True)
            elif args.action == 'ensure':
                result = patch.ensure()
            elif args.action == 'rollback':
                result = patch.rollback()
            else:
                result = patch.status()
            results = {'local': result}
            include_remote = bool(args.all or args.host)
            if include_remote and hosts and (args.action != 'ensure' or result.get('enabled')):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results['remote'] = list(pool.map(lambda host: remote(host, args.action, patch.bundle), hosts))
            print(json.dumps(results if include_remote else result, sort_keys=True))
            remote_ok = all(x.get('ok') for x in results.get('remote', []))
            local_ok = result.get('healthy', True) if args.action == 'status' else True
            return 0 if remote_ok and local_ok else 1
        except Exception as exc:
            print(json.dumps({'ok': False, 'error': str(exc)}))
            return 1


if __name__ == '__main__':
    raise SystemExit(main())
