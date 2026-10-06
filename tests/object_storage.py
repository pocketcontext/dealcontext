#!/usr/bin/env python3
"""Exercise real bootstrap S3 settings and frozen restart using synthetic databases."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PREFIX = ROOT.name.upper()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True)
    binary = str(Path(parser.parse_args().binary).resolve())
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith((PREFIX + '_', 'LITESTREAM_', 'SMTP_')) and k != 'BASE_URL'}
    remote = {PREFIX + '_S3_' + k: v for k, v in {
        'BUCKET': 'synthetic-files', 'ENDPOINT': 'http://127.0.0.1:1',
        'REGION': 'auto', 'ACCESS_KEY_ID': 'synthetic-file-key',
        'SECRET_ACCESS_KEY': 'synthetic-file-secret',
    }.items()}
    # Entrypoint rejects unsafe settings before accessing its absolute container
    # paths or attempting replica restore. Only synthetic values are supplied.
    for values in ({PREFIX + '_S3_BUCKET': 'synthetic-files'},
                   {PREFIX + '_S3_FORCE_PATH_STYLE': 'true'},
                   {**remote, PREFIX + '_S3_FORCE_PATH_STYLE': 'invalid'},
                   {**remote, 'LITESTREAM_BUCKET': 'synthetic-files'},
                   {**remote, 'LITESTREAM_ACCESS_KEY_ID': 'synthetic-file-key'}):
        result = subprocess.run(['python3', str(ROOT / 'docker/entrypoint.py')],
            env={**clean, **values}, capture_output=True, timeout=5)
        assert result.returncode != 0
        assert b'entrypoint: error:' in result.stderr, 'failed after unsafe preflight'
        assert b'synthetic-file-secret' not in result.stdout + result.stderr
        assert b'synthetic-file-key' not in result.stdout + result.stderr
    with tempfile.TemporaryDirectory(prefix=ROOT.name + '-storage-') as tmp:
        root = Path(tmp)
        for name in ('pb_hooks', 'pb_migrations'):
            shutil.copytree(ROOT / name, root / name)
        shutil.copy2(ROOT / 'pocketcontext.json', root / 'pocketcontext.json')
        data = root / 'pb_data'
        common = [binary, '--dir', str(data)]
        def run(values):
            result = subprocess.run(common + ['migrate', 'up'], cwd=root,
                                    env={**clean, **values}, capture_output=True, timeout=30)
            output = result.stdout + result.stderr
            assert b'synthetic-file-secret' not in output
            assert b'synthetic-file-key' not in output
            return result.returncode

        # No network is used: the backend settings are saved by PocketBase but
        # no file operation is performed against this deliberately closed port.
        assert run({}) == 0, 'legacy local bootstrap failed'
        for values in ({PREFIX + '_S3_BUCKET': 'synthetic-files'},
                       {PREFIX + '_S3_FORCE_PATH_STYLE': 'true'},
                       {**remote, PREFIX + '_S3_FORCE_PATH_STYLE': 'invalid'},
                       {**remote, 'LITESTREAM_BUCKET': 'synthetic-files'},
                       {**remote, 'LITESTREAM_ACCESS_KEY_ID': 'synthetic-file-key'}):
            assert run(values) != 0, 'unsafe configuration accepted'
        assert run(remote) == 0, 'complete primary settings rejected'
        assert run(remote) == 0, 'repeat configuration failed'
        assert run({}) != 0, 'remote backend silently reverted without configuration'

        # Verify stored values through the ordinary maintenance API. Then ask the
        # real server to freeze itself so the marker is not fabricated by this test.
        def serve(values, accept, freeze=False):
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            base = f'http://127.0.0.1:{port}'
            with tempfile.TemporaryFile() as log:
                proc = subprocess.Popen(common + ['serve', '--http', f'127.0.0.1:{port}'],
                                        cwd=root, env={**clean, **values}, stdout=log, stderr=log)
                try:
                    ready = False
                    for _ in range(150):
                        if proc.poll() is not None:
                            break
                        try:
                            with urllib.request.urlopen(base + '/up', timeout=.3) as response:
                                ready = response.status == 200
                            if ready:
                                break
                        except OSError:
                            time.sleep(.1)
                    assert ready == accept, 'unexpected frozen/configuration startup result'
                    if freeze:
                        def request(method, path, body=None, token=None):
                            headers = {'Content-Type': 'application/json'}
                            if token:
                                headers['Authorization'] = token
                            req = urllib.request.Request(base + path, method=method, headers=headers,
                                data=None if body is None else json.dumps(body).encode())
                            with urllib.request.urlopen(req, timeout=10) as response:
                                return json.load(response)
                        token = request('POST', '/api/collections/_superusers/auth-with-password',
                            {'identity': 'synthetic@example.test', 'password': 'SyntheticStoragePassword123!'})['token']
                        stored = request('GET', '/api/settings', token=token)['s3']
                        assert stored['enabled'] and stored['bucket'] == 'synthetic-files'
                        assert stored['forcePathStyle'] is True
                        state = request('GET', '/api/context/maintenance', token=token)
                        state = request('PUT', '/api/context/maintenance',
                            {'readOnly': True, 'expectedGeneration': state['generation']}, token)
                        assert state['state'] == 'read_only'
                    if not accept:
                        assert proc.wait(timeout=5) != 0, 'rejected bootstrap did not exit'
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                        proc.wait(timeout=10)
                    log.seek(0)
                    output = log.read()
                    assert b'synthetic-file-secret' not in output
                    assert b'synthetic-file-key' not in output

        result = subprocess.run(common + ['superuser', 'upsert', 'synthetic@example.test',
            'SyntheticStoragePassword123!'], cwd=root, env={**clean, **remote}, capture_output=True, timeout=30)
        assert result.returncode == 0, 'synthetic provisioning failed'
        serve(remote, True, freeze=True)
        serve(remote, True)
        serve({}, False)
        for name in ('BUCKET', 'SECRET_ACCESS_KEY', 'FORCE_PATH_STYLE'):
            serve({**remote, PREFIX + '_S3_' + name: ('false' if name == 'FORCE_PATH_STYLE' else 'changed')}, False)
    print('PASS: primary storage configuration, secret-safe failures, separate replica, frozen backend preservation')


if __name__ == '__main__':
    main()
