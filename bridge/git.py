"""Git plumbing; no worktree checkout, user Git configuration, or content filters."""
import os
from pathlib import Path
import subprocess
import tempfile


class BridgeError(RuntimeError):
    pass


class Git:
    def __init__(self, directory, env=None):
        self.directory = str(directory)
        self.env = {**os.environ, **(env or {}), 'GIT_CONFIG_NOSYSTEM': '1',
                    'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0',
                    'GIT_AUTHOR_NAME': 'Sanitizer Bridge',
                    'GIT_AUTHOR_EMAIL': 'bridge@example.invalid',
                    'GIT_COMMITTER_NAME': 'Sanitizer Bridge',
                    'GIT_COMMITTER_EMAIL': 'bridge@example.invalid'}
        self.run('init', '--bare', self.directory)

    def run(self, *args, data=None, check=True, env=None):
        p = subprocess.run(['git', '-C', self.directory, *args], input=data,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env={**self.env, **(env or {})})
        if check and p.returncode:
            raise BridgeError(p.stderr.decode(errors='replace').strip())
        return p.stdout if check else p

    def head(self, url, branch):
        rows = self.run('ls-remote', '--heads', url, 'refs/heads/' + branch).splitlines()
        return rows[0].split()[0].decode() if rows else None

    def fetch(self, url, sha):
        if sha:
            self.run('fetch', '--no-tags', url, sha)
            self.run('cat-file', '-e', sha + '^{commit}')
        return sha

    def ancestor(self, older, newer):
        return not older or (bool(newer) and self.run(
            'merge-base', '--is-ancestor', older, newer, check=False).returncode == 0)

    def push(self, url, branch, sha, expected):
        ref = 'refs/heads/' + branch
        # Exact compare-and-swap, including creation. Callers only append to expected.
        if expected and not self.ancestor(expected, sha):
            raise BridgeError('Refusing non-append publication: ' + branch)
        p = self.run('push', '--porcelain', '--force-with-lease=' + ref + ':' + (expected or ''),
                     url, sha + ':' + ref, check=False)
        if p.returncode:
            raise BridgeError((p.stderr + p.stdout).decode(errors='replace').strip())

    def blob(self, data):
        return self.run('hash-object', '-w', '--stdin', data=data).decode().strip()

    def delete(self, url, branch, expected):
        """Delete only the exact staging reference owned by a completed operation."""
        actual = self.head(url, branch)
        if actual is None:
            return
        if actual != expected:
            raise BridgeError('Staging branch was changed; refusing to delete ' + branch)
        ref = 'refs/heads/' + branch
        self.run('push', '--force-with-lease=' + ref + ':' + expected, url, ':' + ref)

    def files(self, sha):
        if not sha:
            return {}
        result = {}
        for row in self.run('ls-tree', '-rz', '--full-tree', sha).split(b'\0'):
            if not row:
                continue
            info, path = row.split(b'\t', 1)
            mode, kind, oid = info.decode().split()
            path = os.fsdecode(path)
            if kind != 'blob' or mode not in ('100644', '100755', '120000'):
                raise BridgeError('Unsupported Git entry (including submodule): ' + path)
            if any(part in ('', '.', '..', '.git') for part in path.split('/')):
                raise BridgeError('Unsafe Git path: ' + repr(path))
            result[path] = (mode, oid)
        return result

    def tree(self, files):
        with tempfile.TemporaryDirectory() as d:
            env = {'GIT_INDEX_FILE': d + '/index'}
            self.run('read-tree', '--empty', env=env)
            data = b''.join(mode.encode() + b' ' + oid.encode() + b'\t' + os.fsencode(path) + b'\0'
                            for path, (mode, oid) in sorted(files.items()))
            self.run('update-index', '-z', '--index-info', data=data, env=env)
            return self.run('write-tree', env=env).decode().strip()

    def commit(self, files, parents, message):
        args = ['commit-tree', self.tree(files)]
        for parent in dict.fromkeys(p for p in parents if p):
            args.extend(['-p', parent])
        return self.run(*args, data=(message + '\n').encode()).decode().strip()

    def materialize(self, files, directory):
        for path, (mode, oid) in files.items():
            target = Path(directory) / path
            target.parent.mkdir(parents=True, exist_ok=True)
            content = self.run('cat-file', 'blob', oid)
            if content.startswith(b'version https://git-lfs.github.com/spec/v1\n'):
                raise BridgeError('Git LFS is unsupported: ' + path)
            if mode == '120000':
                target.symlink_to(os.fsdecode(content))
            else:
                target.write_bytes(content)
                target.chmod(0o755 if mode == '100755' else 0o644)
