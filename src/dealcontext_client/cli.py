#!/usr/bin/env python3
"""Command-line client for a DealContext CRM server. Packaged Python CLI.

Configuration comes from three environment variables:
  DEALCONTEXT_URL             server address, for example https://crm.example.com
  DEALCONTEXT_AGENT_EMAIL     email of an account in the `agents` collection
  DEALCONTEXT_AGENT_PASSWORD  optional password; Google sessions use login --google

Exit codes: 0 success; 1 HTTP or transport error; 2 usage or configuration error;
3 `check` found schema differences; 4 HTTP 409 (read the record again, then retry).
"""
import argparse
import base64
import hashlib
import http.client
import http.server
import json
import os
from pathlib import Path
from importlib.resources import files
from observecontext_client.instrumentation import instrument_cli
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ENV = ['DEALCONTEXT_URL', 'DEALCONTEXT_AGENT_EMAIL', 'DEALCONTEXT_AGENT_PASSWORD']
SCHEMA_FILE = files('dealcontext_client').joinpath('schema.json')
STAMPS = ('created_by', 'updated_by')
ID_ALPHABET = 'abcdefghijklmnopqrstuvwxyz0123456789'
TIMEOUT = 30
USER_AGENT = 'DealContext/1.0'
# Bump with the server recommendation when installed skill behavior or guidance changes.
SKILL_REVISION = 3
SKILL_CHECK_TTL = 300
SKILL_VERSION_PATH = '/api/dealcontext/skill-version'
hidden = []  # The password and tokens. say() masks them in everything it prints.


