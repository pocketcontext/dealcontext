#!/usr/bin/env python3
"""Test the installable skill in skills/dealcontext; requires the installed client package.

  python3 tests/skill.py --binary /path/to/pocketcontext
  python3 tests/skill.py --binary /path/to/pocketcontext --write-schema

The first form checks the skill's files, then copies the skill outside the repository and runs its
dealcontext against a temporary server. The second form rewrites skills/dealcontext/references/schema.json
from a temporary server; run it after a migration changes the SQL-readable tables or columns.
"""
import argparse
import contextlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'skills' / 'dealcontext'
SKILL_REVISION = int(re.search(r'^SKILL_REVISION = (\d+)$', (ROOT / 'src/dealcontext_client/cli.py').read_text(), re.MULTILINE).group(1))
EMAIL, PASSWORD = 'skill-agent@example.com', 'SkillAgentPassword123!'
FORBIDDEN = ['POCKETBASE_ADMIN', '.envrc', '.env.admin', '_superusers', 'superuser upsert']
REGENERATE = 'If a migration changed the schema, regenerate references/schema.json: python3 tests/skill.py --binary <pocketcontext> --write-schema'


@contextlib.contextmanager
def item(label):
    try:
        yield
    except Exception as error:
        raise AssertionError(f'FAILED skill check: {label}', repr(error)) from error


