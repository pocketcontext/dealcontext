#!/usr/bin/env python3
"""Exercise the public enquiry endpoint and enquiry triage over real HTTP; no third-party Python dependencies."""
import argparse
import concurrent.futures
import contextlib
from datetime import datetime, timedelta, timezone
import email
import json
import os
from pathlib import Path
import select
import socket
import socketserver
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ['BASE_URL', 'SMTP_ADDRESS', 'SMTP_PORT', 'SMTP_USERNAME', 'SMTP_PASSWORD', 'MAILER_FROM_ADDRESS',
            'DEALCONTEXT_TRUSTED_PROXY_HEADER', 'DEALCONTEXT_RATE_LIMITS', 'DEALCONTEXT_INTAKE_ORIGINS']
ADMIN, ADMIN_PASSWORD = 'test-admin@example.com', 'TestAdminPassword123!'
INTAKE = '/api/intake/enquiry'
ENQUIRIES = '/api/collections/enquiries/records'
RECIPIENTS = '/api/collections/enquiry_notification_recipients/records'
SITE, OTHER_SITE, APP = 'https://pocketcontext.example', 'https://www.pocketcontext.example', 'https://crm.example.test'
USER_AGENT = 'IntakeTestBrowser/1.0'
NOTIFY = 'operator@example.test'
# The keys of the form in the website's src/layouts/HomePage.astro: FormData of every named control (the honeypot
# included, empty), then the three UTM keys from the page URL, empty when absent.
FORM = {
    'name': 'Grace Hopper', 'email': 'grace@example.org', 'interest': 'both', 'workflow': 'Quotes from email threads',
    'requirements': 'We need <b>audit</b> & "SQL" access.\nTwo teams.', 'timeline': 'Within 3 months', 'website': '',
    'application': 'raisecontext', 'entry_offer': 'general', 'utm_source': 'newsletter', 'utm_medium': '', 'utm_campaign': '',
}
DETAILS = ['application', 'interest', 'workflow', 'requirements', 'timeline', 'entry_offer']
COLUMNS = {'collectionId', 'collectionName', 'id', 'name', 'email', 'status', 'source', 'utm_source', 'utm_medium', 'utm_campaign',
           'details', 'person', 'deal', 'updated_by', 'created', 'updated'}


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
    """Minimal SMTP server on 127.0.0.1 that records the envelope and the content of each message."""
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        self.messages, self.delay, self.reject = [], 0, set()
        super().__init__(('127.0.0.1', 0), MailboxHandler)
        threading.Thread(target=self.serve_forever, daemon=True).start()


