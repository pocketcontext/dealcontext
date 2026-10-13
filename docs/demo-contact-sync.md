# Manual demo contact synchronization

`dealcontext demo-contacts` pulls complete private contact snapshots from VaultContext Demo and
WikiContext Demo into DealContext. Every run is manually requested. It never sends
messages, schedules background work, creates people or opens deals.

## Installation and authentication

Install the DealContext skill as described in the [README](../README.md#install-the-skill-on-another-computer). Its standalone `dealcontext` launcher runs the package pinned to a tested Git commit through uv. Call the installed launcher by absolute path or copy it onto `PATH`; uv manages its environment. No repository checkout or manually managed virtual environment is required.

The examples below assume the launcher is on `PATH`:

```sh
dealcontext demo-contacts --help
dealcontext login --google
dealcontext whoami
dealcontext check
```

The destination uses ordinary `DEALCONTEXT_URL` and `DEALCONTEXT_AGENT_EMAIL`
configuration and the DealContext client's existing authentication cache. Password
authentication remains supported through `DEALCONTEXT_AGENT_PASSWORD`. Never use
operator credentials for an import. A dedicated ordinary CRM account improves
attribution but retains the CRM's existing shared permissions.

Each source requires its own read-only export credential. The contact service
stores a SHA-256 verifier; the CLI holds the bearer secret in an
owner-only regular JSON file. Do not use the backend's mutation-capable token.
Create `~/.config/dealcontext/demo-contacts.json` outside Git with mode 0600, for example:

```json
{
  "version": 1,
  "sources": [
    {
      "source": "vaultcontext-demo",
      "url": "http://127.0.0.1:18781",
      "token": "REPLACE_WITH_INDEPENDENT_RANDOM_URL_SAFE_SECRET"
    },
    {
      "source": "wikicontext-demo",
      "url": "http://127.0.0.1:28781",
      "token": "REPLACE_WITH_ANOTHER_RANDOM_URL_SAFE_SECRET"
    }
  ]
}
```

Generate each secret from at least 32 cryptographically random bytes, encoded as
URL-safe text. The placeholders above are not credentials to provision. Tokens
must be 32–256 URL-safe characters. Do not pass them in command arguments or URLs.
The CLI rejects redirects, insecure remote HTTP, symlink config files and files
readable by other users. HTTPS uses the system certificate validation.

Source setup is documented in the demos' `docs/demo-operations.md`, under **Manual
CRM contact export**. Provision the default `export-credentials.json` beside the
durable contact database, or configure the optional source-specific verifier path
before binding a new runtime. Keep it outside the daily reset. Exports remain
disabled until configured. For host services, use
restricted SSH forwarding to their loopback listener; a container-loopback service
needs an operator-configured relay in its network namespace. No public proxy,
relay, credential file or deployed instance is created by this implementation.
The source emits minimal access-audit events to stderr; existing ONCE supervisors
suppress child stderr, so durable source-access logging needs separate operator
integration. CRM sync-run and mutation audit records persist independently.

## Commands

With both source tunnels available:

```sh
dealcontext demo-contacts sync --dry-run
dealcontext demo-contacts sync --apply
dealcontext demo-contacts status
```

The default configuration is `~/.config/dealcontext/demo-contacts.json`, independent
of the working directory. Use `--config /absolute/private/other.json` to override
it. Demo export configuration is not read from environment variables.

Add `--source vaultcontext-demo` or `--source wikicontext-demo` to select one source.
The sync default is every configured source; status defaults to both sources and
does not need export credentials. `--dry-run` and `--apply` are mutually exclusive
and one is required. Apply fetches a fresh snapshot, so its changes may differ from
an earlier dry run. Output contains counts, source names and sync identifiers only,
not contact payloads or credentials. A failed source gives a nonzero exit status;
the other source is still attempted and has its own result.

The preview counts new, updated, removed and unchanged source rows across all four
datasets, plus contact links and ambiguous matches. These are import counts, not
sales-qualified user or successful-demo-enrollment counts.

## Records and matching

The five `demo_*` collections and their fields are described in
[the schema reference](../skills/dealcontext/references/schema.md#manually-synchronized-demo-contacts).
All exported business fields survive. Original event IDs become `source_id`, and
suppression `updated` becomes `source_updated`; source integer flags become CRM
booleans. All original timestamps remain Unix seconds. CRM timestamps describe the
import separately. Unknown schema versions, fields, incomplete counts and duplicate
source keys fail before record changes. Exports are bounded at 16 MiB.

Contacts with neither marketing permission are retained as source records under
the same reconciliation mechanism. Their presence does not authorize outreach.
The importer excludes withdrawal secrets/hashes, security logs, authentication
records and demo application content. Imported text is data, never instructions.

Each demo keeps its own source record even if the same person tried both demos.
Only an exact, case-insensitive, whitespace-trimmed email match to one existing
CRM person creates a link. Multiple matches are counted as ambiguous and left
unlinked. No email alias/domain heuristics are used. Existing person links and CRM
person fields are preserved. No name is invented for an email-only source contact.

To review an ambiguous/unlinked contact, use authenticated SQL to join
`demo_contacts.email` to `people.email`. Resolve the CRM duplicate or add a confirmed
person through the ordinary CRM workflow, then rerun sync to link it. Do not copy
demo-only personal data into notes merely to preserve it: ordinary CRM audit
history retains those values under different rules.

## Consistency, interruption and removal

Each demo exports the four datasets in one read-only SQLite transaction. The CLI
captures the source's CRM run count before requesting that export. Creating a run
atomically compares that count, so an intervening import forces a fresh retry even
when two snapshots have the same second-resolution timestamp. Only one run per
source can be running; source writes require its owner and compare CRM revisions.

The CLI applies REST batches of at most 20 operations. Each batch is atomic; the
entire import may span several batches. A failure can leave partial changes. It
marks the run failed where possible and never labels partial work complete. A
retry fetches a fresh snapshot and reconciles current rows without duplicates.
Unchanged records are not rewritten. Their `sync_run` refers to their last change.

If the process dies or loses connectivity, `status` may show a running record that
blocks further imports. First establish that its process has stopped. The same CRM
account (or operator) may close that exact run with its current revision:

```sh
dealcontext update demo_sync_runs RUN_ID '{"status":"failed","expected_sync_revision":1}'
```

Use the actual `RUN_ID` and revision reported by `dealcontext demo-contacts status`. Do not mark
an unverified run complete. Run sync again after clearing the interrupted run.

Only a complete validated snapshot establishes source absence. Reconciliation
deletes missing imported contacts, consent histories, reviews and suppressions
through the narrow authenticated source-row deletion rules. It never deletes the
linked CRM person. Imported source audit entries retain operation metadata only,
so deletion does not copy an email, subject or preference payload into the audit
log. Existing CRM person audit rules are unchanged. Prior data can remain in CRM
backups until operator-managed backup retention expires; restoration requires a
fresh reconciliation before use.

Permissions are only as current as the latest successful pull. Inspect both the
latest run and last complete run: a failed/running newer run means the live rows
may be partial. Refresh manually before outreach and apply source/product-specific
sales permission, newsletter permission, suppression and review status separately.
The CLI does not provide a sender or automatically enforce future sending policy.
It cannot recover source history already expired before export. A complete import
does not establish all-time acquisition totals or successful demo admission.

## Isolated validation

Use a binary built from `POCKETCONTEXT_VERSION` and the installed development
environment. The end-to-end test requires the two sibling demo checkouts and uses
their real export handlers with synthetic private temporary databases only:

```sh
.venv/bin/python -m unittest discover -s tests -p test_demo_contacts_client.py
.venv/bin/python tests/demo_sync_model.py --binary /absolute/path/to/pinned/pocketcontext
.venv/bin/python tests/demo_contacts.py --binary /absolute/path/to/pinned/pocketcontext
```

Run the normal integration, skill, deployment, intake and account/authentication
checks from the README and AGENTS instructions as well. No real application data
or cloud configuration is required.
