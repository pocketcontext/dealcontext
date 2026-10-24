#!/usr/bin/env python3
"""Manual importer end-to-end checks using real synthetic demo/CRM services."""
import argparse
import contextlib
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def private_json(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    parser.add_argument('--client', type=Path, default=Path(sys.executable).parent / 'dealcontext')
    parser.add_argument('--workspace', type=Path, default=ROOT.parent,
                        help='workspace containing both demo repositories')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='demo-contacts-e2e-') as directory, contextlib.ExitStack() as stack:
        tmp = Path(directory)
        common = [str(Path(args.binary).resolve()), '--dir', str(tmp / 'data'),
                  '--migrationsDir', str(ROOT / 'pb_migrations'), '--hooksDir', str(ROOT / 'pb_hooks')]
        result = subprocess.run(common + ['superuser', 'upsert', 'admin@example.com',
                                'SyntheticAdminPassword123!'], cwd=ROOT, capture_output=True)
        assert result.returncode == 0, 'synthetic server provisioning failed'
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        log = stack.enter_context(open(tmp / 'server.log', 'w'))
        proc = subprocess.Popen(common + ['serve', '--http', f'127.0.0.1:{port}'],
                                cwd=ROOT, stdout=log, stderr=log)
        def stop():
            proc.terminate()
            proc.wait(timeout=20)
        stack.callback(stop)

        def request(method, path, body=None, token=None):
            req = urllib.request.Request(base + path,
                data=json.dumps(body).encode() if body is not None else None,
                headers={'Content-Type': 'application/json', **({'Authorization': token} if token else {})}, method=method)
            with urllib.request.urlopen(req, timeout=20) as response:
                return json.load(response)

        for _ in range(150):
            try:
                request('GET', '/api/health')
                break
            except OSError:
                time.sleep(.1)
        else:
            raise AssertionError('server startup timeout')
        admin = request('POST', '/api/collections/_superusers/auth-with-password',
                        {'identity': 'admin@example.com', 'password': 'SyntheticAdminPassword123!'})['token']
        agent = request('POST', '/api/collections/agents/records', {'email': 'importer@example.com',
            'name': 'Synthetic importer', 'password': 'SyntheticPassword123!',
            'passwordConfirm': 'SyntheticPassword123!'}, admin)
        token = request('POST', '/api/collections/agents/auth-with-password',
                        {'identity': agent['email'], 'password': 'SyntheticPassword123!'})['token']
        people = []
        for name, email in [('Unique', 'unique@example.com'), ('Ambiguous A', 'duplicate@example.com'),
                            ('Ambiguous B', 'duplicate@example.com')]:
            people.append(request('POST', '/api/collections/people/records',
                {'name': name, 'email': email, 'owner': agent['id']}, token))
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('DEALCONTEXT_', 'OBSERVECONTEXT_', 'POCKETCONTEXT_'))}
        env.update(HOME=str(tmp / 'home'), XDG_CACHE_HOME=str(tmp / 'cache'), XDG_STATE_HOME=str(tmp / 'state'),
                   DEALCONTEXT_URL=base, DEALCONTEXT_AGENT_EMAIL=agent['email'],
                   DEALCONTEXT_AGENT_PASSWORD='SyntheticPassword123!')
        def command(*arguments, success=True):
            result = subprocess.run([str(args.client.resolve()), 'demo-contacts', *arguments],
                                    cwd=tmp, env=env, capture_output=True, text=True, timeout=60)
            assert (result.returncode == 0) == success, (result.returncode, result.stdout, result.stderr)
            assert 'SyntheticPassword123!' not in result.stdout + result.stderr
            return json.loads(result.stdout)

        def rows(collection):
            value = request('POST', '/api/context/query',
                {'sql': f'SELECT * FROM {collection} ORDER BY id LIMIT 500'}, token)
            assert not value.get('truncated')
            return [dict(zip(value['columns'], row)) for row in value['rows']]

        from dealcontext_client.demo_contacts import COLLECTIONS, KEYS, mapped
        sources, stores, controls = [], {}, {}
        for name in ('vaultcontext-demo', 'wikicontext-demo'):
            spec = importlib.util.spec_from_file_location(name.replace('-', '_'),
                args.workspace / name / 'deploy/demo/retention.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            store = module.Store(tmp / (name + '.db'))
            stores[name] = store
            credential = 'synthetic_export_' + name.replace('-', '_') + '_1234567890'
            verifier = tmp / (name + '-verifier.json')
            private_json(verifier, {'version': 1, 'credentials': [{'id': 'synthetic',
                'sha256': hashlib.sha256(credential.encode()).hexdigest(), 'enabled': True}]})
            control = {'incomplete': False}
            controls[name] = control
            original = store.export_snapshot
            def snapshot(original=original, control=control):
                value = original()
                if 'generated_at' in control:
                    value['generatedAt'] = control['generated_at']
                if control['incomplete']:
                    value['complete'] = False
                return value
            store.export_snapshot = snapshot
            service = http.server.ThreadingHTTPServer(('127.0.0.1', 0),
                module.handler(store, 'synthetic_backend_secret', str(verifier)))
            thread = threading.Thread(target=service.serve_forever, daemon=True)
            thread.start()
            stack.callback(service.server_close)
            stack.callback(service.shutdown)
            sources.append({'source': name, 'url': f'http://127.0.0.1:{service.server_port}', 'token': credential})
            for subject, email in [('shared-subject', 'unique@example.com'), ('ambiguous-subject', 'duplicate@example.com')]:
                store.enroll({'subject': subject, 'email': email, 'salesContact': True, 'newsletter': True,
                    'termsVersion': 'terms-v1', 'consentVersion': 'consent-v2', 'expectedRevision': 0})
            store.review({'subject': 'shared-subject', 'expectedRevision': 1,
                          'decision': 'retain', 'reason': 'Synthetic retention decision'})
        config = tmp / 'sources.json'
        private_json(config, {'version': 1, 'sources': sources})
        def sync(mode='--apply', **kwargs):
            return command('sync', '--config', str(config), mode, **kwargs)
        def counts():
            return {collection: len(rows(collection)) for collection in [*COLLECTIONS.values(), 'demo_sync_runs']}
        def compare():
            for source, store in stores.items():
                for table, collection in COLLECTIONS.items():
                    actual = {row[KEYS[table]]: row for row in rows(collection) if row['source'] == source}
                    exported = store.export_snapshot()['datasets'][table]
                    assert len(actual) == len(exported), (source, table)
                    for item in exported:
                        expected = mapped(table, item)
                        row = actual[expected[KEYS[table]]]
                        assert all(row[key] == value for key, value in expected.items()), (source, table, expected)
        before = counts()
        preview = sync('--dry-run')
        assert counts() == before
        assert all(item['changes']['linked'] == 1 and item['changes']['ambiguous'] == 1 for item in preview['sources'])
        sync()
        compare()
        contacts = rows('demo_contacts')
        assert len(contacts) == 4
        assert all(row['person'] == (people[0]['id'] if row['subject'] == 'shared-subject' else '') for row in contacts)
        identities = {collection: {row['id'] for row in rows(collection)} for collection in COLLECTIONS.values()}
        repeated = sync()
        assert all(item['changes']['create'] == item['changes']['update'] == item['changes']['delete'] == 0 for item in repeated['sources'])
        assert identities == {collection: {row['id'] for row in rows(collection)} for collection in COLLECTIONS.values()}
        stores['vaultcontext-demo'].touch('shared-subject', now=int(time.time()) + 5)
        sync()
        compare()
        store = stores['vaultcontext-demo']
        store.enroll({'subject': 'shared-subject', 'email': 'unique@example.com', 'salesContact': False,
            'newsletter': False, 'termsVersion': 'terms-v1', 'consentVersion': 'consent-v2', 'expectedRevision': 2})
        sync()
        compare()
        assert len(rows('demo_suppressions')) == 1
        store.review({'subject': 'shared-subject', 'expectedRevision': 3, 'decision': 'delete', 'reason': 'Synthetic deletion'})
        before = counts()
        controls['vaultcontext-demo']['incomplete'] = True
        command('sync', '--config', str(config), '--apply', '--source', 'vaultcontext-demo', success=False)
        assert counts() == before
        controls['vaultcontext-demo']['incomplete'] = False
        bad = json.loads(config.read_text())
        bad['sources'][0]['token'] = 'incorrect_synthetic_token_1234567890'
        private_json(config, bad)
        command('sync', '--config', str(config), '--apply', '--source', 'vaultcontext-demo', success=False)
        assert counts() == before
        private_json(config, {'version': 1, 'sources': sources})
        sync()
        compare()
        assert len(rows('people')) == 3
        assert not any(row['source'] == 'vaultcontext-demo' and row.get('subject') == 'shared-subject'
                       for collection in ('demo_contacts', 'demo_consent_events', 'demo_review_events') for row in rows(collection))
        # A fetched an older opt-in; B commits a withdrawal before A reserves a run.
        # Both exports deliberately share the same second to exercise baseline CAS.
        from dealcontext_client import cli, demo_contacts
        original_fetch = demo_contacts.fetch_snapshot
        def overlap(source):
            old = original_fetch(source)
            controls['wikicontext-demo']['generated_at'] = old['generatedAt']
            stores['wikicontext-demo'].enroll({'subject': 'shared-subject', 'email': 'unique@example.com',
                'salesContact': False, 'newsletter': False, 'termsVersion': 'terms-v1',
                'consentVersion': 'consent-v2', 'expectedRevision': 2})
            command('sync', '--config', str(config), '--apply', '--source', 'wikicontext-demo')
            return old
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(demo_contacts, 'fetch_snapshot', overlap):
            try:
                demo_contacts.synchronize(cli.config(), sources[1], apply=True)
            except cli.Fail as error:
                assert error.code == 4, str(error)
            else:
                raise AssertionError('older overlapping export overwrote a newer withdrawal')
        compare()
        assert all(not row['sales'] for row in rows('demo_contacts')
                   if row['source'] == 'wikicontext-demo' and row['subject'] == 'shared-subject')
        status = command('status')
        assert all(item['lastComplete']['status'] == 'complete' and item['running'] is None for item in status['sources'])
        assert all(row['name'] == person['name'] and row['owner'] == agent['id']
                   for row, person in zip(sorted(rows('people'), key=lambda x:x['id']), sorted(people, key=lambda x:x['id'])))
        print('PASS demo contacts CLI: real exports, dry-run, all fields, linking, retries, touch, withdrawal, erasure, incomplete/auth failures and status')


if __name__ == '__main__':
    main()
