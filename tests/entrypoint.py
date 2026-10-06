#!/usr/bin/env python3
"""Synthetic runtime safety tests; no cloud credentials or production data."""
from contextlib import closing, redirect_stderr
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('entrypoint', Path(__file__).resolve().parents[1] / 'docker/entrypoint.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)

S3 = {f'DEALCONTEXT_S3_{name}': 'synthetic-' + name.lower() for name in
      ('BUCKET', 'ENDPOINT', 'REGION', 'ACCESS_KEY_ID', 'SECRET_ACCESS_KEY')}
REPLICA = {f'LITESTREAM_{name}': 'replica-' + name.lower() for name in
           ('BUCKET', 'PATH', 'ACCESS_KEY_ID', 'SECRET_ACCESS_KEY')}
ENV = dict(S3, **REPLICA)
ORIGINAL = b'Synthetic immutable evidence\n'


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wiki-runtime-test-')
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / 'data'
        self.data.mkdir()
        self.env = patch.dict(os.environ, ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def database(self):
        with closing(sqlite3.connect(self.data / 'data.db')) as conn:
            conn.executescript('CREATE TABLE _collections(id TEXT,name TEXT,type TEXT,fields TEXT); CREATE TABLE sources(id TEXT,original TEXT,sha256 TEXT); CREATE TABLE _params(id TEXT,value TEXT);')
            conn.execute('INSERT INTO _collections VALUES(?,?,?,?)', ('col', 'sources', 'base', json.dumps([{'name':'original','type':'file','maxSelect':1}])))
            conn.execute('INSERT INTO _collections VALUES(?,?,?,?)', ('org', 'organizations', 'base', '[]'))
            conn.execute('INSERT INTO sources VALUES(?,?,?)', ('doc', 'synthetic.txt', hashlib.sha256(ORIGINAL).hexdigest()))
            settings = {field: S3['DEALCONTEXT_S3_' + name] for field, name in
                        {'bucket': 'BUCKET', 'endpoint': 'ENDPOINT', 'region': 'REGION', 'accessKey': 'ACCESS_KEY_ID', 'secret': 'SECRET_ACCESS_KEY'}.items()}
            settings.update(enabled=True, forcePathStyle=True)
            conn.execute('INSERT INTO _params VALUES(?,?)', ('settings', json.dumps({'s3': settings})))
            conn.commit()

    def marker(self, state=None):
        (self.data / 'auxiliary.db').touch()
        marker = self.data / 'maintenance.json'
        marker.write_text(json.dumps(state if state is not None else {'readOnly': True, 'generation': 1}))
        marker.chmod(0o600)
        return marker

    def test_actual_auth_and_multiple_file_fields_are_inventoried(self):
        self.database()
        with closing(sqlite3.connect(self.data / 'data.db')) as conn:
            conn.execute('CREATE TABLE agents(id TEXT, avatar TEXT, documents TEXT)')
            conn.execute('INSERT INTO _collections VALUES(?,?,?,?)', ('auth', 'agents', 'auth', json.dumps([
                {'name': 'avatar', 'type': 'file', 'maxSelect': 1},
                {'name': 'documents', 'type': 'file', 'maxSelect': 4}])))
            conn.execute('INSERT INTO agents VALUES(?,?,?)', ('agent', 'portrait.png', '["a.pdf","b.pdf"]'))
            conn.commit()
        self.assertEqual(set(runtime.refs(self.data)), {'col/doc/synthetic.txt', 'auth/agent/portrait.png',
                         'auth/agent/a.pdf', 'auth/agent/b.pdf'})

    def test_invalid_collection_fields_are_rejected(self):
        self.database()
        with closing(sqlite3.connect(self.data / 'data.db')) as conn:
            conn.execute('UPDATE _collections SET fields=? WHERE name=?', ('[null]', 'sources'))
            conn.commit()
        with self.assertRaises(runtime.StartupError):
            runtime.refs(self.data)

    def test_view_file_aliases_are_not_independent_objects(self):
        self.database()
        with closing(sqlite3.connect(self.data / 'data.db')) as conn:
            conn.execute('CREATE VIEW source_alias AS SELECT * FROM sources')
            conn.execute('INSERT INTO _collections VALUES(?,?,?,?)', ('alias', 'source_alias', 'view',
                json.dumps([{'name': 'original', 'type': 'file', 'maxSelect': 1}])))
            conn.commit()
        self.assertEqual(set(runtime.refs(self.data)), {'col/doc/synthetic.txt'})

    def test_incomplete_readable_object_is_rejected(self):
        self.database()
        body = io.BytesIO(ORIGINAL)
        client = Mock()
        client.get_object.return_value = {'Body': body, 'ContentLength': len(ORIGINAL) + 1}
        with self.assertRaises(runtime.StartupError):
            runtime.verify_remote(self.data, client)
        self.assertTrue(body.closed)

    def test_corrupt_auxiliary_database_rejected_when_frozen(self):
        self.database()
        self.marker()
        (self.data / 'auxiliary.db').write_bytes(b'corrupt')
        with self.assertRaises(sqlite3.DatabaseError):
            runtime.verify_frozen_storage(self.data)

    def test_separate_buckets_and_credentials_required(self):
        for left, right in [('DEALCONTEXT_S3_BUCKET', 'LITESTREAM_BUCKET'),
                            ('DEALCONTEXT_S3_ACCESS_KEY_ID', 'LITESTREAM_ACCESS_KEY_ID')]:
            with patch.dict(os.environ, {right: ' ' + os.environ[left] + ' '}), self.assertRaises(runtime.StartupError):
                runtime.validate_config()

    def test_frozen_auxiliary_database_required(self):
        self.database()
        self.marker()
        (self.data / 'auxiliary.db').unlink()
        with self.assertRaises(runtime.StartupError):
            runtime.verify_frozen_storage(self.data)

    def test_database_path_override_rejected_before_writes_or_replica_sync(self):
        # A second data path would disagree with the fixed Litestream configuration.
        fresh = self.data / 'must-not-be-created'
        for path in ('', '/tmp/other-database', 'relative', '/storage/pb_data/../other'):
            with self.subTest(path=path), patch.dict(os.environ, {'DEALCONTEXT_DATA_DIR': path}), patch.object(runtime, 'run_command') as command, patch.object(runtime, 'restore_database') as restore:
                for initialize in (False, True):
                    with self.assertRaises(runtime.StartupError):
                        runtime.prepare(fresh, initialize=initialize)
                with self.assertRaises(runtime.StartupError):
                    runtime.serve()
                command.assert_not_called()
                restore.assert_not_called()
                self.assertFalse(fresh.exists())
        with patch.dict(os.environ, {'DEALCONTEXT_DATA_DIR': '/storage/pb_data'}):
            runtime.validate_config()
        config = (Path(__file__).resolve().parents[1] / 'docker/litestream.yml').read_text()
        self.assertIn('path: ' + str(runtime.DATA / 'data.db'), config)

    def test_configuration_requires_complete_storage_and_replica(self):
        runtime.validate_config()
        for name in ENV:
            with self.subTest(name=name), patch.dict(os.environ, {name: ''}):
                with self.assertRaises(runtime.StartupError):
                    runtime.validate_config()

    def test_disabled_replication_and_invalid_style_rejected(self):
        for name, value in [('LITESTREAM_DISABLED', 'true'), ('DEALCONTEXT_S3_FORCE_PATH_STYLE', 'invalid')]:
            with self.subTest(name=name), patch.dict(os.environ, {name: value}):
                with self.assertRaises(runtime.StartupError):
                    runtime.validate_config()

    def test_google_configuration_must_be_paired(self):
        for name in ('DEALCONTEXT_GOOGLE_CLIENT_ID', 'DEALCONTEXT_GOOGLE_CLIENT_SECRET'):
            with patch.dict(os.environ, {name: 'synthetic'}), self.assertRaises(runtime.StartupError):
                runtime.validate_config()

    def test_absent_and_valid_maintenance_state(self):
        self.assertFalse(runtime.maintenance_state(self.data))
        for frozen in (False, True):
            self.marker({'readOnly': frozen, 'generation': 2**64 - 1})
            self.assertIs(runtime.maintenance_state(self.data), frozen)

    def test_malformed_maintenance_fails_closed(self):
        states = [{}, {'readOnly': False}, {'generation': 1},
                  {'readOnly': False, 'generation': 1, 'extra': False},
                  {'readOnly': 'false', 'generation': 1}, {'readOnly': False, 'generation': True},
                  {'readOnly': False, 'generation': -1}, {'readOnly': False, 'generation': 2**64}, []]
        for state in states:
            with self.subTest(state=state):
                self.marker(state)
                with self.assertRaises(runtime.StartupError):
                    runtime.maintenance_state(self.data)
        self.marker().write_text('{invalid')
        with self.assertRaises(runtime.StartupError):
            runtime.maintenance_state(self.data)

    def test_unsafe_maintenance_files_fail_closed(self):
        marker = self.marker()
        for mode in (0o644, 0o640, 0o604):
            marker.chmod(mode)
            with self.subTest(mode=mode), self.assertRaises(runtime.StartupError):
                runtime.maintenance_state(self.data)
        marker = self.marker()
        marker.write_text(marker.read_text() + ' ' * 4096)
        with self.assertRaises(runtime.StartupError):
            runtime.maintenance_state(self.data)
        marker.unlink()
        target = self.data / 'target'
        marker.symlink_to(target)
        with self.assertRaises(runtime.StartupError):
            runtime.maintenance_state(self.data)
        target.write_text('{"readOnly": false, "generation": 1}')
        target.chmod(0o600)
        with self.assertRaises(runtime.StartupError):
            runtime.maintenance_state(self.data)
        marker.unlink()
        marker.mkdir()
        with self.assertRaises(runtime.StartupError):
            runtime.maintenance_state(self.data)

    def test_remote_verification_streams_and_closes_without_local_files(self):
        self.database()
        body = io.BytesIO(ORIGINAL)
        client = Mock()
        client.get_object.return_value = {'Body': body}
        runtime.verify_remote(self.data, client)
        self.assertTrue(body.closed)
        client.get_object.assert_called_once_with(Bucket=S3['DEALCONTEXT_S3_BUCKET'], Key='col/doc/synthetic.txt')
        self.assertFalse((self.data / 'storage').exists())

    def test_readable_unhashed_bytes_and_missing_objects(self):
        self.database()
        client = Mock()
        body = io.BytesIO(b'Corrupted')
        client.get_object.return_value = {'Body': body}
        runtime.verify_remote(self.data, client)  # No authoritative checksum: readable bytes suffice.
        self.assertTrue(body.closed)
        client.get_object.side_effect = RuntimeError('secret-provider-response')
        with self.assertRaises(RuntimeError):
            runtime.verify_remote(self.data, client)

    def test_unsafe_references_fail_before_remote_access(self):
        self.database()
        for filename, sha in [('../escape', 'a' * 64), ('nested/path', 'a' * 64)]:
            with closing(sqlite3.connect(self.data / 'data.db')) as conn:
                conn.execute('UPDATE sources SET original=?,sha256=?', (filename, sha))
                conn.commit()
            client = Mock()
            with self.subTest(filename=filename), self.assertRaises(runtime.StartupError):
                runtime.verify_remote(self.data, client)
            client.get_object.assert_not_called()

    def test_frozen_storage_must_match_all_stored_fields(self):
        self.database()
        self.marker()
        runtime.verify_frozen_storage(self.data)
        for name in S3:
            with self.subTest(name=name), patch.dict(os.environ, {name: 'changed'}), self.assertRaises(runtime.StartupError):
                runtime.verify_frozen_storage(self.data)
        with patch.dict(os.environ, {'DEALCONTEXT_S3_FORCE_PATH_STYLE': 'false'}), self.assertRaises(runtime.StartupError):
            runtime.verify_frozen_storage(self.data)

    def test_frozen_settings_checked_before_remote_access(self):
        self.database()
        self.marker()
        with patch.dict(os.environ, {'DEALCONTEXT_S3_BUCKET': 'changed'}), patch.object(runtime, 'verify_remote') as remote:
            with self.assertRaises(runtime.StartupError):
                runtime.verify(self.data)
            remote.assert_not_called()

    def test_invalid_database_fails_before_object_access(self):
        (self.data / 'data.db').write_bytes(b'not a sqlite database')
        client = Mock()
        with self.assertRaises((runtime.StartupError, sqlite3.DatabaseError)):
            runtime.verify_remote(self.data, client)
        client.get_object.assert_not_called()

    def test_existing_database_is_never_restored(self):
        self.database()
        before = (self.data / 'data.db').read_bytes()
        with patch.object(runtime, 'run_command') as command:
            runtime.restore_database(self.data)
        command.assert_not_called()
        self.assertEqual((self.data / 'data.db').read_bytes(), before)

    def test_restore_failure_does_not_install_partial_database(self):
        def interrupted(args, **kwargs):
            output = Path(args[args.index('-o') + 1])
            output.write_bytes(b'interrupted restore')
            raise runtime.StartupError('restore failed')
        with patch.object(runtime, 'run_command', side_effect=interrupted), self.assertRaises(runtime.StartupError):
            runtime.restore_database(self.data)
        self.assertFalse((self.data / 'data.db').exists())

    def test_restore_without_replica_refuses_empty_initialization(self):
        with patch.object(runtime, 'run_command') as command, patch.object(runtime, 'verify', side_effect=runtime.refs), self.assertRaises(runtime.StartupError):
            runtime.restore_database(self.data)
        self.assertNotIn('-if-replica-exists', command.call_args.args[0])
        self.assertFalse((self.data / 'data.db').exists())

    def test_explicit_init_refuses_existing_database(self):
        self.database()
        before = (self.data / 'data.db').read_bytes()
        with patch.object(runtime, 'run_command') as command, self.assertRaises(runtime.StartupError):
            runtime.restore_database(self.data, initialize=True)
        command.assert_not_called()
        self.assertEqual((self.data / 'data.db').read_bytes(), before)

    def test_explicit_init_refuses_existing_replica(self):
        def restored(args, **kwargs):
            output = Path(args[args.index('-o') + 1])
            with closing(sqlite3.connect(output)) as conn:
                conn.execute('CREATE TABLE synthetic(value TEXT)')
        with patch.object(runtime, 'run_command', side_effect=restored) as command, self.assertRaises(runtime.StartupError):
            runtime.restore_database(self.data, initialize=True)
        self.assertIn('-if-replica-exists', command.call_args.args[0])
        self.assertFalse((self.data / 'data.db').exists())

    def test_frozen_start_skips_restore_and_provisioning(self):
        self.database()
        self.marker()
        with patch.object(runtime, 'restore_database') as restore, patch.object(runtime, 'verify') as verify, patch.object(runtime, 'run_command') as command:
            runtime.prepare(self.data)
        restore.assert_not_called()
        command.assert_not_called()
        verify.assert_called_once_with(self.data)

    def test_frozen_missing_database_fails_before_restore(self):
        self.marker()
        with patch.object(runtime, 'restore_database') as restore, patch.object(runtime, 'run_command') as command, self.assertRaises(runtime.StartupError):
            runtime.prepare(self.data)
        restore.assert_not_called()
        command.assert_not_called()

    def test_serve_requires_successful_sync(self):
        with patch.object(runtime, 'run_command', side_effect=runtime.StartupError('sync failed')), patch.object(runtime.os, 'execve') as execute:
            with self.assertRaises(runtime.StartupError):
                runtime.serve()
        execute.assert_not_called()

    def test_server_environment_preserves_s3_and_removes_replica_secrets(self):
        secrets = {'AWS_ACCESS_KEY_ID': 'aws-id', 'AWS_SECRET_ACCESS_KEY': 'aws-secret',
                   'AWS_SESSION_TOKEN': 'aws-session', 'DEALCONTEXT_SUPERUSER_PASSWORD': 'bootstrap-secret'}
        with patch.dict(os.environ, dict(secrets, DEALCONTEXT_SUPERUSER_EMAIL='operator@example.test')), patch.object(runtime, 'run_command') as sync, patch.object(runtime.os, 'execve') as execute:
            runtime.serve()
        self.assertIn('sync', sync.call_args.args[0])
        self.assertIn('-wait', sync.call_args.args[0])
        executable, args, env = execute.call_args.args
        self.assertIn('serve', args)
        for name in (*secrets, 'LITESTREAM_ACCESS_KEY_ID', 'LITESTREAM_SECRET_ACCESS_KEY'):
            self.assertNotIn(name, env)
        for name, value in S3.items():
            self.assertEqual(env[name], value)

    def test_failed_child_output_never_enters_exception(self):
        secret = 'synthetic-private-provider-response'
        with self.assertRaises(runtime.StartupError) as result:
            runtime.run_command([sys.executable, '-c',
                                 'import sys; print("' + secret + '"); print("' + secret + '", file=sys.stderr); sys.exit(1)'])
        self.assertNotIn(secret, str(result.exception))

    def test_top_level_suppresses_provider_and_timeout_details(self):
        failures = [RuntimeError('synthetic-secret'),
                    subprocess.TimeoutExpired(['synthetic-secret'], 1)]
        for error in failures:
            output = io.StringIO()
            with patch.object(runtime.os, 'chdir'), patch.object(runtime.os, 'umask'), patch.object(runtime, 'prepare', side_effect=error), redirect_stderr(output):
                self.assertEqual(runtime.main(['start']), 1)
            self.assertNotIn('synthetic-secret', output.getvalue())
            self.assertIn(type(error).__name__, output.getvalue())

    def test_child_timeout_terminates_child(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            runtime.run_command([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=0.01)

    def test_restore_installs_only_complete_database(self):
        def restored(args, **kwargs):
            output = Path(args[args.index('-o') + 1])
            with closing(sqlite3.connect(output)) as conn:
                conn.execute('CREATE TABLE synthetic(value TEXT)')
                conn.execute('INSERT INTO synthetic VALUES(?)', ('restored',))
                conn.commit()
        with patch.object(runtime, 'run_command', side_effect=restored), patch.object(runtime, 'verify') as verify:
            runtime.restore_database(self.data)
        verify.assert_called_once()
        with closing(sqlite3.connect(self.data / 'data.db')) as conn:
            self.assertEqual(conn.execute('SELECT value FROM synthetic').fetchone(), ('restored',))

    def test_corrupt_restored_database_is_not_installed(self):
        def restored(args, **kwargs):
            Path(args[args.index('-o') + 1]).write_bytes(b'corrupt database')
        with patch.object(runtime, 'run_command', side_effect=restored), patch.object(runtime, 'verify', side_effect=runtime.refs), self.assertRaises((runtime.StartupError, sqlite3.DatabaseError)):
            runtime.restore_database(self.data)
        self.assertFalse((self.data / 'data.db').exists())

    def test_start_execs_litestream_as_supervisor(self):
        with patch.object(runtime.os, 'chdir'), patch.object(runtime.os, 'umask'), patch.object(runtime, 'prepare') as prepare, patch.object(runtime.os, 'execve') as execute:
            self.assertEqual(runtime.main(['start']), 0)
        prepare.assert_called_once_with(runtime.DATA, initialize=False)
        executable, args, env = execute.call_args.args
        self.assertEqual(executable, runtime.LITESTREAM)
        self.assertEqual(args[1], 'replicate')
        self.assertEqual(args[args.index('-exec') + 1], runtime.SELF + ' serve')
        self.assertEqual(env['LITESTREAM_SECRET_ACCESS_KEY'], REPLICA['LITESTREAM_SECRET_ACCESS_KEY'])

    def test_init_is_one_shot_and_never_starts_writer(self):
        with patch.object(runtime.os, 'chdir'), patch.object(runtime.os, 'umask'), patch.object(runtime, 'prepare') as prepare, patch.object(runtime.os, 'execve') as execute:
            self.assertEqual(runtime.main(['init']), 0)
        prepare.assert_called_once_with(runtime.DATA, initialize=True)
        execute.assert_not_called()

    def test_bad_config_stops_before_touching_data(self):
        absent = self.data / 'not-created'
        with patch.dict(os.environ, {'DEALCONTEXT_S3_BUCKET': ''}), patch.object(runtime, 'restore_database') as restore:
            with self.assertRaises(runtime.StartupError):
                runtime.prepare(absent)
        restore.assert_not_called()
        self.assertFalse(absent.exists())

    def test_orphan_local_state_blocks_recovery(self):
        for name in ('data.db-wal', 'maintenance.json', 'auxiliary.db'):
            orphan = self.data / name
            orphan.touch()
            with self.subTest(name=name), patch.object(runtime, 'run_command') as command, self.assertRaises(runtime.StartupError):
                runtime.restore_database(self.data)
            command.assert_not_called()
            orphan.unlink()

    def test_existing_database_symlink_is_not_followed(self):
        target = self.data.parent / 'target.db'
        target.write_bytes(b'untouched')
        (self.data / 'data.db').symlink_to(target)
        with patch.object(runtime, 'run_command') as command, self.assertRaises(runtime.StartupError):
            runtime.restore_database(self.data)
        command.assert_not_called()
        self.assertEqual(target.read_bytes(), b'untouched')

    def test_failed_evidence_check_does_not_install_restored_database(self):
        def restored(args, **kwargs):
            Path(args[args.index('-o') + 1]).write_bytes(b'synthetic staged database')
        with patch.object(runtime, 'run_command', side_effect=restored), patch.object(runtime, 'verify', side_effect=runtime.StartupError('missing evidence')):
            with self.assertRaises(runtime.StartupError):
                runtime.restore_database(self.data)
        self.assertFalse((self.data / 'data.db').exists())
        self.assertEqual(list(self.data.parent.glob('.dealcontext-restore-*')), [])

    def test_sigterm_during_preparation_reaches_child_and_waits(self):
        ready = self.data / 'ready'
        stopped = self.data / 'stopped'
        child = self.data / 'child.py'
        child.write_text('import signal, sys, time\nfrom pathlib import Path\n'
                         'def stop(signum, frame):\n'
                         '    Path(sys.argv[2]).write_text("stopped")\n'
                         '    raise SystemExit(0)\n'
                         'signal.signal(signal.SIGTERM, stop)\n'
                         'Path(sys.argv[1]).write_text("ready")\n'
                         'while True: time.sleep(0.1)\n')
        wrapper = self.data / 'wrapper.py'
        wrapper.write_text('import importlib.util, sys\n'
                           'spec = importlib.util.spec_from_file_location("runtime", sys.argv[1])\n'
                           'runtime = importlib.util.module_from_spec(spec)\n'
                           'spec.loader.exec_module(runtime)\n'
                           'runtime.run_command([sys.executable, *sys.argv[2:]])\n')
        process = subprocess.Popen([sys.executable, str(wrapper), str(Path(runtime.__file__)), str(child), str(ready), str(stopped)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), 'synthetic child did not start')
            process.terminate()
            self.assertEqual(process.wait(timeout=5), 143)
            self.assertEqual(stopped.read_text(), 'stopped')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    def test_interrupted_initialization_remains_blocked_on_restart(self):
        def failed_migration(args, **kwargs):
            self.assertTrue((self.data / 'initialization.pending').exists())
            (self.data / 'data.db').write_bytes(b'partial migration')
            raise runtime.StartupError('interrupted migration')
        with patch.object(runtime, 'restore_database', return_value=None), patch.object(runtime, 'run_command', side_effect=failed_migration):
            with self.assertRaises(runtime.StartupError):
                runtime.prepare(self.data, initialize=True)
        self.assertTrue((self.data / 'initialization.pending').exists())
        with patch.object(runtime, 'restore_database') as restore, patch.object(runtime, 'run_command') as command:
            with self.assertRaises(runtime.StartupError):
                runtime.prepare(self.data)
        restore.assert_not_called()
        command.assert_not_called()

    def test_successful_init_clears_marker_after_verification_and_provisioning(self):
        steps = []
        def command(args, **kwargs):
            self.assertTrue((self.data / 'initialization.pending').exists())
            steps.append(args[1])
        def verify(data):
            self.assertTrue((data / 'initialization.pending').exists())
            steps.append('verify')
        bootstrap = {'DEALCONTEXT_SUPERUSER_EMAIL': 'operator@example.test',
                     'DEALCONTEXT_SUPERUSER_PASSWORD': 'synthetic-bootstrap'}
        with patch.dict(os.environ, bootstrap), patch.object(runtime, 'restore_database', return_value=None), patch.object(runtime, 'run_command', side_effect=command), patch.object(runtime, 'verify', side_effect=verify):
            runtime.prepare(self.data, initialize=True)
        self.assertEqual(steps, ['migrate', 'verify', 'superuser'])
        self.assertFalse((self.data / 'initialization.pending').exists())

    def test_failed_init_verification_retains_marker(self):
        with patch.object(runtime, 'restore_database', return_value=None), patch.object(runtime, 'run_command'), patch.object(runtime, 'verify', side_effect=runtime.StartupError('verification failed')):
            with self.assertRaises(runtime.StartupError):
                runtime.prepare(self.data, initialize=True)
        self.assertTrue((self.data / 'initialization.pending').exists())

    def test_pending_marker_dangling_symlink_refuses_startup(self):
        (self.data / 'initialization.pending').symlink_to(self.data / 'absent')
        with patch.object(runtime, 'restore_database') as restore, self.assertRaises(runtime.StartupError):
            runtime.prepare(self.data)
        restore.assert_not_called()


if __name__ == '__main__':
    unittest.main()
