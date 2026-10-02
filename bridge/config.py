from pathlib import Path
import os
import re
import subprocess
import yaml
from .git import BridgeError


def load(root, project=None, local=False, embedded=False):
    if embedded and project is not None:
        raise BridgeError('Embedded installation manages exactly one project; --project is not supported')
    if not embedded and (not project or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', project)):
        raise BridgeError('Invalid project identifier')
    root = Path(root).resolve()
    path = root / 'config.yaml' if embedded else root / 'projects' / project / 'config.yaml'
    config = yaml.safe_load(path.read_text())
    if not isinstance(config, dict):
        raise BridgeError('Project config must be a mapping')
    if type(config.get('enabled', True)) is not bool:
        raise BridgeError('enabled must be true or false')
    if embedded and ('projects' in config or 'project' in config):
        raise BridgeError('Embedded configuration must describe exactly one pairing, without a projects section')
    for role in ('internal', 'customer'):
        entry = config[role]
        if not isinstance(entry, dict):
            raise BridgeError(role + ' must be a mapping')
        branch = entry['branch']
        if not isinstance(branch, str) or subprocess.run(['git', 'check-ref-format', 'refs/heads/' + branch],
                          capture_output=True).returncode:
            raise BridgeError('Invalid branch: ' + repr(branch))
        repo = entry.get('repository')
        if embedded and role == 'internal' and not repo:
            repo = os.environ.get('GITHUB_REPOSITORY')
            entry['repository'] = repo
        if not isinstance(repo, str):
            raise BridgeError('Expected repository string')
        if local and Path(repo).is_absolute():
            entry['url'] = repo
        elif re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
            entry['url'] = 'https://github.com/' + repo + '.git'
        else:
            raise BridgeError('Expected GitHub owner/repository')
    anchor = config['bootstrap_internal_commit']
    if not isinstance(anchor, str) or not re.fullmatch(r'[0-9a-f]{40}', anchor):
        raise BridgeError('bootstrap_internal_commit must be an exact 40-character SHA')
    if not isinstance(config['sanitizer'], str):
        raise BridgeError('sanitizer must be a path string')
    script = (root / config['sanitizer']).resolve()
    if not script.is_relative_to(root) or not script.is_file():
        raise BridgeError('Sanitizer must be a file inside the trusted bridge checkout')
    config['script'] = script
    detection = config.get('detection')
    if not isinstance(detection, dict) or not isinstance(config.get('secrets'), dict):
        raise BridgeError('detection and secrets must be mappings')
    if embedded:
        config['secrets']['bridge'] = config['secrets']['internal']
    for name in ('polling', 'notification'):
        if type(detection.get(name)) is not bool:
            raise BridgeError('detection.' + name + ' must be true or false')
    for role in ('bridge', 'internal', 'customer'):
        secret = config['secrets'][role]
        if not isinstance(secret, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]*', secret):
            raise BridgeError('Invalid Actions secret reference')
    config['project'] = 'embedded' if embedded else project
    config['mode'] = 'embedded' if embedded else 'standalone'
    return config