class Fail(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def hide(value):
    if value:
        hidden.append(value)
    return value


def say(text, stream=sys.stderr):
    for value in hidden:
        text = text.replace(value, '***')
    print(text, file=stream)


def dump(data, pretty=False):
    if pretty:
        return json.dumps(data, indent=2, ensure_ascii=False)
    return json.dumps(data, separators=(',', ':'), ensure_ascii=False)


def config(names=ENV[:2]):
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise Fail(2, 'missing environment variable: ' + ', '.join(missing) + '. Ask the user to set every missing variable; do not look for credentials elsewhere.')
    url = os.environ[ENV[0]].rstrip('/')
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise Fail(2, 'DEALCONTEXT_URL must be an HTTP(S) URL without credentials, query, or fragment')
    if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise Fail(2, 'Use HTTPS for a remote DealContext server')
    return {'url': url, 'email': os.environ[ENV[1]], 'password': hide(os.environ.get(ENV[2]))}


# Token cache: one file per server URL and email, readable only by the current user.

def cache_file(cfg):
    base = os.environ.get('XDG_CACHE_HOME') or str(Path.home() / '.cache')
    key = hashlib.sha256((cfg['url'] + '\n' + cfg['email']).encode()).hexdigest()[:32]
    return Path(base) / 'dealcontext' / (key + '.json')


def load_session(cfg):
    try:
        session = json.loads(cache_file(cfg).read_text())
        if not isinstance(session, dict) or session.get('url') != cfg['url'] or session.get('email') != cfg['email']:
            return None
        return session if isinstance(session.get('token'), str) and hide(session['token']) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_session(cfg, session):
    path = cache_file(cfg)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.session-', delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(session, handle)
        os.replace(temporary, path)
    except OSError as error:
        say(f'note: token not cached ({error.strerror}); sign-in will be required again')


# HTTP

class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A followed redirect would turn a POST into a GET and could send the token to another host."""
    def redirect_request(self, *args):
        return None


opener = urllib.request.build_opener(NoRedirect)


def send(cfg, method, path, body=None, token=None, timeout=TIMEOUT):
    """Send one request. Returns (status, parsed JSON body, or the text when it is not JSON)."""
    headers = {'Content-Type': 'application/json', 'User-Agent': USER_AGENT}
    if token:
        headers['Authorization'] = token
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(cfg['url'] + path, data=data, headers=headers, method=method)
    try:
        with opener.open(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
        if 300 <= status < 400:
            raise Fail(1, f'HTTP {status}: the server redirects to {error.headers.get("Location")}. Set DEALCONTEXT_URL to the final address.')
    except (OSError, ValueError, http.client.HTTPException) as error:
        reason = getattr(error, 'reason', error)
        raise Fail(1, f'cannot reach {cfg["url"]}: {reason}')
    text = raw.decode('utf-8', 'replace')
    try:
        return status, json.loads(text) if text else None
    except ValueError:
        return status, text[:2000]


def login(cfg):
    if not cfg.get('password'):
        raise Fail(2, 'Set DEALCONTEXT_AGENT_PASSWORD for password login, or run dealcontext login --google for browser sign-in.')
    status, data = send(cfg, 'POST', '/api/collections/agents/auth-with-password', {'identity': cfg['email'], 'password': cfg['password']})
    if status != 200 or not isinstance(data, dict) or 'token' not in data:
        raise Fail(1, f'login as {cfg["email"]} failed: HTTP {status}\n{dump(data)}\nCheck the three DEALCONTEXT_ variables with the user. User credentials only.')
    session = {'url': cfg['url'], 'email': cfg['email'], 'token': hide(data['token'])}
    save_session(cfg, session)
    return session


def auth_session(cfg, data, method):
    """Accept only the expected agents identity; never retain provider metadata."""
    token = data.get('token') if isinstance(data, dict) else None
    if isinstance(token, str):
        hide(token)
    record = data.get('record') if isinstance(data, dict) else None
    if (not isinstance(token, str) or not token or not isinstance(record, dict) or record.get('collectionName') != 'agents'
            or not record.get('id') or not isinstance(record.get('email'), str)
            or record['email'].casefold() != cfg['email'].casefold()):
        raise Fail(1, 'Authentication returned an unexpected identity; no session saved. Check DEALCONTEXT_AGENT_EMAIL.')
    session = {'url': cfg['url'], 'email': cfg['email'], 'token': token, 'method': method, 'refreshed_at': time.time()}
    save_session(cfg, session)
    return session


def oauth_send(cfg, method, path, body=None, token=None):
    try:
        return send(cfg, method, path, body, token)
    except Fail:
        # Redirect locations and transport errors may contain authorization credentials.
        raise Fail(1, 'OAuth authentication request failed; check the server URL and connection, then retry.') from None


def oauth_refresh_needed(session):
    """Unverified JWT claims only schedule renewal; the server always authenticates the token."""
    try:
        payload = session['token'].split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        now = time.time()
        refreshed_at = session['refreshed_at']
        return (not 0 <= now - refreshed_at < 300 or claims['exp'] <= now + 60)
    except (ValueError, TypeError, KeyError, IndexError):
        return True


def google_login(cfg, port=8765, timeout=180):
    if not 1 <= port <= 65535 or not 1 <= timeout <= 600:
        raise Fail(2, 'OAuth port must be 1–65535 and timeout must be 1–600 seconds')
    status, data = oauth_send(cfg, 'GET', '/api/collections/agents/auth-methods')
    oauth = data.get('oauth2', {}) if isinstance(data, dict) else {}
    providers = oauth.get('providers', [])
    provider = next((p for p in providers if isinstance(p, dict) and p.get('name') == 'google'), None)
    if status != 200 or not oauth.get('enabled') or not provider:
        raise Fail(1, 'Google OAuth is not enabled on this DealContext server.')
    auth_url = urllib.parse.urlsplit(provider.get('authURL', ''))
    if auth_url.scheme != 'https' or auth_url.hostname != 'accounts.google.com' or auth_url.username or auth_url.password or auth_url.fragment:
        raise Fail(1, 'Server returned an unexpected Google authorization URL.')
    state = secrets.token_urlsafe(32)
    verifier = hide(secrets.token_urlsafe(48))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    redirect = f'http://127.0.0.1:{port}/callback'
    metadata = urllib.parse.parse_qs(auth_url.query)
    client_ids = metadata.get('client_id', [])
    if len(client_ids) != 1 or not client_ids[0]:
        raise Fail(1, 'Server returned an invalid Google client ID.')
    params = {'client_id': client_ids[0]}
    params.update(state=state, code_challenge=challenge, code_challenge_method='S256', redirect_uri=redirect,
                  login_hint=cfg['email'], response_type='code', scope='openid email profile', access_type='online')
    url = urllib.parse.urlunsplit(auth_url._replace(query=urllib.parse.urlencode(params)))
    outcome = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Callback URLs contain credentials.

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            values = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            code = values.get('code', [])
            valid_state = values.get('state', [])
            valid = (self.headers.get('Host') == f'127.0.0.1:{port}' and parsed.path == '/callback' and len(valid_state) == 1
                     and secrets.compare_digest(valid_state[0], state))
            if not valid:
                status, message = 400, 'Invalid sign-in callback. Return to your terminal.'
            elif 'error' in values:
                outcome['error'] = 'Google sign-in was denied or cancelled; run dealcontext login --google to retry.'
                status, message = 400, 'Sign-in was cancelled. Return to your terminal.'
            elif len(code) != 1 or not code[0]:
                outcome['error'] = 'Google returned an invalid sign-in callback.'
                status, message = 400, 'Invalid sign-in callback. Return to your terminal.'
            else:
                outcome['code'] = hide(code[0])
                status, message = 200, 'Authorization received. Return to your terminal to check sign-in.'
            self.send_response(status)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            self.wfile.write(message.encode())

    class Listener(http.server.HTTPServer):
        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(1)
            return connection, address

        def handle_error(self, request, client_address):
            pass  # Never print request data or exception tracebacks.

    try:
        server = Listener(('127.0.0.1', port), Callback)
    except OSError:
        raise Fail(1, f'Cannot listen on 127.0.0.1:{port}; check for another login process or choose --port.')
    with server:
        server.timeout = 0.25
        say(f'For SSH, forward this port: ssh -L {port}:127.0.0.1:{port} user@ssh-host')
        say('Open this URL in your browser (keep it private):\n' + url)
        deadline = time.monotonic() + timeout
        while not outcome and time.monotonic() < deadline:
            server.handle_request()
    if not outcome:
        raise Fail(1, 'Google sign-in timed out; run dealcontext login --google to retry.')
    if 'error' in outcome:
        raise Fail(1, outcome['error'])
    status, data = oauth_send(cfg, 'POST', '/api/collections/agents/auth-with-oauth2', {
        'provider': 'google', 'code': outcome['code'], 'codeVerifier': verifier, 'redirectURL': redirect,
    })
    if status != 200:
        raise Fail(1, f'Google sign-in failed: HTTP {status}. Check Workspace eligibility, account access, and the redirect URI with your operator.')
    return auth_session(cfg, data, 'google')


def token_rejected(cfg, token):
    return send(cfg, 'POST', '/api/context/query', {'sql': 'SELECT 1'}, token)[0] == 401


def call(cfg, method, path, body=None):
    """Authenticated request. Returns (status, data).

    Only the SQL endpoints answer an expired or revoked token with 401. The records API treats it as no
    token and answers 400, 403, or 404. So after such an error with a cached token, check the token,
    and if the server rejects it, log in once and send the request once more. The first attempt wrote nothing.
    """
    session = load_session(cfg)
    cached = session is not None
    if not cached:
        session = login(cfg)
    if session.get('method') == 'google' and (oauth_refresh_needed(session) or path == '/api/collections/agents/auth-refresh'):
        # Renew at most every five minutes, or near expiry, to respect auth rate limits.
        status, data = oauth_send(cfg, 'POST', '/api/collections/agents/auth-refresh', token=session['token'])
        if status != 200:
            raise Fail(1, f'Google session could not be refreshed (HTTP {status}); run dealcontext login --google again.')
        session = auth_session(cfg, data, 'google')
        if path == '/api/collections/agents/auth-refresh':
            return status, data
    status, data = send(cfg, method, path, body, session['token'])
    if cached and 400 <= status < 500 and status != 409 and (status == 401 or token_rejected(cfg, session['token'])):
        if session.get('method') == 'google':
            raise Fail(1, 'Google session was rejected; run dealcontext login --google again.')
        session = login(cfg)
        status, data = send(cfg, method, path, body, session['token'])
    return status, data


def batch_failures(data):
    """The failed requests of a rejected batch as (index, status, message). The server reports them under data.requests."""
    try:
        failures = []
        for index, entry in data['data']['requests'].items():
            response = entry['response']
            fields = [f'{name}: {detail.get("message")}' for name, detail in (response.get('data') or {}).items() if isinstance(detail, dict)]
            failures.append((index, response.get('status'), ' '.join([response.get('message') or ''] + fields)))
        return failures
    except (AttributeError, KeyError, TypeError):
        return []


def must(cfg, method, path, body=None):
    """Like call(), but an HTTP error ends the command with the server's status and body on stderr."""
    status, data = call(cfg, method, path, body)
    if status < 400:
        return data
    lines = [f'HTTP {status} from {method} {path}', dump(data, pretty=True)]
    conflict = status == 409
    if path == '/api/batch':
        lines.append('Nothing in this batch was saved.')
        for index, inner_status, message in batch_failures(data):
            lines.append(f'Failed request index {index}: HTTP {inner_status}: {message}')
            conflict = conflict or inner_status == 409
    if conflict:
        lines.append('HTTP 409: another request changed the record first. Read it again, confirm the change still applies, then retry.')
    raise Fail(4 if conflict else 1, '\n'.join(lines))


# Commands

def skill_cache_file(cfg):
    return cache_file(cfg).with_suffix('.skill-version.json')


def valid_revision(value):
    return type(value) is int and 0 < value <= 2147483647


def skill_advisory(cfg, force=False):
    """Check a small authenticated recommendation; never gate commands on a revision."""
    path = skill_cache_file(cfg)
    cached = None
    if not force:
        try:
            candidate = json.loads(path.read_text())
            if (isinstance(candidate, dict)
                    and candidate.get('installedRevision') == SKILL_REVISION
                    and type(candidate.get('checkedAt')) in (int, float)
                    and 0 <= time.time() - candidate['checkedAt'] < SKILL_CHECK_TTL
                    and 'recommendedRevision' in candidate
                    and (candidate['recommendedRevision'] is None or valid_revision(candidate['recommendedRevision']))):
                cached = candidate
        except (OSError, ValueError, TypeError):
            pass
    if cached is None:
        # Authentication errors remain normal command errors. Never retry a bad
        # password merely because the advisory request failed.
        session = load_session(cfg) or login(cfg)
        for attempt in range(2):
            try:
                status, data = send(cfg, 'GET', SKILL_VERSION_PATH, token=session['token'], timeout=3)
            except Fail:
                status, data = 0, None
            if status == 401 and attempt == 0:
                if session.get('method') == 'google':
                    raise Fail(1, 'Google session was rejected; run dealcontext login --google again.')
                session = login(cfg)
                continue
            break
        if status == 404:
            recommended = None  # Older servers have no revision endpoint.
        elif status == 200 and isinstance(data, dict) and valid_revision(data.get('recommendedRevision')):
            recommended = data['recommendedRevision']
        else:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            say('WARNING: could not verify the recommended DealContext skill revision. '
                'Continuing; run `dealcontext check` to retry.')
            return
        cached = {'installedRevision': SKILL_REVISION, 'recommendedRevision': recommended, 'checkedAt': time.time()}
        # A separate atomic cache keeps advisory data out of the token cache and
        # avoids partial JSON when commands run concurrently. Cache failures are optional.
        temporary = None
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.skill-version-', delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(cached, handle)
            os.replace(temporary, path)
        except OSError:
            pass
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
    recommended = cached['recommendedRevision']
    if recommended is not None and recommended > SKILL_REVISION:
        say(f'WARNING: DealContext recommends skill revision {recommended}; installed revision is {SKILL_REVISION}. '
            'Update with `npx skills update dealcontext -g` if installed with the skills CLI, '
            'or replace your installed skill with the latest skills/dealcontext directory from the DealContext repository. '
            'This is advisory; the command will continue.')


def read_json(text, kind, what):
    if text == '-':
        text = sys.stdin.read()
    try:
        value = json.loads(text)
    except ValueError as error:
        raise Fail(2, f'{what} is not valid JSON: {error}')
    if not isinstance(value, kind):
        raise Fail(2, f'{what} must be a JSON {"array" if kind is list else "object"}')
    return value


def record_body(text):
    body = read_json(text, dict, 'the record body')
    for field in STAMPS:
        if field in body:
            del body[field]
            say(f'note: removed {field} from the body; the server sets it from your login')
    if not body:
        raise Fail(2, 'the record body has no fields to send')
    return body


def records(collection, record=None):
    path = f'/api/collections/{urllib.parse.quote(collection, safe="")}/records'
    return path if record is None else f'{path}/{urllib.parse.quote(record, safe="")}'


def columns(tables):
    return {table['name']: {column['name']: column.get('type') for column in table['columns']} for table in tables}


def check(cfg):
    """Compare the live SQL schema with references/schema.json. Returns the exit code."""
    try:
        reference = columns(json.loads(SCHEMA_FILE.read_text())['tables'])
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Fail(2, f'cannot read {SCHEMA_FILE}: {error}')
    live = columns(must(cfg, 'GET', '/api/context/schema')['tables'])
    differences = []
    for table in sorted(set(live) | set(reference)):
        if table not in reference:
            differences.append(f'table {table}: on the server, not in the reference files')
        elif table not in live:
            differences.append(f'table {table}: in the reference files, not on the server')
        else:
            for column in sorted(set(live[table]) | set(reference[table])):
                if column not in reference[table]:
                    differences.append(f'column {table}.{column}: on the server, not in the reference files')
                elif column not in live[table]:
                    differences.append(f'column {table}.{column}: in the reference files, not on the server')
                elif live[table][column] != reference[table][column]:
                    differences.append(f'column {table}.{column}: type {live[table][column]} on the server, {reference[table][column]} in the reference files')
    if not differences:
        say(f'OK: the live schema matches references/schema.json ({len(live)} tables)', sys.stdout)
        return 0
    for line in differences:
        say(line, sys.stdout)
    say('The server is authoritative: run `dealcontext schema` and follow the server\'s error messages where the reference files disagree. '
        'Ask the user to update this skill.', sys.stdout)
    return 3


def run(args):
    if args.command == 'newid':
        print(''.join(secrets.choice(ID_ALPHABET) for _ in range(15)))
        return 0
    if args.command == 'logout':
        cfg = config(ENV[:2])
        path = cache_file(cfg)
        path.unlink(missing_ok=True)
        skill_cache_file(cfg).unlink(missing_ok=True)
        say(f'removed {path}', sys.stdout)
        return 0
    cfg = config()
    if args.command == 'login':
        google_login(cfg, args.port, args.timeout)
        say(f'Signed in as {cfg["email"]} at {cfg["url"]}', sys.stdout)
        return 0
    # Reject malformed writes before authentication or any network request.
    body = None
    if args.command in ('create', 'update'):
        body = record_body(args.json)
    elif args.command == 'batch':
        requests = read_json(args.json, list, 'the batch')
        if not all(isinstance(entry, dict) for entry in requests):
            raise Fail(2, 'each batch entry must be an object with method, url, and body')
        body = {'requests': requests}
    skill_advisory(cfg, force=args.command == 'check')
    if args.command == 'check':
        return check(cfg)
    if args.command == 'whoami':
        # auth-refresh returns the account behind the token. The new token it also returns is not used.
        refreshed = must(cfg, 'POST', '/api/collections/agents/auth-refresh')
        hide(refreshed.get('token'))
        record = refreshed['record']
        data = {'id': record['id'], 'name': record.get('name', ''), 'email': cfg['email'], 'url': cfg['url']}
    elif args.command == 'schema':
        data = must(cfg, 'GET', '/api/context/schema')
    elif args.command == 'sql':
        query = sys.stdin.read() if args.query == '-' else args.query
        data = must(cfg, 'POST', '/api/context/query', {'sql': query})
        if isinstance(data, dict) and data.get('truncated'):
            say(f'WARNING: result truncated to {len(data.get("rows", []))} rows by the server\'s row or byte limit. Select fewer columns, narrow the query, or page with ORDER BY and LIMIT/OFFSET.')
    elif args.command == 'get':
        data = must(cfg, 'GET', records(args.collection, args.id))
    elif args.command == 'create':
        data = must(cfg, 'POST', records(args.collection), body)
    elif args.command == 'update':
        data = must(cfg, 'PATCH', records(args.collection, args.id), body)
    elif args.command == 'batch':
        data = must(cfg, 'POST', '/api/batch', body)
    say(dump(data, args.pretty), sys.stdout)
    return 0


def parse(argv):
    pretty = argparse.ArgumentParser(add_help=False)
    pretty.add_argument('--pretty', action='store_true', default=argparse.SUPPRESS, help='indent the JSON output')
    parser = argparse.ArgumentParser(prog='dealcontext', parents=[pretty], description='DealContext CRM client. Reads with SQL, writes through the records API. There is no delete command: agents cannot delete records.',
                                     epilog='Environment: ' + ', '.join(ENV) + '. JSON arguments may be "-" to read standard input. Exit codes: 0 ok, 1 HTTP or transport error, 2 usage or configuration, 3 check found differences, 4 HTTP 409.')
    commands = parser.add_subparsers(dest='command', required=True, metavar='command')
    def add(name, text, *arguments):
        command = commands.add_parser(name, parents=[pretty], help=text, description=text)
        for argument, argument_help in arguments:
            command.add_argument(argument, help=argument_help)
    oauth_login = commands.add_parser('login', help='sign in with Google using a browser and a loopback callback')
    oauth_login.add_argument('--google', action='store_true', required=True)
    oauth_login.add_argument('--port', type=int, default=8765, help='loopback callback port; register the matching redirect URI')
    oauth_login.add_argument('--timeout', type=int, default=180, help='seconds to wait for the browser (1–600)')
    add('whoami', 'print the agent id, name, and server URL; the id is the value for `owner`')
    add('check', 'compare the live schema with references/schema.json; exit 3 when they differ')
    add('schema', 'print the live SQL tables and columns')
    add('sql', 'run one read-only SELECT', ('query', 'SQL text, or - for standard input'))
    add('get', 'read one record', ('collection', 'collection name'), ('id', 'record id'))
    add('create', 'create one record', ('collection', 'collection name'), ('json', 'JSON object, or -'))
    add('update', 'change fields of one record', ('collection', 'collection name'), ('id', 'record id'), ('json', 'JSON object with the fields to change, or -'))
    add('batch', 'send up to 20 writes as one transaction', ('json', 'JSON array of {"method","url","body"}, or -'))
    add('newid', 'print a new 15-character record id for use inside a batch')
    add('logout', 'remove the cached token')
    args = parser.parse_args(argv)
    args.pretty = getattr(args, 'pretty', False)
    return args


def main():
    try:
        with instrument_cli(service='dealcontext.client', opener=opener):
            return run(parse(sys.argv[1:]))
    except Fail as error:
        say(f'dealcontext: {error}')
        return error.code
    except KeyboardInterrupt:
        return 130
    except Exception as error:  # No traceback: keep the output short and free of request data.
        say(f'dealcontext: unexpected {type(error).__name__}: {error}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
