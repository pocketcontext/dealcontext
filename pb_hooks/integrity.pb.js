/// <reference path="../pb_data/types.d.ts" />
// Record rules, attribution, and the audit log. The logic is in integrity.js; handlers cannot see file-level variables.

// Invariants for every validated save (records API and dashboard). Internal relation clears skip validation.
onRecordValidate((e) => {
  e.next();
  require(`${__hooks}/integrity.js`).validate(e.app, e.record);
}, "deals", "activities", "notes", "messages", "outreach_links", "enquiries");

routerUse((e) => require(`${__hooks}/integrity.js`).dates(e));

onRecordCreateRequest((e) => require(`${__hooks}/integrity.js`).write(e),
  "organizations", "people", "pipelines", "stages", "deals", "activities", "notes", "messages", "outreach_links");
// enquiries: update and delete only. Its rows come from intake.pb.js, which writes no audit row for public input.
onRecordUpdateRequest((e) => require(`${__hooks}/integrity.js`).write(e),
  "organizations", "people", "pipelines", "stages", "deals", "activities", "notes", "messages", "outreach_links", "enquiries");
onRecordDeleteRequest((e) => require(`${__hooks}/integrity.js`).audited(e),
  "organizations", "people", "pipelines", "stages", "deals", "activities", "notes", "messages", "outreach_links", "enquiries");

onRecordCreateExecute((e) => require(`${__hooks}/integrity.js`).audit(e, "create"),
  "organizations", "people", "pipelines", "stages", "deals", "activities", "notes", "messages", "outreach_links");
onRecordUpdateExecute((e) => require(`${__hooks}/integrity.js`).audit(e, "update"),
  "organizations", "people", "pipelines", "stages", "deals", "activities", "notes", "messages", "outreach_links", "enquiries");
onRecordDeleteExecute((e) => require(`${__hooks}/integrity.js`).audit(e, "delete"),
  "organizations", "people", "pipelines", "stages", "deals", "activities", "notes", "messages", "outreach_links", "enquiries");
