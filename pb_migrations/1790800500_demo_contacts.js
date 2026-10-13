/// <reference path="../pb_data/types.d.ts" />
migrate((app) => {
  const access = "@request.auth.id != '' && @request.auth.collectionName = 'agents'";
  const text = (name, required = false) => ({name, type: "text", required, max: 4096});
  const number = (name) => ({name, type: "number", min: 0, max: 9007199254740991, onlyInt: true});
  const bool = (name) => ({name, type: "bool"});
  const relation = (name, target) => ({name, type: "relation", collectionId: app.findCollectionByNameOrId(target).id, maxSelect: 1, cascadeDelete: false});
  const add = (name, fields, key, deletable) => {
    app.save(new Collection({name, type: "base", listRule: access, viewRule: access,
      createRule: access, updateRule: access, deleteRule: deletable ? access : null,
      fields: [
        {name: "source", type: "select", required: true, maxSelect: 1, values: ["vaultcontext-demo", "wikicontext-demo"]},
        number("sync_revision"), ...fields,
        relation("created_by", "agents"), relation("updated_by", "agents"),
        {name: "created", type: "autodate", onCreate: true, onUpdate: false},
        {name: "updated", type: "autodate", onCreate: true, onUpdate: true},
      ], indexes: ["CREATE UNIQUE INDEX idx_" + name + "_source_key ON " + name + " (source, " + key + ")", ...(name === "demo_sync_runs" ? ["CREATE UNIQUE INDEX idx_demo_sync_running ON demo_sync_runs (source) WHERE status = 'running'"] : [])],
    }));
  };
  add("demo_sync_runs", [text("snapshot_id", true), number("generated_at"),
    {name: "status", type: "select", required: true, maxSelect: 1, values: ["running", "complete", "failed"]},
    {name: "counts", type: "json", maxSize: 16384}, {name: "started_at", type: "date"}, {name: "completed_at", type: "date"}], "snapshot_id", false);
  const common = () => [relation("sync_run", "demo_sync_runs")];
  add("demo_contacts", [...common(), text("subject", true), text("email", true), bool("sales"), bool("newsletter"),
    number("revision"), number("last_used"), number("review_due"), text("terms"), text("consent"), relation("person", "people")], "subject", true);
  add("demo_consent_events", [...common(), number("source_id"), text("subject", true), number("at"), bool("sales"), bool("newsletter"),
    text("terms"), text("consent"), number("revision")], "source_id", true);
  add("demo_suppressions", [...common(), text("email", true), bool("sales"), bool("newsletter"), number("source_updated")], "email", true);
  add("demo_review_events", [...common(), number("source_id"), text("subject", true), number("at"), text("decision", true),
    text("reason"), number("revision")], "source_id", true);
}, (app) => {
  for (const name of ["demo_review_events", "demo_suppressions", "demo_consent_events", "demo_contacts", "demo_sync_runs"]) {
    app.delete(app.findCollectionByNameOrId(name));
  }
});
