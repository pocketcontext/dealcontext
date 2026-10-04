#!/usr/bin/env python3
"""Exercise an isolated CRM through real HTTP APIs; no third-party Python dependencies."""
import argparse
import contextlib
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CRM = ['organizations', 'people', 'pipelines', 'stages', 'deals', 'activities', 'notes', 'messages']


@contextlib.contextmanager
def item(label):
    """Prefix any failure inside the block with the contract item it belongs to."""
    try:
        yield
    except Exception as error:
        raise AssertionError(f'FAILED contract item {label}', repr(error)) from error


def recent(value):
    """True when value is a PocketBase UTC date within five minutes of now."""
    if not value:
        return False
    stamp = datetime.strptime(value, '%Y-%m-%d %H:%M:%S.%fZ').replace(tzinfo=timezone.utc)
    return abs((datetime.now(timezone.utc) - stamp).total_seconds()) < 300


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    args = parser.parse_args()
    binary = str(Path(args.binary).resolve())
    with tempfile.TemporaryDirectory(prefix='dealcontext-test-') as tmp:
        data = str(Path(tmp) / 'pb_data')
        migrations = Path(tmp) / 'pb_migrations'
        shutil.copytree(ROOT / 'pb_migrations', migrations)
        directory_migration, = migrations.glob('*_agent_directory.js')
        fixture_version = int(directory_migration.name.split('_')[0]) - 1
        # Seed an existing account before the directory migration. Omit runtime
        # hooks to reproduce provisioning before directory support existed.
        (migrations / f'{fixture_version}_legacy_agent_fixture.js').write_text('''
migrate((app) => {
  const agent = new Record(app.findCollectionByNameOrId("agents"));
  agent.set("id", "legacyagent0001");
  agent.set("name", "Existing agent");
  agent.set("email", "legacy@example.com");
  agent.setPassword("TestLegacyPassword123!");
  app.unsafeWithoutHooks().save(agent);
}, (app) => {
  app.delete(app.findRecordById("agents", "legacyagent0001"));
});
''')
        hooks = Path(tmp) / 'pb_hooks'
        shutil.copytree(ROOT / 'pb_hooks', hooks)
        # Force synchronization failures after the account write reaches its
        # execute hook, so these checks prove both records share a transaction.
        (hooks / 'directory_failure_fixture.pb.js').write_text('''
onRecordCreateExecute((e) => {
  if (e.record.id === "dirfailcreate01") throw new Error("Synthetic directory create failure");
  e.next();
}, "agent_directory");
onRecordUpdateExecute((e) => {
  if (e.record.getString("name") === "Reject directory sync") throw new Error("Synthetic directory update failure");
  e.next();
}, "agent_directory");
onRecordDeleteExecute((e) => {
  if (e.record.id === "dirfaildelete01") throw new Error("Synthetic directory delete failure");
  e.next();
}, "agent_directory");
''')
        common = [binary, '--dir', data, '--migrationsDir', str(migrations), '--hooksDir', str(hooks)]
        subprocess.run(common + ['superuser', 'upsert', 'test-admin@example.com', 'TestAdminPassword123!'], cwd=ROOT, check=True, capture_output=True)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        log = open(Path(tmp) / 'server.log', 'w+')
        server = subprocess.Popen(common + ['serve', '--http', f'127.0.0.1:{port}'], cwd=ROOT, stdout=log, stderr=log)
        def request(method, path, body=None, token=None, expected=200, expected_headers=None):
            headers = {'Content-Type': 'application/json'}
            if token:
                headers['Authorization'] = token
            req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=10) as response:
                    status, raw = response.status, response.read()
                    for name, value in (expected_headers or {}).items():
                        assert response.headers.get(name) == value, (name, response.headers.get(name))
            except urllib.error.HTTPError as error:
                status, raw = error.code, error.read()
            assert status in (expected if isinstance(expected, tuple) else (expected,)), (method, path, status, raw.decode())
            return json.loads(raw) if raw else None
        try:
            for _ in range(150):
                try:
                    request('GET', '/api/health')
                    break
                except (OSError, AssertionError):
                    if server.poll() is not None:
                        raise RuntimeError('Server exited during startup')
                    time.sleep(.1)
            else:
                raise RuntimeError('Server did not start')
            admin = request('POST', '/api/collections/_superusers/auth-with-password', {'identity': 'test-admin@example.com', 'password': 'TestAdminPassword123!'})['token']
            agent = request('POST', '/api/collections/agents/records', {'email': 'agent@example.com', 'password': 'TestAgentPassword123!', 'passwordConfirm': 'TestAgentPassword123!', 'name': 'Test agent'}, admin)
            token = request('POST', '/api/collections/agents/auth-with-password', {'identity': 'agent@example.com', 'password': 'TestAgentPassword123!'})['token']
            agent2 = request('POST', '/api/collections/agents/records', {'email': 'agent2@example.com', 'password': 'TestAgentPassword456!', 'passwordConfirm': 'TestAgentPassword456!', 'name': 'Second agent'}, admin)
            token2 = request('POST', '/api/collections/agents/auth-with-password', {'identity': 'agent2@example.com', 'password': 'TestAgentPassword456!'})['token']
            made = []
            def many(collection):
                return f'/api/collections/{collection}/records'
            def one(collection, record):
                return f'/api/collections/{collection}/records/{record["id"]}'
            def create(collection, body):
                record = request('POST', many(collection), body, token)
                made.append((collection, record))
                return record
            def reject(method, path, body, fields, auth=None):
                """The write must fail with 400 and the response must name one of the fields."""
                raw = json.dumps(request(method, path, body, auth or token, expected=400))
                assert any(field in raw for field in fields), ('400 response names none of', fields, raw)
            def sql(query, expected=200):
                return request('POST', '/api/context/query', {'sql': query}, token, expected)
            def audit(collection, record, action):
                result = sql(f"SELECT id, actor, actor_type, changes FROM audit_log WHERE collection = '{collection}' AND record = '{record['id']}' AND action = '{action}' ORDER BY created")
                rows = [dict(zip(result['columns'], row)) for row in result['rows']]
                for row in rows:
                    if isinstance(row['changes'], str):
                        row['changes'] = json.loads(row['changes'])
                return rows
            def audit_count():
                return sql('SELECT count(*) FROM audit_log')['rows'][0][0]

            with item('opt-in traces are private and SQL disclosure requires its own header'):
                query = 'SELECT count(id) AS total FROM deals'
                for opt_in, capture_sql in ((False, False), (True, False), (True, True)):
                    headers = {'Authorization': token, 'Content-Type': 'application/json'}
                    if opt_in:
                        headers['X-Context-Trace'] = '1'
                    if capture_sql:
                        headers['X-Context-Capture-Sql'] = '1'
                    req = urllib.request.Request(base + '/api/context/query',
                        data=json.dumps({'sql': query}).encode(), headers=headers, method='POST')
                    with urllib.request.urlopen(req, timeout=10) as response:
                        assert json.loads(response.read())['rows'] == [[0]]
                        trace_id = response.headers.get('X-Context-Request-Id')
                    if not opt_in:
                        assert not trace_id, 'ordinary requests must not create trace identifiers'
                        continue
                    assert trace_id
                    path = '/api/context/traces/' + trace_id
                    for attempt in range(50):
                        trace = request('GET', path, token=token, expected=(200, 404))
                        if trace.get('request_id') == trace_id:
                            break
                        time.sleep(.01)
                    else:
                        raise AssertionError('completed trace was not retrievable')
                    assert trace['service'] == 'dealcontext'
                    assert trace['user_id'] == agent['id']
                    if capture_sql:
                        assert trace.get('sql') == query
                    else:
                        assert not trace.get('sql')
                    assert any(span['name'] == 'auth' for span in trace['spans'])
                    request('GET', path, token=token2, expected=404)
                    request('GET', path, token=admin, expected=403)
                    request('GET', path, expected=401)

            with item('skill revision metadata is available only to authenticated agents'):
                path = '/api/dealcontext/skill-version'
                client_source = (ROOT / 'src/dealcontext_client/cli.py').read_text()
                revision_match = re.search(r'^SKILL_REVISION = ([1-9][0-9]*)$', client_source, re.MULTILINE)
                assert revision_match, 'bundled client must declare a positive integer SKILL_REVISION'
                expected_revision = int(revision_match.group(1))
                request('GET', path, expected=401)
                request('GET', path, token='invalid-token', expected=401)
                request('GET', path, token=admin, expected=403)
                for auth in (token, token2):
                    metadata = request('GET', path, token=auth, expected_headers={'Cache-Control': 'no-store'})
                    assert metadata == {'recommendedRevision': expected_revision}, metadata

            with item('agent directory backfills existing accounts and exposes only IDs and names'):
                expected_names = {'legacyagent0001': 'Existing agent', agent['id']: 'Test agent', agent2['id']: 'Second agent'}
                result = sql('SELECT * FROM agent_directory')
                assert set(result['columns']) == {'id', 'name'}, result
                assert {dict(zip(result['columns'], row))['id']: dict(zip(result['columns'], row))['name'] for row in result['rows']} == expected_names
                for auth in (token, token2):
                    listed = request('GET', many('agent_directory'), token=auth)
                    assert listed['totalItems'] == 3, listed
                    for entry in listed['items']:
                        assert set(entry) == {'id', 'name', 'collectionId', 'collectionName'}, entry
                        assert entry['name'] == expected_names[entry['id']], entry
                    assert request('GET', one('agent_directory', agent2), token=auth)['name'] == 'Second agent'
                for field in ('email', 'password', 'tokenKey', 'verified'):
                    sql(f'SELECT {field} FROM agent_directory', expected=400)
                sql('SELECT id, name FROM agents', expected=400)
                request('GET', many('agents'), token=token, expected=403)
                request('GET', one('agents', agent2), token=token, expected=404)
            with item('agent directory remains private to authenticated agents and forbids agent writes'):
                anonymous = request('GET', many('agent_directory'))
                assert anonymous['items'] == [] and anonymous['totalItems'] == 0, anonymous
                request('GET', one('agent_directory', agent), expected=404)
                request('POST', '/api/context/query', {'sql': 'SELECT * FROM agent_directory'}, expected=401)
                for auth in (None, token, token2):
                    request('POST', many('agent_directory'), {'id': 'forgedagent0001', 'name': 'Forged'}, auth, expected=403)
                    request('PATCH', one('agent_directory', agent2), {'name': 'Forged'}, auth, expected=403)
                    request('DELETE', one('agent_directory', agent2), token=auth, expected=403)
                assert sql(f"SELECT name FROM agent_directory WHERE id = '{agent2['id']}'")['rows'] == [['Second agent']]
            with item('agent directory tracks renames and preserves disabled identities without requiring unique names'):
                disposable = request('POST', many('agents'), {'email': 'disposable@example.com', 'password': 'TestDisposablePassword123!', 'passwordConfirm': 'TestDisposablePassword123!', 'name': 'Second agent'}, admin)
                assert sql(f"SELECT name FROM agent_directory WHERE id = '{disposable['id']}'")['rows'] == [['Second agent']]
                request('PATCH', one('agents', disposable), {'name': 'Renamed agent'}, admin)
                assert request('GET', one('agent_directory', disposable), token=token)['name'] == 'Renamed agent'
                assert sql(f"SELECT name FROM agent_directory WHERE id = '{disposable['id']}'")['rows'] == [['Renamed agent']]
                reject('PATCH', one('agents', disposable), {'name': ''}, ['name'], admin)
                assert sql(f"SELECT name FROM agent_directory WHERE id = '{disposable['id']}'")['rows'] == [['Renamed agent']]
                request('DELETE', one('agents', disposable), token=admin, expected=403)
                request('PATCH', one('agents', disposable), {'disabled': True}, admin)
                assert request('GET', one('agent_directory', disposable), token=token)['name'] == 'Renamed agent'
            with item('failed directory synchronization rolls back account create and rename; deletion is blocked'):
                fixture = {'id': 'dirfailcreate01', 'email': 'sync-failure@example.com', 'password': 'TestSyncFailurePassword123!', 'passwordConfirm': 'TestSyncFailurePassword123!', 'name': 'Sync failure fixture'}
                request('POST', many('agents'), fixture, admin, expected=400)
                request('GET', one('agents', fixture), token=admin, expected=404)
                request('GET', one('agent_directory', fixture), token=token, expected=404)
                fixture['id'] = 'dirfaildelete01'
                failing_agent = request('POST', many('agents'), fixture, admin)
                request('PATCH', one('agents', failing_agent), {'name': 'Reject directory sync'}, admin, expected=400)
                for collection, auth in (('agents', admin), ('agent_directory', token)):
                    assert request('GET', one(collection, failing_agent), token=auth)['name'] == fixture['name']
                request('DELETE', one('agents', failing_agent), token=admin, expected=403)
                for collection, auth in (('agents', admin), ('agent_directory', token)):
                    assert request('GET', one(collection, failing_agent), token=auth)['name'] == fixture['name']

            request('POST', '/api/collections/organizations/records', {'name': 'Forbidden', 'owner': agent['id']}, expected=400)
            request('POST', '/api/collections/agents/records', {'name': 'Forbidden'}, token, expected=400)
            org = create('organizations', {'name': 'Acme', 'owner': agent['id']})
            person = create('people', {'name': 'Ada', 'email': 'ada@example.com', 'organization': org['id'], 'owner': agent['id']})
            with item('people LinkedIn URL is optional, validated, and SQL-readable'):
                assert person['linkedin_url'] == ''
                profile = 'https://www.linkedin.com/in/ada-example/'
                updated = request('PATCH', one('people', person), {'linkedin_url': profile}, token)
                assert updated['linkedin_url'] == profile
                assert sql(f"SELECT linkedin_url FROM people WHERE id = '{person['id']}' LIMIT 1")['rows'] == [[profile]]
                reject('PATCH', one('people', person), {'linkedin_url': 'not-a-url'}, ['linkedin_url'])
                cleared = request('PATCH', one('people', person), {'linkedin_url': ''}, token)
                assert cleared['linkedin_url'] == ''
            with item('people job title is optional, validated, SQL-readable, and audited'):
                assert person['job_title'] == ''
                updated = request('PATCH', one('people', person), {'job_title': 'CTO'}, token)
                assert updated['job_title'] == 'CTO'
                assert sql(f"SELECT job_title FROM people WHERE id = '{person['id']}' LIMIT 1")['rows'] == [['CTO']]
                changes = audit('people', person, 'update')[-1]['changes']
                assert changes['before']['job_title'] == ''
                assert changes['after']['job_title'] == 'CTO'
                reject('PATCH', one('people', person), {'job_title': 'x' * 201}, ['job_title'])
                cleared = request('PATCH', one('people', person), {'job_title': ''}, token)
                assert cleared['job_title'] == ''
            with item('people pronouns are optional, validated, SQL-readable, and audited'):
                assert person['pronouns'] == ''
                updated = request('PATCH', one('people', person), {'pronouns': 'she/they'}, token)
                assert updated['pronouns'] == 'she/they'
                assert sql(f"SELECT pronouns FROM people WHERE id = '{person['id']}' LIMIT 1")['rows'] == [['she/they']]
                changes = audit('people', person, 'update')[-1]['changes']
                assert changes['before']['pronouns'] == ''
                assert changes['after']['pronouns'] == 'she/they'
                before = audit_count()
                reject('PATCH', one('people', person), {'pronouns': 'x' * 101}, ['pronouns'])
                assert audit_count() == before
                assert request('GET', one('people', person), token=token)['pronouns'] == 'she/they'
                cleared = request('PATCH', one('people', person), {'pronouns': ''}, token)
                assert cleared['pronouns'] == ''
                changes = audit('people', person, 'update')[-1]['changes']
                assert changes['before']['pronouns'] == 'she/they'
                assert changes['after']['pronouns'] == ''
            pipeline = create('pipelines', {'name': 'Sales', 'active': True})
            first = create('stages', {'name': 'Qualified', 'pipeline': pipeline['id'], 'position': 0})
            second = create('stages', {'name': 'Negotiation', 'pipeline': pipeline['id'], 'position': 1})
            deal = create('deals', {'title': 'Acme renewal', 'stage': first['id'], 'person': person['id'], 'organization': org['id'], 'owner': agent['id'], 'value_minor': 250000, 'currency': 'USD', 'status': 'open'})
            with item('agent directory resolves another account owner and attribution stamps'):
                joined = request('POST', '/api/context/query', {'sql': f"SELECT o.name AS owner, c.name AS creator, u.name AS updater FROM deals d LEFT JOIN agent_directory o ON o.id = d.owner LEFT JOIN agent_directory c ON c.id = d.created_by LEFT JOIN agent_directory u ON u.id = d.updated_by WHERE d.id = '{deal['id']}'"}, token2)
                assert joined['rows'] == [['Test agent', 'Test agent', 'Test agent']], joined
                request('DELETE', one('agents', agent), token=admin, expected=403)
                assert request('GET', one('agent_directory', agent), token=token2)['name'] == 'Test agent'
            request('PATCH', f'/api/collections/deals/records/{deal["id"]}', {'stage': second['id']}, token)
            activity = create('activities', {'subject': 'Follow up', 'kind': 'call', 'deal': deal['id'], 'owner': agent['id'], 'due_at': '2030-01-01 09:00:00.000Z'})
            note = create('notes', {'body': 'Customer requested a renewal proposal.', 'deal': deal['id'], 'owner': agent['id'], 'source_url': 'https://example.com/evidence'})
            schema = request('GET', '/api/context/schema', token=token)
            assert 'deals' in json.dumps(schema)
            result = request('POST', '/api/context/query', {'sql': "SELECT d.title,s.name AS stage,p.name AS person FROM deals d JOIN stages s ON s.id=d.stage JOIN people p ON p.id=d.person"}, token)
            assert 'Acme renewal' in json.dumps(result) and 'Negotiation' in json.dumps(result) and 'Ada' in json.dumps(result), result
            completed = request('PATCH', f'/api/collections/activities/records/{activity["id"]}', {'done': True, 'completed_at': '2030-01-01 10:00:00.000Z'}, token)
            result = request('POST', '/api/context/query', {'sql': 'SELECT subject FROM activities WHERE done = 0'}, token)
            assert 'Follow up' not in json.dumps(result), result
            closed = request('PATCH', f'/api/collections/deals/records/{deal["id"]}', {'status': 'won', 'closed_at': '2030-01-02 10:00:00.000Z'}, token)
            request('PATCH', f'/api/collections/deals/records/{deal["id"]}', {'value_minor': -1}, token, expected=400)
            request('PATCH', f'/api/collections/deals/records/{deal["id"]}', {'stage': 'missingstage123'}, token, expected=400)

            # A. Server-enforced record rules.
            new_deal = {'title': 'Lifecycle', 'stage': first['id'], 'owner': agent['id'], 'value_minor': 100, 'currency': 'EUR'}
            with item('A1 open deal: closed_at and lost_reason must be empty'):
                reject('POST', many('deals'), {**new_deal, 'status': 'open', 'closed_at': '2030-01-02 10:00:00.000Z'}, ['closed_at'])
                reject('POST', many('deals'), {**new_deal, 'status': 'open', 'lost_reason': 'Budget'}, ['lost_reason'])
                life = create('deals', {**new_deal, 'status': 'open'})
                assert life['closed_at'] == '' and life['lost_reason'] == '', life
                reject('PATCH', one('deals', life), {'closed_at': '2030-01-02 10:00:00.000Z'}, ['closed_at'])
                reject('PATCH', one('deals', life), {'lost_reason': 'Budget'}, ['lost_reason'])
            with item('A4 closed_at is filled by the server when a request sets status to won, and an explicit value is kept'):
                won = request('PATCH', one('deals', life), {'status': 'won'}, token)
                assert won['status'] == 'won' and recent(won['closed_at']), won
                assert closed['closed_at'].startswith('2030-01-02 10:00:00'), closed
            with item('A2 won deal: closed_at must be set, lost_reason must be empty'):
                reject('PATCH', one('deals', life), {'lost_reason': 'Budget'}, ['lost_reason'])
                reject('POST', many('deals'), {**new_deal, 'status': 'won', 'lost_reason': 'Budget'}, ['lost_reason'])
                reject('PATCH', one('deals', life), {'closed_at': ''}, ['closed_at'])
            with item('A4 reopening a won deal requires clearing closed_at in the same PATCH'):
                reject('PATCH', one('deals', life), {'status': 'open'}, ['closed_at'])
                reopened = request('PATCH', one('deals', life), {'status': 'open', 'closed_at': ''}, token)
                assert reopened['status'] == 'open' and reopened['closed_at'] == '', reopened
            with item('A3 lost deal: closed_at must be set (server-filled), lost_reason optional'):
                lost = request('PATCH', one('deals', life), {'status': 'lost', 'lost_reason': 'Budget'}, token)
                assert lost['status'] == 'lost' and lost['lost_reason'] == 'Budget' and recent(lost['closed_at']), lost
                reject('PATCH', one('deals', life), {'closed_at': ''}, ['closed_at'])
                no_reason = create('deals', {**new_deal, 'title': 'Lost without reason', 'status': 'lost'})
                assert no_reason['lost_reason'] == '' and recent(no_reason['closed_at']), no_reason
            with item('A4 reopening a lost deal requires clearing closed_at and lost_reason in the same PATCH'):
                reject('PATCH', one('deals', life), {'status': 'open'}, ['closed_at', 'lost_reason'])
                reject('PATCH', one('deals', life), {'status': 'open', 'closed_at': ''}, ['lost_reason'])
                reject('PATCH', one('deals', life), {'status': 'open', 'lost_reason': ''}, ['closed_at'])
                reopened = request('PATCH', one('deals', life), {'status': 'open', 'closed_at': '', 'lost_reason': ''}, token)
                assert reopened['status'] == 'open' and reopened['closed_at'] == '' and reopened['lost_reason'] == '', reopened
            with item('A5 currency must be an active ISO 4217 code'):
                for code in ('ZZZ', 'XXX', 'XTS'):
                    reject('POST', many('deals'), {**new_deal, 'status': 'open', 'currency': code}, ['currency'])
                reject('PATCH', one('deals', life), {'currency': 'ZZZ'}, ['currency'])
                assert request('PATCH', one('deals', life), {'currency': 'JPY'}, token)['currency'] == 'JPY'

            new_activity = {'subject': 'Lifecycle call', 'kind': 'call', 'deal': life['id'], 'owner': agent['id'], 'due_at': '2030-01-01 09:00:00.000Z'}
            with item('A6 done activity: completed_at must be set, server-filled when empty, explicit value kept'):
                assert completed['done'] is True and completed['completed_at'].startswith('2030-01-01 10:00:00'), completed
                done_on_create = create('activities', {**new_activity, 'done': True})
                assert recent(done_on_create['completed_at']), done_on_create
                todo = create('activities', new_activity)
                assert todo['done'] is False and todo['completed_at'] == '', todo
                finished = request('PATCH', one('activities', todo), {'done': True}, token)
                assert finished['done'] is True and recent(finished['completed_at']), finished
                # Clearing completed_at alone may be refilled (200) or rejected (400); either way the invariant holds.
                request('PATCH', one('activities', todo), {'completed_at': ''}, token, expected=(200, 400))
                stored = request('GET', one('activities', todo), token=token)
                assert stored['done'] is True and stored['completed_at'] != '', stored
            with item('A7 activity that is not done: completed_at must be empty'):
                reject('POST', many('activities'), {**new_activity, 'done': False, 'completed_at': '2030-01-01 10:00:00.000Z'}, ['completed_at'])
                pending = create('activities', new_activity)
                reject('PATCH', one('activities', pending), {'completed_at': '2030-01-01 10:00:00.000Z'}, ['completed_at'])
                reject('PATCH', one('activities', todo), {'done': False}, ['completed_at'])
                undone = request('PATCH', one('activities', todo), {'done': False, 'completed_at': ''}, token)
                assert undone['done'] is False and undone['completed_at'] == '', undone

            links = ['deal', 'person', 'organization']
            with item('A8 note must link to a deal, person, or organization'):
                reject('POST', many('notes'), {'body': 'Orphan', 'owner': agent['id']}, links)
                person_note = create('notes', {'body': 'About Ada', 'person': person['id'], 'owner': agent['id']})
                create('notes', {'body': 'About Acme', 'organization': org['id'], 'owner': agent['id']})
                reject('PATCH', one('notes', person_note), {'person': ''}, links)
                moved = request('PATCH', one('notes', person_note), {'person': '', 'deal': life['id']}, token)
                assert moved['person'] == '' and moved['deal'] == life['id'], moved

            globex = create('organizations', {'name': 'Globex', 'owner': agent['id']})
            bob = create('people', {'name': 'Bob', 'owner': agent['id']})
            mismatch = {'person': person['id'], 'organization': globex['id']}
            unaffiliated = {'person': bob['id'], 'organization': globex['id']}
            with item('A9 deals: person and organization must agree'):
                reject('POST', many('deals'), {**new_deal, 'status': 'open', **mismatch}, ['person', 'organization'])
                reject('PATCH', one('deals', life), mismatch, ['person', 'organization'])
                create('deals', {**new_deal, 'title': 'Unaffiliated person', 'status': 'open', **unaffiliated})
                create('deals', {**new_deal, 'title': 'Person only', 'status': 'open', 'person': person['id']})
                matched = request('PATCH', one('deals', life), {'person': person['id'], 'organization': org['id']}, token)
                assert matched['person'] == person['id'] and matched['organization'] == org['id'], matched
            with item('A9 activities: person and organization must agree'):
                reject('POST', many('activities'), {**new_activity, **mismatch}, ['person', 'organization'])
                create('activities', {**new_activity, **unaffiliated})
                create('activities', {**new_activity, 'person': person['id'], 'organization': org['id']})
            with item('A9 notes: person and organization must agree'):
                reject('POST', many('notes'), {'body': 'Mismatch', 'owner': agent['id'], **mismatch}, ['person', 'organization'])
                create('notes', {'body': 'Unaffiliated', 'owner': agent['id'], **unaffiliated})
                create('notes', {'body': 'Matched', 'owner': agent['id'], 'person': person['id'], 'organization': org['id']})

            with item('messages: exact text, optional time, SQL access, validation and audit'):
                payload = {'person': person['id'], 'owner': agent['id'], 'channel': 'linkedin',
                           'direction': 'outgoing', 'body': 'Would you like a demo?'}
                message = create('messages', payload)
                assert message['sent_at'] == ''
                assert sql(f"SELECT body, direction FROM messages WHERE id = '{message['id']}' LIMIT 1")['rows'] == [[payload['body'], 'outgoing']]
                assert request('GET', one('messages', message), token=token2)['body'] == payload['body']
                for field, value in [('person', ''), ('owner', ''), ('body', ''), ('channel', 'invalid'),
                                     ('direction', 'invalid'), ('sent_at', 'not-a-date'), ('source_url', 'not-a-url')]:
                    reject('POST', many('messages'), {**payload, field: value}, [field])
                incoming = create('messages', {**payload, 'direction': 'incoming', 'body': 'Yes, please.',
                                              'sent_at': '2030-01-01 09:00:00.000Z',
                                              'source_url': 'https://example.com/thread/1'})
                assert incoming['sent_at'].startswith('2030-01-01 09:00:00')
                request('PATCH', one('messages', message), {'body': 'Corrected message',
                        'created_by': agent2['id'], 'updated_by': agent['id']}, token2)
                saved = request('GET', one('messages', message), token=token)
                assert saved['created_by'] == agent['id'] and saved['updated_by'] == agent2['id']
                assert audit('messages', message, 'update')[0]['changes']['after'] == {'body': 'Corrected message'}
                before = audit_count()
                request('POST', '/api/batch', {'requests': [
                    {'method': 'POST', 'url': many('messages'), 'body': {**payload, 'id': 'batchmessage001'}},
                    {'method': 'POST', 'url': many('messages'), 'body': {**payload, 'sent_at': 'not-a-date'}},
                ]}, token, expected=400)
                assert sql("SELECT id FROM messages WHERE id = 'batchmessage001' LIMIT 1")['rows'] == []
                assert audit_count() == before
                request('DELETE', one('messages', incoming), token=token, expected=403)
                request('DELETE', one('messages', incoming), token=admin, expected=204)
                assert audit('messages', incoming, 'delete')[0]['changes']['before']['body'] == 'Yes, please.'

            # B. Deletes, attribution, audit log.
            with item('B11 created_by and updated_by are stamped from the authenticated agent on create'):
                assert {collection for collection, _ in made} == set(CRM), made
                for collection, record in made:
                    assert record.get('created_by') == agent['id'] and record.get('updated_by') == agent['id'], (collection, record)
            with item('B11 client-supplied created_by and updated_by are overwritten for agent requests'):
                spoof = request('POST', many('organizations'), {'name': 'Spoof', 'owner': agent['id'], 'created_by': agent2['id'], 'updated_by': agent2['id']}, token)
                assert spoof['created_by'] == agent['id'] and spoof['updated_by'] == agent['id'], spoof
                spoof2 = request('PATCH', one('organizations', spoof), {'name': 'Spoof 2', 'created_by': agent2['id'], 'updated_by': agent['id']}, token2)
                assert spoof2['created_by'] == agent['id'] and spoof2['updated_by'] == agent2['id'], spoof2
            with item('B11 superuser requests leave created_by and updated_by as they are'):
                operator_org = request('POST', many('organizations'), {'name': 'Operator org', 'owner': agent['id']}, admin)
                assert operator_org['created_by'] == '' and operator_org['updated_by'] == '', operator_org
                attributed = request('POST', many('organizations'), {'name': 'Attributed org', 'owner': agent['id'], 'created_by': agent2['id']}, admin)
                assert attributed['created_by'] == agent2['id'] and attributed['updated_by'] == '', attributed
                spoof3 = request('PATCH', one('organizations', spoof), {'name': 'Spoof 3'}, admin)
                assert spoof3['created_by'] == agent['id'] and spoof3['updated_by'] == agent2['id'], spoof3

            with item('B12 audit_log create rows: after values, actor, actor_type, all eight collections'):
                for collection in CRM:
                    record = next(record for name, record in made if name == collection)
                    rows = audit(collection, record, 'create')
                    assert len(rows) == 1 and rows[0]['actor'] == agent['id'] and rows[0]['actor_type'] == 'agent', (collection, rows)
                    assert 'before' not in rows[0]['changes'] and isinstance(rows[0]['changes']['after'], dict), (collection, rows)
                after = audit('deals', deal, 'create')[0]['changes']['after']
                expected = {'title': 'Acme renewal', 'stage': first['id'], 'person': person['id'], 'organization': org['id'], 'owner': agent['id'], 'value_minor': 250000, 'currency': 'USD', 'status': 'open'}
                assert {key: after.get(key) for key in expected} == expected, after
                rows = audit('organizations', operator_org, 'create')
                assert len(rows) == 1 and rows[0]['actor'] == '' and rows[0]['actor_type'] == 'superuser', rows
            with item('B12 audit_log update rows hold only the changed fields'):
                rows = audit('deals', deal, 'update')
                assert len(rows) == 2, rows
                assert rows[0]['changes'] == {'before': {'stage': first['id']}, 'after': {'stage': second['id']}}, rows[0]
                assert rows[0]['actor'] == agent['id'] and rows[0]['actor_type'] == 'agent', rows[0]
                assert set(rows[1]['changes']['before']) == {'status', 'closed_at'} and set(rows[1]['changes']['after']) == {'status', 'closed_at'}, rows[1]
                assert rows[1]['changes']['before']['status'] == 'open' and rows[1]['changes']['after']['status'] == 'won', rows[1]
                assert str(rows[1]['changes']['after']['closed_at']).startswith('2030-01-02 10:00:00'), rows[1]
                rows = audit('activities', activity, 'update')
                assert len(rows) == 1 and rows[0]['changes']['before']['done'] is False and rows[0]['changes']['after']['done'] is True, rows
                rows = audit('organizations', spoof, 'update')
                assert len(rows) == 2, rows
                assert rows[0]['changes'] == {'before': {'name': 'Spoof'}, 'after': {'name': 'Spoof 2'}}, rows[0]
                assert rows[0]['actor'] == agent2['id'] and rows[0]['actor_type'] == 'agent', rows[0]
                assert rows[1]['changes'] == {'before': {'name': 'Spoof 2'}, 'after': {'name': 'Spoof 3'}}, rows[1]
                assert rows[1]['actor'] == '' and rows[1]['actor_type'] == 'superuser', rows[1]
                filled = audit('deals', life, 'update')[0]
                assert set(filled['changes']['after']) == {'status', 'closed_at'} and filled['changes']['after']['closed_at'], filled
            with item('B12 a no-op PATCH writes no audit row'):
                count = audit_count()
                request('PATCH', one('deals', deal), {'stage': second['id']}, token)
                assert audit_count() == count, 'no-op PATCH by the same agent wrote an audit row'
                request('PATCH', one('deals', deal), {'stage': second['id']}, token2)
                assert audit_count() == count, 'PATCH that changed only updated_by wrote an audit row'
            with item('B12 a rejected write writes no audit row'):
                count = audit_count()
                request('PATCH', one('deals', deal), {'value_minor': -1}, token, expected=400)
                reject('PATCH', one('deals', deal), {'status': 'open'}, ['closed_at'])
                reject('POST', many('deals'), {**new_deal, 'status': 'open', 'currency': 'ZZZ'}, ['currency'])
                reject('POST', many('notes'), {'body': 'Orphan', 'owner': agent['id']}, links)
                assert audit_count() == count, 'rejected writes changed the audit_log row count'

            with item('B10 agent DELETE is rejected with 403 on every CRM collection'):
                deletes_before = sql("SELECT count(*) FROM audit_log WHERE action = 'delete'")['rows'][0][0]
                for collection, record in [('messages', message), ('notes', note), ('activities', activity), ('deals', deal), ('stages', first), ('pipelines', pipeline), ('people', person), ('organizations', org)]:
                    request('DELETE', one(collection, record), token=token, expected=403)
                    request('GET', one(collection, record), token=token)
                result = sql("SELECT count(*) FROM audit_log WHERE action = 'delete'")
                assert result['rows'][0][0] == deletes_before, result
            with item('B10/A cascade: superuser can delete an organization referenced by a deal and by a note with no other link'):
                doomed = create('organizations', {'name': 'Doomed', 'owner': agent['id']})
                doomed_deal = create('deals', {**new_deal, 'title': 'Doomed deal', 'status': 'open', 'organization': doomed['id']})
                doomed_note = create('notes', {'body': 'Only linked to Doomed', 'organization': doomed['id'], 'owner': agent['id']})
                request('DELETE', one('organizations', doomed), token=token, expected=403)
                request('DELETE', one('organizations', doomed), token=admin, expected=204)
                request('GET', one('organizations', doomed), token=token, expected=404)
                assert request('GET', one('deals', doomed_deal), token=token)['organization'] == ''
                orphaned = request('GET', one('notes', doomed_note), token=token)
                assert orphaned['organization'] == '' and orphaned['deal'] == '' and orphaned['person'] == '', orphaned
            with item('A8 a note orphaned by a superuser delete must be linked again on its next save'):
                reject('PATCH', one('notes', doomed_note), {'body': 'Still orphaned'}, links)
                request('PATCH', one('notes', doomed_note), {'body': 'Linked again', 'deal': doomed_deal['id']}, token)
            with item('B12 audit_log delete row: full before record, superuser actor'):
                rows = audit('organizations', doomed, 'delete')
                assert len(rows) == 1 and rows[0]['actor'] == '' and rows[0]['actor_type'] == 'superuser', rows
                assert 'after' not in rows[0]['changes'] and rows[0]['changes']['before']['name'] == 'Doomed', rows
                assert rows[0]['changes']['before']['owner'] == agent['id'], rows

            with item('B12 audit_log is readable by agents through the records API and append-only'):
                listed = request('GET', many('audit_log') + '?perPage=1', token=token)
                assert listed['totalItems'] > 0 and len(listed['items']) == 1, listed
                entry = listed['items'][0]
                assert request('GET', one('audit_log', entry), token=token)['id'] == entry['id']
                assert request('GET', many('audit_log'))['items'] == [], 'audit_log is listed without authentication'
                request('POST', many('audit_log'), {'action': 'create', 'collection': 'deals', 'record': deal['id'], 'actor': agent['id'], 'actor_type': 'agent', 'changes': {}}, token, expected=403)
                request('PATCH', one('audit_log', entry), {'actor': agent2['id']}, token, expected=403)
                request('DELETE', one('audit_log', entry), token=token, expected=403)
                assert request('GET', one('audit_log', entry), token=token)['actor'] == entry['actor']
            with item('B13 audit_log is SQL-readable, including the json_extract stage history query'):
                assert 'audit_log' in json.dumps(request('GET', '/api/context/schema', token=token))
                history = create('deals', {**new_deal, 'title': 'Stage history', 'status': 'open'})
                for stage in (second, first):
                    time.sleep(.02)
                    request('PATCH', one('deals', history), {'stage': stage['id']}, token)
                request('PATCH', one('deals', history), {'title': 'Stage history renamed'}, token)
                result = sql(f"SELECT created, actor, json_extract(changes,'$.before.stage') , json_extract(changes,'$.after.stage') FROM audit_log WHERE collection='deals' AND record='{history['id']}' AND json_extract(changes,'$.after.stage') IS NOT NULL ORDER BY created")
                moves = [row[1:] for row in result['rows']]
                assert moves == [[agent['id'], None, first['id']], [agent['id'], first['id'], second['id']], [agent['id'], second['id'], first['id']]], result
                assert all(row[0] for row in result['rows']), result
                named = sql(f"SELECT f.name AS from_stage, t.name AS to_stage FROM audit_log a LEFT JOIN stages f ON f.id = json_extract(a.changes, '$.before.stage') LEFT JOIN stages t ON t.id = json_extract(a.changes, '$.after.stage') WHERE a.collection = 'deals' AND a.record = '{history['id']}' AND json_extract(a.changes, '$.after.stage') IS NOT NULL ORDER BY a.created")
                assert named['rows'] == [[None, 'Qualified'], ['Qualified', 'Negotiation'], ['Negotiation', 'Qualified']], named
            with item('B13 auth tables stay out of SQL'):
                sql('SELECT id FROM agents', expected=400)
            with item('A4 A6 unparseable dates are rejected, not replaced by the server time'):
                dated = create('deals', {**new_deal, 'title': 'Bad dates', 'status': 'open'})
                before = audit_count()
                for deals, activities in (('deals', 'activities'), (dated['collectionId'], activity['collectionId'])):
                    for bad in ('09/15/2026', '2026-13-45', 'Sep 15 2026'):
                        reject('PATCH', one(deals, dated), {'status': 'won', 'closed_at': bad}, ['closed_at'])
                        reject('PATCH', one(deals, dated), {'expected_close': bad}, ['expected_close'])
                        reject('POST', many(deals), {**new_deal, 'status': 'open', 'expected_close': bad}, ['expected_close'])
                        reject('POST', many(activities), {'subject': 'Bad date', 'kind': 'task', 'owner': agent['id'], 'due_at': '2030-01-01 09:00:00.000Z', 'done': True, 'completed_at': bad}, ['completed_at'])
                assert request('GET', one('deals', dated), token=token)['status'] == 'open' and audit_count() == before
                kept = request('PATCH', one(dated['collectionId'], dated), {'status': 'won', 'closed_at': '2026-09-15T10:00:00+02:00'}, token)
                assert kept['closed_at'] == '2026-09-15 08:00:00.000Z', kept
                by_id = request('POST', many(dated['collectionId']), {**new_deal, 'status': 'open', 'expected_close': '2030-01-01T09:00:00Z'}, token)
                assert by_id['expected_close'] == '2030-01-01 09:00:00.000Z', by_id
            with item('unknown collections retain PocketBase errors'):
                request('POST', many('missing_collection'), {'expected_close': 'invalid'}, token, expected=404)
                request('PATCH', one('missing_collection', dated), {'expected_close': 'invalid'}, token, expected=404)
            with item('B12 concurrent writes to one record: the loser gets 409, no update is lost, audit_log matches the data'):
                contested = create('deals', {**new_deal, 'title': 'Contested', 'status': 'open', 'value_minor': 100})
                conflicts = []
                def hammer(auth, field, values):
                    for value in values:
                        while request('PATCH', one('deals', contested), {field: value}, auth, expected=(200, 409)).get('status') == 409:
                            conflicts.append(field)
                workers = [threading.Thread(target=hammer, args=(token, 'title', [f't{n}' for n in range(25)])),
                           threading.Thread(target=hammer, args=(token2, 'value_minor', list(range(1000, 1025))))]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join()
                final = request('GET', one('deals', contested), token=token)
                assert (final['title'], final['value_minor']) == ('t24', 1024), (final, len(conflicts))
                replayed = dict(audit('deals', contested, 'create')[0]['changes']['after'])
                updates = audit('deals', contested, 'update')
                for row in updates:
                    replayed.update(row['changes']['after'])
                assert len(updates) == 50 and (replayed['title'], replayed['value_minor']) == ('t24', 1024), (len(updates), replayed)

            # C. Batch API: POST /api/batch runs up to 20 record requests in one transaction.
            def post(collection, body):
                return {'method': 'POST', 'url': many(collection), 'body': body}
            def patch(collection, record, body):
                return {'method': 'PATCH', 'url': one(collection, record), 'body': body}
            def batch(requests, auth=token):
                """The batch must succeed; returns the bodies in request order."""
                results = request('POST', '/api/batch', {'requests': requests}, auth)
                assert [result['status'] for result in results] == [200] * len(requests), results
                return [result['body'] for result in results]
            def batch_fails(requests, index, status, fields=(), auth=token):
                """The batch must fail with 400, blame only the request at index, and carry that request's own error."""
                result = request('POST', '/api/batch', {'requests': requests}, auth, expected=400)
                failed = result['data']['requests']
                assert list(failed) == [str(index)], result
                response = failed[str(index)]['response']
                assert response['status'] == status, result
                assert not fields or any(field in json.dumps(response) for field in fields), ('response names none of', fields, result)
            def tally(table, where='1=1'):
                return sql(f'SELECT count(*) FROM {table} WHERE {where}')['rows'][0][0]
            def counts():
                return {table: tally(table) for table in CRM + ['audit_log']}
            def absent(collection, record_id):
                request('GET', one(collection, {'id': record_id}), token=token, expected=404)

            batch_deal = {**new_deal, 'status': 'open'}
            with item('agent directory synchronization rolls back with failed account batches'):
                fixture = {'email': 'batch-agent@example.com', 'password': 'TestBatchAgentPassword123!', 'passwordConfirm': 'TestBatchAgentPassword123!', 'name': 'Batch agent', 'id': 'batchagent00001'}
                batch_fails([post('agents', fixture), post('missing_collection', {})], 1, 404, auth=admin)
                request('GET', one('agents', fixture), token=admin, expected=404)
                absent('agent_directory', fixture['id'])
                assert sql(f"SELECT id FROM agent_directory WHERE id = '{fixture['id']}'")['rows'] == []
                [batch_agent] = batch([post('agents', fixture)], auth=admin)
                assert sql(f"SELECT name FROM agent_directory WHERE id = '{batch_agent['id']}'")['rows'] == [['Batch agent']]
                batch_fails([patch('agents', batch_agent, {'name': 'Rolled back name'}), post('missing_collection', {})], 1, 404, auth=admin)
                assert request('GET', one('agents', batch_agent), token=admin)['name'] == 'Batch agent'
                assert request('GET', one('agent_directory', batch_agent), token=token)['name'] == 'Batch agent'
                batch_fails([{'method': 'DELETE', 'url': one('agents', batch_agent)}, post('missing_collection', {})], 0, 403, auth=admin)
                assert request('GET', one('agents', batch_agent), token=admin)['name'] == 'Batch agent'
                assert sql(f"SELECT name FROM agent_directory WHERE id = '{batch_agent['id']}'")['rows'] == [['Batch agent']]
                request('DELETE', one('agents', batch_agent), token=admin, expected=403)
                assert request('GET', one('agent_directory', batch_agent), token=token)['name'] == 'Batch agent'
            with item('C1 a batch returns 200 and one result per request in order; rules, auto-fill and created_by apply to each request'):
                before = counts()
                bodies = batch([post('deals', {**batch_deal, 'id': 'batchdeal000001', 'title': 'Batch deal'}),
                                post('activities', {**new_activity, 'subject': 'Batch call', 'deal': 'batchdeal000001', 'done': True}),
                                post('notes', {'body': 'Batch note', 'deal': 'batchdeal000001', 'owner': agent['id']}),
                                post('deals', {**batch_deal, 'title': 'Batch won', 'status': 'won'})])
                assert [body['collectionName'] for body in bodies] == ['deals', 'activities', 'notes', 'deals'], bodies
                assert [body.get('title') or body.get('subject') or body.get('body') for body in bodies] == ['Batch deal', 'Batch call', 'Batch note', 'Batch won'], bodies
                for body in bodies:
                    assert body['created_by'] == agent['id'] and body['updated_by'] == agent['id'], body
                assert recent(bodies[1]['completed_at']) and recent(bodies[3]['closed_at']), bodies
                assert request('GET', one('activities', bodies[1]), token=token)['completed_at'] == bodies[1]['completed_at']
                after = counts()
                assert {table: after[table] - before[table] for table in after if after[table] != before[table]} == {'deals': 2, 'activities': 1, 'notes': 1, 'audit_log': 4}, (before, after)
            with item('C3 a later request references an earlier one only through a client-chosen id'):
                assert bodies[0]['id'] == 'batchdeal000001' and bodies[1]['deal'] == 'batchdeal000001' and bodies[2]['deal'] == 'batchdeal000001', bodies
                assert request('GET', one('deals', bodies[0]), token=token)['title'] == 'Batch deal'
                joined = sql("SELECT a.subject, n.body FROM deals d JOIN activities a ON a.deal = d.id JOIN notes n ON n.deal = d.id WHERE d.id = 'batchdeal000001'")
                assert joined['rows'] == [['Batch call', 'Batch note']], joined
                before = counts()
                # Without a client-chosen id the second request has nothing to point at.
                batch_fails([post('deals', {**batch_deal, 'title': 'No id'}),
                             post('activities', {**new_activity, 'deal': '@request.0.id'})], 1, 400, ['deal'])
                batch_fails([post('deals', {**batch_deal, 'id': 'short', 'title': 'Bad id'})], 0, 400, ['id'])
                batch_fails([post('deals', {**batch_deal, 'id': 'batchdeal000001', 'title': 'Taken id'})], 0, 400, ['id'])
                assert counts() == before, (before, counts())
            with item('C9 audit_log rows of a successful batch: one create row per request, the authenticated agent as actor'):
                for body in bodies:
                    rows = audit(body['collectionName'], body, 'create')
                    assert len(rows) == 1 and rows[0]['actor'] == agent['id'] and rows[0]['actor_type'] == 'agent', (body, rows)
                assert audit('deals', bodies[0], 'create')[0]['changes']['after']['title'] == 'Batch deal'
                assert audit('activities', bodies[1], 'create')[0]['changes']['after']['deal'] == 'batchdeal000001'
                [second_org] = batch([post('organizations', {'name': 'Batch by second agent', 'owner': agent['id']})], token2)
                rows = audit('organizations', second_org, 'create')
                assert len(rows) == 1 and rows[0]['actor'] == agent2['id'] and rows[0]['actor_type'] == 'agent', rows
            with item('C2 one failing request fails the whole batch with 400 and its own error; nothing is persisted, including audit rows'):
                before = counts()
                batch_fails([post('deals', {**batch_deal, 'id': 'batchdeal000002', 'title': 'Rolled back'}),
                             post('activities', {**new_activity, 'deal': 'batchdeal000002'}),
                             post('notes', {'body': 'Orphan', 'owner': agent['id']})], 2, 400, links)
                absent('deals', 'batchdeal000002')
                batch_fails([post('deals', {**batch_deal, 'id': 'batchdeal000002', 'title': 'Rolled back', 'currency': 'ZZZ'}),
                             post('notes', {'body': 'Fine', 'deal': 'batchdeal000002', 'owner': agent['id']})], 0, 400, ['currency'])
                batch_fails([patch('deals', bodies[0], {'title': 'Rolled back title'}),
                             patch('deals', bodies[0], {'lost_reason': 'Budget'})], 1, 400, ['lost_reason'])
                assert request('GET', one('deals', bodies[0]), token=token)['title'] == 'Batch deal'
                assert counts() == before, (before, counts())
            with item('C2 B10 an agent DELETE inside a batch fails the batch with 403 for that request'):
                deletes_before = tally('audit_log', "action = 'delete'")
                before = counts()
                batch_fails([patch('deals', bodies[0], {'title': 'Rolled back title'}),
                             {'method': 'DELETE', 'url': one('notes', bodies[2])}], 1, 403)
                batch_fails([{'method': 'DELETE', 'url': one('deals', bodies[0])}], 0, 403)
                assert request('GET', one('deals', bodies[0]), token=token)['title'] == 'Batch deal'
                request('GET', one('notes', bodies[2]), token=token)
                assert counts() == before and tally('audit_log', "action = 'delete'") == deletes_before, (before, counts())
            with item('C4 two PATCHes of one record in one batch both apply, with one audit row each and no 409'):
                patched = batch([patch('deals', bodies[0], {'status': 'won'}),
                                 patch('deals', bodies[0], {'title': 'Batch deal renamed'})])
                assert patched[0]['status'] == 'won' and recent(patched[0]['closed_at']) and patched[0]['title'] == 'Batch deal', patched
                assert patched[1]['status'] == 'won' and patched[1]['title'] == 'Batch deal renamed' and patched[1]['closed_at'] == patched[0]['closed_at'], patched
                stored = request('GET', one('deals', bodies[0]), token=token)
                assert (stored['status'], stored['title'], stored['closed_at']) == ('won', 'Batch deal renamed', patched[0]['closed_at']), stored
                rows = audit('deals', bodies[0], 'update')
                assert len(rows) == 2 and all(row['actor'] == agent['id'] for row in rows), rows
                assert set(rows[0]['changes']['after']) == {'status', 'closed_at'}, rows
                assert rows[1]['changes'] == {'before': {'title': 'Batch deal'}, 'after': {'title': 'Batch deal renamed'}}, rows
            with item('C5 a batch of more than 20 requests is rejected; 20 are accepted'):
                before = counts()
                result = request('POST', '/api/batch', {'requests': [post('organizations', {'name': f'Too many {n}', 'owner': agent['id']}) for n in range(21)]}, token, expected=400)
                assert 'requests' in result['data'] and '20' in json.dumps(result['data']['requests']), result
                request('POST', '/api/batch', {'requests': []}, token, expected=400)
                assert counts() == before, (before, counts())
                assert len(batch([post('organizations', {'name': f'Twenty {n}', 'owner': agent['id']}) for n in range(20)])) == 20
                assert tally('organizations', "name LIKE 'Twenty %'") == 20 and tally('organizations', "name LIKE 'Too many %'") == 0
            with item('C6 an unauthenticated batch creates nothing'):
                before = counts()
                batch_fails([post('organizations', {'name': 'Anonymous batch', 'owner': agent['id']})], 0, 400, auth=None)
                batch_fails([post('organizations', {'id': 'batchorg0000001', 'name': 'Anonymous batch', 'owner': agent['id']}),
                             patch('deals', bodies[0], {'title': 'Anonymous batch'})], 0, 400, auth=None)
                batch_fails([patch('deals', bodies[0], {'title': 'Anonymous batch'})], 0, 404, auth=None)
                absent('organizations', 'batchorg0000001')
                assert request('GET', one('deals', bodies[0]), token=token)['title'] == 'Batch deal renamed'
                assert counts() == before and tally('organizations', "name = 'Anonymous batch'") == 0, (before, counts())
            with item('C7 B11 spoofed created_by and updated_by inside a batch are overwritten'):
                [spoofed] = batch([post('organizations', {'name': 'Batch spoof', 'owner': agent['id'], 'created_by': agent2['id'], 'updated_by': agent2['id']})])
                assert spoofed['created_by'] == agent['id'] and spoofed['updated_by'] == agent['id'], spoofed
                [respoofed] = batch([patch('organizations', spoofed, {'name': 'Batch spoof 2', 'created_by': agent2['id'], 'updated_by': agent['id']})], token2)
                assert respoofed['created_by'] == agent['id'] and respoofed['updated_by'] == agent2['id'], respoofed
                stored = request('GET', one('organizations', spoofed), token=token)
                assert stored['created_by'] == agent['id'] and stored['updated_by'] == agent2['id'], stored
                assert audit('organizations', spoofed, 'update')[0]['actor'] == agent2['id']
            with item('C8 A9 person and organization must agree when the person is created earlier in the same batch'):
                before = counts()
                batch_fails([post('people', {'id': 'batchperson0001', 'name': 'Cy', 'organization': org['id'], 'owner': agent['id']}),
                             post('deals', {**batch_deal, 'title': 'Batch mismatch', 'person': 'batchperson0001', 'organization': globex['id']})], 1, 400, ['organization'])
                absent('people', 'batchperson0001')
                assert counts() == before, (before, counts())
                cy, cy_deal, cy_note = batch([post('people', {'id': 'batchperson0001', 'name': 'Cy', 'organization': org['id'], 'owner': agent['id']}),
                                              post('deals', {**batch_deal, 'title': 'Batch match', 'person': 'batchperson0001', 'organization': org['id']}),
                                              post('notes', {'body': 'Person only', 'person': 'batchperson0001', 'owner': agent['id']})])
                assert cy['id'] == 'batchperson0001' and cy_deal['person'] == cy['id'] and cy_deal['organization'] == org['id'] and cy_note['person'] == cy['id'], (cy, cy_deal, cy_note)
                # A person without an organization may be paired with any organization, also inside a batch.
                dee, dee_deal = batch([post('people', {'id': 'batchperson0002', 'name': 'Dee', 'owner': agent['id']}),
                                       post('deals', {**batch_deal, 'title': 'Batch unaffiliated', 'person': 'batchperson0002', 'organization': globex['id']})])
                assert dee_deal['person'] == dee['id'] and dee_deal['organization'] == globex['id'], dee_deal
                # Moving the person in the same batch changes what the following request is checked against.
                before = counts()
                batch_fails([patch('people', cy, {'organization': globex['id']}),
                             post('activities', {**new_activity, 'person': cy['id'], 'organization': org['id']})], 1, 400, ['organization'])
                assert request('GET', one('people', cy), token=token)['organization'] == org['id'] and counts() == before
            with item('C10 A4 A6 unparseable dates inside a batch are rejected before any request runs'):
                before = counts()
                for deals, activities in (('deals', 'activities'), (dated['collectionId'], activity['collectionId'])):
                    for bad in ('09/15/2026', '2026-13-45', 'Sep 15 2026'):
                        reject('POST', '/api/batch', {'requests': [post(deals, {**batch_deal, 'title': 'Bad batch date', 'status': 'won', 'closed_at': bad})]}, ['closed_at'])
                        reject('POST', '/api/batch', {'requests': [post('notes', {'body': 'kept out', 'deal': dee_deal['id'], 'owner': agent['id']}), patch(deals, dee_deal, {'expected_close': bad})]}, ['expected_close'])
                        reject('POST', '/api/batch', {'requests': [post(activities, {**new_activity, 'done': True, 'completed_at': bad})]}, ['completed_at'])
                assert counts() == before, (before, counts())
            with item('collection IDs accept valid dates in batch creates and updates'):
                created, patched = batch([
                    post(dated['collectionId'], {**batch_deal, 'expected_close': '2030-01-01T09:00:00Z'}),
                    patch(dee_deal['collectionId'], dee_deal, {'expected_close': '2030-01-02T09:00:00Z'}),
                ])
                assert created['expected_close'] == '2030-01-01 09:00:00.000Z', created
                assert patched['expected_close'] == '2030-01-02 09:00:00.000Z', patched
            with item('unknown collections retain per-request batch errors and atomic rollback'):
                before = counts()
                batch_fails([post('notes', {'body': 'kept out', 'deal': dee_deal['id'], 'owner': agent['id']}),
                             post('missing_collection', {'expected_close': 'invalid'})], 1, 404)
                assert counts() == before, (before, counts())
            print('PASS: provisioning, authenticated skill revision metadata, CRM BaaS writes, authorization, validation, SQL joins, stage moves, activities, notes, closing deal, '
                  'deal and activity lifecycle rules, server-filled closed_at and completed_at, reopening, ISO 4217 currency, linked notes, '
                  'person and organization consistency, superuser-only deletes, created_by and updated_by stamps, '
                  'append-only audit_log for create, update, and delete, SQL stage history, agents table excluded from SQL, '
                  'agent directory backfill, private read access, owner joins, lifecycle synchronization and batch rollback, '
                  'invalid dates rejected, concurrent writes to one record, '
                  'batch API: ordered results, per-request rules, auto-fill, attribution and audit rows, atomic rollback, client-chosen ids, '
                  'two PATCHes of one record, 20 request limit, unauthenticated batch, agent DELETE in a batch, '
                  'person and organization consistency inside a batch')
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


if __name__ == '__main__':
    main()
