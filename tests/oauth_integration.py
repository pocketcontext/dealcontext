#!/usr/bin/env python3
"""Exercise PocketBase's Google exchange with a local provider and synthetic users."""
import argparse
import base64
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets
import threading
import time
import urllib.parse
from unittest.mock import patch

from auth_server import server


@contextlib.contextmanager
def google_fixture():
    codes, tokens = {}, {}

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, data):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != '/token':
                return self.reply(404, {})
            form = urllib.parse.parse_qs(self.rfile.read(int(self.headers.get('Content-Length', 0))).decode())
            get = lambda key: form.get(key, [''])[0]
            pending = codes.get(get('code'))
            challenge = base64.urlsafe_b64encode(hashlib.sha256(get('code_verifier').encode()).digest()).decode().rstrip('=')
            if not pending or challenge != pending['challenge'] or get('redirect_uri') != REDIRECT:
                return self.reply(400, {'error': 'invalid_grant'})
            if get('grant_type') != 'authorization_code':
                return self.reply(400, {'error': 'unsupported_grant_type'})
            del codes[get('code')]
            token = secrets.token_urlsafe(24)
            tokens[token] = pending['user']
            self.reply(200, {'access_token': token, 'token_type': 'Bearer', 'expires_in': 3600})

        def do_GET(self):
            user = tokens.get(self.headers.get('Authorization', '').removeprefix('Bearer '))
            if self.path != '/userinfo' or user is None:
                return self.reply(401, {})
            self.reply(200, user)

    http = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{http.server_port}', codes
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


REDIRECT = 'http://127.0.0.1:8765/callback'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True)
    args = parser.parse_args()
    with google_fixture() as (provider_url, codes), server(args.binary) as request:
        admin = request('POST', '/api/collections/_superusers/auth-with-password', {
            'identity': 'admin@example.com', 'password': 'SyntheticAdminPassword123!',
        })['token']
        collection = request('GET', '/api/collections/users', token=admin)
        assert collection['createRule'] == "@request.context = 'oauth2'"
        request('PATCH', '/api/collections/users', {'oauth2': {
            'enabled': True,
            'providers': [{'name': 'google', 'clientId': 'synthetic-client', 'clientSecret': 'synthetic-secret',
                           'authURL': provider_url + '/authorize', 'tokenURL': provider_url + '/token',
                           'userInfoURL': provider_url + '/userinfo'}],
        }}, admin)
        user = request('POST', '/api/collections/users/records', {
            'email': 'member@example.com', 'name': 'Workspace member', 'verified': True,
            'password': 'SyntheticUserPassword123!', 'passwordConfirm': 'SyntheticUserPassword123!',
        }, admin)

        def exchange(email, *, verified=True, expected=200, wrong_verifier=False, subject='workspace-member'):
            methods = request('GET', '/api/collections/users/auth-methods')
            provider = next(p for p in methods['oauth2']['providers'] if p['name'] == 'google')
            assert provider['codeChallengeMethod'] == 'S256'
            code = secrets.token_urlsafe(24)
            codes[code] = {'challenge': provider['codeChallenge'], 'user': {
                'sub': subject, 'email': email, 'email_verified': verified, 'name': 'Google display name',
            }}
            return request('POST', '/api/collections/users/auth-with-oauth2', {
                'provider': 'google', 'code': code, 'redirectURL': REDIRECT,
                'codeVerifier': 'wrong-verifier' if wrong_verifier else provider['codeVerifier'],
            }, expected=expected)

        exchange('member@example.com', wrong_verifier=True, expected=400)
        exchange('outsider@example.com', subject='outsider', expected=(400, 403))
        exchange('member@example.com', verified=False, subject='unverified', expected=(400, 403))
        auth = exchange('member@example.com')
        assert auth['record']['id'] == user['id']
        assert auth['record']['name'] == 'Workspace member'
        # A second login reuses the provider link and user identity.
        assert exchange('member@example.com')['record']['id'] == user['id']
        token = auth['token']
        refreshed = request('POST', '/api/collections/users/auth-refresh', token=token)
        assert refreshed['record']['id'] == user['id'] and refreshed['token']
        for issued in (token, refreshed['token']):
            claims = json.loads(base64.urlsafe_b64decode(issued.split('.')[1] + '=='))
            assert abs(claims['exp'] - time.time() - 604800) < 30
        project = request('POST', '/api/collections/organizations/records', {
            'name': 'Synthetic OAuth project', 'owner': user['id'],
        }, token)
        assert project['created_by'] == user['id']
        result = request('POST', '/api/context/query', {'sql': 'SELECT name FROM organizations'}, token)
        assert 'Synthetic OAuth project' in json.dumps(result)
        history = request('GET', '/api/collections/audit_log/records', token=token)['items']
        assert any(row['record'] == project['id'] and row['actor'] == user['id'] for row in history)
        assert request('GET', '/api/collections/users/records', token=admin)['totalItems'] == 1
        assert request('GET', '/api/collections/users', token=admin)['createRule'] == "@request.context = 'oauth2'"
        # Google login leaves the existing verified user's password login available.
        password = request('POST', '/api/collections/users/auth-with-password', {
            'identity': 'member@example.com', 'password': 'SyntheticUserPassword123!',
        })
        assert password['record']['id'] == user['id']
    print('DealContext OAuth integration checks passed')
    jit_checks(args.binary)


