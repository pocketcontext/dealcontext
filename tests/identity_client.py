#!/usr/bin/env python3
"""Hosted identity renewals fail closed and keep direct fallback explicit."""
import os
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch
from dealcontext_client import cli


class IdentityError(Exception):
    pass


class IdentityClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'DEALCONTEXT_SESSION_DIR': self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.cfg = {'url': 'https://app.example.com', 'email': 'member@example.com', 'password': 'must-not-fallback'}
        self.response = {'token': 'synthetic-token', 'record': {'collectionName': 'users', 'id': 'user00000000001', 'email': self.cfg['email']}, 'meta': {'accessToken': 'never-store'}}
        self.identity = types.SimpleNamespace(IdentityError=IdentityError, login=Mock(), assertion=Mock(return_value='synthetic-assertion'))
        modules = patch.dict(sys.modules, {'pocketcontext_identity': self.identity})
        modules.start()
        self.addCleanup(modules.stop)

    def test_bare_login_selects_hosted_flow(self):
        args = cli.parse(['login', '--no-browser'])
        self.assertFalse(args.google)
        self.assertFalse(args.password)
        self.assertTrue(cli.parse(['login', '--google']).google)

    def test_exchange_checks_identity_and_only_saves_app_token(self):
        with patch.object(cli, 'oauth_send', return_value=(200, self.response)) as send:
            session = cli.identity_login(self.cfg, interactive=True, no_browser=True, timeout=60)
        self.identity.login.assert_called_once_with(self.cfg['email'], no_browser=True, timeout=60)
        self.identity.assertion.assert_called_once_with(self.cfg['email'], self.cfg['url'])
        self.assertEqual(send.call_args.args[2], '/api/context/auth-with-assertion')
        self.assertEqual(session['method'], 'identity')
        saved = cli.session_file(self.cfg).read_text()
        self.assertNotIn('synthetic-assertion', saved)
        self.assertNotIn('never-store', saved)
        self.assertEqual(cli.load_session(self.cfg), session)

    def test_wrong_account_never_saved(self):
        self.response['record']['email'] = 'other@example.com'
        with patch.object(cli, 'oauth_send', return_value=(200, self.response)), self.assertRaises(cli.Fail):
            cli.identity_login(self.cfg)
        self.assertIsNone(cli.load_session(self.cfg))

    def test_central_rejection_never_downgrades_or_sends_app_request(self):
        cli.save_session(self.cfg, {'url': self.cfg['url'], 'email': self.cfg['email'], 'method': 'identity', 'token': 'expired', 'refreshed_at': 0})
        self.identity.assertion.side_effect = IdentityError('secret provider detail')
        with patch.object(cli, 'login') as password, patch.object(cli, 'google_login') as google, patch.object(cli, 'send') as send:
            with self.assertRaises(cli.Fail) as caught:
                cli.call(self.cfg, 'POST', '/api/context/query', {'sql': 'SELECT 1'})
        self.assertNotIn('secret provider detail', str(caught.exception))
        password.assert_not_called()
        google.assert_not_called()
        send.assert_not_called()
        self.identity.login.assert_not_called()

    def test_expired_session_renews_through_identity_before_request(self):
        cli.save_session(self.cfg, {'url': self.cfg['url'], 'email': self.cfg['email'], 'method': 'identity', 'token': 'expired', 'refreshed_at': 0})
        with patch.object(cli, 'oauth_send', return_value=(200, self.response)) as exchange, patch.object(cli, 'send', return_value=(200, {'ok': True})) as send:
            self.assertEqual(cli.call(self.cfg, 'GET', '/api/context/schema'), (200, {'ok': True}))
        self.assertEqual(exchange.call_args.args[2], '/api/context/auth-with-assertion')
        self.assertEqual(send.call_args.args[4], 'synthetic-token')
        self.identity.login.assert_not_called()

    def test_whoami_refresh_uses_new_identity_token_without_extending_it(self):
        cli.save_session(self.cfg, {'url': self.cfg['url'], 'email': self.cfg['email'], 'method': 'identity', 'token': 'previous', 'refreshed_at': 0})
        with patch.object(cli, 'oauth_send', return_value=(200, self.response)) as exchange, patch.object(cli, 'send', return_value=(200, self.response)) as send:
            status, data = cli.call(self.cfg, 'POST', '/api/collections/users/auth-refresh')
        self.assertEqual(status, 200)
        self.assertEqual(data['record']['email'], self.cfg['email'])
        self.assertEqual(exchange.call_args.args[2], '/api/context/auth-with-assertion')
        self.assertEqual(send.call_args.args[4], 'synthetic-token')
        self.identity.login.assert_not_called()

    def test_rejected_fresh_app_token_never_downgrades(self):
        session = {'method': 'identity', 'token': 'fresh', 'refreshed_at': time.time()}
        with patch.object(cli, 'load_session', return_value=session), patch.object(cli, 'oauth_refresh_needed', return_value=False), patch.object(cli, 'send', return_value=(401, {})), patch.object(cli, 'login') as password:
            with self.assertRaises(cli.Fail):
                cli.call(self.cfg, 'POST', '/api/context/query', {'sql': 'SELECT 1'})
        password.assert_not_called()
        self.identity.assertion.assert_not_called()


if __name__ == '__main__':
    unittest.main()
