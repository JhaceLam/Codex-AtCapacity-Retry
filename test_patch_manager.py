"""Upgrade, repeat application, integrity, and rollback tests using isolated homes."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from patch_manager import FILES, PATCH_ID, LABEL, Patch, archive, digest, managed_hosts, valid_host, validate


class Runner:
    def __init__(self):
        self.environment = ''
        self.loaded = False
        self.versions = 0
        self.schemas = 0
        self.fail_schema = False

    def __call__(self, args, timeout=12, input_data=None):
        if args[-1] == '--version':
            self.versions += 1
            return 0, 'codex-cli ' + Path(args[0]).read_text(errors='replace')[-6:].strip() + '\n', ''
        if 'generate-json-schema' in args:
            self.schemas += 1
            if self.fail_schema:
                return 1, '', 'unsupported'
            root = Path(args[-1]) / 'v2'
            root.mkdir()
            (root / 'TurnStartParams.json').write_text(json.dumps({'required': ['threadId', 'input']}))
            (root / 'TurnCompletedNotification.json').write_text(json.dumps({
                'required': ['threadId', 'turn'], 'definitions': {'error': 'serverOverloaded'}}))
            return 0, '', ''
        if args[0] == '/bin/launchctl':
            command = args[1]
            if command == 'getenv':
                return 0, self.environment + '\n', ''
            if command == 'setenv':
                self.environment = args[-1]
            elif command == 'unsetenv':
                self.environment = ''
            elif command == 'print':
                return (0 if self.loaded else 1), '', ''
            elif command == 'bootstrap':
                self.loaded = True
            elif command == 'bootout' and args[-1].endswith('/' + LABEL):
                self.loaded = False
            return 0, '', ''
        raise AssertionError(args)


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.bundle = self.base / 'bundle'
        self.bundle.mkdir()
        for name in FILES:
            shutil.copy2(Path(__file__).with_name(name), self.bundle / name)
        self.manifest()
        self.home = self.base / 'home'
        self.runner = Runner()

    def manifest(self):
        (self.bundle / 'manifest.json').write_text(json.dumps({
            'patch_id': PATCH_ID, 'version': '1.0.0', 'retry_delay_ms': 0,
            'retry_limit': None, 'files': {name: digest((self.bundle / name).read_bytes()) for name in FILES}}))

    def executable(self, path, version='0.164.0'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'\x7fELF' + version.encode())
        path.chmod(0o700)
        return path

    def remote(self):
        patch = Patch(self.home, 'remote', self.bundle, self.runner)
        real = self.executable(patch.candidates()[0])
        patch.entry.parent.mkdir(parents=True, exist_ok=True)
        patch.entry.symlink_to(real)
        return patch, real

    def test_apply_twice_is_unchanged_and_reuses_validation(self):
        patch, real = self.remote()
        self.assertTrue(patch.ensure(activate=True)['changed'])
        self.assertFalse(patch.ensure()['changed'])
        self.assertTrue(patch.status()['healthy'])
        self.assertEqual(self.runner.versions, 1)
        self.assertEqual(self.runner.schemas, 1)
        self.assertTrue((patch.root / 'current/config.json').samefile(patch.root / 'config.json'))

    def test_upgrade_overwrites_remote_symlink_then_ensure_reapplies_and_rollback_keeps_upgrade(self):
        patch, real = self.remote()
        patch.ensure(activate=True)
        upgraded = self.executable(self.home / 'new-cli/bin/codex', '0.165.0')
        patch.entry.unlink()
        patch.entry.symlink_to(upgraded)
        self.assertIn('entry_overwritten', patch.status()['errors'])
        patch.ensure()
        self.assertTrue(patch.status()['healthy'])
        self.assertEqual(json.loads((patch.root / 'config.json').read_text())['real_bin'], str(upgraded))
        patch.rollback()
        self.assertTrue(patch.entry.is_symlink())
        self.assertEqual(patch.entry.resolve(), upgraded.resolve())
        self.assertFalse(patch.ensure()['enabled'])
        self.assertEqual(patch.entry.resolve(), upgraded.resolve())

    def test_installer_replaces_entry_with_native_file_preserved_for_rollback(self):
        patch, real = self.remote()
        patch.ensure(activate=True)
        self.executable(patch.entry, '0.166.0')
        upgraded_bytes = patch.entry.read_bytes()
        patch.ensure()
        config = json.loads((patch.root / 'config.json').read_text())
        self.assertEqual(Path(config['real_bin']).read_bytes(), upgraded_bytes)
        patch.rollback()
        self.assertEqual(patch.entry.read_bytes(), upgraded_bytes)

    def test_new_app_layout_and_cleared_environment_are_repaired(self):
        app = self.base / 'ChatGPT.app'
        patch = Patch(self.home, 'mac', self.bundle, self.runner, app)
        real = self.executable(patch.candidates()[0])
        self.runner.environment = '/prior/custom-codex'
        patch.ensure(activate=True)
        real.unlink()
        new = self.executable(patch.candidates()[1], '0.165.0')
        self.runner.environment = ''
        patch.ensure()
        self.assertEqual(self.runner.environment, str(patch.entry))
        self.assertEqual(json.loads((patch.root / 'config.json').read_text())['real_bin'], str(new))
        self.assertTrue(patch.status()['healthy'])
        patch.rollback()
        self.assertEqual(self.runner.environment, '/prior/custom-codex')
        self.assertFalse((self.home / 'Library/LaunchAgents' / (LABEL + '.plist')).exists())

    def test_same_path_app_binary_replaced_is_revalidated(self):
        patch, real = self.remote()
        patch.ensure(activate=True)
        self.executable(real, '0.165.0')
        patch.ensure()
        self.assertEqual(self.runner.schemas, 2)

    def test_unknown_external_launcher_and_incompatible_protocol_are_preserved(self):
        patch, real = self.remote()
        patch.ensure(activate=True)
        patch.entry.write_text('#!/bin/sh\nexec unknown-custom-tool\n')
        with self.assertRaises(ValueError):
            patch.ensure()
        self.assertIn('unknown-custom-tool', patch.entry.read_text())
        patch.entry.unlink()
        upgraded = self.executable(self.home / 'new-cli', '0.199.0')
        patch.entry.symlink_to(upgraded)
        self.runner.fail_schema = True
        with self.assertRaises(ValueError):
            patch.ensure()
        self.assertEqual(patch.entry.resolve(), upgraded.resolve())

    def test_manifest_tampering_and_payload_tampering_fail_before_mutation(self):
        patch, real = self.remote()
        with (self.bundle / 'wrapper.py').open('a') as stream:
            stream.write('\n# modified\n')
        with self.assertRaises(ValueError):
            patch.ensure(activate=True)
        self.assertTrue(patch.entry.is_symlink())
        self.manifest()
        manifest = json.loads((self.bundle / 'manifest.json').read_text())
        manifest['retry_delay_ms'] = 1000
        (self.bundle / 'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            validate(self.bundle)

    def test_no_remote_hosts_are_configured_by_default(self):
        self.assertEqual(managed_hosts(self.home), [])
        self.assertEqual(managed_hosts(self.home, ['remote-a', 'user@example.com', 'remote-a']),
                         ['remote-a', 'user@example.com'])
        self.assertEqual(managed_hosts(self.home), ['remote-a', 'user@example.com'])
        self.assertEqual(managed_hosts(self.home, []), [])

    def test_invalid_remote_aliases_are_not_saved(self):
        for name in ['-oProxyCommand=bad', 'host;command', 'host name', 'host\nname', '']:
            self.assertFalse(valid_host(name))
            with self.assertRaises(ValueError):
                managed_hosts(self.home, [name])
        self.assertFalse((self.home / 'patch-targets.json').exists())

    def test_explicit_original_cli_is_preserved(self):
        patch, original = self.remote()
        explicit = self.executable(self.home / 'alternate/codex', '0.168.0')
        patch.real_cli = explicit
        patch.ensure(activate=True)
        config = json.loads((patch.root / 'config.json').read_text())
        self.assertEqual(config['real_bin'], str(explicit))

    def test_release_archive_is_reproducible(self):
        self.assertEqual(archive(self.bundle), archive(self.bundle))


if __name__ == '__main__':
    unittest.main()