def jit_checks(binary):
    with patch.dict(os.environ, {'DEALCONTEXT_GOOGLE_WORKSPACE_DOMAIN': 'example.com'}), google_fixture() as (url, codes), server(binary) as request:
        admin = request('POST', '/api/collections/_superusers/auth-with-password', {
            'identity': 'admin@example.com', 'password': 'SyntheticAdminPassword123!',
        })['token']
        provider = {'name': 'google', 'clientId': 'synthetic-client', 'clientSecret': 'synthetic-secret',
                    'authURL': url + '/authorize', 'tokenURL': url + '/token', 'userInfoURL': url + '/userinfo'}
        request('PATCH', '/api/collections/users', {'oauth2': {'enabled': True, 'providers': [provider]}}, admin)

        def exchange(email='new@example.com', *, hd='example.com', verified=True, expected=200,
                     name='New Workspace member', create_data=None, subject=None, token=None):
            metadata = request('GET', '/api/collections/users/auth-methods')['oauth2']['providers'][0]
            code = secrets.token_urlsafe(24)
            identity = {'sub': subject or email, 'email': email, 'email_verified': verified, 'name': name}
            if hd is not None:
                identity['hd'] = hd
            codes[code] = {'challenge': metadata['codeChallenge'], 'user': identity}
            body = {'provider': 'google', 'code': code, 'redirectURL': REDIRECT, 'codeVerifier': metadata['codeVerifier']}
            if create_data is not None:
                body['createData'] = create_data
            return request('POST', '/api/collections/users/auth-with-oauth2', body, token, expected)

        # Neither a client-supplied email nor public registration can bypass Google membership.
        for options in [
            {'hd': None}, {'hd': 'outside.com'}, {'hd': 'example.com.attacker.test'},
            {'verified': False}, {'verified': 'true'}, {'email': 'outsider@other.com'},
            {'email': 'new@example.com.attacker.test'},
            {'hd': None, 'create_data': {'hd': 'example.com', 'verified': True, 'email': 'new@example.com'}},
        ]:
            exchange(**options, expected=(400, 403))
        signup = {'email': 'forged@example.com', 'name': 'Forged', 'password': 'SyntheticPassword123!',
                  'passwordConfirm': 'SyntheticPassword123!', 'verified': True, 'disabled': False}
        request('POST', '/api/collections/users/records', signup, expected=(400, 403))
        request('POST', '/api/collections/users/records?context=oauth2', signup, expected=(400, 403))
        assert request('GET', '/api/collections/users/records', token=admin)['totalItems'] == 0
        auth = exchange(create_data={**signup, 'id': 'forged000000001', 'name': 'Forged name', 'disabled': True})
        user, token = auth['record'], auth['token']
        assert user['email'] == 'new@example.com' and user['name'] == 'New Workspace member'
        assert user['verified'] and not user['disabled'] and user['id'] != 'forged000000001'
        path = '/api/collections/users/records/' + user['id']
        assert request('GET', '/api/collections/agent_directory/records/' + user['id'], token=admin)['name'] == user['name']
        assert exchange()['record']['id'] == user['id']
        # Supplied passwords are discarded, and ordinary users still cannot provision accounts.
        request('POST', '/api/collections/users/auth-with-password', {
            'identity': user['email'], 'password': signup['password'],
        }, expected=(400, 401, 403))
        request('POST', '/api/collections/users/records', signup, token, expected=(400, 403))
        project = request('POST', '/api/collections/organizations/records', {'name': 'JIT project', 'owner': user['id']}, token)
        assert project['created_by'] == user['id']
        assert user['id'] in json.dumps(request('POST', '/api/context/query', {'sql': 'SELECT actor FROM audit_log'}, token))
        # A linked identity must still satisfy domain checks on subsequent login.
        exchange(hd='outside.com', expected=(400, 403))
        request('PATCH', path, {'disabled': True}, admin)
        exchange(expected=(400, 403))
        exchange(create_data={'disabled': False, 'email': user['email']}, expected=(400, 403))
        request('POST', '/api/collections/users/auth-refresh', token=token, expected=(400, 401, 403))
        assert request('GET', path, token=admin)['disabled']
        request('PATCH', path, {'disabled': False}, admin)
        assert exchange()['record']['id'] == user['id']
        request('GET', '/api/context/schema', token=token, expected=(401, 403))
        # Existing accounts are adopted by normalized email, including disabled ones.
        existing = request('POST', '/api/collections/users/records', {
            'email': 'EXISTING@EXAMPLE.COM', 'name': 'Existing identity', 'disabled': True,
            'password': 'SyntheticExistingPassword123!', 'passwordConfirm': 'SyntheticExistingPassword123!',
        }, admin)
        exchange('existing@example.com', expected=(400, 403))
        existing_path = '/api/collections/users/records/' + existing['id']
        request('PATCH', existing_path, {'disabled': False}, admin)
        adopted = exchange('existing@example.com')['record']
        assert adopted['id'] == existing['id'] and adopted['name'] == 'Existing identity'
        # A logged-in user cannot link someone else's identity to their own record.
        current = exchange()['token']
        exchange('other@example.com', token=current, expected=(400, 403))
        assert request('GET', '/api/collections/users/records', token=admin)['totalItems'] == 2
        request('POST', '/api/batch', {'requests': [
            {'method': 'POST', 'url': '/api/collections/users/records?context=oauth2', 'body': signup},
        ]}, current, expected=(400, 403))
        # Case-variant ambiguity must fail closed instead of choosing an arbitrary identity.
        request('POST', '/api/collections/users/records', {
            'email': 'existing@example.com', 'name': 'Conflicting identity',
            'password': 'SyntheticOtherPassword123!', 'passwordConfirm': 'SyntheticOtherPassword123!',
        }, admin)
        exchange('existing@example.com', expected=(400, 403))
        assert request('GET', '/api/collections/users/records', token=admin)['totalItems'] == 3
    for domain in ('EXAMPLE.COM', 'https://example.com', 'example.com/path', ' example.com', 'example.com '):
        with patch.dict(os.environ, {'DEALCONTEXT_GOOGLE_WORKSPACE_DOMAIN': domain}):
            try:
                with server(binary):
                    raise RuntimeError('Invalid Workspace domain allowed startup')
            except AssertionError as error:
                assert 'DEALCONTEXT_GOOGLE_WORKSPACE_DOMAIN' in str(error)
    print('PASS: Google Workspace JIT, trusted identity fields, no public signup, disabled-account rejection and preserved identity')


if __name__ == '__main__':
    main()
