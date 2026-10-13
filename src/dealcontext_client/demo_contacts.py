"""Manual, bounded demo-contact export and CRM reconciliation. Never sends mail."""
import json
import os
import re
import secrets
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import cli

DEFAULT_CONFIG = '~/.config/dealcontext/demo-contacts.json'
SOURCES = ('vaultcontext-demo', 'wikicontext-demo')
MAX_BYTES = 16 * 1024 * 1024
FIELDS = {
    'contacts': ('subject', 'email', 'sales', 'newsletter', 'revision', 'last_used', 'review_due', 'terms', 'consent'),
    'consent_events': ('id', 'subject', 'at', 'sales', 'newsletter', 'terms', 'consent', 'revision'),
    'suppression': ('email', 'sales', 'newsletter', 'updated'),
    'review_events': ('id', 'subject', 'at', 'decision', 'reason', 'revision'),
}
COLLECTIONS = {'contacts': 'demo_contacts', 'consent_events': 'demo_consent_events',
               'suppression': 'demo_suppressions', 'review_events': 'demo_review_events'}
KEYS = {'contacts': 'subject', 'consent_events': 'source_id', 'suppression': 'email', 'review_events': 'source_id'}
TEXT_LIMITS = {'subject': 255, 'email': 254, 'terms': 100, 'consent': 100, 'decision': 10, 'reason': 240}


def fail(message, code=1):
    raise cli.Fail(code, message)


def load_config(filename):
    """Secrets are read only from the selected private regular file."""
    try:
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                fail('export configuration must be an owner-only regular file', 2)
            raw = handle.read(65537)
        if len(raw) > 65536:
            fail('export configuration exceeds size limit', 2)
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {'version', 'sources'} or type(value['version']) is not int or value['version'] != 1:
            fail('unsupported export configuration schema', 2)
        sources = value['sources']
        if not isinstance(sources, list) or not 1 <= len(sources) <= 2:
            fail('configure one or both demo sources', 2)
        seen = set()
        for source in sources:
            if not isinstance(source, dict) or set(source) != {'source', 'url', 'token'}:
                fail('invalid export source configuration', 2)
            name, url, token = source['source'], source['url'], source['token']
            if not isinstance(name, str) or name not in SOURCES or name in seen:
                fail('invalid or repeated demo source', 2)
            seen.add(name)
            if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', token):
                fail('export token must be 32–256 URL-safe characters', 2)
            cli.hide(token)
            if not isinstance(url, str) or any(c.isspace() for c in url) or '\\' in url:
                fail('invalid export URL', 2)
            parsed = urllib.parse.urlsplit(url)
            if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or parsed.path not in ('', '/')
                    or (parsed.scheme == 'http' and parsed.hostname not in ('127.0.0.1', '::1', 'localhost'))):
                fail('export URL must be an HTTPS origin or a loopback HTTP origin', 2)
            # Validate the port before making any authenticated request.
            parsed.port
            source['url'] = url.rstrip('/')
        return sources
    except cli.Fail:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        fail('cannot read valid private export configuration', 2)


