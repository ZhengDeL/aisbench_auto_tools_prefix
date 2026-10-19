"""Offline publication contracts: an immutable upstream tree plus public add/.

Every Git operation targets a temporary local repository. No upstream fetch,
remote server, account credential, signing key or real model is accessed.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import validate_publication as publication


class PublicationContracts(unittest.TestCase):
    def setUp(self):
        if not shutil.which('git'):
            self.skipTest('Git is required for the publication tree contracts')
        temporary = tempfile.TemporaryDirectory(prefix='aisbench_publication_contract_')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'repository'
        self.root.mkdir()
        self.git('init', '-q')
        self.git('config', 'core.autocrlf', 'false')
        self.git('config', 'core.filemode', 'false')
        # Construct this documentation-only address at runtime: the public test
        # source itself must obey add/'s example-address policy.
        self.external_address = '.'.join(('192', '0', '2', '10'))
        self.original_files = {
            'README.md': ('# Original upstream\nExample: http://' + self.external_address + ':8000\n').encode(),
            'runner.sh': ('#!/bin/sh\nSERVER=' + self.external_address + '\n').encode(),
        }
        for name, content in self.original_files.items():
            self.write(name, content)
        self.write('add/feature.py', b"EXAMPLE_HOST = '192.168.1.10'\n")
        self.repository = 'https://github.com/rayn-zzz/aisbench_auto_tools_prefix'
        self.commit = 'e' * 40
        self.manifest = {
            'repository': self.repository,
            'commit': self.commit,
            'files': {name: {'sha256': hashlib.sha256(content).hexdigest(), 'mode': '100644'}
                      for name, content in self.original_files.items()},
        }
        self.original_manifest = self.encode_manifest(self.manifest)
        self.write('add/upstream_manifest.json', self.original_manifest)
        self.git('add', '--all')
        for name, value in (('UPSTREAM_REPOSITORY', self.repository),
                            ('UPSTREAM_COMMIT', self.commit),
                            ('TRUSTED_MANIFEST_SHA256', hashlib.sha256(self.original_manifest).hexdigest())):
            replacement = patch.object(publication, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)

    @staticmethod
    def encode_manifest(payload):
        return (json.dumps(payload, sort_keys=True, indent=2) + '\n').encode('utf-8')

    def write(self, name, content):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def git(self, *arguments):
        return subprocess.run(['git', '-C', str(self.root), *arguments],
                              text=True, capture_output=True, check=True).stdout

    def audit(self, root=None):
        findings, count, tracked = publication.audit(root or self.root)
        self.assertNotIn(self.external_address, json.dumps(findings),
                         'publication findings disclosed the rejected address')
        return findings, count, tracked

    def assert_rejected_path(self, expected):
        findings, count, tracked = self.audit()
        self.assertTrue(findings, 'non-upstream public tree unexpectedly passed')
        self.assertTrue(tracked)
        self.assertGreater(count, 0)
        self.assertIn(expected, {str(item['path']).replace('\\', '/') for item in findings})

    def test_exact_upstream_addresses_are_exempt_and_public_add_can_change_in_git_and_archives(self):
        findings, count, tracked = self.audit()
        self.assertEqual(findings, [])
        self.assertEqual(count, 4)
        self.assertTrue(tracked)
        self.write('add/feature.py', b"EXAMPLE_HOST = '192.168.1.20'\nNEW_FEATURE = True\n")
        self.git('add', 'add/feature.py')
        self.assertEqual(self.audit()[0], [])
        archive = self.directory / 'archive'
        for relative in (*self.original_files, 'add/feature.py', 'add/upstream_manifest.json'):
            destination = archive / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((self.root / relative).read_bytes())
        findings, count, tracked = self.audit(archive)
        self.assertEqual(findings, [])
        self.assertEqual(count, 4)
        self.assertFalse(tracked)

    def test_modified_outer_file_is_rejected_without_updating_git_index(self):
        self.write('README.md', self.original_files['README.md'] + b'Unapproved downstream edit.\n')
        self.assert_rejected_path('README.md')

    def test_added_outer_file_is_rejected(self):
        self.write('extra.md', b'An extra downstream root file.\n')
        self.git('add', 'extra.md')
        # Unknown outer filenames are deliberately redacted by the publisher.
        self.assert_rejected_path('[non-add-file]')

    def test_removed_outer_file_is_rejected_even_when_removed_from_index(self):
        (self.root / 'runner.sh').unlink()
        self.git('add', '--update')
        self.assert_rejected_path('runner.sh')

    def test_git_file_mode_change_is_rejected_with_identical_bytes(self):
        self.git('update-index', '--chmod=+x', 'runner.sh')
        self.assertEqual((self.root / 'runner.sh').read_bytes(), self.original_files['runner.sh'])
        self.assert_rejected_path('runner.sh')

    def test_manifest_is_required_trusted_and_bound_to_pinned_repository_and_commit(self):
        manifest_path = self.root / 'add' / 'upstream_manifest.json'
        for kind in ('missing', 'malformed', 'forged-file-hash', 'wrong-repository', 'wrong-commit'):
            with self.subTest(manifest=kind):
                self.write('README.md', self.original_files['README.md'])
                self.write('add/upstream_manifest.json', self.original_manifest)
                payload = json.loads(self.original_manifest)
                trusted_digest = hashlib.sha256(self.original_manifest).hexdigest()
                if kind == 'missing':
                    manifest_path.unlink()
                elif kind == 'malformed':
                    manifest_path.write_bytes(b'not json\n')
                elif kind == 'forged-file-hash':
                    changed = self.original_files['README.md'] + b'Forged upstream revision.\n'
                    self.write('README.md', changed)
                    payload['files']['README.md']['sha256'] = hashlib.sha256(changed).hexdigest()
                    manifest_path.write_bytes(self.encode_manifest(payload))
                else:
                    payload['repository' if kind == 'wrong-repository' else 'commit'] = (
                        'https://github.com/example/other' if kind == 'wrong-repository' else 'f' * 40)
                    content = self.encode_manifest(payload)
                    manifest_path.write_bytes(content)
                    # Isolate provenance checks from the raw-byte trust check.
                    trusted_digest = hashlib.sha256(content).hexdigest()
                with patch.object(publication, 'TRUSTED_MANIFEST_SHA256', trusted_digest):
                    self.assertTrue(self.audit()[0], 'untrusted or unpinned manifest was accepted')

    def test_upstream_exemption_does_not_allow_the_same_address_in_add(self):
        self.write('add/private_example.py', ('HOST = ' + repr(self.external_address) + '\n').encode())
        self.git('add', 'add/private_example.py')
        findings, _, tracked = self.audit()
        self.assertTrue(tracked)
        address_findings = [item for item in findings if 'address' in item['reason']]
        self.assertEqual({str(item['path']).replace('\\', '/') for item in address_findings},
                         {'add/private_example.py'})

    def test_tracked_local_configuration_is_rejected_even_with_public_example_values(self):
        self.write('add/test.local.env', b'HOST_IP=192.168.1.10\n')
        self.git('add', 'add/test.local.env')
        findings, _, tracked = self.audit()
        self.assertTrue(tracked)
        self.assertTrue(any(str(item['path']).replace('\\', '/') == 'add/test.local.env'
                            and item['reason'] == 'local artifact is tracked' for item in findings))


if __name__ == '__main__':
    unittest.main()
