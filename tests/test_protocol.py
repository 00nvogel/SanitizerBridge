"""Behavioral coverage for both packages and the metadata-only publication protocol."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from bridge.config import load
from bridge.engine import Engine
from bridge.git import BridgeError, Git
from test_bridge import Fixture


def setup_mode(fixture, mode):
    fixture.config['mode'] = mode
    if mode == 'embedded':
        fixture.config['project'] = 'embedded'
        fixture.repos['bridge'] = fixture.repos['internal']
    return fixture


def fresh_run(fixture, action='import'):
    with tempfile.TemporaryDirectory() as d:
        return Engine(Git(d), fixture.config, fixture.repos['bridge']).run(action)


class ProtocolTests(unittest.TestCase):
    def test_empty_and_excluded_only_customer_commits_never_create_import_commits(self):
        for mode in ('standalone', 'embedded'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as d:
                f = setup_mode(Fixture(d, customer={'shared.txt': 'internal'}), mode)
                fresh_run(f)
                internal = f.git.head(f.repos['internal'], 'main')
                f.commit('customer', {})  # An actual empty Git commit.
                fresh_run(f)
                self.assertIsNone(f.state()['generated'])
                f.commit('customer', {'customer-private/noise': 'excluded'})
                fresh_run(f)
                self.assertIsNone(f.state()['generated'])
                f.commit('customer', {'shared.txt': 'meaningful'})
                fresh_run(f)
                generated, branch = f.state()['generated'], f.state()['branch']
                for changes in ({}, {'customer-private/noise': 'changed again'}):
                    customer = f.commit('customer', changes)
                    fresh_run(f)
                    self.assertEqual(f.state()['observed'], customer)
                    self.assertEqual(f.state()['generated'], generated)
                    self.assertEqual(f.state()['branch'], branch)
                    self.assertEqual(f.git.head(f.repos['internal'], branch), generated)
                self.assertEqual(f.git.head(f.repos['internal'], 'main'), internal)

    def test_metadata_ancestry_contains_no_project_objects(self):
        fdirs = [tempfile.TemporaryDirectory() for _ in range(2)]
        for d in fdirs:
            self.addCleanup(d.cleanup)
        for mode, directory in zip(('standalone', 'embedded'), fdirs):
            with self.subTest(mode=mode):
                f = setup_mode(Fixture(directory.name, customer={'shared.txt': 'customer'}), mode)
                fresh_run(f)
                generated = f.state()['generated']
                fresh_run(f, 'export')
                revision = f.engine().revision
                with tempfile.TemporaryDirectory() as d:
                    metadata = Git(d)
                    metadata.fetch(f.repos['bridge'], revision)
                    for sha in metadata.run('rev-list', revision).decode().splitlines():
                        self.assertEqual(set(metadata.files(sha)), {'state.json'})
                        self.assertLessEqual(len(metadata.run('show', '-s', '--format=%P', sha).split()), 1)
                    for sha in (f.anchor, generated, f.state()['checkpoint']):
                        self.assertNotEqual(metadata.run('cat-file', '-e', sha, check=False).returncode, 0)
                for role in ('internal', 'customer'):
                    self.assertFalse(f.git.run('ls-remote', '--heads', f.repos[role], 'refs/heads/sanitizer-bridge-stage/*'))

    def test_customer_rewrite_suspends_until_export_preserves_new_snapshot(self):
        for mode in ('standalone', 'embedded'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as d:
                f = setup_mode(Fixture(d), mode)
                fresh_run(f, 'export')
                rewritten = f.git.commit({'rewritten.txt': ('100644', f.git.blob(b'preserve this'))}, [], 'Customer reset')
                f.git.run('push', '--force', f.repos['customer'], rewritten + ':refs/heads/main')
                self.assertEqual(fresh_run(f)['status'], 'imports-suspended')
                checkpoint = f.state()['checkpoint']
                f.commit('customer', {'more.txt': 'current snapshot'})
                self.assertEqual(fresh_run(f)['status'], 'imports-suspended')
                self.assertEqual(f.state()['checkpoint'], checkpoint)
                self.assertIsNone(f.state()['generated'])
                fresh_run(f, 'export')
                self.assertIsNone(f.state()['suspension'])
                branch = 'sanitizer-bridge/import-1' if mode == 'embedded' else 'bridge/demo/import-1'
                self.assertIn('rewritten.txt', f.contents('internal', branch))
                self.assertIn('more.txt', f.contents('internal', branch))
                self.assertNotIn('rewritten.txt', f.contents('customer'))
                f.commit('customer', {'new-epoch.txt': 'new'})
                self.assertEqual(fresh_run(f)['status'], 'synchronized')
                self.assertNotIn('rewritten.txt', f.contents('internal', f.state()['branch']))

    def test_deleted_customer_branch_can_be_reinitialized_by_explicit_export(self):
        with tempfile.TemporaryDirectory() as d:
            f = Fixture(d)
            fresh_run(f, 'export')
            f.git.run('push', f.repos['customer'], ':refs/heads/main')
            self.assertEqual(fresh_run(f)['status'], 'imports-suspended')
            fresh_run(f, 'export')
            self.assertIsNone(f.state()['suspension'])
            self.assertEqual(f.contents('customer')['shared.txt'][1], b'internal')

    def test_missing_internal_commit_fails_without_substituting_base(self):
        with tempfile.TemporaryDirectory() as d:
            f = Fixture(d, customer={'shared.txt': 'customer'})
            fresh_run(f)
            state = f.state()
            f.git.fetch(f.repos['internal'], state['generated'])  # Team-owned backup before removing its remote ref.
            f.git.run('push', f.repos['internal'], ':refs/heads/' + state['branch'])
            subprocess.run(['git', '-C', f.repos['internal'], 'reflog', 'expire', '--expire=now', '--all'], check=True)
            subprocess.run(['git', '-C', f.repos['internal'], 'gc', '--prune=now'], check=True, capture_output=True)
            for action in ('import', 'export'):
                with self.assertRaisesRegex(BridgeError, 'Required internal commit is unavailable'):
                    fresh_run(f, action)
            self.assertEqual(f.state(), state)
            # The team's retained copy can restore availability on any internal branch.
            f.git.push(f.repos['internal'], 'team-retained', state['generated'], None)
            fresh_run(f)
            self.assertEqual(f.state()['generated'], state['generated'])
            self.assertNotEqual(f.state()['branch'], state['branch'])

    def test_prepared_commit_survives_runner_loss_without_bridge_archive(self):
        for role in ('internal', 'customer'):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as d:
                f = Fixture(d, customer={'shared.txt': 'customer'})
                if role == 'customer':
                    fresh_run(f)
                with tempfile.TemporaryDirectory() as objects:
                    git = Git(objects)
                    engine = Engine(git, f.config, f.repos['bridge'])
                    original = git.push
                    def fail_destination(url, branch, sha, expected):
                        if url == f.repos[role] and not branch.startswith('sanitizer-bridge-stage/'):
                            raise BridgeError('Injected publication outage')
                        return original(url, branch, sha, expected)
                    with patch.object(git, 'push', fail_destination), self.assertRaises(BridgeError):
                        engine.run('export' if role == 'customer' else 'import')
                pending = f.state()['pending']
                self.assertEqual(f.git.head(f.repos[role], pending['stage']), pending['commit'])
                fresh_run(f, 'export' if role == 'customer' else 'import')
                self.assertIsNone(f.state()['pending'])
                self.assertEqual(f.git.head(f.repos[role], pending['branch']), pending['commit'])
                self.assertIsNone(f.git.head(f.repos[role], pending['stage']))

    def test_cleanup_failure_is_recoverable_without_republication(self):
        with tempfile.TemporaryDirectory() as d:
            f = Fixture(d, customer={'shared.txt': 'customer'})
            with patch.object(f.git, 'delete', side_effect=BridgeError('Temporary cleanup failure')):
                with self.assertRaisesRegex(BridgeError, 'cleanup needs retry'):
                    f.run()
            state = f.state()
            self.assertIsNone(state['pending'])
            self.assertEqual(len(state['cleanup']), 1)
            fresh_run(f)
            self.assertEqual(f.state()['generated'], state['generated'])
            self.assertEqual(f.state()['cleanup'], [])

    def test_customer_recovery_export_noop_resumes_and_failed_export_does_not(self):
        with tempfile.TemporaryDirectory() as d:
            f = Fixture(d)
            fresh_run(f, 'export')
            f.git.fetch(f.repos['customer'], f.state()['checkpoint'])
            same = f.git.commit(f.git.files(f.state()['checkpoint']), [], 'Rewritten but equal')
            f.git.run('push', '--force', f.repos['customer'], same + ':refs/heads/main')
            fresh_run(f)
            f.script.write_text('exit 9\n')
            with self.assertRaises(BridgeError):
                fresh_run(f, 'export')
            self.assertIsNotNone(f.state()['suspension'])
            f.script.write_text('rm -rf -- "$1/private" "$1/customer-private" "$1/.github"\n')
            result = fresh_run(f, 'export')
            self.assertFalse(result['changed'])
            self.assertIsNone(f.state()['suspension'])
            self.assertEqual(f.git.head(f.repos['customer'], 'main'), same)

    def test_embedded_control_files_are_excluded_even_with_permissive_script(self):
        with tempfile.TemporaryDirectory() as d:
            f = setup_mode(Fixture(d, internal={'shared': 'yes', '.sanitizer-bridge/config.yaml': 'private config',
                '.sanitizer-bridge/engine/run.py': 'engine', '.github/workflows/sanitizer-bridge.yml': 'control'}), 'embedded')
            f.script.write_text('true\n')
            fresh_run(f, 'export')
            self.assertEqual(set(f.contents('customer')), {'shared'})
            f.commit('customer', {'.sanitizer-bridge/config.yaml': 'customer value', '.github/workflows/other.yml': 'customer control'})
            fresh_run(f)
            self.assertIsNone(f.state()['generated'])
            f.commit('customer', {'shared': 'changed'})
            fresh_run(f)
            files = f.contents('internal', f.state()['branch'])
            self.assertEqual(files['.sanitizer-bridge/config.yaml'][1], b'private config')
            self.assertNotIn('.github/workflows/other.yml', files)

    def test_single_config_infers_internal_repo_and_rejects_project_selection(self):
        import yaml
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'sanitization.sh').write_text('true\n')
            config = {'internal': {'branch': 'main'}, 'customer': {'repository': 'example/customer', 'branch': 'main'},
                      'bootstrap_internal_commit': 'a' * 40, 'sanitizer': 'sanitization.sh',
                      'secrets': {'internal': 'INTERNAL_TOKEN', 'customer': 'CUSTOMER_TOKEN'},
                      'detection': {'polling': True, 'notification': False}}
            (root / 'config.yaml').write_text(yaml.safe_dump(config))
            with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'example/internal'}):
                loaded = load(root, embedded=True)
            self.assertEqual(loaded['internal']['url'], 'https://github.com/example/internal.git')
            self.assertEqual(loaded['secrets']['bridge'], 'INTERNAL_TOKEN')
            with self.assertRaisesRegex(BridgeError, 'exactly one'):
                load(root, 'another', embedded=True)
            config['projects'] = {}
            (root / 'config.yaml').write_text(yaml.safe_dump(config))
            with self.assertRaisesRegex(BridgeError, 'exactly one'):
                load(root, embedded=True)

    def test_migration_preserves_mapping_without_inheriting_legacy_history(self):
        with tempfile.TemporaryDirectory() as d:
            f = Fixture(d, customer={'shared.txt': 'customer'})
            engine = f.engine()
            customer = f.git.head(f.repos['customer'], 'main')
            old = {'schema': 1, 'binding': engine.binding, 'base': f.anchor, 'checkpoint': customer,
                   'observed': customer, 'generated': None, 'branch': None, 'sequence': 0, 'mode': 'ready', 'pending': None}
            blob = f.git.blob(json.dumps(old).encode())
            archive = f.git.commit({'state.json': ('100644', blob)}, [f.anchor, customer], 'Legacy archive')
            f.git.push(f.repos['bridge'], 'bridge-state/demo', archive, None)
            with self.assertRaisesRegex(BridgeError, 'migrate'):
                fresh_run(f)
            fresh_run(f, 'migrate')
            self.assertEqual(f.state()['base'], old['base'])
            self.assertEqual(f.git.head(f.repos['bridge'], 'bridge-state/demo'), archive)
            revision = f.engine().revision
            self.assertEqual(f.git.run('show', '-s', '--format=%P', revision).strip(), b'')
            fresh_run(f)
            self.assertIsNotNone(f.state()['generated'])


if __name__ == '__main__':
    unittest.main()
