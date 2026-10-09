# Schema

Every CRM record has `id`, `created`, `updated`, `created_by`, and `updated_by`. IDs are PocketBase record IDs: 15 characters from `[a-z0-9]`. The server generates one unless a create sends its own `id`, which a batch uses to link new records (see [workflows.md](workflows.md)). Dates use UTC strings, for example `2026-09-19 14:00:00.000Z`. A date-only value such as `2026-12-31` is accepted and stored as midnight UTC. Missing optional numbers are stored as 0 and missing bools as false; in SQL a bool compares as `0` or `1` and comes back as JSON `true` or `false`. Missing optional strings, dates, and relations are empty strings, not SQL NULL. Single relations store record IDs.

| Collection | Fields |
| --- | --- |
| organizations | name (required), website, address, owner (required agent ID) |
| people | name (required), email, phone, linkedin_url, job_title, pronouns, organization, owner (required) |
| pipelines | name (required, unique), active |
| stages | name (required), pipeline (required), position (nonnegative integer), probability (0–100) |
| deals | title, stage, owner, currency, status (all required); organization, person, value_minor, expected_close, closed_at, lost_reason |
| activities | subject, kind, owner, due_at (required); deal, person, organization, done, completed_at, description |
| notes | body, owner (required); deal, person, organization, source_url |
| messages | person, owner, channel, direction, body (required); sent_at, source_url |
| outreach_links | person, owner, token, destination, campaign, content_version (all required) |

Deal status is `open`, `won`, or `lost`. Activity kind is `call`, `meeting`, `email`, or `task`. Stage position is unique within its pipeline. A deal belongs to a pipeline through its stage; join `deals.stage = stages.id` and `stages.pipeline = pipelines.id`.

`value_minor` is an integer amount in the currency's minor unit (USD 12500 means $125.00; JPY 12500 means ¥12500). `currency` is a required three-letter uppercase code and must be an active ISO 4217 alphabetic code; the server rejects others, including `ZZZ`, `XXX`, and `XTS`. Conversion is an application responsibility. Do not add amounts across currencies without an explicit conversion policy. Values are limited to JavaScript's safe integer range.

`agents` is the shared human and agent auth collection with a required display `name`. It supports password login and Google Workspace OAuth. Eligible verified Workspace identities can be created on first login when JIT is enabled; operators manage existing accounts. Auth records are excluded from SQL. Use your authenticated record ID when assigning work to yourself; query `agent_directory` for other account IDs and names.

`created_by` and `updated_by` are optional relations to `agents`. For agent requests the server sets both to the authenticated agent on create, and sets `updated_by` on update while `created_by` keeps its stored value. Values sent by an agent are ignored. Superuser requests leave both fields as they are, so records created by a superuser have them empty unless the superuser sets them. Accounts cannot be deleted. Operators disable them to revoke access while preserving these stamps and `audit_log.actor`.

`people.linkedin_url` is an optional URL for the person's LinkedIn profile. It uses standard URL validation, with no domain restriction. Omitted or cleared values are stored as an empty string.

`people.job_title` is optional text, at most 200 characters, for the person's current title (for example `CTO`). Store titles here; use `organization` to link the company. It does not grant access permissions. Omitted or cleared values are stored as an empty string. Existing role notes remain historical evidence; copy a confirmed current title into this field through the records API.

