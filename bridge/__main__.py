import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import yaml
from .config import load
from .engine import Engine
from .git import Git, BridgeError
from .github import credentials


def main():
    parser = argparse.ArgumentParser(description='Independent-history Git sanitizer bridge')
    parser.add_argument('action', choices=['import', 'export', 'matrix'])
    parser.add_argument('--root', default='.')
    parser.add_argument('--project')
    parser.add_argument('--trigger', choices=['poll', 'notification', 'manual'], default='manual')
    parser.add_argument('--bridge-url', default=os.environ.get('BRIDGE_URL'))
    parser.add_argument('--local', action='store_true', help='Allow local fixture repositories without authentication')
    args = parser.parse_args()
    try:
        if args.action == 'matrix':
            projects = [args.project] if args.project else sorted(p.name for p in (Path(args.root) / 'projects').iterdir() if (p / 'config.yaml').exists())
            matrix = []
            for project in projects:
                cfg = load(args.root, project, args.local)
                if cfg.get('enabled', True) is False:
                    continue
                method = {'poll': 'polling', 'notification': 'notification'}.get(args.trigger)
                if method and not cfg['detection'][method]:
                    continue
                matrix.append({'project': project, **{role + '_secret': cfg['secrets'][role] for role in ('bridge', 'internal', 'customer')}})
            print(json.dumps({'include': matrix}))
            return
        if not args.project or not args.bridge_url:
            raise BridgeError('--project and --bridge-url are required')
        config = load(args.root, args.project, args.local)
        env = {} if args.local else credentials([(args.bridge_url, os.environ.get('BRIDGE_TOKEN')),
            (config['internal']['url'], os.environ.get('INTERNAL_TOKEN')),
            (config['customer']['url'], os.environ.get('CUSTOMER_TOKEN'))])
        with tempfile.TemporaryDirectory(prefix='bridge-git-') as directory:
            engine = Engine(Git(directory, env), config, args.bridge_url)
            print(json.dumps(engine.run(args.action), sort_keys=True))
    except (BridgeError, KeyError, ValueError, OSError, yaml.YAMLError) as error:
        print('Bridge failed: ' + str(error), file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
