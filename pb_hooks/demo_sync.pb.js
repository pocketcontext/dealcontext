/// <reference path="../pb_data/types.d.ts" />
onRecordCreateRequest((e) => require(`${__hooks}/demo_sync.js`).write(e, "create"),
  "demo_contacts", "demo_consent_events", "demo_suppressions", "demo_review_events", "demo_sync_runs");
onRecordUpdateRequest((e) => require(`${__hooks}/demo_sync.js`).write(e, "update"),
  "demo_contacts", "demo_consent_events", "demo_suppressions", "demo_review_events", "demo_sync_runs");
onRecordDeleteRequest((e) => require(`${__hooks}/demo_sync.js`).write(e, "delete"),
  "demo_contacts", "demo_consent_events", "demo_suppressions", "demo_review_events", "demo_sync_runs");
