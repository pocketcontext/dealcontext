#!/usr/bin/env python3
"""Synthetic freeze, restart and thaw acceptance; never opens application pb_data."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
APP = 'dealcontext'
AUTH = 'agents'
TABLE = 'organizations'
PAYLOAD = {'name': 'Synthetic freeze organization'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    args = parser.parse_args()
    binary = str(Path(args.binary).resolve())
    with tempfile.TemporaryDirectory(prefix=APP + '-maintenance-') as tmp:
        root = Path(tmp)
        data = root / 'pb_data'
        for name in ('pb_migrations', 'pb_hooks'):
            shutil.copytree(ROOT / name, root / name)
        shutil.copy2(ROOT / 'pocketcontext.json', root / 'pocketcontext.json')
        env = {k: v for k, v in os.environ.items()
               if not k.startswith((APP.upper() + '_', 'SMTP_', 'LITESTREAM_')) and k != 'BASE_URL'}
        env[APP.upper() + '_RATE_LIMITS'] = 'false'
        common = [binary, '--dir', str(data), '--migrationsDir', str(root / 'pb_migrations'),
                  '--hooksDir', str(root / 'pb_hooks')]
        password = 'SyntheticMaintenancePassword123!'
        result = subprocess.run(common + ['superuser', 'upsert', 'admin@example.test', password],
                                cwd=root, env=env, capture_output=True)
        assert result.returncode == 0, 'isolated provisioning failed; output withheld'
        process = None
        base = ''
        log = (root / 'server.log').open('w+')
        def request(method, path, body=None, token=None, expected=200):
            headers = {'Content-Type': 'application/json'}
            if token:
                headers['Authorization'] = token
            req = urllib.request.Request(base + path, method=method, headers=headers,
                data=None if body is None else json.dumps(body).encode())
            try:
                response = urllib.request.urlopen(req, timeout=20)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                status, raw = response.status, response.read()
            assert status in ((expected,) if isinstance(expected, int) else expected), (method, path, status)
            return (raw.decode() if path == '/up' else json.loads(raw)) if raw else None
        def start():
            nonlocal process, base
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            base = f'http://127.0.0.1:{port}'
            process = subprocess.Popen(common + ['serve', '--http', f'127.0.0.1:{port}'],
                cwd=root, env=env, stdout=log, stderr=log)
            for _ in range(150):
                assert process.poll() is None, 'server exited; synthetic log retained until test cleanup'
                try:
                    request('GET', '/up')
                    return
                except (OSError, AssertionError):
                    time.sleep(.1)
            raise AssertionError('server startup timed out')
        def stop():
            nonlocal process
            if process is not None:
                process.terminate()
                process.wait(timeout=20)
                process = None
        def stored_config():
            with contextlib.closing(sqlite3.connect((data / 'data.db').as_uri() + '?mode=ro', uri=True)) as db:
                return tuple(db.execute('SELECT * FROM ' + table + ' ORDER BY id').fetchall()
                             for table in ('_params', '_collections', AUTH, '_superusers'))
        records = lambda name: '/api/collections/' + name + '/records'
        try:
            start()
            admin = request('POST', '/api/collections/_superusers/auth-with-password',
                            {'identity': 'admin@example.test', 'password': password})['token']
            user = request('POST', records(AUTH), {'email': 'member@example.test', 'name': 'Synthetic member',
                'password': password, 'passwordConfirm': password}, admin)
            token = request('POST', '/api/collections/' + AUTH + '/auth-with-password',
                            {'identity': 'member@example.test', 'password': password})['token']
            if APP == 'peoplecontext':
                request('POST', records('hr_members'), {'account': user['id']}, admin)
            PAYLOAD['owner'] = user['id']
            row = request('POST', records(TABLE), PAYLOAD, token)
            read = lambda: request('POST', '/api/context/query', {'sql': 'SELECT id FROM ' + TABLE}, token)['rows']
            baseline = read()
            assert [row['id']] in baseline
            request('GET', '/api/context/maintenance', expected=(401, 403))
            request('GET', '/api/context/maintenance', token=token, expected=(401, 403))
            state = request('GET', '/api/context/maintenance', token=admin)
            body = {'readOnly': True, 'expectedGeneration': state['generation']}
            request('PUT', '/api/context/maintenance', body, token, (401, 403))
            frozen = request('PUT', '/api/context/maintenance', body, admin)
            assert frozen['state'] == 'read_only'
            marker = json.loads((data / 'maintenance.json').read_text())
            assert marker == {'readOnly': True, 'generation': frozen['generation']}
            assert (data / 'maintenance.json').stat().st_mode & 0o077 == 0
            assert read() == baseline
            request('POST', '/api/collections/' + AUTH + '/auth-refresh', {}, token)
            request('GET', records(TABLE) + '/' + row['id'], token=token)
            for identity in (token, admin):
                request('POST', records(TABLE), PAYLOAD, identity, 503)
                request('PATCH', records(TABLE) + '/' + row['id'], {}, identity, 503)
                request('DELETE', records(TABLE) + '/' + row['id'], token=identity, expected=503)
                request('POST', '/api/batch', {'requests': [{'method': 'POST',
                    'url': records(TABLE), 'body': PAYLOAD}]}, identity, 503)
            if APP == 'dealcontext':
                request('GET', '/api/dealcontext/skill-version', token=token, expected=503)
                request('POST', '/api/intake/enquiry', {'message': 'Synthetic blocked enquiry'}, expected=503)
            config = stored_config()
            stop()
            env.update(BASE_URL='https://must-not-apply.example.test')
            env[APP.upper() + '_GOOGLE_CLIENT_ID'] = 'synthetic-changed-client'
            env[APP.upper() + '_GOOGLE_CLIENT_SECRET'] = 'synthetic-changed-secret'
            start()
            restarted = request('GET', '/api/context/maintenance', token=admin)
            assert restarted['state'] == 'read_only' and restarted['generation'] == frozen['generation']
            assert stored_config() == config, 'frozen restart changed settings or auth identities'
            assert read() == baseline
            request('POST', records(TABLE), PAYLOAD, token, 503)
            request('PUT', '/api/context/maintenance', {'readOnly': False,
                'expectedGeneration': state['generation']}, admin, 409)
            thawed = request('PUT', '/api/context/maintenance', {'readOnly': False,
                'expectedGeneration': frozen['generation']}, admin)
            assert thawed['state'] == 'writable'
            request('POST', records(TABLE), PAYLOAD, token)
            assert len(read()) == len(baseline) + 1
            # Pending migration cannot silently alter a frozen database on restart.
            state = request('PUT', '/api/context/maintenance', {'readOnly': True,
                'expectedGeneration': thawed['generation']}, admin)
            stop()
            (root / 'pb_migrations' / '9999999999_pending_freeze_fixture.js').write_text(
                'migrate((app) => {}, (app) => {});\n')
            log.flush()
            log.seek(0, os.SEEK_END)
            failure_offset = log.tell()
            process = subprocess.Popen(common + ['serve', '--http', '127.0.0.1:0'],
                cwd=root, env=env, stdout=log, stderr=log)
            assert process.wait(timeout=20) != 0, 'frozen startup accepted pending migration'
            process = None
            log.seek(failure_offset)
            failure = log.read()
            assert ('9999999999_pending_freeze_fixture.js' in failure and
                    'readonly database' in failure.lower()), 'startup failed for a reason other than frozen pending migration'
            assert stored_config() == config, 'failed frozen startup changed stored configuration'
        finally:
            stop()
            log.close()
    print('PASS: ' + APP + ' maintenance authorization, mutations/batch, reads, durable restart, identities, thaw, pending migration')


if __name__ == '__main__':
    main()
