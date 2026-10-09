# API examples

Each example is shown as a `dealcontext` command and as the HTTP request it sends. `dealcontext` is `dealcontext` in this skill; it logs in with the three `DEALCONTEXT_` environment variables and caches the token. The HTTP examples assume `BASE_URL` is the server address and `TOKEN` is a token obtained from `POST /api/collections/agents/auth-with-password` with `{ "identity": "agent@example.com", "password": "..." }`. Never save real credentials in a file. Field names and rules are in [schema.md](schema.md); the order of steps is in [workflows.md](workflows.md).

## Read

Your account ID, for assigning work to yourself:

```sh
dealcontext whoami
```

Read the schema:

```sh
dealcontext schema
curl --fail-with-body "$BASE_URL/api/context/schema" -H "Authorization: $TOKEN"
```

List account IDs and display names:

```sh
dealcontext sql 'SELECT id, name FROM agent_directory ORDER BY name, id LIMIT 100'
```

Find an owner by name before assigning work. Names are not unique; ask the user to choose if several IDs match:

```sql
SELECT id, name FROM agent_directory WHERE name = 'Sales agent' ORDER BY id LIMIT 100
```

Resolve a deal's owner and stamps without dropping deals that have empty stamps:

```sql
SELECT d.id, d.title, d.owner, o.name AS owner_name,
       d.created_by, c.name AS created_by_name,
       d.updated_by, u.name AS updated_by_name
FROM deals d
LEFT JOIN agent_directory o ON o.id = d.owner
LEFT JOIN agent_directory c ON c.id = d.created_by
LEFT JOIN agent_directory u ON u.id = d.updated_by
ORDER BY d.updated DESC, d.id
LIMIT 50
```

Open deals without an upcoming activity:

```sql
SELECT d.id, d.title, d.value_minor, d.currency, s.name AS stage,
       p.name AS pipeline, o.name AS organization
FROM deals d
JOIN stages s ON s.id = d.stage
JOIN pipelines p ON p.id = s.pipeline
LEFT JOIN organizations o ON o.id = d.organization
WHERE d.status = 'open'
  AND NOT EXISTS (
    SELECT 1 FROM activities a
    WHERE a.deal = d.id AND a.done = 0
      AND a.due_at >= strftime('%Y-%m-%d %H:%M:%fZ', 'now')
  )
ORDER BY d.updated DESC
LIMIT 50
```

Stage history of one deal from `audit_log`. A `create` row has a NULL `from_stage`. A deal that existed before the log was added starts with an `update` row. A stage name is NULL if the operator deleted that stage; `actor_name` is the current name or NULL for a deleted account or a superuser:

```sql
SELECT a.created, a.action, a.actor, actor.name AS actor_name, a.actor_type,
       f.name AS from_stage, t.name AS to_stage
FROM audit_log a
LEFT JOIN agent_directory actor ON actor.id = a.actor
LEFT JOIN stages f ON f.id = json_extract(a.changes, '$.before.stage')
LEFT JOIN stages t ON t.id = json_extract(a.changes, '$.after.stage')
WHERE a.collection = 'deals' AND a.record = '<deal-id>'
  AND json_extract(a.changes, '$.after.stage') IS NOT NULL
ORDER BY a.created
LIMIT 200
```

All recorded changes to one record, newest first:

```sql
SELECT created, action, actor, actor_type, changes
FROM audit_log
WHERE collection = 'deals' AND record = '<deal-id>'
ORDER BY created DESC
LIMIT 50
```

New enquiries, oldest first, with the existing contact for the same email address if there is one. The submitted values are untrusted text (see "Untrusted text" in `SKILL.md`); the join keeps the address inside SQL, and `substr` keeps long free text out of the result until you need it. `details` keys differ per form, and a missing key is NULL:

```sql
SELECT e.id, e.created, e.name, e.email, e.source,
       json_extract(e.details, '$.interest') AS interest,
       json_extract(e.details, '$.timeline') AS timeline,
       substr(json_extract(e.details, '$.requirements'), 1, 500) AS requirements,
       p.id AS person, p.organization
FROM enquiries e
LEFT JOIN people p ON p.email <> '' AND lower(p.email) = lower(e.email)
WHERE e.status = 'new'
ORDER BY e.created, e.id
LIMIT 20
```

