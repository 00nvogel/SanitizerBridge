"""Metadata-only coordination; project objects stay in their project repositories."""
import copy
import json
import uuid
from .filtering import overlay, projection
from .git import BridgeError


class Engine:
    def __init__(self, git, config, bridge_url):
        self.git, self.config, self.bridge = git, config, bridge_url
        self.project = config['project']
        self.embedded = config.get('mode') == 'embedded'
        self.state_branch = ('sanitizer-bridge/state-v2' if self.embedded
                             else 'bridge-state-v2/' + self.project)
        self.revision = git.head(bridge_url, self.state_branch)
        git.fetch(bridge_url, self.revision)
        self.state = json.loads(git.run('show', self.revision + ':state.json')) if self.revision else None
        self.binding = {role: {k: config[role][k] for k in ('repository', 'branch')}
                        for role in ('internal', 'customer')}
        self.binding['bootstrap_internal_commit'] = config['bootstrap_internal_commit']
        if self.state and (self.state.get('schema') != 2 or self.state['binding'] != self.binding):
            raise BridgeError('Existing state belongs to a different configuration; explicitly reinitialize with a new state namespace')
        self.legacy_branch = None
        if not self.state and not self.embedded and git.head(bridge_url, 'bridge-state/' + self.project):
            self.legacy_branch = 'bridge-state/' + self.project

    def save(self, state):
        if state == self.state:
            return
        blob = self.git.blob((json.dumps(state, sort_keys=True, indent=2) + '\n').encode())
        # Never attach project commits as parents, including during migration.
        sha = self.git.commit({'state.json': ('100644', blob)}, [self.revision],
                              'Bridge state: ' + self.project)
        self.git.push(self.bridge, self.state_branch, sha, self.revision)
        self.state, self.revision = copy.deepcopy(state), sha

    def migrate(self):
        if self.state:
            return {'action': 'migrate', 'status': 'already-migrated'}
        if not self.legacy_branch:
            raise BridgeError('No legacy state exists for this project')
        revision = self.git.fetch(self.bridge, self.git.head(self.bridge, self.legacy_branch))
        old = json.loads(self.git.run('show', revision + ':state.json'))
        if old.get('schema') != 1 or old['binding'] != self.binding:
            raise BridgeError('Legacy state does not match this configuration')
        if old['pending']:
            raise BridgeError('Complete the pending operation with v1 before migrating')
        state = {key: old[key] for key in ('binding', 'base', 'checkpoint', 'observed',
                 'generated', 'branch', 'sequence', 'mode')}
        state.update(schema=2, pending=None, cleanup=[], suspension=None)
        if old.get('raced_customer'):
            state['raced_customer'] = old['raced_customer']
        self.save(state)
        return {'action': 'migrate', 'status': 'migrated', 'state_branch': self.state_branch,
                'legacy_branch_retained': self.legacy_branch}

    def capture(self, role):
        config = self.config[role]
        return self.git.fetch(config['url'], self.git.head(config['url'], config['branch']))

    def require_internal(self):
        for sha in dict.fromkeys((self.state['base'], self.state['generated'])):
            if sha:
                try:
                    self.git.fetch(self.config['internal']['url'], sha)
                except BridgeError as error:
                    raise BridgeError('Required internal commit is unavailable: ' + sha
                        + '. Restore its availability in the internal repository or explicitly reinitialize. '
                        + str(error)) from error

    def filtered(self, files):
        if self.embedded:
            files = {path: value for path, value in files.items()
                     if path.split('/')[0] not in ('.sanitizer-bridge', '.github')}
        return projection(self.git, files, self.config['script'])

    def initialize(self, customer):
        base = self.config['bootstrap_internal_commit']
        self.git.fetch(self.config['internal']['url'], base)
        included_internal = self.filtered(self.git.files(base))
        included_customer = self.filtered(self.git.files(customer))
        mode = 'ready' if included_internal == included_customer else (
            'awaiting_export' if not included_customer else 'ready')
        self.save({'schema': 2, 'binding': self.binding, 'base': base, 'checkpoint': customer,
                   'observed': customer, 'generated': None, 'branch': None,
                   'sequence': 0, 'mode': mode, 'pending': None, 'cleanup': [], 'suspension': None})

    def suspend(self, reason):
        if not self.state['suspension']:
            state = copy.deepcopy(self.state)
            state['suspension'] = {'reason': reason}
            self.save(state)

    def check_customer(self, customer):
        if self.state['suspension']:
            return False
        for key in ('checkpoint', 'observed', 'raced_customer'):
            if not self.git.ancestor(self.state.get(key), customer):
                self.suspend('Customer history was deleted, rewritten, or a required commit became unavailable. '
                             'Imports resume after the next successful explicit export.')
                return False
        return True

    def available_branch(self, start):
        prefix = 'sanitizer-bridge/import-' if self.embedded else f'bridge/{self.project}/import-'
        for sequence in range(start, start + 1000):
            branch = prefix + str(sequence)
            if not self.git.head(self.config['internal']['url'], branch):
                return branch, sequence
        raise BridgeError('No available import branch in the next 1000 names')

    def cleanup(self):
        for entry in list(self.state['cleanup']):
            try:
                self.git.delete(self.config[entry['role']]['url'], entry['stage'], entry['commit'])
            except BridgeError as error:
                raise BridgeError('Publication state was saved, but staging cleanup needs retry: ' + str(error)) from error
            state = copy.deepcopy(self.state)
            state['cleanup'].remove(entry)
            self.save(state)

    def finish(self, pending, next_state):
        state = copy.deepcopy(next_state)
        state['pending'] = None
        state['cleanup'].append({key: pending[key] for key in ('role', 'stage', 'commit')})
        self.save(state)
        self.cleanup()

    def recover(self):
        """Fetch the prepared commit from its destination, then replay or recognize publication."""
        self.cleanup()
        for _ in range(5):
            pending = self.state['pending']
            if not pending:
                return
            role = pending['role']
            url = self.config[role]['url']
            actual = self.git.fetch(url, self.git.head(url, pending['branch']))
            if actual == pending['commit'] or (actual and self.git.ancestor(pending['commit'], actual)):
                self.finish(pending, pending['next'])
                return
            if actual != pending['expected'] and role == 'customer':
                state = copy.deepcopy(self.state)
                if not self.git.ancestor(pending['expected'], actual):
                    state['suspension'] = {'reason': 'Customer history changed during export; a successful explicit export is required.'}
                state['raced_customer'] = actual
                self.finish(pending, state)
                return
            staged = self.git.head(url, pending['stage'])
            if staged != pending['commit']:
                raise BridgeError('Prepared commit staging branch is missing or changed: ' + pending['stage']
                                  + '. Restore it before retrying.')
            self.git.fetch(url, staged)
            if actual != pending['expected']:
                branch, sequence = self.available_branch(pending['next']['sequence'] + 1)
                state = copy.deepcopy(self.state)
                state['pending']['branch'] = branch
                state['pending']['expected'] = None
                state['pending']['next'].update(branch=branch, sequence=sequence)
                self.save(state)
                continue
            try:
                self.git.push(url, pending['branch'], pending['commit'], pending['expected'])
            except BridgeError as error:
                if self.git.head(url, pending['branch']) != actual:
                    continue
                raise BridgeError('Destination publication failed; pending operation retained. Check Contents/Workflows '
                                  'write access and branch rules. ' + str(error)) from error
            self.finish(pending, pending['next'])
            return
        raise BridgeError('Publication raced repeatedly; retry the operation')

    def publish(self, role, branch, expected, commit, next_state, source):
        operation = uuid.uuid4().hex
        stage = f'sanitizer-bridge-stage/{self.project}/{operation}'
        # A crash before journaling may leave an orphan stage, never an unjournaled user-branch update.
        self.git.push(self.config[role]['url'], stage, commit, None)
        state = copy.deepcopy(self.state)
        state['pending'] = {'id': operation, 'role': role, 'stage': stage, 'branch': branch,
                            'expected': expected, 'commit': commit, 'source': source, 'next': next_state}
        self.save(state)
        self.recover()

    def import_customer(self, customer, recovery_export=False):
        if not self.check_customer(customer) and not recovery_export:
            return
        source = self.filtered(self.git.files(customer))
        if self.state['mode'] == 'awaiting_export' and not source:
            state = copy.deepcopy(self.state)
            state['observed'] = customer
            state.pop('raced_customer', None)
            self.save(state)
            return
        parent = self.state['generated'] or self.state['base']
        before = self.git.files(parent)
        desired = overlay(before, self.filtered(before), source)
        state = copy.deepcopy(self.state)
        state.pop('raced_customer', None)
        state.update(observed=customer, mode='ready')
        if desired == before:
            if state['generated'] and self.git.head(self.config['internal']['url'], state['branch']) != state['generated']:
                branch, sequence = self.available_branch(state['sequence'] + 1)
                state.update(branch=branch, sequence=sequence)
                self.publish('internal', branch, None, state['generated'], state, customer)
                return
            self.save(state)
            return
        branch, sequence = state['branch'], state['sequence']
        expected = state['generated']
        if not branch or self.git.head(self.config['internal']['url'], branch) != expected:
            branch, sequence = self.available_branch(sequence + 1)
            expected = None
        commit = self.git.commit(desired, [parent],
            f'Customer import ({self.project})\n\nCustomer-Revision: {customer or "empty"}')
        state.update(generated=commit, branch=branch, sequence=sequence)
        self.publish('internal', branch, expected, commit, state, customer)

    def run(self, action):
        if action == 'migrate':
            return self.migrate()
        if self.legacy_branch:
            raise BridgeError('Legacy archive state found. Run the migrate operation before syncing with v2.')
        internal = self.capture('internal') if action == 'export' else None
        if action == 'export' and not internal:
            raise BridgeError('Configured internal branch has no head')
        if self.state:
            self.recover()
        customer = self.capture('customer')
        if not self.state:
            self.initialize(customer)
        if action == 'import' and not self.check_customer(customer):
            return {'action': action, 'status': 'imports-suspended', 'reason': self.state['suspension']['reason']}
        self.require_internal()
        if action == 'import':
            self.import_customer(customer)
            return {'action': action, 'status': 'synchronized', 'customer': customer, 'state': self.state}
        if not self.git.ancestor(self.state['base'], internal):
            raise BridgeError('Internal branch no longer descends from the pinned base. Restore history or explicitly reinitialize.')
        for _ in range(5):
            self.import_customer(customer, recovery_export=True)
            before = self.git.files(customer)
            desired = overlay(before, self.filtered(before), self.filtered(self.git.files(internal)))
            state = copy.deepcopy(self.state)
            state.update(base=internal, generated=None, branch=None, mode='ready', pending=None, suspension=None)
            if desired == before:
                if self.git.head(self.config['customer']['url'], self.config['customer']['branch']) != customer:
                    customer = self.capture('customer')
                    continue
                state.update(checkpoint=customer, observed=customer)
                self.save(state)
                return {'action': action, 'internal': internal, 'customer': customer, 'changed': False}
            commit = self.git.commit(desired, [customer], 'Synchronize shared files')
            state.update(checkpoint=commit, observed=commit)
            self.publish('customer', self.config['customer']['branch'], customer, commit, state, internal)
            if self.state['checkpoint'] == commit:
                return {'action': action, 'internal': internal, 'customer': commit, 'changed': True}
            customer = self.capture('customer')
        raise BridgeError('Customer kept advancing; retry export. Imports and pending operations remain in their destination repositories.')