def validate_snapshot(value, source):
    """Fail closed on unknown fields, incomplete snapshots, and ambiguous keys."""
    required = {'schemaVersion', 'source', 'snapshotId', 'generatedAt', 'complete', 'counts', 'datasets'}
    if not isinstance(value, dict) or set(value) != required:
        fail('invalid export envelope; no CRM records changed')
    if (type(value['schemaVersion']) is not int or value['schemaVersion'] != 1 or value['source'] != source
            or value['complete'] is not True or not isinstance(value['snapshotId'], str)
            or not re.fullmatch(r'[a-f0-9]{32}', value['snapshotId'])
            or type(value['generatedAt']) is not int or not 0 < value['generatedAt'] <= time.time() + 300):
        fail('unsupported or incomplete export snapshot; no CRM records changed')
    if (not isinstance(value['datasets'], dict) or set(value['datasets']) != set(FIELDS)
            or not isinstance(value['counts'], dict) or set(value['counts']) != set(FIELDS)):
        fail('unexpected export datasets; no CRM records changed')
    subjects = set()
    for table, fields in FIELDS.items():
        rows = value['datasets'][table]
        if (not isinstance(rows, list) or type(value['counts'][table]) is not int
                or value['counts'][table] != len(rows)):
            fail('export counts do not match its datasets; no CRM records changed')
        seen = set()
        key = 'subject' if table == 'contacts' else 'email' if table == 'suppression' else 'id'
        for row in rows:
            if not isinstance(row, dict) or set(row) != set(fields):
                fail('unknown or missing export field; no CRM records changed')
            for field, item in row.items():
                if field in TEXT_LIMITS:
                    if not isinstance(item, str) or not 1 <= len(item) <= TEXT_LIMITS[field]:
                        fail('invalid export text field; no CRM records changed')
                elif type(item) is not int or not 0 <= item <= 9007199254740991:
                    fail('invalid export numeric field; no CRM records changed')
            if any(row.get(flag, 0) not in (0, 1) for flag in ('sales', 'newsletter')):
                fail('invalid export permission flag; no CRM records changed')
            if 'id' in row and row['id'] < 1 or 'revision' in row and row['revision'] < 1:
                fail('invalid export identity or revision; no CRM records changed')
            if 'email' in row and (row['email'].count('@') != 1 or any(c.isspace() for c in row['email'])):
                fail('invalid export email; no CRM records changed')
            if 'decision' in row and row['decision'] not in ('retain', 'delete'):
                fail('invalid export review decision; no CRM records changed')
            if row[key] in seen:
                fail('duplicate export source key; no CRM records changed')
            seen.add(row[key])
        if table == 'contacts':
            subjects = seen
    for table in ('consent_events', 'review_events'):
        if any(row['subject'] not in subjects for row in value['datasets'][table]):
            fail('export history references an absent contact; no CRM records changed')
    return value


def fetch_snapshot(source):
    request = urllib.request.Request(source['url'] + '/export/v1', headers={
        'Authorization': 'Bearer ' + source['token'], 'Accept': 'application/json', 'User-Agent': cli.USER_AGENT})
    try:
        # No redirects: export credentials must never travel to a second origin.
        with urllib.request.build_opener(cli.NoRedirect).open(request, timeout=60) as response:
            if response.status != 200 or response.headers.get_content_type() != 'application/json':
                fail('export did not return a JSON snapshot')
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            fail('export exceeds supported size; no CRM records changed')
        value = json.loads(raw)
    except urllib.error.HTTPError as error:
        fail(f'export request rejected (HTTP {error.code}); no CRM records changed')
    except cli.Fail:
        raise
    except (OSError, ValueError):
        fail('export unavailable or invalid; no CRM records changed')
    return validate_snapshot(value, source['source'])


def crm(cfg, method, path, body=None):
    """Do not echo source payloads or response error bodies into logs."""
    try:
        return cli.must(cfg, method, path, body)
    except cli.Fail as error:
        fail('CRM request failed; inspect sync status before retrying', error.code)


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def query(cfg, sql):
    page = crm(cfg, 'POST', '/api/context/query', {'sql': sql})
    if not isinstance(page, dict) or 'columns' not in page or 'rows' not in page:
        fail('unexpected CRM query response')
    return [dict(zip(page['columns'], row)) for row in page['rows']], bool(page.get('truncated'))


def read_rows(cfg, collection, source):
    rows, last = [], ''
    while True:
        page, truncated = query(cfg, f'SELECT * FROM {collection} WHERE source = {literal(source)} '
                                f'AND id > {literal(last)} ORDER BY id LIMIT 100')
        rows.extend(page)
        if not page:
            if truncated:
                fail('CRM query truncated without progress')
            return rows
        if len(page) < 100 and not truncated:
            return rows
        last = page[-1]['id']


def mapped(table, row):
    return {('source_id' if key == 'id' else 'source_updated' if table == 'suppression' and key == 'updated' else key):
            bool(value) if key in ('sales', 'newsletter') else value for key, value in row.items()}


