#!/usr/bin/env python3
"""Exercise the deployment hooks and the security migration over real HTTP; no third-party Python dependencies."""
import argparse
import base64
import contextlib
import json
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ['BASE_URL', 'SMTP_ADDRESS', 'SMTP_PORT', 'SMTP_USERNAME', 'SMTP_PASSWORD', 'MAILER_FROM_ADDRESS',
            'DEALCONTEXT_TRUSTED_PROXY_HEADER', 'DEALCONTEXT_RATE_LIMITS']
ADMIN, ADMIN_PASSWORD = 'test-admin@example.com', 'TestAdminPassword123!'
SMTP_PASSWORD = 'TestSmtpPassword123!'
RULES = [
    {'label': '*:auth', 'audience': '', 'duration': 60, 'maxRequests': 10},
    {'label': '/api/batch', 'audience': '', 'duration': 10, 'maxRequests': 10},
    {'label': '/api/context/', 'audience': '', 'duration': 10, 'maxRequests': 60},
    {'label': '/api/intake/', 'audience': '', 'duration': 60, 'maxRequests': 5},
    {'label': '/api/', 'audience': '', 'duration': 10, 'maxRequests': 300},
]


@contextlib.contextmanager
def item(label):
    """Prefix any failure inside the block with the contract item it belongs to."""
    try:
        yield
    except Exception as error:
        raise AssertionError(f'FAILED contract item {label}', repr(error)) from error


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class Mailbox(socketserver.ThreadingTCPServer):
    """Minimal SMTP server on 127.0.0.1 that records the AUTH PLAIN credentials and the envelope of each message."""
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        self.messages = []
        super().__init__(('127.0.0.1', 0), MailboxHandler)
        threading.Thread(target=self.serve_forever, daemon=True).start()


