#!/usr/bin/env python3
"""Synthetic protected S3 files and exact Litestream recovery using real containers."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import secrets
import sqlite3
import tempfile
import time
import urllib.error
import urllib.request


def database_digest(path):
    digest = hashlib.sha256()
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        for pragma in ('user_version', 'application_id'):
            digest.update(json.dumps([pragma, db.execute('PRAGMA ' + pragma).fetchone()[0]]).encode())
        schema = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
        digest.update(json.dumps(schema).encode())
        for kind, name, _, _ in schema:
            if kind != 'table':
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            try:
                rows = db.execute('SELECT rowid,* FROM ' + quoted)
            except sqlite3.OperationalError:
                rows = db.execute('SELECT * FROM ' + quoted)
            encoded = [json.dumps([(type(v).__name__, v.hex() if isinstance(v, bytes) else v)
                                   for v in row]) for row in rows]
            digest.update(json.dumps([name, sorted(encoded)]).encode())
    return digest.digest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image')
    parser.add_argument('--minio-image')
    parser.add_argument('--compare', nargs=2)
    parser.add_argument('--auxiliary-source')
    parser.add_argument('--auxiliary-target')
    args = parser.parse_args()
    if args.compare:
        assert database_digest(args.compare[0]) == database_digest(args.compare[1]), 'database mismatch'
        if bool(args.auxiliary_source) != bool(args.auxiliary_target):
            parser.error('auxiliary source and target must be supplied together')
        if args.auxiliary_source:
            auxiliary = Path(args.auxiliary_source)
            assert auxiliary.is_file(), 'missing auxiliary migration snapshot'
            target_path = Path(args.auxiliary_target)
            assert not target_path.exists(), 'auxiliary target already exists'
            target_path.touch(mode=0o600)
            with sqlite3.connect(auxiliary.resolve().as_uri() + '?mode=ro', uri=True) as source:
                with sqlite3.connect(target_path) as target:
                    source.backup(target)
                    assert target.execute('PRAGMA integrity_check').fetchone() == ('ok',)
            target_path.chmod(0o600)
        print('PASS: full logical database and integrity match')
        return
    if not args.image or not args.minio_image:
        parser.error('--image and --minio-image are required for the container drill')
    root = Path(__file__).resolve().parents[1]
    prefix = root.name.upper()
    auth = 'users' if root.name == 'raisecontext' else 'agents'
    spec = importlib.util.spec_from_file_location('smoke', root / 'docker/smoke.py')
    s = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(s)
    run = root.name + '-s3-' + secrets.token_hex(4)
    minio = run + '-minio'
    failed = True
    try:
        s.docker('network', 'create', run); s.networks.append(run)
        access = s.secret('synthetic-root')
        secret = s.secret(secrets.token_hex(24))
        mc = {'MC_HOST_test': s.secret(f'http://{access}:{secret}@127.0.0.1:9000')}
        s.containers.append(minio)
        s.docker('run', '-d', '--name', minio, '--network', run,
                 '-e', 'MINIO_ROOT_USER', '-e', 'MINIO_ROOT_PASSWORD', args.minio_image,
                 'server', '/data', env={'MINIO_ROOT_USER': access, 'MINIO_ROOT_PASSWORD': secret})
        for _ in range(60):
            status, _ = s.docker('exec', '-e', 'MC_HOST_test', minio, 'mc', 'mb',
                                '--ignore-existing', 'test/files', 'test/replica', env=mc, ok=False)
            if status == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('MinIO fixture not ready')
        with tempfile.TemporaryDirectory(prefix=run) as directory:
            tmp = Path(directory)
            keys = {}
            for bucket in ('files', 'replica'):
                key = s.secret('synthetic-' + bucket)
                password = s.secret(secrets.token_hex(24))
                keys[bucket] = (key, password)
                policy = tmp / (bucket + '.json')
                policy.write_text(json.dumps({'Version': '2012-10-17', 'Statement': [{
                    'Effect': 'Allow', 'Action': ['s3:*'],
                    'Resource': ['arn:aws:s3:::' + bucket, 'arn:aws:s3:::' + bucket + '/*']}]}))
                s.docker('cp', str(policy), minio + ':/tmp/' + policy.name)
                s.docker('exec', '-e', 'MC_HOST_test', minio, 'mc', 'admin', 'user', 'add',
                         'test', key, password, env=mc)
                s.docker('exec', '-e', 'MC_HOST_test', minio, 'mc', 'admin', 'policy', 'create',
                         'test', bucket, '/tmp/' + policy.name, env=mc)
                s.docker('exec', '-e', 'MC_HOST_test', minio, 'mc', 'admin', 'policy', 'attach',
                         'test', bucket, '--user', key, env=mc)
            env = s.once_env({prefix + '_S3_BUCKET': 'files', prefix + '_S3_ENDPOINT': f'http://{minio}:9000',
                prefix + '_S3_REGION': 'us-east-1', prefix + '_S3_ACCESS_KEY_ID': keys['files'][0],
                prefix + '_S3_SECRET_ACCESS_KEY': keys['files'][1], prefix + '_RATE_LIMITS': 'false',
                'LITESTREAM_BUCKET': 'replica', 'LITESTREAM_PATH': 'synthetic/data',
                'LITESTREAM_ENDPOINT': f'http://{minio}:9000', 'LITESTREAM_REGION': 'us-east-1',
                'LITESTREAM_ACCESS_KEY_ID': keys['replica'][0], 'LITESTREAM_SECRET_ACCESS_KEY': keys['replica'][1],
                'LITESTREAM_SYNC_INTERVAL': '1h'})
            first, second = run + '-a', run + '-b'
            s.run_app(args.image, first, first, env, run); base = s.wait_up(first)
            admin = s.superuser_token(base, env[prefix + '_SUPERUSER_EMAIL'], env[prefix + '_SUPERUSER_PASSWORD'])
            def api(method, path, body=None, token=admin):
                status, _, result = s.http(method, base + path, body, token)
                assert status == 200, (method, path, status)
                return result
            users = []
            for n in range(2):
                password = s.secret(secrets.token_hex(24))
                email = f'synthetic{n}@example.test'
                user = api('POST', f'/api/collections/{auth}/records', {'email': email, 'name': 'Synthetic',
                    'password': password, 'passwordConfirm': password})
                token = s.secret(api('POST', f'/api/collections/{auth}/auth-with-password',
                    {'identity': email, 'password': password}, None)['token'])
                users.append((user['id'], token))
            rule = '@request.auth.id != "" && owner = @request.auth.id'
            collection = api('POST', '/api/collections', {'name': 'synthetic_storage', 'type': 'base',
                'listRule': rule, 'viewRule': rule, 'createRule': rule, 'updateRule': rule,
                'fields': [{'name': 'owner', 'type': 'text', 'required': True},
                           {'name': 'original', 'type': 'file', 'protected': True, 'maxSelect': 1}]})
            payload = b'Synthetic immutable file bytes after initial replica sync\n'
            boundary = secrets.token_hex(24)
            body = (f'--{boundary}\r\nContent-Disposition: form-data; name="owner"\r\n\r\n{users[0][0]}\r\n'
                    f'--{boundary}\r\nContent-Disposition: form-data; name="original"; filename="fixture.txt"\r\n'
                    'Content-Type: text/plain\r\n\r\n').encode() + payload + f'\r\n--{boundary}--\r\n'.encode()
            req = urllib.request.Request(base + '/api/collections/synthetic_storage/records', data=body,
                headers={'Authorization': users[0][1], 'Content-Type': 'multipart/form-data; boundary=' + boundary})
            with urllib.request.urlopen(req, timeout=20) as response:
                record = json.load(response)
            key = collection['id'] + '/' + record['id'] + '/' + record['original']
            assert s.docker('exec', '-e', 'MC_HOST_test', minio, 'mc', 'cat', 'test/files/' + key, env=mc)[1].encode() == payload
            def verify():
                api('GET', '/api/collections/synthetic_storage/records/' + record['id'], token=users[0][1])
                for token, allowed in [(None, False), (users[1][1], False), (users[0][1], True)]:
                    file_token = '' if token is None else s.secret(api('POST', '/api/files/token', {}, token)['token'])
                    req = urllib.request.Request(base + '/api/files/' + key + '?token=' + file_token)
                    try:
                        response = urllib.request.urlopen(req, timeout=20)
                    except urllib.error.HTTPError as error:
                        response = error
                    with response:
                        data, code = response.read(), response.status
                    assert (code == 200 and data == payload) if allowed else code in (401,403,404)
            verify()
            assert not s.docker('exec', first, 'sh', '-c',
                'find /storage/pb_data/storage -type f 2>/dev/null || true')[1].strip()
            state = api('GET', '/api/context/maintenance')
            frozen = api('PUT', '/api/context/maintenance', {'readOnly': True, 'expectedGeneration': state['generation']})
            assert frozen['state'] == 'read_only'
            status, _, _ = s.http('POST', base + '/api/collections/synthetic_storage/records', {'owner':users[0][0]}, users[0][1])
            assert status == 503
            verify()
            s.stop(first)
            s.docker('volume', 'create', second); s.volumes.append(second)
            restore = ['run', '--rm', '--network', run, '-v', second + ':/storage']
            for keyname in env:
                restore.extend(['-e', keyname])
            s.docker(*restore, '--entrypoint', 'sh', args.image, '-c',
                'mkdir -p /storage/pb_data; exec litestream restore -config /etc/litestream.yml /storage/pb_data/data.db', env=env)
            s.docker('run', '--rm', '-v', first + ':/source:ro', '-v', second + ':/restored',
                '-v', str(Path(__file__).resolve()) + ':/compare.py:ro', '--entrypoint', 'sh', args.image, '-c',
                'cp -a /source/pb_data /tmp/source; cp -a /restored/pb_data /tmp/restored; '
                'python3 /compare.py --compare /tmp/source/data.db /tmp/restored/data.db '
                '--auxiliary-source /tmp/source/auxiliary.db '
                '--auxiliary-target /restored/pb_data/auxiliary.db && '
                'cp -p /source/pb_data/maintenance.json /restored/pb_data/maintenance.json')
            s.check(True, 'entire database recovered after one-hour interval clean shutdown; frozen marker preserved')
            s.docker('rm', first); s.docker('volume', 'rm', first); s.volumes.remove(first)
            s.run_app(args.image, second, second, env, run); s.volumes.remove(second); base = s.wait_up(second)
            assert api('GET', '/api/context/maintenance')['state'] == 'read_only'
            verify()
            status, _, _ = s.http('POST', base + '/api/collections/synthetic_storage/records', {'owner':users[0][0]}, users[0][1])
            assert status == 503
            state = api('GET', '/api/context/maintenance')
            api('PUT', '/api/context/maintenance', {'readOnly': False, 'expectedGeneration': state['generation']})
            verify()
            late = api('POST', '/api/collections/synthetic_storage/records',
                       {'owner': users[0][0]}, users[0][1])
            s.stop(second)
            s.check_logs(second)
            s.docker('rm', second); s.docker('volume', 'rm', second); s.volumes.remove(second)
            # Writable disaster recovery has no migration bundle. The ordinary
            # entrypoint must restore data.db and initialize auxiliary state.
            third = run + '-c'
            s.run_app(args.image, third, third, env, run); base = s.wait_up(third)
            verify()
            api('GET', '/api/collections/synthetic_storage/records/' + late['id'], token=users[0][1])
            recovered_admin = s.superuser_token(base, env[prefix + '_SUPERUSER_EMAIL'], env[prefix + '_SUPERUSER_PASSWORD'])
            assert api('GET', '/api/context/maintenance', token=recovered_admin)['state'] == 'writable'
            assert 'post-restore integrity check passed' in s.logs(third)
            s.docker('exec', third, 'test', '-f', '/storage/pb_data/auxiliary.db')
            s.stop(third)
            s.check_logs(third)
            s.check(True, 'automatic empty-volume S3 recovery retained late record and protected object with no auxiliary/marker bundle')
            failed = False
            print('PASS: protected S3 upload, independent file access, exact late-write recovery, frozen fresh-volume handoff')
    finally:
        s.report_and_clean(failed)


if __name__ == '__main__':
    main()
