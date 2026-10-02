import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bridge.engine import Engine
from bridge.git import BridgeError, Git


class Fixture:
    def __init__(self, root, customer=None, internal=None, project='demo'):
        self.root = Path(root)
        self.repos = {}
        for role in ('internal', 'customer', 'bridge', 'objects'):
            directory = self.root / role
            directory.mkdir(exist_ok=True)
            self.repos[role] = str(directory)
            Git(directory)
        self.git = Git(self.repos['objects'])
        self.anchor = self.commit('internal', internal or {'shared.txt': 'internal', 'private/key': 'internal-secret'})
        if customer is not None:
            self.commit('customer', customer)
        self.script = self.root / 'sanitization.sh'
        self.script.write_text('set -eu\nrm -rf -- "$1/private" "$1/customer-private" "$1/.github"\n')
        self.config = {'project': project, 'bootstrap_internal_commit': self.anchor, 'script': self.script,
            **{role: {'repository': self.repos[role], 'url': self.repos[role], 'branch': 'main'} for role in ('internal', 'customer')}}

    def commit(self, role, changes, branch='main', parent=None):
        url = self.repos[role]
        actual = self.git.fetch(url, self.git.head(url, branch))
        if parent is None:
            parent = actual
        files = self.git.files(parent)
        for path, value in changes.items():
            if value is None:
                files.pop(path, None)
            else:
                mode, content = value if isinstance(value, tuple) else ('100644', value)
                files[path] = (mode, self.git.blob(content.encode() if isinstance(content, str) else content))
        sha = self.git.commit(files, [parent], 'Synthetic fixture')
        self.git.push(url, branch, sha, actual)
        return sha

    def engine(self):
        return Engine(self.git, self.config, self.repos['bridge'])

    def run(self, action='import'):
        return self.engine().run(action)

    def state(self):
        return self.engine().state

    def contents(self, role, branch='main', sha=None):
        sha = self.git.fetch(self.repos[role], sha or self.git.head(self.repos[role], branch))
        return {p: (m, self.git.run('cat-file', 'blob', oid)) for p, (m, oid) in self.git.files(sha).items()}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def fixture(self, **kwargs):
        return Fixture(self.tmp.name, **kwargs)

    def test_empty_bootstrap_export_latest_and_independent_ancestry(self):
        f = self.fixture()
        f.run()
        self.assertEqual(f.state()['mode'], 'awaiting_export')
        self.assertIsNone(f.git.head(f.repos['customer'], 'main'))
        latest = f.commit('internal', {'shared.txt': 'latest'})
        f.run('export')
        state = f.state()
        self.assertEqual(state['base'], latest)
        self.assertEqual(f.contents('customer'), {'shared.txt': ('100644', b'latest')})
        self.assertFalse(f.git.ancestor(f.anchor, state['checkpoint']))
        before = f.git.head(f.repos['bridge'], 'bridge-state-v2/demo')
        f.run(); f.run('export'); f.run()
        self.assertEqual(before, f.git.head(f.repos['bridge'], 'bridge-state-v2/demo'))

    def test_populated_bootstrap_incremental_and_manual_branch(self):
        f = self.fixture(customer={'shared.txt': 'customer', 'new.txt': 'new', 'customer-private/keep': 'secret'},
                         internal={'shared.txt': 'internal', 'delete.txt': 'remove', 'private/key': 'keep'})
        f.run()
        first = f.state()
        files = f.contents('internal', first['branch'])
        self.assertEqual(set(files), {'shared.txt', 'new.txt', 'private/key'})
        self.assertEqual(f.git.head(f.repos['internal'], 'main'), f.anchor)
        f.commit('customer', {'new.txt': None, 'next.txt': 'next'})
        f.run()
        second = f.state()
        self.assertEqual(first['branch'], second['branch'])
        self.assertTrue(f.git.ancestor(first['generated'], second['generated']))
        manual = f.commit('internal', {'manual.txt': 'manual'}, branch=second['branch'])
        f.commit('customer', {'third.txt': 'third'})
        f.run()
        third = f.state()
        self.assertNotEqual(second['branch'], third['branch'])
        self.assertEqual(f.git.head(f.repos['internal'], second['branch']), manual)
        self.assertNotIn('manual.txt', f.contents('internal', third['branch']))
        self.assertTrue(f.git.ancestor(second['generated'], third['generated']))

    def test_equal_and_filtered_empty_bootstrap(self):
        f = self.fixture(customer={'shared.txt': 'internal', 'customer-private/key': 'keep'})
        f.run()
        self.assertIsNone(f.state()['generated'])
        self.assertEqual(f.state()['mode'], 'ready')
        f.commit('customer', {'shared.txt': None})
        f.run()
        self.assertNotIn('shared.txt', f.contents('internal', f.state()['branch']))
        with tempfile.TemporaryDirectory() as d:
            g = Fixture(d, customer={'customer-private/key': 'keep'})
            g.run()
            self.assertEqual(g.state()['mode'], 'awaiting_export')
            g.run('export')
            self.assertIn('customer-private/key', g.contents('customer'))

    def test_export_preserves_unobserved_changes_resets_epoch_and_late_integration(self):
        f = self.fixture()
        f.run('export')
        f.commit('customer', {'unmerged.txt': 'retain me'})
        f.run('export')
        old_branch = 'bridge/demo/import-1'
        old = f.git.head(f.repos['internal'], old_branch)
        self.assertIn('unmerged.txt', f.contents('internal', old_branch))
        self.assertNotIn('unmerged.txt', f.contents('customer'))
        f.commit('customer', {'later.txt': 'later'})
        f.run()
        new = f.state()
        self.assertNotEqual(old_branch, new['branch'])
        self.assertNotIn('unmerged.txt', f.contents('internal', new['branch']))
        f.commit('internal', {'unmerged.txt': 'retain me'}) # team squash integration
        f.run('export')
        self.assertIn('unmerged.txt', f.contents('customer'))
        self.assertEqual(f.git.head(f.repos['internal'], old_branch), old)

    def test_current_rules_include_existing_and_exclude_previous(self):
        f = self.fixture(customer={'shared.txt': 'internal', 'private/key': 'customer-secret'})
        f.run()
        f.script.write_text('set -eu\nrm -rf -- "$1/.github" "$1/customer-private"\n')
        f.run()
        state = f.state()
        self.assertEqual(f.contents('internal', state['branch'])['private/key'][1], b'customer-secret')
        f.script.write_text('set -eu\nrm -rf -- "$1/private" "$1/shared.txt"\n')
        f.commit('customer', {'private/key': 'new-private', 'shared.txt': 'new-shared', 'visible': 'yes'})
        f.run()
        files = f.contents('internal', f.state()['branch'])
        self.assertEqual(files['private/key'][1], b'customer-secret')
        self.assertEqual(files['shared.txt'][1], b'internal')
        self.assertIn('visible', files)

    def test_binary_modes_symlinks_names_and_file_directory_transitions(self):
        f = self.fixture(customer={'shared.txt': 'customer', 'binary': b'\x00\xff\n',
            'run': ('100755', '#!/bin/sh\n'), 'link': ('120000', 'shared.txt'), 'odd\n\t name': 'yes'})
        f.run()
        files = f.contents('internal', f.state()['branch'])
        for path, entry in f.contents('customer').items():
            self.assertEqual(files[path], entry)
        f.commit('customer', {'binary': None, 'binary/child': 'nested'})
        f.run()
        self.assertIn('binary/child', f.contents('internal', f.state()['branch']))

    def test_sanitizer_failures_do_not_advance_state(self):
        f = self.fixture(customer={'shared.txt': 'internal'})
        f.run()
        state = f.state()
        for script in ('exit 9\n', 'echo rewrite > "$1/shared.txt"\n', 'touch "$1/addition"\n'):
            f.script.write_text(script)
            with self.assertRaises(BridgeError):
                f.run()
            self.assertEqual(f.state(), state)

    def test_recovery_after_destination_push_before_state_commit(self):
        for action in ('import', 'export'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as d:
                f = Fixture(d, customer={'shared.txt': 'customer'})
                f.run()
                f.commit('customer', {'new': 'new'})
                original = f.git.push
                published = []
                def failing(url, branch, sha, expected):
                    if url == f.repos['bridge'] and published:
                        raise BridgeError('Injected state outage')
                    original(url, branch, sha, expected)
                    if url == f.repos['internal' if action == 'import' else 'customer'] and not branch.startswith('sanitizer-bridge-stage/'):
                        published.append(sha)
                # For export first preserve changes, so the fault lands after customer publication.
                if action == 'export':
                    f.run()
                with patch.object(f.git, 'push', failing), self.assertRaises(BridgeError):
                    f.run(action)
                self.assertIsNotNone(f.state()['pending'])
                f.run(action)
                self.assertIsNone(f.state()['pending'])
                if action == 'import':
                    self.assertEqual(f.state()['generated'], published[0])
                else:
                    self.assertEqual(f.state()['checkpoint'], published[0])

    def test_customer_race_is_preserved_and_export_retries(self):
        f = self.fixture()
        f.run('export')
        f.commit('internal', {'shared.txt': 'new export'})
        original = f.git.push
        raced = []
        def racing(url, branch, sha, expected):
            if url == f.repos['customer'] and not raced:
                raced.append(True)
                f.commit('customer', {'racing.txt': 'raced'})
            return original(url, branch, sha, expected)
        with patch.object(f.git, 'push', racing):
            f.run('export')
        self.assertIn('racing.txt', f.contents('internal', 'bridge/demo/import-1'))
        self.assertNotIn('racing.txt', f.contents('customer'))

    def test_import_write_boundary_race_and_deleted_branch(self):
        f = self.fixture(customer={'shared.txt': 'first'})
        f.run()
        old = f.state()
        f.commit('customer', {'shared.txt': 'second'})
        original = f.git.push
        raced = []
        def racing(url, branch, sha, expected):
            if url == f.repos['internal'] and branch == old['branch'] and not raced:
                raced.append(True)
                f.commit('internal', {'manual': 'keep'}, branch)
            return original(url, branch, sha, expected)
        with patch.object(f.git, 'push', racing):
            f.run()
        state = f.state()
        self.assertNotEqual(old['branch'], state['branch'])
        self.assertNotIn('manual', f.contents('internal', state['branch']))
        # Synthetic remote branch deletion; the bridge must allocate a new name.
        f.git.run('push', f.repos['internal'], ':refs/heads/' + state['branch'])
        f.run('export')
        self.assertEqual(f.git.head(f.repos['internal'], 'bridge/demo/import-3'), state['generated'])

    def test_two_projects_and_state_cas(self):
        f = self.fixture(customer={'shared.txt': 'customer'})
        f.run()
        first = f.state()
        other = copy.deepcopy(f.config)
        other['project'] = 'other'
        Engine(f.git, other, f.repos['bridge']).run('import')
        self.assertEqual(f.state(), first)
        stale, current = f.engine(), f.engine()
        state = copy.deepcopy(current.state)
        state['mode'] = 'awaiting_export'
        current.save(state)
        stale_state = copy.deepcopy(stale.state)
        stale_state['sequence'] += 10
        with self.assertRaises(BridgeError):
            stale.save(stale_state)

    def test_force_push_suspends_until_export_and_excluded_collision(self):
        f = self.fixture(customer={'shared.txt': 'customer'})
        f.run()
        state = f.state()
        rewritten = f.git.commit({}, [], 'Synthetic rewrite')
        f.git.run('push', '--force', f.repos['customer'], rewritten + ':refs/heads/main')
        self.assertEqual(f.run()['status'], 'imports-suspended')
        self.assertEqual(f.state()['generated'], state['generated'])
        self.assertIsNotNone(f.state()['suspension'])
        f.run('export')
        self.assertIsNone(f.state()['suspension'])
        self.assertEqual(f.state()['base'], f.anchor)
        from bridge.filtering import overlay
        with self.assertRaisesRegex(BridgeError, 'collision'):
            overlay({'dir/private': ('100644', 'x')}, {}, {'dir': ('100644', 'y')})

    def test_both_projections_empty_and_unsupported_lfs(self):
        f = self.fixture(internal={'private/key': 'only excluded'}, customer={'customer-private/key': 'only excluded'})
        f.run()
        self.assertEqual(f.state()['mode'], 'ready')
        self.assertIsNone(f.state()['generated'])
        f.commit('customer', {'large': 'version https://git-lfs.github.com/spec/v1\noid sha256:123\nsize 12\n'})
        before = f.state()
        with self.assertRaisesRegex(BridgeError, 'LFS'):
            f.run()
        self.assertEqual(f.state(), before)

    def test_recovery_from_fresh_object_database(self):
        f = self.fixture(customer={'shared.txt': 'customer'})
        original = f.git.push
        def fail_destination(url, branch, sha, expected):
            if url == f.repos['internal'] and not branch.startswith('sanitizer-bridge-stage/'):
                raise BridgeError('Injected destination outage')
            return original(url, branch, sha, expected)
        with patch.object(f.git, 'push', fail_destination), self.assertRaises(BridgeError):
            f.run()
        pending = f.state()['pending']
        self.assertIsNotNone(pending)
        with tempfile.TemporaryDirectory() as d:
            recovered = Engine(Git(d), f.config, f.repos['bridge'])
            recovered.run('import')
            self.assertEqual(recovered.state['generated'], pending['commit'])
            self.assertIsNone(recovered.state['pending'])

    def test_export_source_is_captured_once(self):
        f = self.fixture()
        f.run('export')
        expected = f.commit('internal', {'shared.txt': 'captured'})
        engine = f.engine()
        original = engine.capture
        advanced = []
        def capture(role):
            sha = original(role)
            if role == 'internal' and not advanced:
                advanced.append(f.commit('internal', {'shared.txt': 'next export'}))
            return sha
        with patch.object(engine, 'capture', capture):
            result = engine.run('export')
        self.assertEqual(result['internal'], expected)
        self.assertEqual(f.contents('customer')['shared.txt'][1], b'captured')
        f.run('export')
        self.assertEqual(f.contents('customer')['shared.txt'][1], b'next export')

    def test_observed_racing_customer_revision_cannot_be_silently_rewound(self):
        f = self.fixture()
        f.run('export')
        checkpoint = f.state()['checkpoint']
        f.commit('internal', {'shared.txt': 'new internal'})
        engine = f.engine()
        original_push, original_capture = f.git.push, engine.capture
        raced = []
        def racing(url, branch, sha, expected):
            if url == f.repos['customer'] and not raced:
                raced.append(True)
                f.commit('customer', {'raced': 'must remain observable'})
            return original_push(url, branch, sha, expected)
        def rewind(role):
            if role == 'customer' and engine.state.get('raced_customer'):
                f.git.run('push', '--force', f.repos['customer'], checkpoint + ':refs/heads/main')
            return original_capture(role)
        with patch.object(f.git, 'push', racing), patch.object(engine, 'capture', rewind):
            engine.run('export')
        self.assertIsNone(f.state()['suspension'])
        self.assertNotEqual(f.state()['checkpoint'], checkpoint)

    def test_submodule_rejected_without_advancing_state(self):
        f = self.fixture(customer={'shared.txt': 'internal'})
        f.run()
        before = f.state()
        parent = f.git.head(f.repos['customer'], 'main')
        files = f.git.files(parent)
        files['module'] = ('160000', f.anchor)
        commit = f.git.commit(files, [parent], 'Synthetic gitlink')
        f.git.push(f.repos['customer'], 'main', commit, parent)
        with self.assertRaisesRegex(BridgeError, 'submodule'):
            f.run()
        self.assertEqual(f.state(), before)

    def test_project_configuration_and_binding_validation(self):
        from bridge.config import load
        import yaml
        f = self.fixture(customer={'shared.txt': 'internal'})
        root = Path(self.tmp.name)
        directory = root / 'projects' / 'demo'
        directory.mkdir(parents=True)
        cfg = {key: value for key, value in f.config.items() if key not in ('script', 'project')}
        cfg.update(sanitizer='sanitization.sh', detection={'polling': True, 'notification': False},
                   secrets={role: 'TEST_TOKEN' for role in ('bridge', 'internal', 'customer')})
        path = directory / 'config.yaml'
        path.write_text(yaml.safe_dump(cfg))
        self.assertEqual(load(root, 'demo', local=True)['script'], f.script)
        cfg['enabled'] = 'false'
        path.write_text(yaml.safe_dump(cfg))
        with self.assertRaisesRegex(BridgeError, 'enabled'):
            load(root, 'demo', local=True)
        f.run()
        changed = copy.deepcopy(f.config)
        changed['customer']['branch'] = 'other-branch'
        with self.assertRaisesRegex(BridgeError, 'different configuration'):
            Engine(f.git, changed, f.repos['bridge'])


if __name__ == '__main__':
    unittest.main()