class MailboxHandler(socketserver.StreamRequestHandler):
    def handle(self):
        message = {'from': '', 'to': []}
        def reply(text):
            self.wfile.write(text.encode() + b'\r\n')
        time.sleep(self.server.delay)
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
            elif verb.startswith('AUTH'):
                reply('235 ok')
            elif verb.startswith('MAIL FROM:'):
                message['from'] = line[10:].split()[0].strip('<>')
                reply('250 ok')
            elif verb.startswith('RCPT TO:'):
                recipient = line[8:].split()[0].strip('<>')
                if recipient in self.server.reject:
                    reply('550 recipient rejected: ' + recipient)
                else:
                    message['to'].append(recipient)
                    reply('250 ok')
            elif verb.startswith('DATA'):
                reply('354 go on')
                content = b''
                while True:
                    part = self.rfile.readline()
                    if part in (b'.\r\n', b''):
                        break
                    content += part[1:] if part.startswith(b'..') else part
                parsed = email.message_from_bytes(content)
                texts = [part.get_payload(decode=True).decode() for part in parsed.walk() if part.get_content_maintype() == 'text']
                self.server.messages.append({**message, 'subject': str(parsed['Subject']), 'text': '\n'.join(texts),
                                             'types': [part.get_content_type() for part in parsed.walk()],
                                             'headers': '\n'.join(f'{key}: {value}' for key, value in parsed.items())})
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
    with tempfile.TemporaryDirectory(prefix='dealcontext-intake-') as tmp:
        data = str(Path(tmp) / 'pb_data')
        common = [binary, '--dir', data, '--migrationsDir', str(ROOT / 'pb_migrations'), '--hooksDir', str(ROOT / 'pb_hooks'),
                  '--contextConfig', str(ROOT / 'pocketcontext.json')]
        state = {'base': '', 'port': 0, 'server': None, 'log': None}

        def call(method, path, body=None, token=None, headers=None, raw=None):
            """One request; returns (status, response headers, body bytes)."""
            sent = {'Content-Type': 'application/json', 'User-Agent': USER_AGENT, **(headers or {})}
            if token:
                sent['Authorization'] = token
            content = raw if raw is not None else None if body is None else json.dumps(body).encode()
            req = urllib.request.Request(state['base'] + path, data=content, headers={k: v for k, v in sent.items() if v is not None}, method=method)
            try:
                with urllib.request.urlopen(req, timeout=10) as response:
                    return response.status, response.headers, response.read()
            except urllib.error.HTTPError as error:
                return error.code, error.headers, error.read()

        def request(method, path, body=None, token=None, expected=200, headers=None):
            status, _, content = call(method, path, body, token, headers)
            assert status in (expected if isinstance(expected, tuple) else (expected,)), (method, path, status, content.decode())
            return json.loads(content) if content else None

        def submit(body, expected=200, ip=None, origin=SITE, raw=None, content_type='application/json'):
            """POST to the intake endpoint the way a browser does: no credentials, an Origin header."""
            headers = {'Content-Type': content_type, 'Origin': origin, 'X-Forwarded-For': ip}
            status, _, content = call('POST', INTAKE, body, headers=headers, raw=raw)
            text = content.decode()
            assert status == expected, (status, text)
            if status == 200:
                assert content == b'{"ok":true}', text
            else:
                assert json.loads(text)['status'] == status and 'goja' not in text and '.js' not in text and 'at ' not in text.replace('at most', ''), text
            return text

        def rejected(body, *names, **options):
            """400 with a message that names every given key."""
            text = submit(body, expected=400, **options)
            answer = json.loads(text)
            for name in names:
                assert name.lower() in answer['message'].lower() and any(name in field for field in answer['data']), (name, text)

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

        def start(name, extra, flags=()):
            port = free_port()
            state['base'], state['port'] = f'http://127.0.0.1:{port}', port
            state['log'] = open(Path(tmp) / f'{name}.log', 'w+')
            state['server'] = subprocess.Popen(common + ['serve', '--http', f'127.0.0.1:{port}', *flags], cwd=ROOT, env={**clean, **extra},
                                               stdout=state['log'], stderr=state['log'])
            for _ in range(150):
                try:
                    assert call('GET', '/up')[0] == 200
                    return
                except (OSError, AssertionError):
                    if state['server'].poll() is not None:
                        raise RuntimeError('Server exited during startup: ' + stop())
                    time.sleep(.1)
            raise RuntimeError('Server did not start')

        def login(collection, identity, password, ip=None):
            return request('POST', f'/api/collections/{collection}/auth-with-password', {'identity': identity, 'password': password},
                           headers={'X-Forwarded-For': ip})['token']

        def entrypoint(**env):
            """Exercise the actual entrypoint origin builder without a replica daemon."""
            import importlib.util
            spec = importlib.util.spec_from_file_location('runtime', ROOT / 'docker/entrypoint.py')
            runtime = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(runtime)
            origins = runtime.browser_origins(env)
            return '--origins=' + origins if origins else None

        try:
            subprocess.run(common + ['superuser', 'upsert', ADMIN, ADMIN_PASSWORD], cwd=ROOT, env=clean, check=True, capture_output=True)

            with item('S5 the entrypoint builds --origins from BASE_URL and DEALCONTEXT_INTAKE_ORIGINS'):
                assert entrypoint() is None
                assert entrypoint(BASE_URL=APP + '/') == '--origins=' + APP
                assert entrypoint(DEALCONTEXT_INTAKE_ORIGINS=SITE) == '--origins=' + SITE
                origins = entrypoint(BASE_URL=APP + '/', DEALCONTEXT_INTAKE_ORIGINS=f' {SITE}/ ,{OTHER_SITE},, ')
                assert origins == f'--origins={APP},{SITE},{OTHER_SITE}', origins
                assert entrypoint(DEALCONTEXT_INTAKE_ORIGINS=' , ') is None

            start('first', {}, [origins])
            admin = login('_superusers', ADMIN, ADMIN_PASSWORD)
            def agent(number):
                mail, password = f'agent{number}@example.test', f'TestAgentPassword{number}!'
                record = request('POST', '/api/collections/agents/records', {'email': mail, 'password': password, 'passwordConfirm': password, 'name': f'Agent {number}'}, admin)
                return record, login('agents', mail, password)
            agent1, token = agent(1)
            agent2, _ = agent(2)
            with item('notification recipients are normalized, unique, and managed only by superusers'):
                recipient = request('POST', RECIPIENTS, {'email': '  OPERATOR@example.test  ', 'name': 'Operator'}, admin)
                assert recipient['email'] == NOTIFY and recipient['enabled'] is False, recipient
                recipient_path = RECIPIENTS + '/' + recipient['id']
                for invalid in ('', 'not-an-address', 'name\r\n@example.test'):
                    request('POST', RECIPIENTS, {'email': invalid}, admin, expected=400)
                request('POST', RECIPIENTS, {'email': 'Operator@EXAMPLE.test'}, admin, expected=400)
                second_recipient = request('POST', RECIPIENTS, {'email': 'second@example.test'}, admin)
                second_path = RECIPIENTS + '/' + second_recipient['id']
                request('PATCH', second_path, {'email': ' OPERATOR@EXAMPLE.test '}, admin, expected=400)
                updated = request('PATCH', second_path, {'email': ' Renamed@EXAMPLE.test ', 'name': 'Renamed'}, admin)
                assert updated['email'] == 'renamed@example.test' and updated['name'] == 'Renamed', updated
                for credentials in (None, token):
                    request('GET', RECIPIENTS, token=credentials, expected=403)
                    request('GET', recipient_path, token=credentials, expected=403)
                    request('POST', RECIPIENTS, {'email': 'forbidden@example.test'}, credentials, expected=403)
                    request('PATCH', recipient_path, {'enabled': True}, credentials, expected=403)
                    request('DELETE', recipient_path, token=credentials, expected=403)
                schema = request('GET', '/api/context/schema', token=token)
                assert 'enquiry_notification_recipients' not in json.dumps(schema), schema
                request('POST', '/api/context/query', {'sql': 'SELECT * FROM enquiry_notification_recipients'}, token, expected=400)
                request('DELETE', second_path, token=admin, expected=204)
                request('GET', second_path, token=admin, expected=404)
            def sql(query):
                result = request('POST', '/api/context/query', {'sql': query}, token)
                return [dict(zip(result['columns'], row)) for row in result['rows']]
            def count(where='1=1'):
                return sql(f'SELECT count(*) AS n FROM enquiries WHERE {where}')[0]['n']
            def stored(mail):
                found = request('GET', ENQUIRIES + '?filter=' + urllib.request.quote(f"email='{mail}'"), token=admin)['items']
                assert len(found) == 1, found
                return found[0]
            def audit(record=None):
                rows = sql("SELECT action, collection, record, actor_type, actor, changes FROM audit_log WHERE collection = 'enquiries'"
                           + (f" AND record = '{record}'" if record else '') + ' ORDER BY created')
                for row in rows:
                    row['changes'] = json.loads(row['changes'])
                return rows

            with item('S5 CORS preflight: a listed origin is allowed, another origin gets no Access-Control-Allow-Origin'):
                preflight = {'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'content-type', 'Content-Type': None}
                for origin in (SITE, OTHER_SITE, APP):
                    status, headers, _ = call('OPTIONS', INTAKE, headers={**preflight, 'Origin': origin})
                    assert status == 204 and headers['Access-Control-Allow-Origin'] == origin, (origin, status, dict(headers))
                    assert 'content-type' in headers['Access-Control-Allow-Headers'].lower(), dict(headers)
                    assert 'POST' in headers['Access-Control-Allow-Methods'], dict(headers)
                for origin in ('https://evil.example', SITE + '.evil.example', 'http://' + SITE.split('//')[1]):
                    status, headers, _ = call('OPTIONS', INTAKE, headers={**preflight, 'Origin': origin})
                    assert status == 204 and headers['Access-Control-Allow-Origin'] is None, (origin, status, dict(headers))
                status, headers, _ = call('POST', INTAKE, {**FORM, 'email': 'cors@example.org'}, headers={'Origin': SITE})
                assert status == 200 and headers['Access-Control-Allow-Origin'] == SITE, dict(headers)
                status, headers, _ = call('POST', INTAKE, {**FORM, 'email': 'cors2@example.org'}, headers={'Origin': 'https://evil.example'})
                assert headers['Access-Control-Allow-Origin'] is None, dict(headers)

            with item('S1 S7 the payload of the website form is accepted and lands in the right columns'):
                assert submit(FORM) == '{"ok":true}'
                row = stored(FORM['email'])
                assert set(row) == COLUMNS, sorted(row)
                assert (row['name'], row['email'], row['status'], row['source']) == (FORM['name'], FORM['email'], 'new', SITE), row
                assert (row['utm_source'], row['utm_medium'], row['utm_campaign']) == ('newsletter', '', ''), row
                assert row['details'] == {key: FORM[key] for key in DETAILS}, row['details']
                assert (row['person'], row['deal'], row['updated_by']) == ('', '', ''), row
            with item('S1 neither the client address nor the user agent is stored'):
                text = json.dumps(sql(f"SELECT * FROM enquiries WHERE email = '{FORM['email']}'"))
                assert USER_AGENT not in text and '127.0.0.1' not in text, text
            with item('S1 name and email are trimmed; source is empty without an Origin header and cut at 200 characters'):
                submit({'name': '  Ada  ', 'email': ' ada@example.org '}, origin=None)
                row = stored('ada@example.org')
                assert (row['name'], row['source'], row['details']) == ('Ada', '', {}), row
                submit({'name': 'Long origin', 'email': 'origin@example.org'}, origin='https://' + 'o' * 300)
                assert stored('origin@example.org')['source'] == ('https://' + 'o' * 300)[:200]
            with item('S1 honeypot: same response, nothing stored'):
                before = count()
                assert submit({**FORM, 'email': 'bot@example.org', 'website': 'https://spam.example'}) == '{"ok":true}'
                submit({'website': 'x'})
                submit({'name': 5, 'Bad Key': [], 'website': 1})
                assert count() == before and count("email = 'bot@example.org'") == 0
            with item('S1 duplicate: same email and identical details within 10 minutes stores nothing new'):
                before = count()
                assert submit(FORM) == '{"ok":true}'
                submit({key: FORM[key] for key in reversed(list(FORM))})  # key order does not matter
                submit({**FORM, 'name': 'Another name', 'utm_source': 'other'})
                assert count() == before
                submit({**FORM, 'requirements': FORM['requirements'] + ' More.'})
                submit({**FORM, 'email': 'grace2@example.org'})
                assert count() == before + 2 and count(f"email = '{FORM['email']}'") == 2
            with item('S1 duplicate: the window is 10 minutes'):
                def backdate(mail, seconds):
                    """No API sets created, so write it in the test's own database file."""
                    stamp = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime('%Y-%m-%d %H:%M:%S.000Z')
                    with contextlib.closing(sqlite3.connect(Path(data) / 'data.db', timeout=10)) as db, db:
                        assert db.execute('UPDATE enquiries SET created = ? WHERE email = ?', (stamp, mail)).rowcount == 1
                window = {'name': 'Window', 'email': 'window@example.org', 'requirements': 'Same text'}
                submit(window)
                backdate(window['email'], 570)
                submit(window)
                assert count("email = 'window@example.org'") == 1
                backdate(window['email'], 630)
                submit(window)
                assert count("email = 'window@example.org'") == 2
            with item('S1 name and email are required strings within their limits; email format is checked'):
                valid = {'name': 'Valid', 'email': 'valid@example.org'}
                before = count()
                rejected({}, 'name', 'email')
                rejected({**valid, 'name': ''}, 'name')
                rejected({**valid, 'name': '   '}, 'name')
                rejected({**valid, 'name': None}, 'name')
                rejected({**valid, 'email': ''}, 'email')
                rejected({**valid, 'name': 5}, 'name')
                rejected({**valid, 'email': ['a@example.org']}, 'email')
                rejected({**valid, 'name': 'n' * 201}, 'name')
                rejected({**valid, 'email': 'e' * 243 + '@example.org'}, 'email')
                for bad in ('nope', 'a@b', 'a b@example.org', 'a@b@example.org', '@example.org', 'a..b@example.org', 'a@-example.org', 'a@example.org.'):
                    rejected({**valid, 'email': bad}, 'email')
                for key in ('utm_source', 'utm_medium', 'utm_campaign'):
                    rejected({**valid, key: 'u' * 201}, key)
                    rejected({**valid, key: 7}, key)
                assert count() == before
                submit({'name': 'n' * 200, 'email': 'e' * 242 + '@example.org', 'utm_source': 'u' * 200, 'utm_medium': None})
                assert count() == before + 1
            with item('S1 review: name, email, and the utm keys refuse line breaks and control characters; details values keep them'):
                before = count()
                for bad in ("Ada\nJSON\ntouch x", 'Ada\u2028Record: x', 'Ada\x9bx', 'Ada\ttab', 'Ada\x00'):
                    rejected({**FORM, 'name': bad, 'email': 'control@example.org'}, 'name')
                rejected({**FORM, 'utm_source': 'a\nb', 'email': 'control@example.org'}, 'utm_source')
                assert count() == before
                submit({**FORM, 'email': 'multiline@example.org', 'requirements': 'line one\nline two'})
                assert json.loads(stored('multiline@example.org')['details'] if isinstance(stored('multiline@example.org')['details'], str) else json.dumps(stored('multiline@example.org')['details']))['requirements'] == 'line one\nline two'
            with item('S1 details: at most 20 keys, key names [a-z0-9_]{1,40}, string values of at most 4000 characters'):
                before = count()
                for key in ('Interest', 'bad-key', 'bad key', 'k' * 41, '', 'é', '__proto__', 'a.b'):
                    rejected({**valid, key: 'x'}, key[:40])
                for value in (5, 1.5, True, None, ['x'], {'x': 'y'}):
                    rejected({**valid, 'requirements': value}, 'requirements')
                rejected({**valid, 'requirements': 'x' * 4001}, 'requirements')
                rejected({**valid, 'requirements': 5, 'Workflow': 'x'}, 'requirements', 'Workflow')
                rejected({**valid, **{f'key_{number}': 'x' for number in range(21)}}, 'details')
                assert count() == before
                odd = {'constructor': 'c', 'tostring': 't', 'hasownproperty': 'h', 'k' * 40: 'x' * 4000, '0': '', 'closed_at': 'not a date'}
                submit({**valid, 'email': 'odd@example.org', **odd})
                assert stored('odd@example.org')['details'] == odd
                submit({**valid, 'email': 'twenty@example.org', **{f'key_{number}': 'x' for number in range(20)}})
                assert len(stored('twenty@example.org')['details']) == 20
            with item('S1 the body must be a JSON object sent as application/json'):
                before = count()
                for raw in (b'[1]', b'"text"', b'5', b'null', b'true', b'{oops', b'', b'{"name":"a","email":"a@example.org"} trailing'):
                    assert 'JSON object' in submit(None, expected=400, raw=raw)
                for content_type in ('application/x-www-form-urlencoded', 'text/plain', 'multipart/form-data; boundary=x', None):
                    submit(None, expected=415, raw=json.dumps({**valid, 'email': 'type@example.org'}).encode(), content_type=content_type)
                submit({**valid, 'email': 'charset@example.org'}, content_type='application/json; charset=utf-8')
                assert count() == before + 1
                assert call('GET', INTAKE)[0] in (404, 405)
            with item('S1 body limit of 16 KB, refused without reading the whole body'):
                def padded(size):
                    body = {**valid, 'email': f'size{size}@example.org', **{f'pad_{number}': 'x' * 3900 for number in range(4)}, 'pad_4': ''}
                    body['pad_4'] = 'x' * (size - len(json.dumps(body).encode()))
                    assert len(json.dumps(body).encode()) == size
                    return body
                before = count()
                submit(padded(16384))
                submit(padded(16385), expected=413)
                assert count() == before + 1
                def raw_socket(head, body, size):
                    """Offer up to size body bytes and stop as soon as the server answers. Returns the bytes sent and the answer."""
                    with socket.create_connection(('127.0.0.1', state['port']), timeout=10) as sock:
                        sock.sendall(f'POST {INTAKE} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n{head}\r\n\r\n'.encode())
                        sent = 0
                        while sent < size:
                            readable, writable, _ = select.select([sock], [sock], [], 10)
                            if readable or not writable:
                                break
                            sent += sock.send(body[sent % len(body):][:65536])
                        return sent, sock.recv(4096).decode()
                # A declared length over the limit is refused before any body byte is sent.
                sent, answer = raw_socket('Content-Length: 67108864', b'x', 0)
                assert sent == 0 and answer.startswith('HTTP/1.1 413'), answer
                # Without a declared length the handler reads 16 KB and Go discards at most 256 KB more before it answers
                # and closes the connection: the answer arrives while most of a 64 MB body is still unsent.
                sent, answer = raw_socket('Transfer-Encoding: chunked', b'2000\r\n' + b'x' * 8192 + b'\r\n', 64 << 20)
                assert answer.startswith('HTTP/1.1 413') and 'Connection: close' in answer and sent < 16 << 20, (sent, answer)
                assert count() == before + 1
            with item('S7 concurrency: 20 parallel distinct submissions are stored exactly once each; identical ones once'):
                before = count()
                with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
                    list(pool.map(lambda number: submit({**FORM, 'email': f'parallel{number}@example.org'}), range(20)))
                    list(pool.map(lambda number: submit({**FORM, 'email': 'same@example.org'}), range(20)))
                rows = sql("SELECT email, count(*) AS n FROM enquiries WHERE email LIKE 'parallel%' GROUP BY email")
                assert len(rows) == 20 and all(row['n'] == 1 for row in rows), rows
                assert count("email = 'same@example.org'") == 1 and count() == before + 21
            with item('S1 S7 creates write no audit_log row'):
                assert sql('SELECT count(*) AS n FROM audit_log')[0]['n'] == 0

            with item('S3 S7 enquiries is readable through /api/context/query, details through json_extract'):
                assert 'enquiries' in json.dumps(request('GET', '/api/context/schema', token=token))
                rows = sql(f"SELECT name, status, json_extract(details, '$.requirements') AS requirements, json_extract(details, '$.interest') AS interest "
                           f"FROM enquiries WHERE email = '{FORM['email']}' ORDER BY created LIMIT 1")
                assert rows == [{'name': FORM['name'], 'status': 'new', 'requirements': FORM['requirements'], 'interest': 'both'}], rows
                assert request('GET', ENQUIRIES + '?perPage=1', token=token)['totalItems'] == count()
                assert request('GET', ENQUIRIES)['items'] == []  # no credentials: nothing is listed

            first = stored('cors@example.org')
            one = f'{ENQUIRIES}/{first["id"]}'
            with item('S2 an agent changes status, person, and deal; updated_by comes from the token'):
                changed = request('PATCH', one, {'status': 'rejected', 'updated_by': agent2['id']}, token)
                assert (changed['status'], changed['updated_by']) == ('rejected', agent1['id']), changed
                assert set(changed) == COLUMNS, sorted(changed)
                assert audit(first['id']) == [{'action': 'update', 'collection': 'enquiries', 'record': first['id'], 'actor_type': 'agent',
                                               'actor': agent1['id'], 'changes': {'before': {'status': 'new'}, 'after': {'status': 'rejected'}}}]
            with item('S2 status qualified requires person'):
                answer = request('PATCH', one, {'status': 'qualified'}, token, expected=400)
                assert 'person' in answer['data'] and 'person' in answer['message'].lower(), answer
                request('PATCH', one, {'status': 'qualified', 'person': 'missingperson12'}, token, expected=400)
                person = request('POST', '/api/collections/people/records', {'name': 'Cors', 'email': 'cors@example.org', 'owner': agent1['id']}, token)
                pipeline = request('POST', '/api/collections/pipelines/records', {'name': 'Sales', 'active': True}, token)
                stage = request('POST', '/api/collections/stages/records', {'name': 'New', 'pipeline': pipeline['id'], 'position': 0}, token)
                deal = request('POST', '/api/collections/deals/records', {'title': 'Enquiry', 'stage': stage['id'], 'person': person['id'], 'owner': agent1['id'],
                                                                         'value_minor': 0, 'currency': 'EUR', 'status': 'open'}, token)
                qualified = request('PATCH', one, {'status': 'qualified', 'person': person['id'], 'deal': deal['id']}, token)
                assert (qualified['status'], qualified['person'], qualified['deal']) == ('qualified', person['id'], deal['id']), qualified
                answer = request('PATCH', one, {'person': ''}, token, expected=400)
                assert 'person' in answer['data'], answer
                assert audit(first['id'])[-1]['changes'] == {'before': {'status': 'rejected', 'person': '', 'deal': ''},
                                                            'after': {'status': 'qualified', 'person': person['id'], 'deal': deal['id']}}
            with item('S2 triage works inside a batch and is audited'):
                second = stored('cors2@example.org')
                result = request('POST', '/api/batch', {'requests': [{'method': 'PATCH', 'url': f'{ENQUIRIES}/{second["id"]}', 'body': {'status': 'spam'}}]}, token)
                assert result[0]['status'] == 200 and result[0]['body']['updated_by'] == agent1['id'], result
                assert audit(second['id'])[-1]['changes'] == {'before': {'status': 'new'}, 'after': {'status': 'spam'}}
            with item('S2 parallel updates of one enquiry are applied or refused with 409, and every applied change is audited'):
                logged = len(audit(second['id']))
                def flip(number):
                    return call('PATCH', f'{ENQUIRIES}/{second["id"]}', {'status': ('rejected', 'spam')[number % 2]}, token)[0]
                with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
                    statuses = list(pool.map(flip, range(10)))
                assert set(statuses) <= {200, 409} and 200 in statuses, statuses
                chain = [row['changes'] for row in audit(second['id'])[logged:]]
                assert all(earlier['after'] == later['before'] for earlier, later in zip(chain, chain[1:])), chain
                # Every winner may have written the status that was already stored, which is a no-op and writes no row.
                assert not chain or chain[-1]['after']['status'] == request('GET', f'{ENQUIRIES}/{second["id"]}', token=token)['status'], chain
            with item('S2 S7 an agent cannot change a submitted value, create, or delete; no credentials change nothing'):
                logged = len(audit())
                for field, value in (('name', 'Changed'), ('email', 'changed@example.org'), ('details', {'requirements': 'changed'}), ('source', 'x'),
                                     ('utm_source', 'x'), ('utm_medium', 'x'), ('utm_campaign', 'x')):
                    request('PATCH', one, {field: value}, token, expected=404)
                    request('PATCH', one, {field: value, 'status': 'spam'}, token, expected=404)
                    request('PATCH', one, {field: first[field]}, token, expected=404)  # even an unchanged value
                request('PATCH', one, {'status': 'nonsense'}, token, expected=400)
                request('POST', ENQUIRIES, {'name': 'Made', 'email': 'made@example.org', 'status': 'new'}, token, expected=403)
                request('DELETE', one, token=token, expected=403)
                request('POST', ENQUIRIES, {'name': 'Made', 'email': 'made@example.org', 'status': 'new'}, expected=403)
                request('PATCH', one, {'status': 'spam'}, expected=404)
                request('DELETE', one, expected=403)
                result = request('POST', '/api/batch', {'requests': [{'method': 'DELETE', 'url': one}]}, token, expected=(400, 403))
                after = request('GET', one, token=token)
                assert {**after, 'updated': ''} == {**qualified, 'updated': ''} and len(audit()) == logged, after
            with item('S2 a superuser delete is audited with the status only, and the audit log holds no submitted value'):
                request('PATCH', one, {'name': 'Corrected name', 'details': {'requirements': 'Corrected text'}}, admin)
                assert len(audit()) == logged, 'a change of submitted values must not reach audit_log'
                request('PATCH', one, {'name': 'Corrected again', 'status': 'rejected', 'person': ''}, admin)
                assert audit(first['id'])[-1]['changes'] == {'before': {'status': 'qualified', 'person': person['id']}, 'after': {'status': 'rejected', 'person': ''}}
                request('DELETE', one, token=admin, expected=204)
                request('GET', one, token=admin, expected=404)
                last = audit(first['id'])[-1]
                assert last == {'action': 'delete', 'collection': 'enquiries', 'record': first['id'], 'actor_type': 'superuser', 'actor': '',
                                'changes': {'before': {'status': 'rejected'}}}, last
                text = json.dumps(sql("SELECT * FROM audit_log WHERE collection = 'enquiries'"))
                for value in ('cors@example.org', FORM['name'], 'Corrected', FORM['workflow'], 'newsletter', SITE):
                    assert value not in text, value
                assert first['id'] in text
            with item('S1 successful submissions stay out of the request log, which holds the client address'):
                def logged_requests(condition):
                    return request('GET', '/api/logs?perPage=1&filter=' + urllib.request.quote(f"data.url = '{INTAKE}' && " + condition), token=admin)['totalItems']
                for _ in range(100):  # the log writer flushes every few seconds; the rejected requests show that it did
                    if logged_requests('data.status = 413'):
                        break
                    time.sleep(.2)
                else:
                    raise AssertionError('the rejected requests never reached the request log')
                assert logged_requests('data.status = 400') > 0 and logged_requests('data.status = 200') == 0
            output = stop()
            assert 'goja' not in output and 'panic' not in output, output

            closed = free_port()
            limits = {'MAILER_FROM_ADDRESS': 'Info <info@notifications.example.test>', 'SMTP_ADDRESS': '127.0.0.1', 'SMTP_PORT': str(mailbox.server_address[1]),
                      'DEALCONTEXT_TRUSTED_PROXY_HEADER': 'X-Forwarded-For', 'DEALCONTEXT_RATE_LIMITS': 'true'}
            start('second', limits)
            admin = login('_superusers', ADMIN, ADMIN_PASSWORD, '198.51.100.1')
            def total():
                return request('GET', ENQUIRIES + '?perPage=1', token=admin)['totalItems']
            request('PATCH', recipient_path, {'enabled': True}, admin)
            def delivered(number, address=NOTIFY):
                """Messages to one recipient, exactly this many. Superuser login alerts go elsewhere."""
                for _ in range(100):
                    found = [message for message in mailbox.messages if message['to'] == [address]]
                    if len(found) >= number:
                        break
                    time.sleep(.1)
                time.sleep(.3)
                found = [message for message in mailbox.messages if message['to'] == [address]]
                assert len(found) == number, found
                return found
            with item('S6 a stored enquiry sends one plain-text email with the name, the email, and the record id, not the free text'):
                # A name that tries to add a mail header never gets as far as the mailer.
                rejected({**FORM, 'email': 'notify@example.org', 'name': 'Notify\r\nBcc: someone@example.org'}, 'name', ip='198.51.100.10')
                note = {**FORM, 'email': 'notify@example.org', 'name': 'Notify Person'}
                submit(note, ip='198.51.100.10')
                message, row = delivered(1)[0], stored('notify@example.org')
                assert (message['from'], message['subject']) == ('info@notifications.example.test', 'New enquiry'), message
                assert [kind for kind in message['types'] if not kind.startswith('multipart/')] == ['text/plain'], message['types']
                assert 'Notify Person' in message['text'] and 'Bcc' not in message['text'] and 'notify@example.org' in message['text'] and row['id'] in message['text'], message
                for value in (FORM['requirements'], FORM['workflow'], FORM['timeline'], 'Quotes', 'audit'):
                    assert value not in message['text'] and value not in message['subject'], message
            with item('S6 a duplicate, a honeypot submission, and a rejected one send no email'):
                submit(note, ip='198.51.100.10')
                submit({**note, 'email': 'notify-bot@example.org', 'website': 'x'}, ip='198.51.100.10')
                submit({**note, 'email': 'nope'}, expected=400, ip='198.51.100.10')
                delivered(1)
            with item('S6 the response does not wait for the mail server'):
                mailbox.delay = 4
                begin = time.monotonic()
                submit({**FORM, 'email': 'slow@example.org'}, ip='198.51.100.11')
                assert time.monotonic() - begin < 2, time.monotonic() - begin
                delivered(2)
                mailbox.delay = 0
            with item('S4 /api/intake/ allows 5 requests per 60 seconds per client address; preflight requests do not count'):
                rules = request('GET', '/api/settings', token=admin)['rateLimits']['rules']
                position = [rule['label'] for rule in rules]
                assert {'label': '/api/intake/', 'audience': '', 'duration': 60, 'maxRequests': 5} in rules, rules
                assert position.index('/api/intake/') < position.index('/api/'), position
                for attempt in range(3):  # a window starts at the client's first request; a run slower than that repeats
                    limited, begin, before = f'203.0.113.{30 + attempt}', time.monotonic(), total()
                    for _ in range(8):
                        status, headers, _ = call('OPTIONS', INTAKE, headers={'Origin': SITE, 'Access-Control-Request-Method': 'POST',
                                                                              'Access-Control-Request-Headers': 'content-type', 'X-Forwarded-For': limited})
                        assert status == 204, status
                    for number in range(5):
                        submit({**FORM, 'email': f'limit{attempt}{number}@example.org'}, ip=limited)
                    if time.monotonic() - begin < 58:
                        break
                answer = json.loads(submit({**FORM, 'email': 'limit5@example.org'}, expected=429, ip=limited))
                assert answer['status'] == 429, answer
                submit({**FORM, 'email': 'limit5@example.org', 'website': 'x'}, expected=429, ip=limited)
                submit({**FORM, 'email': 'limit5@example.org'}, expected=429, ip='203.0.113.99, ' + limited)  # the rightmost entry counts
                submit({**FORM, 'email': 'limit6@example.org'}, ip='203.0.113.21')
                assert total() == before + 6
                request('GET', '/api/health', headers={'X-Forwarded-For': limited})  # the other rules are separate
                mails = 2 + 5 * (attempt + 1) + 1
                delivered(mails)
            stop()

            start('third', {})
            with item('S6 disabling recipients takes effect without a restart, even with SMTP enabled'):
                admin = login('_superusers', ADMIN, ADMIN_PASSWORD, '198.51.100.1')
                assert request('GET', '/api/settings', token=admin)['smtp']['enabled'] is True
                request('PATCH', recipient_path, {'enabled': False}, admin)
                submit({**FORM, 'email': 'silent@example.org'}, ip='198.51.100.12')
                assert stored('silent@example.org')['status'] == 'new'
                delivered(mails)
            with item('S6 live recipient creation and editing send separate private messages to enabled addresses'):
                extra = request('POST', RECIPIENTS, {'email': 'extra@example.test', 'enabled': True}, admin)
                disabled = request('POST', RECIPIENTS, {'email': 'disabled@example.test'}, admin)
                request('PATCH', recipient_path, {'enabled': True}, admin)
                submit({**FORM, 'email': 'multiple@example.org'}, ip='198.51.100.14')
                mails += 1
                first_message = delivered(mails)[-1]
                extra_message = delivered(1, extra['email'])[0]
                assert extra['email'] not in first_message['headers'], first_message
                assert NOTIFY not in extra_message['headers'], extra_message
                delivered(0, disabled['email'])
                extra_path = RECIPIENTS + '/' + extra['id']
                request('PATCH', extra_path, {'email': 'replacement@example.test'}, admin)
                submit({**FORM, 'email': 'edited@example.org'}, ip='198.51.100.15')
                mails += 1
                delivered(mails)
                delivered(1, 'replacement@example.test')
                delivered(1, extra['email'])
                request('DELETE', extra_path, token=admin, expected=204)
                submit({**FORM, 'email': 'deleted@example.org'}, ip='198.51.100.16')
                mails += 1
                delivered(mails)
                delivered(1, 'replacement@example.test')
            with item('S6 one rejected recipient does not prevent delivery to another or expose addresses in logs'):
                failing = request('POST', RECIPIENTS, {'id': '000000000000001', 'email': 'reject@example.test', 'enabled': True}, admin)
                mailbox.reject.add(failing['email'])
                submit({**FORM, 'email': 'partial@example.org'}, ip='198.51.100.17')
                row = stored('partial@example.org')
                mails += 1
                delivered(mails)
                delivered(0, failing['email'])
                for _ in range(100):
                    found = request('GET', '/api/logs?filter=' + urllib.request.quote("message ~ 'notification email'"), token=admin)['items']
                    if row['id'] in json.dumps(found):
                        break
                    time.sleep(.2)
                else:
                    raise AssertionError('the recipient rejection never reached the log')
                text = json.dumps(found)
                assert failing['id'] in text and failing['email'] not in text and NOTIFY not in text, text
                assert 'partial@example.org' not in text and FORM['name'] not in text, text
                request('DELETE', RECIPIENTS + '/' + failing['id'], token=admin, expected=204)
                mailbox.reject.clear()
            output = stop()
            assert failing['email'] not in output, output

            start('fourth', {**limits, 'SMTP_PORT': str(closed)})
            with item('S6 a mail failure is logged and does not fail the request'):
                admin = login('_superusers', ADMIN, ADMIN_PASSWORD, '198.51.100.1')
                submit({**FORM, 'email': 'unreachable@example.org'}, ip='198.51.100.13')
                row = stored('unreachable@example.org')
                for _ in range(100):
                    found = request('GET', '/api/logs?filter=' + urllib.request.quote("message ~ 'notification email'"), token=admin)['items']
                    if row['id'] in json.dumps(found):
                        break
                    time.sleep(.2)
                else:
                    raise AssertionError('the mail failure never reached the log')
                text = json.dumps(found)
                assert row['id'] in text and recipient['id'] in text and NOTIFY not in text and 'unreachable@example.org' not in text and FORM['name'] not in text, text
                delivered(mails)
            start_output = stop()
            assert 'unreachable@example.org' not in start_output, start_output

            start('cap', {'DEALCONTEXT_RATE_LIMITS': 'false', 'DEALCONTEXT_INTAKE_HOURLY_CAP': '3'})
            with item('S1 review: a global hourly cap bounds stored rows whatever the client addresses; the refusal stores nothing'):
                admin = login('_superusers', ADMIN, ADMIN_PASSWORD)
                rows = lambda: request('GET', ENQUIRIES + '?perPage=1', token=admin)['totalItems']
                before = rows()
                request('POST', '/api/batch', {'requests': []}, admin, expected=400)  # superuser still works; the cap is only on the public route
                hour_old = (datetime.now(timezone.utc) - timedelta(minutes=61)).strftime('%Y-%m-%d %H:%M:%S.000Z')
                stop()
                database = sqlite3.connect(Path(tmp) / 'pb_data' / 'data.db')
                database.execute('UPDATE enquiries SET created = ?', (hour_old,))
                database.commit()
                database.close()
                start('cap2', {'DEALCONTEXT_RATE_LIMITS': 'false', 'DEALCONTEXT_INTAKE_HOURLY_CAP': '3'})
                for number in range(3):
                    submit({**FORM, 'email': f'cap{number}@example.org'})
                answer = json.loads(submit({**FORM, 'email': 'cap3@example.org'}, expected=429))
                assert 'last hour' in answer['message'], answer
                submit({**FORM, 'email': 'cap0@example.org'})  # a duplicate is still answered like a success and stores nothing
                admin = login('_superusers', ADMIN, ADMIN_PASSWORD)
                assert rows() == before + 3, (before, rows())
            with item('S1 review: at most 5 enquiries per email inside the duplicate window; further ones are dropped silently'):
                stop()
                start('cap3', {'DEALCONTEXT_RATE_LIMITS': 'false'})
                for number in range(7):
                    submit({**FORM, 'email': 'flood@example.org', 'requirements': f'variant {number}'})
                admin = login('_superusers', ADMIN, ADMIN_PASSWORD)
                found = request('GET', ENQUIRIES + '?filter=' + urllib.request.quote("email='flood@example.org'"), token=admin)['totalItems']
                assert found == 5, found
            stop()
        finally:
            stop()
            mailbox.shutdown()
            mailbox.server_close()
    print('PASS: public enquiry endpoint (website payload, validation, details limits, honeypot, duplicates, JSON only, 16 KB body limit, '
          'no address or user agent stored, no audit row on create, parallel submissions), agent triage (status, person, deal, updated_by, '
          'qualified needs person, audited, submitted values locked, no create or delete), status-only delete audit, SQL access, '
          'intake rate limit per client address, CORS origins from the entrypoint, live notification mailing list, recipient permissions and failure isolation')


if __name__ == '__main__':
    main()
