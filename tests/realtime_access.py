#!/usr/bin/env python3
"""An existing realtime subscription loses access on disable and stays revoked."""
import argparse
import json
import queue
import threading
import urllib.request

from auth_server import server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    args = parser.parse_args()
    with server(args.binary) as request:
        admin = request('POST', '/api/collections/_superusers/auth-with-password', {
            'identity': 'admin@example.com', 'password': 'SyntheticAdminPassword123!',
        })['token']
        credentials = {'identity': 'realtime@example.test', 'password': 'SyntheticRealtimePassword123!'}
        user = request('POST', '/api/collections/users/records', {
            'email': credentials['identity'], 'name': 'Realtime',
            'password': credentials['password'], 'passwordConfirm': credentials['password'],
        }, admin)
        user_path = '/api/collections/users/records/' + user['id']
        token = request('POST', '/api/collections/users/auth-with-password', credentials)['token']
        messages = queue.Queue()

        def listen():
            try:
                with urllib.request.urlopen(request.base_url + '/api/realtime', timeout=10) as response:
                    event, data = '', ''
                    for raw in response:
                        line = raw.decode().strip()
                        if line.startswith('event:'):
                            event = line[6:].strip()
                        elif line.startswith('data:'):
                            data = line[5:].strip()
                        elif not line and event:
                            messages.put((event, json.loads(data)))
                            event, data = '', ''
            except Exception as error:
                messages.put(('error', str(error)))

        threading.Thread(target=listen, daemon=True).start()
        event, data = messages.get(timeout=5)
        assert event == 'PB_CONNECT', (event, data)
        client_id = data['clientId']
        subscriptions = {'clientId': client_id, 'subscriptions': ['organizations/*']}
        request('POST', '/api/realtime', subscriptions, token, expected=204)
        project = request('POST', '/api/collections/organizations/records', {'name': 'Before disable', 'owner': user['id']}, admin)
        event, data = messages.get(timeout=5)
        assert event.startswith('organizations/') and data['record']['id'] == project['id'], (event, data)
        project_path = '/api/collections/organizations/records/' + project['id']
        for disabled in (True, False):
            request('PATCH', user_path, {'disabled': disabled}, admin)
            project = request('PATCH', project_path, {
                'name': 'Disabled' if disabled else 'Reenabled', 
            }, admin)
            try:
                unexpected = messages.get(timeout=0.5)
                raise AssertionError(('Revoked subscriber received data', unexpected))
            except queue.Empty:
                pass
            request('POST', '/api/realtime', subscriptions, token, expected=401)
        fresh = request('POST', '/api/collections/users/auth-with-password', credentials)['token']
        request('POST', '/api/realtime', subscriptions, fresh, expected=204)
        project = request('PATCH', project_path, {'name': 'Fresh login'}, admin)
        event, data = messages.get(timeout=5)
        assert data['record']['name'] == 'Fresh login', (event, data)
    print('PASS: realtime subscriptions lose access on disable and require fresh login after re-enable')


if __name__ == '__main__':
    main()
