import os
from pathlib import Path
import re
import subprocess
import yaml
from .git import BridgeError


def load(root, project, local=False):
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', project):
        raise BridgeError('Invalid project identifier')
    root = Path(root).resolve()
    config = yaml.safe_load((root / 'projects' / project / 'config.yaml').read_text())
    if not isinstance(config, dict):
        raise BridgeError('Project config must be a mapping')
    for role in ('internal', 'customer'):
        entry = config[role]
        branch = entry['branch']
        if subprocess.run(['git', 'check-ref-format', 'refs/heads/' + branch],
                          capture_output=True).returncode:
            raise BridgeError('Invalid branch: ' + branch)
        repo = entry['repository']
        if local and Path(repo).is_absolute():
            entry['url'] = repo
        elif re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
            entry['url'] = 'https://github.com/' + repo + '.git'
        else:
            raise BridgeError('Expected GitHub owner/repository')
    anchor = config['bootstrap_internal_commit']
    if not isinstance(anchor, str) or not re.fullmatch(r'[0-9a-f]{40}', anchor):
        raise BridgeError('bootstrap_internal_commit must be an exact 40-character SHA')
    script = (root / config['sanitizer']).resolve()
    if not script.is_relative_to(root) or not script.is_file():
        raise BridgeError('Sanitizer must be a file inside the trusted bridge checkout')
    config['script'] = script
    for name in ('polling', 'notification'):
        if type(config.get('detection', {}).get(name)) is not bool:
            raise BridgeError('detection.' + name + ' must be true or false')
    for role in ('bridge', 'internal', 'customer'):
        secret = config['secrets'][role]
        if not isinstance(secret, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]*', secret):
            raise BridgeError('Invalid Actions secret reference')
    config['project'] = project
    return config