def people_matches(cfg, emails):
    matches = {}
    emails = sorted(set(email.strip().lower() for email in emails))
    for start in range(0, len(emails), 40):
        selected = ','.join(literal(email) for email in emails[start:start + 40])
        last = ''
        while True:
            rows, truncated = query(cfg, 'SELECT id, email FROM people WHERE lower(trim(email)) IN (' + selected + ') '
                                    f'AND id > {literal(last)} ORDER BY id LIMIT 100')
            for row in rows:
                matches.setdefault(row['email'].strip().lower(), []).append(row['id'])
            if not rows:
                if truncated:
                    fail('CRM people query truncated without progress')
                break
            if len(rows) < 100 and not truncated:
                break
            last = rows[-1]['id']
    return matches


def plan(cfg, snapshot):
    source = snapshot['source']
    current = {table: read_rows(cfg, collection, source) for table, collection in COLLECTIONS.items()}
    linked = {row['subject']: row.get('person', '') for row in current['contacts']}
    matches = people_matches(cfg, [row['email'] for row in snapshot['datasets']['contacts'] if not linked.get(row['subject'])])
    counts = dict(create=0, update=0, delete=0, unchanged=0, linked=0, ambiguous=0)
    writes, deletes = [], []
    for table, collection in COLLECTIONS.items():
        key = KEYS[table]
        existing = {row[key]: row for row in current[table]}
        if len(existing) != len(current[table]):
            fail('CRM has duplicate source identities; resolve before importing')
        for exported in snapshot['datasets'][table]:
            body = mapped(table, exported)
            old = existing.pop(body[key], None)
            if table == 'contacts':
                person = old.get('person', '') if old else ''
                candidates = matches.get(body['email'].strip().lower(), [])
                if not person and len(candidates) == 1:
                    person = candidates[0]
                    counts['linked'] += 1
                elif not person and len(candidates) > 1:
                    counts['ambiguous'] += 1
                body['person'] = person
            if old is None:
                body.update(source=source, id=''.join(secrets.choice(cli.ID_ALPHABET) for _ in range(15)))
                writes.append({'method': 'POST', 'url': cli.records(collection), 'body': body})
                counts['create'] += 1
            elif any(old.get(field) != value for field, value in body.items()):
                body['expected_sync_revision'] = old['sync_revision']
                writes.append({'method': 'PATCH', 'url': cli.records(collection, old['id']), 'body': body})
                counts['update'] += 1
            else:
                counts['unchanged'] += 1
        for old in existing.values():
            deletes.append({'method': 'DELETE', 'url': cli.records(collection, old['id']),
                            'body': {'expected_sync_revision': old['sync_revision']}})
            counts['delete'] += 1
    # Erase old history before its source contact; no CRM people are deleted.
    return writes + list(reversed(deletes)), counts


def latest_runs(cfg, source):
    fields = 'id, source, snapshot_id, generated_at, status, counts, started_at, completed_at, sync_revision'
    total, _ = query(cfg, f'SELECT COUNT(*) AS total FROM demo_sync_runs WHERE source = {literal(source)} LIMIT 1')
    latest, _ = query(cfg, f'SELECT {fields} FROM demo_sync_runs WHERE source = {literal(source)} '
                     'ORDER BY started_at DESC, id DESC LIMIT 1')
    complete, _ = query(cfg, f'SELECT {fields} FROM demo_sync_runs WHERE source = {literal(source)} '
                       "AND status = 'complete' ORDER BY completed_at DESC, started_at DESC, id DESC LIMIT 1")
    running, _ = query(cfg, f'SELECT {fields} FROM demo_sync_runs WHERE source = {literal(source)} '
                      "AND status = 'running' ORDER BY started_at DESC LIMIT 1")
    return {'source': source, 'runCount': total[0]['total'], 'latest': latest[0] if latest else None,
            'lastComplete': complete[0] if complete else None, 'running': running[0] if running else None}


