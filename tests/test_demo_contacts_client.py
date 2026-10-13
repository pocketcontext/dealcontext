"""Synthetic unit checks for the private export boundary and recovery behavior."""
import copy
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from dealcontext_client import cli, demo_contacts as sync


def snapshot():
    data = {table: [] for table in sync.FIELDS}
    data['contacts'] = [{'subject': 'synthetic', 'email': 'person@example.com', 'sales': 1,
        'newsletter': 0, 'revision': 1, 'last_used': 100, 'review_due': 1000, 'terms': 'v1', 'consent': 'v1'}]
    return {'schemaVersion': 1, 'source': 'vaultcontext-demo', 'snapshotId': 'a' * 32,
        'generatedAt': int(time.time()), 'complete': True,
        'counts': {table: len(rows) for table, rows in data.items()}, 'datasets': data}


class Validation(unittest.TestCase):
    def test_nested_help_and_usage_require_no_configuration(self):
        with patch.object(cli, 'config', side_effect=AssertionError('must not authenticate')), \
             patch.object(sync, 'load_config', side_effect=AssertionError('must not read secrets')):
            for arguments in (['demo-contacts', '--help'],
                              ['demo-contacts', 'sync', '--help'],
                              ['demo-contacts', 'status', '--help']):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    with self.assertRaises(SystemExit) as result:
                        cli.parse(arguments)
                self.assertEqual(result.exception.code, 0)
                self.assertIn('dealcontext demo-contacts', output.getvalue())
            for arguments in (['demo-contacts'], ['demo-contacts', 'sync'],
                              ['demo-contacts', 'sync', '--config', '/missing'],
                              ['demo-contacts', 'sync', '--dry-run'],
                              ['demo-contacts', 'sync', '--config', '/missing', '--dry-run', '--apply']):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as result:
                        cli.parse(arguments)
                self.assertEqual(result.exception.code, 2)

    def test_unknown_fields_and_incomplete_exports_fail(self):
        good = snapshot()
        self.assertEqual(sync.validate_snapshot(good, 'vaultcontext-demo'), good)
        for alter in (
            lambda s: s.update(complete=False),
            lambda s: s.update(schemaVersion=2),
            lambda s: s.update(source='wikicontext-demo'),
            lambda s: s['datasets']['contacts'][0].update(token_hash='hidden'),
            lambda s: s['counts'].update(contacts=2),
            lambda s: s['datasets']['contacts'][0].update(sales=2),
            lambda s: s.update(generatedAt=int(time.time()) + 1000),
        ):
            bad = copy.deepcopy(good)
            alter(bad)
            with self.assertRaises(cli.Fail):
                sync.validate_snapshot(bad, 'vaultcontext-demo')

    def test_duplicate_keys_and_orphan_history_fail(self):
        bad = snapshot()
        bad['datasets']['contacts'] *= 2
        bad['counts']['contacts'] = 2
        with self.assertRaises(cli.Fail):
            sync.validate_snapshot(bad, 'vaultcontext-demo')
        bad = snapshot()
        bad['datasets']['consent_events'] = [{'id': 1, 'subject': 'missing', 'at': 100,
            'sales': 1, 'newsletter': 0, 'terms': 'v1', 'consent': 'v1', 'revision': 1}]
        bad['counts']['consent_events'] = 1
        with self.assertRaises(cli.Fail):
            sync.validate_snapshot(bad, 'vaultcontext-demo')

    def test_private_config_and_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            config = {'version': 1, 'sources': [{'source': 'vaultcontext-demo',
                'url': 'http://127.0.0.1:8781', 'token': 'x' * 43}]}
            path.write_text(json.dumps(config))
            path.chmod(0o600)
            self.assertEqual(sync.load_config(path)[0]['source'], 'vaultcontext-demo')
            path.chmod(0o644)
            with self.assertRaises(cli.Fail):
                sync.load_config(path)
            path.chmod(0o600)
            link = Path(tmp) / 'link'
            link.symlink_to(path)
            with self.assertRaises(cli.Fail):
                sync.load_config(link)
            fifo = Path(tmp) / 'fifo'
            os.mkfifo(fifo, 0o600)
            with self.assertRaises(cli.Fail):
                sync.load_config(fifo)
            for url in ('http://example.com', 'https://x:y@example.com', 'https://example.com/?token=x',
                        'https://example.com/#fragment', 'https://example.com/export/v1'):
                config['sources'][0]['url'] = url
                path.write_text(json.dumps(config))
                with self.assertRaises(cli.Fail):
                    sync.load_config(path)

    def test_last_used_updates_without_source_revision(self):
        value = snapshot()
        old = dict(value['datasets']['contacts'][0], id='a' * 15, sync_revision=4, person='b' * 15)
        old['last_used'] = 90
        def rows(_cfg, collection, _source):
            return [old] if collection == 'demo_contacts' else []
        with patch.object(sync, 'read_rows', side_effect=rows), patch.object(sync, 'people_matches', return_value={}):
            writes, counts = sync.plan({}, value)
        self.assertEqual(counts['update'], 1)
        self.assertEqual(writes[0]['body']['last_used'], 100)
        self.assertEqual(writes[0]['body']['revision'], 1)
        self.assertEqual(writes[0]['body']['person'], 'b' * 15)
        self.assertEqual(writes[0]['body']['expected_sync_revision'], 4)

    def test_ambiguous_match_does_not_create_person(self):
        with patch.object(sync, 'read_rows', return_value=[]), patch.object(sync, 'people_matches',
                return_value={'person@example.com': ['a' * 15, 'b' * 15]}):
            writes, counts = sync.plan({}, snapshot())
        self.assertEqual(counts['ambiguous'], 1)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]['body']['person'], '')
        self.assertEqual(writes[0]['url'], '/api/collections/demo_contacts/records')

    def test_failed_batch_marks_failed_and_never_claims_rollback(self):
        source = {'source': 'vaultcontext-demo'}
        operations = []
        def request(_cfg, method, path, body=None):
            operations.append((method, path, body))
            if path == '/api/batch':
                raise cli.Fail(1, 'synthetic failure')
            return {'sync_revision': 1}
        with patch.object(sync, 'fetch_snapshot', return_value=snapshot()), \
             patch.object(sync, 'latest_runs', return_value={'running': None, 'lastComplete': None, 'runCount': 0}), \
             patch.object(sync, 'plan', return_value=([{'method': 'POST', 'url': '/synthetic', 'body': {}}], {})), \
             patch.object(sync, 'crm', side_effect=request), \
             patch.object(sync, 'query', return_value=([{'status': 'running'}], False)):
            with self.assertRaisesRegex(cli.Fail, 'partial changes may exist'):
                sync.synchronize({}, source, True)
        self.assertEqual(operations[-1][2]['status'], 'failed')

    def test_lost_completion_response_reconciles_complete(self):
        def request(_cfg, method, path, body=None):
            if method == 'PATCH':
                raise cli.Fail(1, 'lost response')
            return {'sync_revision': 1}
        with patch.object(sync, 'fetch_snapshot', return_value=snapshot()), \
             patch.object(sync, 'latest_runs', return_value={'running': None, 'lastComplete': None, 'runCount': 0}), \
             patch.object(sync, 'plan', return_value=([], {})), patch.object(sync, 'crm', side_effect=request), \
             patch.object(sync, 'query', return_value=([{'status': 'complete'}], False)):
            result = sync.synchronize({}, {'source': 'vaultcontext-demo'}, True)
        self.assertEqual(result['status'], 'complete')


if __name__ == '__main__':
    unittest.main()
