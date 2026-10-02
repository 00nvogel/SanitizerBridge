"""Durable per-project transactions across independent Git repositories."""
import copy
import json
from .filtering import overlay, projection
from .git import BridgeError


class Engine:
    def __init__(self, git, config, bridge_url):
        self.git, self.config, self.bridge = git, config, bridge_url
        self.project = config['project']
        self.state_branch = 'bridge-state/' + self.project
        self.revision = git.head(bridge_url, self.state_branch)
        git.fetch(bridge_url, self.revision)
        self.state = json.loads(git.run('show', self.revision + ':state.json')) if self.revision else None
        self.binding = {role: {k: config[role][k] for k in ('repository', 'branch')}
                        for role in ('internal', 'customer')}
        self.binding['bootstrap_internal_commit'] = config['bootstrap_internal_commit']
        if self.state and (self.state.get('schema') != 1 or self.state['binding'] != self.binding):
            raise BridgeError('Existing state belongs to a different configuration; register a new project')

    def save(self, state, extra=()):
        if state == self.state and not extra:
            return
        refs = [self.revision, *extra]
        def collect(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in ('base', 'checkpoint', 'observed', 'raced_customer', 'generated', 'commit', 'expected', 'source') and item:
                        refs.append(item)
                    elif isinstance(item, (dict, list)):
                        collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)
        collect(state)
        blob = self.git.blob((json.dumps(state, sort_keys=True, indent=2) + '\n').encode())
        sha = self.git.commit({'state.json': ('100644', blob)}, refs,
                              'Bridge state: ' + self.project)
        self.git.push(self.bridge, self.state_branch, sha, self.revision)
        self.state, self.revision = copy.deepcopy(state), sha

    def capture(self, role):
        config = self.config[role]
        return self.git.fetch(config['url'], self.git.head(config['url'], config['branch']))

    def filtered(self, files):
        return projection(self.git, files, self.config['script'])

    def initialize(self, customer):
        base = self.config['bootstrap_internal_commit']
        self.git.fetch(self.config['internal']['url'], base)
        included_internal = self.filtered(self.git.files(base))
        included_customer = self.filtered(self.git.files(customer))
        mode = 'ready' if included_internal == included_customer else (
            'awaiting_export' if not included_customer else 'ready')
        self.save({'schema': 1, 'binding': self.binding, 'base': base, 'checkpoint': customer,
                   'observed': customer, 'generated': None, 'branch': None,
                   'sequence': 0, 'mode': mode, 'pending': None}, [customer, base])

    def check_customer(self, customer):
        if not self.git.ancestor(self.state.get('raced_customer') or self.state['observed'], customer):
            raise BridgeError('Customer history was deleted or rewritten; preserved state retained. Restore history or register a new project.')

    def available_branch(self, start):
        url = self.config['internal']['url']
        for sequence in range(start, start + 1000):
            branch = f'bridge/{self.project}/import-{sequence}'
            if not self.git.head(url, branch):
                return branch, sequence
        raise BridgeError('No available import branch in the next 1000 names')

    def recover(self):
        """Replay the saved exact commit; never infer success from tree equality."""
        for _ in range(5):
            pending = self.state.get('pending')
            if not pending:
                return
            role = pending['role']
            url = self.config[role]['url']
            actual = self.git.fetch(url, self.git.head(url, pending['branch']))
            if actual == pending['commit'] or (actual and self.git.ancestor(pending['commit'], actual)):
                self.save(pending['next'])
                return
            if actual != pending['expected']:
                if role == 'customer':
                    # A competing customer append is preserved by the next import before retry.
                    if not self.git.ancestor(pending['expected'], actual):
                        raise BridgeError('Customer history rewritten during export; pending transaction retained')
                    state = copy.deepcopy(self.state)
                    state['pending'] = None
                    state['raced_customer'] = actual
                    self.save(state, [actual])
                    return
                branch, sequence = self.available_branch(pending['next']['sequence'] + 1)
                state = copy.deepcopy(self.state)
                state['pending']['branch'] = branch
                state['pending']['expected'] = None
                state['pending']['next']['branch'] = branch
                state['pending']['next']['sequence'] = sequence
                self.save(state)
                continue
            try:
                self.git.push(url, pending['branch'], pending['commit'], pending['expected'])
            except BridgeError as error:
                if self.git.head(url, pending['branch']) != actual:
                    continue
                raise BridgeError('Destination publication failed; pending transaction retained. Check Contents/Workflows write access and branch protection. ' + str(error)) from error
            self.save(pending['next'])
            return
        raise BridgeError('Publication raced repeatedly; retry the operation')

    def publish(self, role, branch, expected, commit, next_state, source):
        state = copy.deepcopy(self.state)
        state['pending'] = {'role': role, 'branch': branch, 'expected': expected,
                            'commit': commit, 'source': source, 'next': next_state}
        self.save(state)
        self.recover()

    def import_customer(self, customer):
        self.check_customer(customer)
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
                # Keep a usable published branch even when a user removed/replaced the old one.
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
        # Capture the export source once, even if customer publication needs retries.
        internal = self.capture('internal') if action == 'export' else None
        if action == 'export' and not internal:
            raise BridgeError('Configured internal branch has no head')
        self.recover() if self.state else None
        customer = self.capture('customer')
        if not self.state:
            self.initialize(customer)
        if action == 'import':
            self.import_customer(customer)
            return {'action': action, 'customer': customer, 'state': self.state}
        if not self.git.ancestor(self.state['base'], internal):
            raise BridgeError('Internal branch no longer descends from the pinned base; preserved state retained')
        for _ in range(5):
            self.import_customer(customer)
            before = self.git.files(customer)
            desired = overlay(before, self.filtered(before), self.filtered(self.git.files(internal)))
            state = copy.deepcopy(self.state)
            state.update(base=internal, generated=None, branch=None, mode='ready', pending=None)
            if desired == before:
                # There is no destination write; recheck head before adopting the correspondence.
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
        raise BridgeError('Customer kept advancing; preserved work retained. Retry export.')