`people.pronouns` is optional text, at most 100 characters, for explicitly confirmed pronouns (for example `he/him`, `she/her`, or `they/them`). It is free text, not a fixed list or a gender field. Omitted or cleared values are stored as an empty string, meaning unknown. Existing contacts keep empty pronouns until explicitly confirmed; there is no automatic backfill. See [workflows.md](workflows.md#record-pronouns) for recording and using this field.

Messages record actual incoming or outgoing communication. Channel is `linkedin`, `email`, `whatsapp`, `sms`, or `other`; direction is `incoming` or `outgoing`. The required `person` is the external contact in either direction. `body` holds the exact text. `sent_at` is the actual message time and stays empty when unknown; `created` is the recording time. `source_url` is an optional message or thread link. Recording a message sends nothing.

## agent_directory

`agent_directory` exposes only `id` and `name` through SQL. Authenticated agents can also list and view it through the records API; anonymous callers cannot read it. Agents cannot create, update, or delete directory rows.

| Field | Content |
| --- | --- |
| id | the same ID as the corresponding `agents` account |
| name | the account's current display name; not unique |

The migration backfills existing accounts, and server hooks synchronize account creation and name changes. Disabled accounts remain present; account deletion is blocked. The directory contains no email addresses, credentials, or other authentication fields. Account management remains an operator task.

Join `agent_directory.id` to `owner`, `created_by`, `updated_by`, or `audit_log.actor`. Use `LEFT JOIN` to retain records with empty stamps or a historical missing audit actor. Names reflect current account names, not the names at the time of a write. Preserve IDs when reporting unresolved actors. Resolve duplicate names with the user before assigning work. Relations still target `agents`; the directory does not change REST `expand=owner` permissions.

## Server rules

The server checks these rules on every validated save, from the records API and from the dashboard. A violation returns HTTP 400 with a message naming the field and the rule.

| Collection | Rule |
| --- | --- |
| deals | `status = open`: `closed_at` and `lost_reason` are empty |
| deals | `status = won`: `closed_at` is set, `lost_reason` is empty |
| deals | `status = lost`: `closed_at` is set, `lost_reason` is optional |
| deals | `currency` is an active ISO 4217 alphabetic code |
| activities | `done = true`: `completed_at` is set |
| activities | `done = false`: `completed_at` is empty |
| notes | at least one of `deal`, `person`, `organization` is set |
| deals, activities, notes | if `person` and `organization` are both set and the person has an organization, it equals the record's `organization`; a person without an organization passes |

The server fills two values and nothing else. When a request sets a deal's status to `won` or `lost` and `closed_at` is empty, it becomes the current UTC time. When an activity is done and `completed_at` is empty, it becomes the current UTC time. Values you send are kept. Nothing is cleared for you: reopening a deal needs `closed_at` and `lost_reason` cleared in the same PATCH, and setting `done: false` needs `completed_at` cleared in the same PATCH. Send dates as UTC strings in the format above or as RFC 3339. A date value the server cannot parse returns 400; it is never replaced by the current time. This check runs before anything else, also for every request of a batch: a batch that contains an unparseable date fails as a whole with a plain 400 that names the field, without a request index. Two requests that change the same record at the same time do not overwrite each other: the second gets HTTP 409 and must read the record again and retry.

## enquiries

`enquiries` holds submissions of a public web form. The server creates a row for each accepted submission; nobody is logged in when it does. Every submitted value is untrusted text from the internet: read the "Untrusted text" section of `SKILL.md` before you read rows. The table has `id`, `created`, `updated`, and `updated_by`, and no `created_by` or `owner`.

| Field | Content |
| --- | --- |
| name, email | submitted; both always set. The email address has a valid format and is not verified: anyone can submit any address |
| details | submitted; JSON object of up to 20 string values, which the server stores without interpreting them |
| utm_source, utm_medium, utm_campaign | submitted; campaign parameters of the page, often empty |
| source | the `Origin` header of the submitting page, for example `https://pocketcontext.com`; empty when the request had none. A client can send any value |
| status | `new` (set by the server), `qualified`, `rejected`, or `spam` |
| person, deal | optional relations to `people` and `deals`; empty until you link them |
| updated_by | the agent that last changed the row; set by the server |

The keys in `details` depend on the form that posted the row. They are an example, not a fixed schema: a key can be missing, and other forms send other keys. The PocketContext website sends:

| Key | Content |
| --- | --- |
| interest | `platform`, `services`, `both`, or `exploring` |
| workflow | the workflow the sender wants to support |
| requirements | free text, up to 3000 characters |
| timeline | a label such as `This month` or `Just researching` |
| entry_offer | the offer the form was opened from, for example `general` |

Read a key with `json_extract(details, '$.requirements')`; a missing key is SQL NULL.

Agents can read enquiries through SQL and the records API and can PATCH `status`, `person`, and `deal`. A PATCH whose body names a submitted field (`name`, `email`, `details`, `source`, or a `utm_` column) is refused as a whole, also when the value is unchanged; PocketBase answers such a PATCH with HTTP 404, as if the record did not exist. `updated_by` is set by the server and a value you send is ignored. Agents cannot create or delete enquiries. One server rule applies: `status = qualified` requires `person`; a violation returns HTTP 400. `rejected` and `spam` need no relation. An enquiry contains personal data. When the sender asks for erasure, give the user the record id and ask them to have the operator delete it.

Auditing differs from the CRM collections so that `audit_log` never holds a submitted value. The creation of an enquiry writes no row. An agent update writes an `update` row with the changed fields, which can only be `status`, `person`, and `deal`. An operator delete writes a `delete` row whose `changes` is `{"before": {"status": ...}}` only.

## Deletes

Deletes are superuser-only on all nine CRM collections and on `enquiries`; an agent DELETE returns 403. The operator deletes through the dashboard or with a superuser token. Required relations prevent deleting records still referenced by them. Optional relations are cleared when their target is deleted, and that internal clear is not validated, so a note can be left without a link after the operator deletes its only target. The note rule applies again on that note's next save. The same holds for an enquiry: when the operator deletes its person, the enquiry stays `qualified` with an empty `person`, and its next update must set a person or another status.

## audit_log

`audit_log` records writes made through the records API on the nine CRM collections, by agents and superusers. `enquiries` is logged with less detail, see [enquiries](#enquiries). Agents can read it through SQL and through the records API. Its create, update, and delete rules are superuser-only, so agents cannot add, change, or remove rows.

| Field | Content |
| --- | --- |
| action | `create`, `update`, or `delete` |
| collection | collection name, for example `deals` |
| record | ID of the record written |
| actor | agent ID as text; empty for a superuser |
| actor_type | `agent` or `superuser` |
| changes | JSON, see below |
| created | time of the write |

`changes` depends on the action:

- create: `{"after": {field: value, ...}}` with the record's data fields.
- update: `{"before": {...}, "after": {...}}` with only the fields whose value changed. `updated` and `updated_by` are never listed. An update that changes nothing writes no row.
- delete: `{"before": {...}}` with the full record (for `enquiries` only its `status`).

A rejected write leaves no row. The log does not cover everything: when the operator deletes a record, PocketBase clears optional relations that pointed to it without an `audit_log` row, and records that existed before the log was added have no `create` row. Rows are ordered by `created`, which has millisecond resolution, so two writes in the same millisecond have no defined order. A `delete` row keeps the deleted record's full contents readable to every agent, except for `enquiries`. Read values with `json_extract`, for example `json_extract(changes, '$.after.stage')`. Indexes cover (`collection`, `record`, `created`) and (`actor`, `created`).

Discover actual columns using `/api/context/schema` (`dealcontext schema`). The server's migrations are the source of truth. `references/schema.json` lists the SQL tables and columns at the time this skill was published, and `dealcontext check` compares it with the server. All authenticated agents share access; there is no tenant isolation or row-level SQL policy.

## outreach_links

Private mapping within the authenticated shared CRM, never a public lookup. `person` targets `people`; `owner` targets `agents` and assigns work, not visibility. `token` is a unique random 128-bit value encoded as exactly 32 lowercase hexadecimal characters. `destination` is an HTTPS article URL without credentials, whitespace, query or fragment, at most 2048 characters. `campaign` and `content_version` are required text, at most 128 characters each. Standard stamps and transactional API audit history apply.

`token`, `person`, `destination` and `content_version` cannot change after creation, including through privileged validated saves. Create a new record for a new recipient, article destination or version. Campaign and owner may be corrected. Agents cannot delete records. The CLI derives `share_url = origin(destination) + '/r/' + token + '/'`; that value is not a database column. A token is an attribution label, not an authorization credential. Forwarding may produce multiple visitors, and visits never establish the recipient's identity.