def synchronize(cfg, source, apply=False):
    # Capture a monotonically increasing CRM baseline BEFORE obtaining the export.
    # Server CAS prevents even same-second snapshots overwriting a concurrent sync.
    status = latest_runs(cfg, source['source'])
    if status['running']:
        fail('source already has a running sync; inspect status and resolve it before retrying', 4)
    snapshot = fetch_snapshot(source)
    if status['lastComplete'] and snapshot['generatedAt'] < status['lastComplete']['generated_at']:
        fail('source snapshot predates the latest completed import', 4)
    if not apply:
        _, counts = plan(cfg, snapshot)
        return {'source': source['source'], 'mode': 'dry-run', 'snapshotId': snapshot['snapshotId'],
                'generatedAt': snapshot['generatedAt'], 'sourceCounts': snapshot['counts'], 'changes': counts}
    run_id = ''.join(secrets.choice(cli.ID_ALPHABET) for _ in range(15))
    run = crm(cfg, 'POST', cli.records('demo_sync_runs'), {'id': run_id, 'source': source['source'],
              'snapshot_id': snapshot['snapshotId'], 'generated_at': snapshot['generatedAt'],
              'status': 'running', 'expected_run_count': status['runCount'], 'counts': {'source': snapshot['counts']}})
    counts = {}
    try:
        writes, counts = plan(cfg, snapshot)
        for offset in range(0, len(writes), 20):
            batch = writes[offset:offset + 20]
            for entry in batch:
                entry['body']['sync_run'] = run_id
            crm(cfg, 'POST', '/api/batch', {'requests': batch})
        crm(cfg, 'PATCH', cli.records('demo_sync_runs', run_id), {'status': 'complete',
            'expected_sync_revision': run['sync_revision'], 'counts': {'source': snapshot['counts'], 'changes': counts}})
    except Exception:
        # A lost completion response may already have committed. Do not turn it into failure.
        try:
            rows, _ = query(cfg, f'SELECT status FROM demo_sync_runs WHERE id = {literal(run_id)} LIMIT 1')
            if rows and rows[0]['status'] == 'complete':
                return {'source': source['source'], 'status': 'complete', 'runId': run_id, 'changes': counts}
            crm(cfg, 'PATCH', cli.records('demo_sync_runs', run_id), {'status': 'failed',
                'expected_sync_revision': run['sync_revision'], 'counts': {'source': snapshot['counts'], 'changes': counts}})
        except Exception:
            pass  # A running record remains visible and blocks a blind retry.
        fail(f'sync {run_id} did not complete; partial changes may exist; inspect status before retrying')
    return {'source': source['source'], 'status': 'complete', 'runId': run_id, 'changes': counts}


def add_parser(parent_commands, pretty):
    parser = parent_commands.add_parser('demo-contacts', parents=[pretty],
        help='manually synchronize demo contacts',
        description='Manually reconcile demo contact snapshots into DealContext. Never sends messages.')
    commands = parser.add_subparsers(dest='demo_command', required=True)
    sync = commands.add_parser('sync', parents=[pretty], help='preview or apply fresh source snapshots')
    sync.add_argument('--config', default=DEFAULT_CONFIG,
        help=f'owner-only JSON file containing demo URLs and export tokens (default: {DEFAULT_CONFIG})')
    mode = sync.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', action='store_true')
    sync.add_argument('--source', choices=SOURCES, help='select one configured source; default is every configured source')
    status = commands.add_parser('status', parents=[pretty], help='read CRM sync freshness; no export credentials required')
    status.add_argument('--source', choices=SOURCES)


def run(args):
    sources = load_config(os.path.expanduser(args.config)) if args.demo_command == 'sync' else []
    if args.demo_command == 'sync' and args.source:
        sources = [source for source in sources if source['source'] == args.source]
        if not sources:
            fail('requested source is not configured', 2)
    cfg = cli.config()
    cli.skill_advisory(cfg)
    if args.demo_command == 'status':
        result = [latest_runs(cfg, source) for source in ((args.source,) if args.source else SOURCES)]
        cli.say(cli.dump({'sources': result}, args.pretty), sys.stdout)
        return 0
    results, code = [], 0
    for source in sources:
        try:
            results.append(synchronize(cfg, source, args.apply))
        except cli.Fail as error:
            code = max(code, error.code)
            results.append({'source': source['source'], 'status': 'error', 'error': str(error)})
    cli.say(cli.dump({'sources': results}, args.pretty), sys.stdout)
    return code
