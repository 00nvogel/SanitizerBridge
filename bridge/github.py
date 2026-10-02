"""GitHub-specific authentication and dispatch, separate from synchronization."""
import base64
import json
import urllib.error
import urllib.request
from .git import BridgeError


def credentials(urls):
    entries = {}
    for url, token in urls:
        if not token:
            raise BridgeError('Missing access token for ' + url)
        value = base64.b64encode(('x-access-token:' + token).encode()).decode()
        entries['http.' + url + '.extraheader'] = 'AUTHORIZATION: basic ' + value
    result = {'GIT_CONFIG_COUNT': str(len(entries))}
    for index, (key, value) in enumerate(entries.items()):
        result['GIT_CONFIG_KEY_' + str(index)] = key
        result['GIT_CONFIG_VALUE_' + str(index)] = value
    return result


def dispatch(repository, workflow, ref, inputs, token):
    url = f'https://api.github.com/repos/{repository}/actions/workflows/{workflow}/dispatches'
    request = urllib.request.Request(url, data=json.dumps({'ref': ref, 'inputs': inputs}).encode(),
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                 'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request) as response:
            return response.status
    except urllib.error.HTTPError as error:
        raise BridgeError('GitHub dispatch failed (%s); check Actions write access, workflow and ref' % error.code) from error
