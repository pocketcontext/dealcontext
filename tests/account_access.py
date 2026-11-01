#!/usr/bin/env python3
"""Disabled accounts cannot authenticate, use old tokens, or lose their history."""
import argparse

from auth_server import server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    args = parser.parse_args()
    with server(args.binary) as request:
        admin = request('POST', '/api/collections/_superusers/auth-with-password', {
            'identity': 'admin@example.com', 'password': 'SyntheticAdminPassword123!',
        })['token']
        credentials = {'identity': 'member@example.test', 'password': 'SyntheticMemberPassword123!'}
        user = request('POST', '/api/collections/users/records', {
            'email': credentials['identity'], 'name': 'Member', 'password': credentials['password'],
            'passwordConfirm': credentials['password'],
        }, admin)
        path = '/api/collections/users/records/' + user['id']
        token = request('POST', '/api/collections/users/auth-with-password', credentials)['token']
        project = request('POST', '/api/collections/organizations/records', {'name': 'Access test', 'owner': user['id']}, token)
        project_path = '/api/collections/organizations/records/' + project['id']
        # Users cannot manage their own access state, even if submitting the current value.
        for value in (True, False):
            request('PATCH', path, {'disabled': value}, token, expected=(400, 403, 404))
            request('POST', '/api/batch', {'requests': [
                {'method': 'PATCH', 'url': path, 'body': {'disabled': value}},
            ]}, token, expected=(400, 403))
        request('DELETE', path, token=admin, expected=(400, 403))
        request('PATCH', path, {'disabled': True}, admin)

        def rejected(old_token):
            for method, endpoint, body in [
                ('GET', '/api/context/schema', None),
                ('POST', '/api/context/query', {'sql': 'SELECT * FROM organizations'}),
                ('GET', '/api/collections/organizations/records', None),
                ('GET', project_path, None),
                ('POST', '/api/collections/organizations/records', {'name': 'Forbidden'}),
                ('PATCH', project_path, {'name': 'Forbidden'}),
                ('GET', path, None),
                ('POST', '/api/collections/users/auth-refresh', None),
                ('POST', '/api/files/token', None),
                ('POST', '/api/batch', {'requests': [
                    {'method': 'POST', 'url': '/api/collections/organizations/records', 'body': {'name': 'Forbidden'}},
                ]}),
            ]:
                request(method, endpoint, body, old_token, expected=(400, 401, 403, 404))

        rejected(token)
        request('POST', '/api/collections/users/auth-with-password', credentials, expected=(400, 401, 403))
        disabled = request('GET', path, token=admin)
        assert disabled['disabled'] and disabled['id'] == user['id']
        assert request('GET', project_path, token=admin)['name'] == 'Access test'
        assert request('GET', '/api/collections/agent_directory/records/' + user['id'], token=admin)['name'] == 'Member'
        # Re-enabling does not resurrect previously issued tokens.
        request('PATCH', path, {'disabled': False}, admin)
        rejected(token)
        fresh = request('POST', '/api/collections/users/auth-with-password', credentials)['token']
        request('GET', '/api/context/schema', token=fresh)
        assert request('GET', project_path, token=fresh)['created_by'] == user['id']
        assert request('GET', '/api/collections/organizations/records', token=admin)['totalItems'] == 1
    print('PASS: disabled accounts reject authentication and old REST/SQL tokens; identity and attribution preserved')


if __name__ == '__main__':
    main()
