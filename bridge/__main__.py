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
    parser.add_argument('action', choices=['import', 'export', 'migrate', 'matrix', 'settings'])
    parser.add_argument('--root', default='.')
    parser.add_argument('--project')
    parser.add_argument('--embedded', action='store_true', help='Use one config.yaml and store metadata in the internal repository')
    parser.add_argument('--trigger', choices=['poll', 'notification', 'manual'], default='manual')
    parser.add_argument('--bridge-url', default=os.environ.get('BRIDGE_URL'))
    parser.add_argument('--local', action='store_true', help='Allow local fixture repositories without authentication')
    args = parser.parse_args()
    try:
        if args.embedded and args.project:
            raise BridgeError('Embedded installation manages exactly one project; --project is not supported')
        if args.action == 'settings':
            cfg = load(args.root, args.project, args.local, args.embedded)
            method = {'poll': 'polling', 'notification': 'notification'}.get(args.trigger)
            active = cfg.get('enabled', True) and (not method or cfg['detection'][method])
            print(json.dumps({'active': active, **{role + '_secret': cfg['secrets'][role]
                              for role in ('bridge', 'internal', 'customer')}}))
            return
        if args.action == 'matrix':
            if args.embedded:
                raise BridgeError('Embedded installation has one configuration; use settings instead of a project matrix')
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
        if not args.embedded and (not args.project or not args.bridge_url):
            raise BridgeError('--project and --bridge-url are required')
        config = load(args.root, args.project, args.local, args.embedded)
        if not config.get('enabled', True):
            raise BridgeError('Configuration is disabled; complete setup and set enabled: true')
        bridge_url = config['internal']['url'] if args.embedded else args.bridge_url
        if args.embedded and args.bridge_url and args.bridge_url != bridge_url:
            raise BridgeError('Embedded metadata must be stored in the internal repository')
        bridge_token = os.environ.get('INTERNAL_TOKEN') if args.embedded else os.environ.get('BRIDGE_TOKEN')
        env = {} if args.local else credentials([(bridge_url, bridge_token),
            (config['internal']['url'], os.environ.get('INTERNAL_TOKEN')),
            (config['customer']['url'], os.environ.get('CUSTOMER_TOKEN'))])
        with tempfile.TemporaryDirectory(prefix='bridge-git-') as directory:
            engine = Engine(Git(directory, env), config, bridge_url)
            print(json.dumps(engine.run(args.action), sort_keys=True))
    except (BridgeError, KeyError, ValueError, OSError, yaml.YAMLError) as error:
        print('Bridge failed: ' + str(error), file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