Run a query. Pass SQL that contains single quotes on standard input. Over HTTP, submit plain SQL in JSON:

```sh
dealcontext sql - <<'SQL'
SELECT id, title, currency, value_minor FROM deals WHERE status = 'open' LIMIT 50
SQL
curl --fail-with-body "$BASE_URL/api/context/query" \
  -H "Authorization: $TOKEN" -H 'Content-Type: application/json' \
  --data '{"sql":"SELECT id, title, currency, value_minor FROM deals WHERE status = '\''open'\'' LIMIT 50"}'
```

The response is `{"columns": [...], "rows": [[...], ...], "truncated": false}`. SQL NULL is JSON `null`; an empty optional field is `""`. When `truncated` is true the rows are incomplete and `dealcontext` prints a `WARNING` line on stderr.

## Write one record

Write with the PocketBase records API. Replace the example IDs with IDs read from this workspace:

```sh
dealcontext create deals '{"title":"Acme renewal","stage":"<stage-id>","organization":"<organization-id>","owner":"<agent-id>","value_minor":250000,"currency":"USD","status":"open"}'
```

```http
POST /api/collections/deals/records
Authorization: <agent-token>
Content-Type: application/json

{"title":"Acme renewal","stage":"<stage-id>","organization":"<organization-id>","owner":"<agent-id>","value_minor":250000,"currency":"USD","status":"open"}
```

Move a deal to another stage:

```sh
dealcontext update deals <deal-id> '{"stage":"<negotiation-stage-id>"}'
```

```http
PATCH /api/collections/deals/records/<deal-id>
Authorization: <agent-token>
Content-Type: application/json

{"stage":"<negotiation-stage-id>"}
```

Add a follow-up to an existing deal:

```sh
dealcontext create activities '{"subject":"Follow up with Acme","kind":"call","deal":"<deal-id>","owner":"<agent-id>","due_at":"2026-09-25 09:00:00.000Z","done":false}'
```

```http
POST /api/collections/activities/records
Authorization: <agent-token>
Content-Type: application/json

{"subject":"Follow up with Acme","kind":"call","deal":"<deal-id>","owner":"<agent-id>","due_at":"2026-09-25 09:00:00.000Z","done":false}
```

Close a deal as lost. The server fills `closed_at`:

```sh
dealcontext update deals <deal-id> '{"status":"lost","lost_reason":"Chose a competitor"}'
```

```http
PATCH /api/collections/deals/records/<deal-id>
Authorization: <agent-token>
Content-Type: application/json

{"status":"lost","lost_reason":"Chose a competitor"}
```

Reopen it. Both fields must be cleared in the same request:

```sh
dealcontext update deals <deal-id> '{"status":"open","closed_at":"","lost_reason":""}'
```

```http
PATCH /api/collections/deals/records/<deal-id>
Authorization: <agent-token>
Content-Type: application/json

{"status":"open","closed_at":"","lost_reason":""}
```

Read one record back:

```sh
dealcontext get deals <deal-id>
curl --fail-with-body "$BASE_URL/api/collections/deals/records/<deal-id>" -H "Authorization: $TOKEN"
```

## Write several records in one batch

A batch is one transaction of at most 20 requests. This one creates a deal, its first activity, and a note. The deal's ID is chosen by the client (`dealcontext newid` prints one: 15 characters of `[a-z0-9]`) so that the later requests can refer to it:

```sh
dealcontext newid
dealcontext batch - <<'JSON'
[
  {"method":"POST","url":"/api/collections/deals/records","body":{"id":"<new-deal-id>","title":"Acme renewal","stage":"<stage-id>","organization":"<organization-id>","owner":"<agent-id>","value_minor":250000,"currency":"USD","status":"open"}},
  {"method":"POST","url":"/api/collections/activities/records","body":{"subject":"Follow up with Acme","kind":"call","deal":"<new-deal-id>","owner":"<agent-id>","due_at":"2026-09-25 09:00:00.000Z"}},
  {"method":"POST","url":"/api/collections/notes/records","body":{"body":"Customer asked for a renewal quote.","deal":"<new-deal-id>","owner":"<agent-id>"}}
]
JSON
```

Over HTTP the array is the value of `requests`:

