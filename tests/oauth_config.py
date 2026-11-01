#!/usr/bin/env python3
"""Google configuration on an initially empty custom auth collection."""
import argparse
import os
from unittest.mock import patch
from auth_server import server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    args = parser.parse_args()
    client = 'synthetic-deal-google-client'
    secret = 'synthetic-deal-google-secret'
    with patch.dict(os.environ, {
        'DEALCONTEXT_GOOGLE_CLIENT_ID': client,
        'DEALCONTEXT_GOOGLE_CLIENT_SECRET': secret,
        'DEALCONTEXT_GOOGLE_WORKSPACE_DOMAIN': 'example.com',
    }), server(args.binary) as request:
        admin = request('POST', '/api/collections/_superusers/auth-with-password', {
            'identity': 'admin@example.com', 'password': 'SyntheticAdminPassword123!',
        })['token']
        collection = request('GET', '/api/collections/users', token=admin)
        assert collection['oauth2']['enabled'] and collection['passwordAuth']['enabled']
        provider, = collection['oauth2']['providers']
        assert provider['name'] == 'google' and provider['clientId'] == client and provider.get('clientSecret') is None
        assert collection['authToken']['duration'] == 604800
        assert collection['authRule'] == 'disabled = false'
        assert collection['createRule'] == "@request.context = 'oauth2'"
        methods = request('GET', '/api/collections/users/auth-methods')
        assert methods['oauth2']['enabled']
    for values in [(client, ''), ('', secret), (client, secret + ' '), (client + ' ', secret)]:
        with patch.dict(os.environ, dict(zip(['DEALCONTEXT_GOOGLE_CLIENT_ID', 'DEALCONTEXT_GOOGLE_CLIENT_SECRET'], values))):
            try:
                with server(args.binary):
                    raise RuntimeError('Invalid OAuth configuration unexpectedly started')
            except AssertionError as error:
                assert client not in str(error) and secret not in str(error), 'credentials leaked in startup output'
    print('PASS: Google env applied on first custom-collection install; invalid pairs fail without secrets')


if __name__ == '__main__':
    main()