@contextlib.contextmanager
def crm_server(binary, tmp):
    """Start a server on a temporary database and provision one agent. Yields (base URL, agent record)."""
    common = [binary, '--dir', str(tmp / 'pb_data'), '--migrationsDir', str(ROOT / 'pb_migrations'), '--hooksDir', str(ROOT / 'pb_hooks')]
    subprocess.run(common + ['superuser', 'upsert', 'test-admin@example.com', 'TestAdminPassword123!'], cwd=ROOT, check=True, capture_output=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    log = open(tmp / 'server.log', 'w+')
    server = subprocess.Popen(common + ['serve', '--http', f'127.0.0.1:{port}'], cwd=ROOT, stdout=log, stderr=log)

    def request(method, path, body=None, token=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = token
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read())
    try:
        for _ in range(150):
            try:
                request('GET', '/api/health')
                break
            except OSError:
                if server.poll() is not None:
                    raise RuntimeError('Server exited during startup')
                time.sleep(.1)
        else:
            raise RuntimeError('Server did not start')
        admin = request('POST', '/api/collections/_superusers/auth-with-password', {'identity': 'test-admin@example.com', 'password': 'TestAdminPassword123!'})['token']
        agent = request('POST', '/api/collections/agents/records', {'email': EMAIL, 'password': PASSWORD, 'passwordConfirm': PASSWORD, 'name': 'Skill agent'}, admin)
        yield base, agent
    except Exception:
        log.flush()
        log.seek(0)
        print(log.read())
        raise
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        log.close()


class Conflict(BaseHTTPRequestHandler):
    """Stub server for HTTP 409, which the real server returns only when two writes race."""
    logins = 0

    def reply(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        conflict = {'status': 409, 'message': 'The record was changed by another request. Read it again and retry.', 'data': {}}
        if self.path.endswith('/auth-with-password'):
            Conflict.logins += 1
            self.reply(200, {'token': 'stub-token-0123456789', 'record': {'id': 'stubagent000001', 'name': 'Stub'}})
        elif self.path == '/api/batch':
            self.reply(400, {'status': 400, 'message': 'Batch transaction failed.', 'data': {'requests': {'0': {'code': 'batch_request_failed', 'response': conflict}}}})
        elif self.path == '/api/context/query':
            self.reply(200, {'columns': ['1'], 'rows': [[1]], 'truncated': False})
        else:
            self.reply(409, conflict)

    do_PATCH = do_POST

    def do_GET(self):
        self.reply(404, {'message': 'Not found'})

    def log_message(self, *args):
        pass


class ClientIdentityGate(Conflict):
    """Model an edge filter that rejects the generic Python user-agent."""
    requests = []

    def do_GET(self):
        ClientIdentityGate.requests.append((self.path, self.headers.get('User-Agent', ''), self.headers.get('Authorization')))
        self.reply(200, {'recommendedRevision': SKILL_REVISION})

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        agent = self.headers.get('User-Agent', '')
        ClientIdentityGate.requests.append((self.path, agent, self.headers.get('Authorization')))
        if agent != 'DealContext/1.0':
            self.reply(403, {'message': 'Client identification required'})
        elif self.path.endswith('/auth-with-password'):
            self.reply(200, {'token': 'identity-test-token-0123456789', 'record': {'id': 'identityagent01'}})
        elif self.path == '/api/context/query' and self.headers.get('Authorization') == 'identity-test-token-0123456789':
            self.reply(200, {'columns': ['1'], 'rows': [[1]], 'truncated': False})
        else:
            self.reply(401, {'message': 'Authentication required'})


class SkillVersion(Conflict):
    """Metadata fixture with independent CRM success and unchanged schema."""
    token = 'version-test-token-0123456789'
    metadata = {'recommendedRevision': SKILL_REVISION}
    status = 200
    requests = []
    schema = None

    def do_GET(self):
        cls = type(self)
        cls.requests.append((self.path, self.headers.get('Authorization'), self.headers.get('User-Agent')))
        if self.path == '/api/dealcontext/skill-version':
            if cls.status == 'disconnect':
                self.close_connection = True
                return
            self.reply(cls.status, cls.metadata)
        elif self.path == '/api/context/schema':
            self.reply(200, cls.schema)
        else:
            self.reply(200, {'id': 'stubrecord00001'})

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        if self.path.endswith('/auth-with-password') or self.path.endswith('/auth-refresh'):
            self.reply(200, {'token': self.token, 'record': {'id': 'stubagent000001', 'name': 'Stub'}})
        elif self.path == '/api/context/query':
            self.reply(200, {'columns': ['1'], 'rows': [[1]], 'truncated': False})
        elif self.path == '/api/batch':
            self.reply(200, [])
        else:
            self.reply(200, {'id': 'stubrecord00001'})

    do_PATCH = do_POST


def frontmatter(text):
    """Parse the simple `key: value` frontmatter of SKILL.md. Values must be plain one-line YAML scalars."""
    lines = text.split('\n')
    assert lines[0] == '---', 'SKILL.md must start with ---'
    fields = {}
    for line in lines[1:lines.index('---', 1)]:
        key, separator, value = line.partition(': ')
        assert separator and re.fullmatch(r'[a-z-]+', key), ('not a key: value line', line)
        assert ': ' not in value and ' #' not in value and value[0] not in '"\'[{>|*&!%@`', ('value is not a plain YAML scalar', line)
        fields[key] = value.strip()
    return fields


def static_checks():
    with item('SKILL.md frontmatter: name equals the directory name, description within 600 characters'):
        fields = frontmatter((SKILL / 'SKILL.md').read_text())
        assert fields['name'] == SKILL.name and re.fullmatch(r'[a-z0-9-]{1,64}', fields['name']), fields
        assert 50 <= len(fields['description']) <= 600, len(fields['description'])
        assert len((SKILL / 'SKILL.md').read_text().split('\n')) <= 130, 'SKILL.md is meant to stay short; move detail to references/'
    with item('every relative link and skill path mentioned in the documents exists inside the skill'):
        documents = [SKILL / 'SKILL.md'] + sorted((SKILL / 'references').glob('*.md'))
        assert len(documents) == 4, documents
        for document in documents:
            text = document.read_text()
            links = [(document.parent, target.split('#')[0]) for target in re.findall(r'\]\(([^)\s]+)\)', text) if not re.match(r'[a-z]+:|#', target)]
            mentions = [(SKILL, path.rstrip('.')) for path in re.findall(r'`((?:scripts|references)/[\w./-]+)', text)]
            for folder, target in links + mentions:
                resolved = (folder / target).resolve()
                assert resolved.exists() and SKILL.resolve() in resolved.parents, (document.name, target)
            assert not re.search(r'(^|[\s(`\[])agent/', text), (document.name, 'mentions the old agent/ directory')
    with item('no file in the skill mentions operator credentials or a superuser login'):
        for path in sorted(SKILL.rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts:
                text = path.read_text()
                assert not [word for word in FORBIDDEN if word in text], (path, FORBIDDEN)
    with item('dealcontext is executable and has no delete command'):
        script = SKILL / 'dealcontext'
        assert os.access(script, os.X_OK) and script.read_text().startswith('#!/usr/bin/env -S uv run --script\n'), script
        source = (ROOT / 'src/dealcontext_client/cli.py').read_text()
        assert "'DELETE'" not in source and "add('delete'" not in source


def sorted_tables(schema):
    tables = [{'name': table['name'], 'columns': sorted(table['columns'], key=lambda column: column['name'])} for table in schema['tables']]
    return {'tables': sorted(tables, key=lambda table: table['name'])}


def schema_text(schema):
    """One column per line, so a migration shows up as a small diff."""
    tables = []
    for table in sorted_tables(schema)['tables']:
        columns = ',\n'.join('      ' + json.dumps(column) for column in table['columns'])
        tables.append('    {"name": %s, "columns": [\n%s\n    ]}' % (json.dumps(table['name']), columns))
    return '{\n  "tables": [\n' + ',\n'.join(tables) + '\n  ]\n}\n'


def version_checks(dc, tmp, installed, tokens):
    stub = HTTPServer(('127.0.0.1', 0), SkillVersion)
    thread = threading.Thread(target=stub.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{stub.server_port}'
    cache = tmp / 'version-cache'
    options = {'DEALCONTEXT_URL': base, 'XDG_CACHE_HOME': str(cache)}
    SkillVersion.schema = json.loads((installed / 'references' / 'schema.json').read_text())
    tokens.add(SkillVersion.token)

    def calls():
        return sum(path == '/api/dealcontext/skill-version' for path, _, _ in SkillVersion.requests)

    def files():
        return sorted(cache.glob('dealcontext/*.skill-version.json'))

    def query(**extra):
        stdout, stderr = dc('sql', 'SELECT 1', **{**options, **extra})
        assert json.loads(stdout) == {'columns': ['1'], 'rows': [[1]], 'truncated': False}, stdout
        return stderr

    def expire(path):
        data = json.loads(path.read_text())
        data['checkedAt'] = time.time() - 301
        path.write_text(json.dumps(data))

    try:
        with item('matching skill revision is silent, authenticated, privately cached without credentials'):
            assert query() == ''
            (path,) = files()
            assert calls() == 1
            assert SkillVersion.requests[0] == ('/api/dealcontext/skill-version', SkillVersion.token, 'DealContext/1.0')
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            data = json.loads(path.read_text())
            assert data['installedRevision'] == SKILL_REVISION and data['recommendedRevision'] == SKILL_REVISION
            assert PASSWORD not in path.read_text() and SkillVersion.token not in path.read_text() and EMAIL not in path.read_text()
            assert query() == '' and calls() == 1
        with item('new revision warns without schema changes, preserves JSON, and warns from cache'):
            SkillVersion.metadata = {'recommendedRevision': SKILL_REVISION + 1, 'message': 'UNTRUSTED_VERSION_MESSAGE', 'updateCommand': 'UNTRUSTED_VERSION_COMMAND'}
            expire(path)
            warning = query()
            assert 'WARNING' in warning and 'update' in warning.lower(), warning
            assert 'UNTRUSTED_VERSION' not in warning, warning
            assert calls() == 2
            assert query() == warning and calls() == 2
            for command in [('whoami',), ('schema',), ('get', 'deals', 'stubrecord00001'),
                            ('create', 'pipelines', '{"name":"Sales"}'),
                            ('update', 'pipelines', 'stubrecord00001', '{"name":"Sales"}'), ('batch', '[]')]:
                stdout, stderr = dc(*command, **options)
                json.loads(stdout)
                assert stderr == warning, (command, stderr)
            assert calls() == 2
            stdout, stderr = dc('check', **options)
            assert stdout.startswith('OK') and stderr == warning and calls() == 3, (stdout, stderr)
        with item('check refreshes fresh metadata; newer installed client gets no downgrade prompt'):
            SkillVersion.metadata = {'recommendedRevision': SKILL_REVISION}
            stdout, stderr = dc('check', **options)
            assert stdout.startswith('OK') and stderr == '' and calls() == 4
            script = installed / 'dealcontext_client/cli.py'
            original = script.read_text()
            try:
                assert f'SKILL_REVISION = {SKILL_REVISION}' in original
                script.write_text(original.replace(f'SKILL_REVISION = {SKILL_REVISION}', f'SKILL_REVISION = {SKILL_REVISION + 2}'))
                assert query() == '' and calls() == 5
                assert json.loads(path.read_text())['installedRevision'] == SKILL_REVISION + 2
            finally:
                script.write_text(original)
            assert query() == '' and calls() == 6
        with item('revision cache is isolated by email and server URL'):
            assert query(DEALCONTEXT_AGENT_EMAIL='second-agent@example.com') == '' and calls() == 7
            assert query(DEALCONTEXT_URL=base.replace('127.0.0.1', 'localhost')) == '' and calls() == 8
            assert len(files()) == 3
        with item('older servers continue silently and cache absent endpoint briefly'):
            SkillVersion.status = 404
            expire(path)
            assert query() == '' and calls() == 9
            assert json.loads(path.read_text())['recommendedRevision'] is None
            assert query() == '' and calls() == 9
            expire(path)
            assert query() == '' and calls() == 10
        with item('HTTP, transport, and malformed metadata errors are advisory, sanitized, and retried'):
            failures = [(503, {'message': 'UNTRUSTED_VERSION_ERROR'}), ('disconnect', {}),
                        (200, {}), (200, {'recommendedRevision': True}),
                        (200, {'recommendedRevision': 0}), (200, {'recommendedRevision': -1}),
                        (200, {'recommendedRevision': 1.5}), (200, {'recommendedRevision': '2'}),
                        (200, ['UNTRUSTED_VERSION_ERROR']), (200, 'UNTRUSTED_VERSION_ERROR')]
            for status, metadata in failures:
                SkillVersion.status, SkillVersion.metadata = 200, {'recommendedRevision': SKILL_REVISION}
                dc('check', **options)
                expire(path)
                SkillVersion.status, SkillVersion.metadata = status, metadata
                before = calls()
                for _ in range(2):
                    warning = query()
                    assert 'WARNING' in warning and 'UNTRUSTED_VERSION' not in warning, warning
                    assert not path.exists(), 'errors must not leave stale successful metadata'
                assert calls() == before + 2
        with item('newid and logout do not request metadata; logout clears both caches'):
            SkillVersion.status, SkillVersion.metadata = 200, {'recommendedRevision': SKILL_REVISION}
            query()
            assert path.exists()
            before = calls()
            dc('newid', **options)
            dc('logout', **{**options, 'DEALCONTEXT_AGENT_PASSWORD': None})
            assert calls() == before and not path.exists()
            assert not path.with_name(path.name.replace('.skill-version.json', '.json')).exists()
    finally:
        stub.shutdown()
        stub.server_close()
        thread.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--binary', required=True)
    parser.add_argument('--write-schema', action='store_true', help='rewrite skills/dealcontext/references/schema.json and exit')
    args = parser.parse_args()
    binary = str(Path(args.binary).resolve())
    if not args.write_schema:
        static_checks()
    with tempfile.TemporaryDirectory(prefix='dealcontext-skill-') as tmp, crm_server(binary, Path(tmp)) as (base, agent):
        tmp = Path(tmp)
        assert ROOT not in tmp.resolve().parents, 'the temporary directory must be outside the repository'
        installed = tmp / 'installed' / 'dealcontext'
        shutil.copytree(SKILL, installed, ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(ROOT / 'src/dealcontext_client', installed / 'dealcontext_client', ignore=shutil.ignore_patterns('__pycache__'))
        environment = {'PYTHONDONTWRITEBYTECODE': '1', 'PATH': os.environ.get('PATH', ''), 'HOME': str(tmp / 'home'), 'XDG_CACHE_HOME': str(tmp / 'cache'),
                       'DEALCONTEXT_URL': base, 'DEALCONTEXT_AGENT_EMAIL': EMAIL, 'DEALCONTEXT_AGENT_PASSWORD': PASSWORD}
        (tmp / 'home').mkdir()
        (tmp / 'elsewhere').mkdir()
        cache = tmp / 'cache' / 'dealcontext'
        outputs, tokens = [], set()

        def dc(*argv, expect=0, stdin=None, **overrides):
            """Run the copied dealcontext from an unrelated directory. Returns (stdout, stderr)."""
            env = {key: value for key, value in {**environment, **overrides}.items() if value is not None}
            done = subprocess.run([sys.executable, str(installed / 'dealcontext'), *argv], cwd=tmp / 'elsewhere', env=env, input=stdin, capture_output=True, text=True, timeout=120)
            outputs.append(done.stdout + done.stderr)
            assert done.returncode == expect, (argv, 'exit code', done.returncode, 'expected', expect, done.stdout, done.stderr)
            assert 'Traceback' not in done.stderr, done.stderr
            return done.stdout, done.stderr

        def out(*argv, **options):
            return json.loads(dc(*argv, **options)[0])

        def session_files():
            return sorted(path for path in cache.glob('*.json') if not path.name.endswith('.skill-version.json')) if cache.exists() else []

        def break_token(path):
            """Replace the cached token with one the server rejects, as if it had expired."""
            session = json.loads(path.read_text())
            tokens.add(session['token'])
            session['token'] = 'expired.' + session['token'][::-1]
            path.write_text(json.dumps(session))

        if args.write_schema:
            target = SKILL / 'references' / 'schema.json'
            target.write_text(schema_text(out('schema')))
            (ROOT / 'src/dealcontext_client/schema.json').write_text(target.read_text())
            print(f'wrote {target}')
            return

        with item('a missing environment variable exits 2 and is named; nothing is cached'):
            for name in ('DEALCONTEXT_URL', 'DEALCONTEXT_AGENT_EMAIL', 'DEALCONTEXT_AGENT_PASSWORD'):
                _, err = dc('whoami', expect=2, **{name: None})
                assert name in err, err
                _, err = dc('whoami', expect=2, **{name: ''})
                assert name in err, err
            assert session_files() == []
        with item('usage errors exit 2: invalid JSON, wrong JSON type, no delete command'):
            dc('create', 'pipelines', '{not json', expect=2)
            dc('create', 'pipelines', '[]', expect=2)
            dc('batch', '{}', expect=2)
            dc('delete', 'pipelines', 'abc', expect=2)
            assert session_files() == []
        with item('newid prints a 15-character [a-z0-9] id and needs no configuration'):
            ids = {dc('newid', DEALCONTEXT_URL=None, DEALCONTEXT_AGENT_PASSWORD=None)[0].strip() for _ in range(5)}
            assert len(ids) == 5 and all(re.fullmatch(r'[a-z0-9]{15}', value) for value in ids), ids
        with item('whoami prints the agent id, name, and server URL'):
            me = out('whoami')
            assert me['id'] == agent['id'] and me['name'] == 'Skill agent' and me['url'] == base, me
        with item('token cache: one file, mode 0600, directory 0700, under XDG_CACHE_HOME'):
            (session_file,) = session_files()
            assert stat.S_IMODE(session_file.stat().st_mode) == 0o600 and stat.S_IMODE(cache.stat().st_mode) == 0o700
            assert PASSWORD not in session_file.read_text()
            assert not (tmp / 'home' / '.cache').exists()
        with item('token cache is reused: a second call works with a wrong password, so it did not log in'):
            before = session_file.read_text()
            assert out('whoami', DEALCONTEXT_AGENT_PASSWORD='wrong-password')['id'] == agent['id']
            assert session_file.read_text() == before
        with item('without XDG_CACHE_HOME the cache is under ~/.cache/dealcontext'):
            out('whoami', XDG_CACHE_HOME=None)
            (home_file,) = (path for path in (tmp / 'home' / '.cache' / 'dealcontext').glob('*.json') if not path.name.endswith('.skill-version.json'))
            assert stat.S_IMODE(home_file.stat().st_mode) == 0o600
            tokens.add(json.loads(home_file.read_text())['token'])
        with item('check exits 0 against a server built from the current migrations. ' + REGENERATE):
            stdout, _ = dc('check')
            assert stdout.startswith('OK'), stdout
            committed = json.loads((SKILL / 'references' / 'schema.json').read_text())
            assert committed == sorted_tables(committed), 'references/schema.json is not in the generated form'
        with item('check prints the differences and exits 3 when the reference schema is out of date'):
            snapshot = installed / 'dealcontext_client/schema.json'
            original = snapshot.read_text()
            changed = json.loads(original)
            changed['tables'] = [table for table in changed['tables'] if table['name'] != 'notes'] + [{'name': 'invoices', 'columns': []}]
            deals = next(table for table in changed['tables'] if table['name'] == 'deals')
            deals['columns'] = [column for column in deals['columns'] if column['name'] != 'currency'] + [{'name': 'discount', 'type': 'NUMERIC'}]
            snapshot.write_text(json.dumps(changed))
            stdout, _ = dc('check', expect=3)
            for expected in ('table notes', 'table invoices', 'deals.currency', 'deals.discount'):
                assert expected in stdout, (expected, stdout)
            snapshot.write_text(original)
            dc('check')
        with item('schema prints the live tables'):
            names = {table['name'] for table in out('schema')['tables']}
            assert {'deals', 'activities', 'notes', 'audit_log'} <= names and 'agents' not in names, names

        with item('create, update, get'):
            pipeline = out('create', 'pipelines', json.dumps({'name': 'Sales', 'active': True}))
            stage = out('create', 'stages', '-', stdin=json.dumps({'name': 'Qualified', 'pipeline': pipeline['id'], 'position': 0}))
            org = out('create', 'organizations', json.dumps({'name': 'Acme', 'owner': me['id']}))
            assert org['created_by'] == me['id'], org
            renamed = out('update', 'organizations', org['id'], json.dumps({'name': 'Acme Corp'}))
            assert renamed['name'] == 'Acme Corp' and out('get', 'organizations', org['id'])['name'] == 'Acme Corp'
            pretty, _ = dc('get', 'organizations', org['id'], '--pretty')
            assert pretty.startswith('{\n  "') and json.loads(pretty)['id'] == org['id'], pretty
            assert json.loads(dc('--pretty', 'get', 'organizations', org['id'])[0])['id'] == org['id']
        with item('create and update drop created_by and updated_by with a note on stderr'):
            stdout, err = dc('create', 'organizations', json.dumps({'name': 'Stamped', 'owner': me['id'], 'created_by': 'someoneelse00001'}))
            assert 'created_by' in err and json.loads(stdout)['created_by'] == me['id'], (stdout, err)
            _, err = dc('update', 'organizations', json.loads(stdout)['id'], json.dumps({'name': 'Stamped 2', 'updated_by': 'someoneelse00001'}))
            assert 'updated_by' in err, err
        with item('HTTP errors exit 1 with the status and the server message on stderr'):
            _, err = dc('get', 'organizations', 'missing00000001', expect=1)
            assert 'HTTP 404' in err, err
            stdout, err = dc('create', 'notes', json.dumps({'body': 'Orphan', 'owner': me['id']}), expect=1)
            assert stdout == '' and 'HTTP 400' in err and 'At least one of deal, person, organization' in err, err
            _, err = dc('sql', 'SELECT id FROM agents', expect=1)
            assert 'HTTP 400' in err, err
            _, err = dc('whoami', expect=1, DEALCONTEXT_URL='http://127.0.0.1:1', XDG_CACHE_HOME=str(tmp / 'cache-unreachable'))
            assert 'cannot reach' in err, err
        with item('sql: NULL stays null, an empty string stays empty, stdin works'):
            result = out('sql', "SELECT NULL AS missing, '' AS empty, name FROM organizations WHERE id = '" + org['id'] + "'")
            assert result == {'columns': ['missing', 'empty', 'name'], 'rows': [[None, '', 'Acme Corp']], 'truncated': False}, result
            stdout, err = dc('sql', '-', stdin='SELECT count(*) AS n FROM pipelines')
            assert json.loads(stdout)['rows'] == [[1]] and 'WARNING' not in err, (stdout, err)
        with item('sql: a truncated result prints a WARNING on stderr'):
            stdout, err = dc('sql', 'WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 5000) SELECT x FROM c')
            result = json.loads(stdout)
            assert result['truncated'] is True and 0 < len(result['rows']) < 5000, (len(result['rows']), result['truncated'])
            assert err.startswith('WARNING') and len(err.strip().split('\n')) == 1, err

        def count(table, where):
            return out('sql', f'SELECT count(*) FROM {table} WHERE {where}')['rows'][0][0]
        with item('batch success: deal, activity, and note linked through a client-chosen id'):
            deal_id = dc('newid')[0].strip()
            deal = {'id': deal_id, 'title': 'Acme renewal', 'stage': stage['id'], 'organization': org['id'], 'owner': me['id'], 'value_minor': 250000, 'currency': 'USD', 'status': 'open'}
            requests = [{'method': 'POST', 'url': '/api/collections/deals/records', 'body': deal},
                        {'method': 'POST', 'url': '/api/collections/activities/records', 'body': {'subject': 'Follow up', 'kind': 'call', 'deal': deal_id, 'owner': me['id'], 'due_at': '2030-01-01 09:00:00.000Z'}},
                        {'method': 'POST', 'url': '/api/collections/notes/records', 'body': {'body': 'Asked for a renewal quote.', 'deal': deal_id, 'owner': me['id']}}]
            results = out('batch', json.dumps(requests))
            assert [entry['status'] for entry in results] == [200, 200, 200] and results[0]['body']['id'] == deal_id, results
            assert results[1]['body']['deal'] == deal_id and results[2]['body']['created_by'] == me['id'], results
            assert count('activities', f"deal = '{deal_id}'") == 1 and count('notes', f"deal = '{deal_id}'") == 1
        with item('batch failure: exit 1, the failed index and its message are reported, nothing is saved'):
            audit_rows = count('audit_log', '1 = 1')
            failing = [{'method': 'POST', 'url': '/api/collections/deals/records', 'body': {**deal, 'id': dc('newid')[0].strip(), 'title': 'Must not exist'}},
                       {'method': 'POST', 'url': '/api/collections/notes/records', 'body': {'body': 'Orphan', 'owner': me['id']}}]
            stdout, err = dc('batch', '-', stdin=json.dumps(failing), expect=1)
            assert stdout == '' and 'index 1' in err and 'At least one of deal, person, organization' in err and 'Nothing in this batch was saved' in err, err
            assert count('deals', "title = 'Must not exist'") == 0 and count('audit_log', '1 = 1') == audit_rows
            _, err = dc('batch', json.dumps([{'method': 'DELETE', 'url': f'/api/collections/deals/records/{deal_id}'}]), expect=1)
            assert 'index 0' in err and 'HTTP 403' in err, err
            assert out('get', 'deals', deal_id)['title'] == 'Acme renewal'

        def valid_token():
            token = json.loads(session_file.read_text())['token']
            tokens.add(token)
            return not token.startswith('expired.')
        with item('expired metadata plus rejected token refreshes authentication and continues the command'):
            version_file = session_file.with_suffix('.skill-version.json')
            metadata = json.loads(version_file.read_text())
            metadata['checkedAt'] = 0
            version_file.write_text(json.dumps(metadata))
            break_token(session_file)
            stdout, stderr = dc('sql', 'SELECT count(*) FROM deals')
            assert json.loads(stdout)['rows'] == [[1]] and valid_token() and stderr == '', stderr
            assert json.loads(version_file.read_text())['recommendedRevision'] == SKILL_REVISION
            assert json.loads(version_file.read_text())['checkedAt'] > 0
        with item('a rejected token (401 from the SQL endpoint): log in once, retry once'):
            break_token(session_file)
            assert out('sql', 'SELECT count(*) FROM deals')['rows'] == [[1]] and valid_token()
        with item('a rejected token on the records API (the server answers 400 or 404, not 401): log in once, retry once'):
            break_token(session_file)
            assert out('get', 'deals', deal_id)['id'] == deal_id and valid_token()
            break_token(session_file)
            out('create', 'organizations', json.dumps({'name': 'After expiry', 'owner': me['id']}))
            assert count('organizations', "name = 'After expiry'") == 1 and valid_token()
            break_token(session_file)
            assert len(out('batch', json.dumps([{'method': 'PATCH', 'url': f'/api/collections/deals/records/{deal_id}', 'body': {'value_minor': 300000}}]))) == 1 and valid_token()
        with item('a rejected token and a wrong password: exit 1, login failure reported, password not printed'):
            break_token(session_file)
            _, err = dc('sql', 'SELECT 1', expect=1, DEALCONTEXT_AGENT_PASSWORD='wrong-password-Zq7')
            assert 'login' in err and 'wrong-password-Zq7' not in err, err
        with item('a valid token is not replaced after an ordinary 400'):
            out('whoami')
            before = session_file.read_text()
            dc('create', 'notes', json.dumps({'body': 'Orphan', 'owner': me['id']}), expect=1, DEALCONTEXT_AGENT_PASSWORD='wrong-password')
            assert session_file.read_text() == before

        with item('login and authenticated requests identify the DealContext client through an edge filter'):
            stub = HTTPServer(('127.0.0.1', 0), ClientIdentityGate)
            thread = threading.Thread(target=stub.serve_forever, daemon=True)
            thread.start()
            try:
                stub_url = f'http://127.0.0.1:{stub.server_port}'
                result = out('sql', 'SELECT 1', DEALCONTEXT_URL=stub_url, XDG_CACHE_HOME=str(tmp / 'identity-cache'))
                assert result['rows'] == [[1]], result
                assert ClientIdentityGate.requests == [
                    ('/api/collections/agents/auth-with-password', 'DealContext/1.0', None),
                    ('/api/dealcontext/skill-version', 'DealContext/1.0', 'identity-test-token-0123456789'),
                    ('/api/context/query', 'DealContext/1.0', 'identity-test-token-0123456789'),
                ], ClientIdentityGate.requests
                tokens.add('identity-test-token-0123456789')
            finally:
                stub.shutdown()
                stub.server_close()
                thread.join()

        version_checks(dc, tmp, installed, tokens)

        with item('HTTP 409 exits 4, also inside a batch (stub server: the real 409 needs two racing writes)'):
            stub = HTTPServer(('127.0.0.1', 0), Conflict)
            threading.Thread(target=stub.serve_forever, daemon=True).start()
            try:
                stub_url = f'http://127.0.0.1:{stub.server_port}'
                _, err = dc('update', 'deals', 'abc', '{"title":"x"}', expect=4, DEALCONTEXT_URL=stub_url)
                assert 'HTTP 409' in err and 'Read it again' in err, err
                _, err = dc('batch', '[{"method":"PATCH","url":"/api/collections/deals/records/abc","body":{}}]', expect=4, DEALCONTEXT_URL=stub_url)
                assert 'index 0' in err and 'HTTP 409' in err, err
                assert Conflict.logins == 1, ('the second call must reuse the cached token', Conflict.logins)
                assert len(session_files()) == 2, 'the cache is keyed by server URL'
                tokens.add('stub-token-0123456789')
            finally:
                stub.shutdown()
                stub.server_close()
        with item('logout removes the cached token and needs no password'):
            dc('logout', DEALCONTEXT_AGENT_PASSWORD=None)
            assert not session_file.exists()
            dc('logout')
        with item('the password and tokens never appear in stdout or stderr'):
            assert len(tokens) >= 2 and all(len(token) > 15 for token in tokens), len(tokens)  # Logins in the same second return the same token.
            for number, text in enumerate(outputs):
                assert PASSWORD not in text and not [token for token in tokens if token in text], f'secret in the output of dealcontext call number {number}'
        print('PASS: skill files, links, frontmatter, no operator credentials; dealcontext from a copy outside the repository: configuration errors, '
              'whoami, token cache mode and reuse, advisory skill revisions and cache isolation, check against the live schema, schema, create, update, get, stamp removal, HTTP errors, '
              'SQL NULL and truncation warning, atomic batch success and failure, recovery from a rejected token, 409 exit code, logout, no secrets in output')


if __name__ == '__main__':
    main()