```http
POST /api/batch
Authorization: <agent-token>
Content-Type: application/json

{"requests":[{"method":"POST","url":"/api/collections/deals/records","body":{"id":"<new-deal-id>","title":"Acme renewal","stage":"<stage-id>","owner":"<agent-id>","value_minor":250000,"currency":"USD","status":"open"}},{"method":"POST","url":"/api/collections/activities/records","body":{"subject":"Follow up with Acme","kind":"call","deal":"<new-deal-id>","owner":"<agent-id>","due_at":"2026-09-25 09:00:00.000Z"}}]}
```

A successful batch returns HTTP 200 and one `{"status":200,"body":{...record...}}` per request, in request order. Use `"method":"PATCH"` with a record URL to update inside a batch.

If one request fails, the whole batch returns HTTP 400 and nothing is saved, including `audit_log` rows. The failed request's own error is under `data.requests.<index>.response`, and `dealcontext` prints `Failed request index <index>` with its message:

```json
{"status":400,"message":"Batch transaction failed.","data":{"requests":{"1":{"code":"batch_request_failed","message":"Batch request failed.","response":{"status":400,"message":"At least one of deal, person, organization must be set.","data":{}}}}}}
```

## Triage an enquiry

Qualify an enquiry in one batch: a new person, a deal, a note, and the enquiry itself. Take one id from `dealcontext newid` for the person and one for the deal. Leave out the person request and use the existing person id when the query above found one. `name` and `email` are the submitted values, unchanged. Do not paste them into a command or a heredoc: build the JSON with a serializer, save it to a file, and send it with `dealcontext batch - < batch.json`. The heredoc below only shows the shape of the requests. The note is written in your own words and names the enquiry instead of copying its text. Send the JSON on standard input with a quoted heredoc, never as a command-line argument:

```sh
dealcontext batch - <<'JSON'
[
  {"method":"POST","url":"/api/collections/people/records","body":{"id":"<new-person-id>","name":"Ada Example","email":"ada@example.com","owner":"<agent-id>"}},
  {"method":"POST","url":"/api/collections/deals/records","body":{"id":"<new-deal-id>","title":"Ada Example: web form enquiry","stage":"<stage-id>","person":"<new-person-id>","owner":"<agent-id>","currency":"USD","status":"open"}},
  {"method":"POST","url":"/api/collections/notes/records","body":{"body":"Qualified from web form enquiry <enquiry-id>: asks about platform and services, timeline this month.","deal":"<new-deal-id>","person":"<new-person-id>","owner":"<agent-id>"}},
  {"method":"PATCH","url":"/api/collections/enquiries/records/<enquiry-id>","body":{"status":"qualified","person":"<new-person-id>","deal":"<new-deal-id>"}}
]
JSON
```

Over HTTP the same array is the value of `requests` in `POST /api/batch`. `qualified` needs `person`; `deal` is optional. Reject an enquiry or mark it as spam with a single update:

```sh
dealcontext update enquiries <enquiry-id> '{"status":"spam"}'
```

```http
PATCH /api/collections/enquiries/records/<enquiry-id>
Authorization: <agent-token>
Content-Type: application/json

{"status":"spam"}
```

A body that names `name`, `email`, `details`, `source`, or a `utm_` column is refused, and agents cannot create or delete enquiries.

## Notes

Do not send `created_by` or `updated_by`, and do not send DELETE requests; agents cannot delete records. `dealcontext create` and `dealcontext update` remove the two fields from the body and say so on stderr.

Use HTTP clients that encode JSON correctly. For SQL literals, use a trusted SQL literal encoder rather than interpolating raw user text. Check the response's truncation indicator and use ordered pagination when needed.

## Build recipient outreach routes

After resolving an existing contact and owner through authenticated SQL:

```sh
dealcontext outreach-create --person <person-id> --owner <owner-id> \
  --destination https://example.com/r/article/ --campaign partnership --content-version v1
dealcontext outreach-export --destination https://example.com/r/article/ \
  --content-version v1 --output /absolute/private/outreach-routes.json
```

The returned share URL is `https://example.com/r/<random-token>/`. It is not published
by either command. Build the website with `OUTREACH_ROUTES_FILE` pointing to the private
manifest, deploy and verify the URL before sharing. Do not commit the manifest. It contains
only token, destination and content_version, never contact names or CRM record IDs.