class MailboxHandler(socketserver.StreamRequestHandler):
    def handle(self):
        message = {'username': '', 'password': '', 'from': '', 'to': ''}
        def reply(text):
            self.wfile.write(text.encode() + b'\r\n')
        reply('220 localhost test mailbox')
        while True:
            line = self.rfile.readline().decode(errors='replace').strip()
            verb = line.upper()
            if not line or verb.startswith('QUIT'):
                reply('221 bye')
                return
            if verb.startswith(('EHLO', 'HELO')):
                reply('250-localhost')
                reply('250 AUTH PLAIN')
            elif verb.startswith('AUTH PLAIN'):
                parts = base64.b64decode(line.split()[2]).decode().split('\0')
                message['username'], message['password'] = parts[1], parts[2]
                reply('235 ok')
            elif verb.startswith('MAIL FROM:'):
                message['from'] = line[10:].split()[0].strip('<>')
                reply('250 ok')
            elif verb.startswith('RCPT TO:'):
                message['to'] = line[8:].split()[0].strip('<>')
                reply('250 ok')
            elif verb.startswith('DATA'):
                reply('354 go on')
                while self.rfile.readline() not in (b'.\r\n', b''):
                    pass
                self.server.messages.append(dict(message))
                reply('250 ok')
            else:
                reply('250 ok')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    args = parser.parse_args()
    binary = str(Path(args.binary).resolve())
    clean = {key: value for key, value in os.environ.items() if key not in CONTRACT}
    mailbox = Mailbox()
    contract = {
        'BASE_URL': 'https://crm.example.test/', 'MAILER_FROM_ADDRESS': 'Info <info@notifications.example.test>',
        'SMTP_ADDRESS': '127.0.0.1', 'SMTP_PORT': str(mailbox.server_address[1]),
        'SMTP_USERNAME': 'mailer', 'SMTP_PASSWORD': SMTP_PASSWORD,
        'DEALCONTEXT_TRUSTED_PROXY_HEADER': 'X-Forwarded-For', 'DEALCONTEXT_RATE_LIMITS': 'true',
    }
    with tempfile.TemporaryDirectory(prefix='dealcontext-deploy-') as tmp:
        data = str(Path(tmp) / 'pb_data')
        common = [binary, '--dir', data, '--migrationsDir', str(ROOT / 'pb_migrations'), '--hooksDir', str(ROOT / 'pb_hooks'),
                  '--contextConfig', str(ROOT / 'pocketcontext.json')]
        state = {'base': '', 'server': None, 'log': None}

        def request(method, path, body=None, token=None, expected=200, ip=None, header='X-Forwarded-For', raw=False):
            headers = {'Content-Type': 'application/json'}
            if token:
                headers['Authorization'] = token
            if ip:
                headers[header] = ip
            req = urllib.request.Request(state['base'] + path, data=None if body is None else json.dumps(body).encode(), headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=10) as response:
                    status, content = response.status, response.read()
            except urllib.error.HTTPError as error:
                status, content = error.code, error.read()
            assert status in (expected if isinstance(expected, tuple) else (expected,)), (method, path, status, content.decode())
            return content.decode() if raw else json.loads(content) if content else None

        def stop():
            server = state['server']
            if server is None:
                return ''
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
            state['log'].seek(0)
            output = state['log'].read()
            state['log'].close()
            state['server'] = None
            return output

        def start(name, extra):
            """Start the server on a new port with exactly the given contract variables; wait for /up."""
            port = free_port()
            state['base'] = f'http://127.0.0.1:{port}'
            state['log'] = open(Path(tmp) / f'{name}.log', 'w+')
            state['server'] = subprocess.Popen(common + ['serve', '--http', f'127.0.0.1:{port}'], cwd=ROOT, env={**clean, **extra},
                                               stdout=state['log'], stderr=state['log'])
            for _ in range(150):
                try:
                    request('GET', '/up', raw=True)
                    return
                except (OSError, AssertionError):
                    if state['server'].poll() is not None:
                        raise RuntimeError('Server exited during startup: ' + stop())
                    time.sleep(.1)
            raise RuntimeError('Server did not start')

        def login(collection, identity, password, ip, expected=200):
            return request('POST', f'/api/collections/{collection}/auth-with-password', {'identity': identity, 'password': password}, expected=expected, ip=ip)

        def send_test_email(admin):
            """The settings API never returns the SMTP password, so read it from a delivered message instead."""
            before = len(mailbox.messages)
            request('POST', '/api/settings/test/email', {'email': 'inbox@example.test', 'template': 'verification'}, admin, expected=204)
            assert len(mailbox.messages) == before + 1, 'no message reached the test mailbox'
            return mailbox.messages[-1]

        def shown(settings):
            return {key: settings[key] for key in ('meta', 'smtp', 'trustedProxy', 'rateLimits', 'batch')}

        try:
            # The image runs `superuser upsert` with the same environment before `serve`, so the settings are applied here.
            upsert = subprocess.run(common + ['superuser', 'upsert', ADMIN, ADMIN_PASSWORD], cwd=ROOT, env={**clean, **contract},
                                    check=True, capture_output=True, text=True)
            output = upsert.stdout + upsert.stderr
            with item('D2 one log line per applied group, without secret values'):
                assert output.count('deploy: applied') == 5, output
                assert SMTP_PASSWORD not in output and ADMIN_PASSWORD not in output, 'a secret reached the command output'

            start('first', contract)
            with item('D1 /up answers 200 without authentication'):
                assert request('GET', '/up', raw=True) == 'OK'
                request('POST', '/up', expected=(404, 405))
            admin = login('_superusers', ADMIN, ADMIN_PASSWORD, '198.51.100.1')['token']
            with item('D2 settings match the environment'):
                settings = request('GET', '/api/settings', token=admin)
                assert settings['meta']['appURL'] == 'https://crm.example.test', settings['meta']
                assert (settings['meta']['senderName'], settings['meta']['senderAddress']) == ('Info', 'info@notifications.example.test'), settings['meta']
                smtp = settings['smtp']
                assert 'password' not in smtp, sorted(smtp)
                assert (smtp['enabled'], smtp['host'], smtp['port'], smtp['username'], smtp['tls']) == (True, '127.0.0.1', mailbox.server_address[1], 'mailer', False), \
                    {key: value for key, value in smtp.items() if key != 'password'}
                assert settings['trustedProxy'] == {'headers': ['X-Forwarded-For'], 'useLeftmostIP': False}, settings['trustedProxy']
                assert settings['rateLimits'] == {'rules': RULES, 'excludedIPs': [], 'enabled': True}, settings['rateLimits']
                assert settings['batch']['enabled'] is True and settings['batch']['maxRequests'] == 20, settings['batch']
            with item('D2 the SMTP password and the sender are in use'):
                message = send_test_email(admin)
                assert message['password'] == SMTP_PASSWORD, 'the SMTP password sent to the mail server differs from SMTP_PASSWORD'
                assert (message['username'], message['from'], message['to']) == ('mailer', 'info@notifications.example.test', 'inbox@example.test'), \
                    {key: value for key, value in message.items() if key != 'password'}

            def agent(number, ip):
                email, password = f'agent{number}@example.test', f'TestAgentPassword{number}!'
                record = request('POST', '/api/collections/users/records', {'email': email, 'password': password, 'passwordConfirm': password, 'name': f'Agent {number}'}, admin)
                return record, email, password, login('users', email, password, ip)['token']
            first, email1, password1, token1 = agent(1, '198.51.100.2')
            second, email2, password2, token2 = agent(2, '198.51.100.2')
            own, other = f'/api/collections/users/records/{first["id"]}', f'/api/collections/users/records/{second["id"]}'

            with item('D3 agents collection options'):
                collection = request('GET', '/api/collections/users', token=admin)
                assert collection['authToken']['duration'] == 604800, collection['authToken']
                assert collection['authAlert']['enabled'] is False, collection['authAlert']
                claims = json.loads(base64.urlsafe_b64decode(token1.split('.')[1] + '=='))
                assert abs(claims['exp'] - time.time() - 604800) < 300, claims['exp']
            with item('D3 an agent changes only its own password, and only with oldPassword'):
                new = 'ChangedAgentPassword1!'
                change = {'password': new, 'passwordConfirm': new}
                request('PATCH', own, change, token1, expected=400, ip='198.51.100.3')
                request('PATCH', own, {**change, 'oldPassword': 'WrongOldPassword1!'}, token1, expected=400, ip='198.51.100.3')
                request('PATCH', own, {**change, 'oldPassword': password1}, expected=404, ip='198.51.100.3')
                request('PATCH', other, {**change, 'oldPassword': password2}, token1, expected=404, ip='198.51.100.3')
                request('PATCH', other, {**change, 'oldPassword': password1}, token1, expected=404, ip='198.51.100.3')
                for field, value in (('email', 'moved@example.test'), ('email', email1), ('name', 'Renamed'), ('verified', True),
                                     ('verified', False), ('emailVisibility', True)):
                    request('PATCH', own, {**change, 'oldPassword': password1, field: value}, token1, expected=404, ip='198.51.100.3')
                    request('PATCH', own, {field: value}, token1, expected=404, ip='198.51.100.3')
                request('DELETE', own, token=token1, expected=403, ip='198.51.100.3')
                request('POST', '/api/collections/users/records', {'email': 'self@example.test', 'password': new, 'passwordConfirm': new, 'name': 'Self'}, token1, expected=400, ip='198.51.100.3')
                stored = request('GET', own, token=admin)
                assert (stored['email'], stored['name'], stored['verified'], stored['emailVisibility']) == (email1, 'Agent 1', False, False), stored
                assert request('GET', own, token=token1, ip='198.51.100.3')['id'] == first['id']
                login('users', email1, password1, '198.51.100.3')
                changed = request('PATCH', own, {**change, 'oldPassword': password1}, token1, ip='198.51.100.3')
                assert changed['id'] == first['id'] and 'password' not in changed and 'tokenKey' not in changed, changed
            with item('D3 the old token and the old password stop working after the change'):
                request('POST', '/api/collections/users/auth-refresh', token=token1, expected=401, ip='198.51.100.3')
                request('POST', '/api/context/query', {'sql': 'SELECT 1'}, token1, expected=401, ip='198.51.100.3')
                login('users', email1, password1, '198.51.100.3', expected=400)
                token1 = login('users', email1, new, '198.51.100.3')['token']
                request('POST', '/api/context/query', {'sql': 'SELECT 1'}, token1, ip='198.51.100.3')
                request('POST', '/api/context/query', {'sql': 'SELECT 1'}, token2, ip='198.51.100.3')
                stored = request('GET', own, token=admin)
                assert (stored['email'], stored['name'], stored['verified']) == (email1, 'Agent 1', False), stored

            def bad_login(ip, expected, header='X-Forwarded-For'):
                request('POST', '/api/collections/users/auth-with-password', {'identity': email2, 'password': 'WrongPassword123!'}, expected=expected, ip=ip, header=header)
            fresh = (f'203.0.113.{number}' for number in range(100, 250))
            def exhaust(count, window, call):
                """Use up a rule's allowance from a new client IP and see the next call rejected. PocketBase counts in
                fixed windows that start at the client's first request, so a run slower than the window repeats."""
                for _ in range(3):
                    ip, begin = next(fresh), time.monotonic()
                    for _ in range(count):
                        call(ip, (200, 400))
                    if time.monotonic() - begin < window - 2:
                        call(ip, 429)
                        call(next(fresh), (200, 400))
                        return ip
                raise AssertionError(f'{count} requests took longer than {window} seconds three times')
            with item('D2 the batch, SQL, and general API rules apply per forwarded client IP'):
                exhaust(10, 10, lambda ip, expected: request('POST', '/api/batch', {'requests': []}, token2, expected=expected, ip=ip))
                query = lambda ip, expected: request('POST', '/api/context/query', {'sql': 'SELECT 1'}, token2, expected=expected, ip=ip)
                limited = exhaust(60, 10, query)
                request('GET', '/api/context/schema', token=token2, expected=429, ip=limited)
                limited = exhaust(300, 10, lambda ip, expected: request('GET', '/api/health', expected=expected, ip=ip))
                assert request('GET', '/up', ip=limited, raw=True) == 'OK'
                request('GET', '/api/settings', token=admin, ip=limited)  # superuser requests are not limited
                # PocketBase counts collection routes apart from other routes: the "/api/" rule gives every
                # collection action its own allowance per client.
                records = '/api/collections/deals/records?perPage=1&skipTotal=1'
                exhaust(300, 10, lambda ip, expected: request('GET', records, token=token2, expected=expected, ip=ip))
            with item('D2 login attempts are limited per forwarded client IP; D1 /up stays 200 for a limited client under frequent polling'):
                limited = exhaust(10, 60, bad_login)
                # The proxy appends the address it saw, so the rightmost entry counts and a forged leftmost entry does not.
                bad_login('203.0.113.99, ' + limited, 429)
                bad_login(limited + ', 203.0.113.98', 400)
                login('users', email2, password2, limited, expected=429)
                login('users', email2, password2, '203.0.113.97')
                for _ in range(350):
                    assert request('GET', '/up', ip=limited, raw=True) == 'OK'
                bad_login(limited, 429)
            with item('D1 successful /up checks stay out of the request log'):
                def logged(condition):
                    return request('GET', '/api/logs?perPage=1&filter=' + urllib.request.quote(condition), token=admin)['totalItems']
                for _ in range(100):  # the log writer flushes every few seconds; the rejected requests show that it did
                    if logged("data.url = '/api/health' && data.status = 429"):
                        break
                    time.sleep(.2)
                else:
                    raise AssertionError('the rate limited requests never reached the request log')
                assert logged("data.url = '/up' && data.status = 200") == 0
            before = shown(request('GET', '/api/settings', token=admin))
            output = stop()
            with item('D2 a start with unchanged variables saves nothing, and the log holds no secret'):
                assert 'deploy:' not in output, output
                assert SMTP_PASSWORD not in output and ADMIN_PASSWORD not in output, 'a secret reached the server log'

            start('second', {})
            with item('D2 a start without the variables leaves the stored settings untouched'):
                admin = login('_superusers', ADMIN, ADMIN_PASSWORD, '198.51.100.1')['token']
                assert shown(request('GET', '/api/settings', token=admin)) == before
                assert send_test_email(admin)['password'] == SMTP_PASSWORD, 'the stored SMTP password changed'
                limited = exhaust(10, 60, bad_login)
                assert request('GET', '/up', ip=limited, raw=True) == 'OK'
            assert 'deploy:' not in stop()

            start('third', {'DEALCONTEXT_RATE_LIMITS': 'false', 'DEALCONTEXT_TRUSTED_PROXY_HEADER': 'CF-Connecting-IP', 'BASE_URL': 'not a url'})
            with item('D2 every group is applied on its own; a rejected value keeps the stored settings and the server still starts'):
                admin = login('_superusers', ADMIN, ADMIN_PASSWORD, None)['token']
                after = shown(request('GET', '/api/settings', token=admin))
                assert after['rateLimits'] == {'rules': RULES, 'excludedIPs': [], 'enabled': False}, after['rateLimits']
                assert after['trustedProxy'] == {'headers': ['CF-Connecting-IP'], 'useLeftmostIP': False}, after['trustedProxy']
                assert after['meta'] == before['meta'] and after['smtp'] == before['smtp'] and after['batch'] == before['batch'], after
                for _ in range(12):
                    bad_login('203.0.113.40', 400, header='CF-Connecting-IP')
            output = stop()
            assert output.count('deploy: applied') == 2 and output.count('deploy: could not apply') == 1, output

            start('fourth', {'DEALCONTEXT_RATE_LIMITS': 'true'})
            with item('D2 the forwarded address comes from the configured header only'):
                exhaust(10, 60, lambda ip, expected: bad_login(ip, expected, header='CF-Connecting-IP'))
                # X-Forwarded-For is no longer trusted, so these two share the bucket of the socket address.
                for _ in range(10):
                    bad_login('203.0.113.52', 400)
                bad_login('203.0.113.53', 429)
            output = stop()
            assert output.count('deploy: applied') == 1, output
        finally:
            stop()
            mailbox.shutdown()
            mailbox.server_close()
    print('PASS: /up health check, settings from the environment, SMTP credentials in use, idempotent and group-wise apply, '
          'stored settings kept without variables, trusted proxy header, rate limit rules per client IP, /up never limited or logged, '
          'agent self password change, token invalidation, seven-day account tokens, no secrets in output')


if __name__ == '__main__':
    main()
