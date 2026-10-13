/// <reference path="../pb_data/types.d.ts" />
// Imports retain business data only in erasable source rows. Audit never copies it.
function write(e, action) {
  const originalApp = e.app;
  originalApp.runInTransaction((app) => {
    e.app = app;
    try {
      const record = e.record, collection = record.collection().name, body = e.requestInfo().body;
      const actor = e.auth && e.auth.collection().name === "agents" ? e.auth.id : "";
      if (!actor && !e.hasSuperuserAuth()) throw new ForbiddenError("Agent authentication required");
      let current = null;
      if (action !== "create") {
        current = app.findRecordById(collection, record.id);
        if (!Number.isSafeInteger(body.expected_sync_revision) || body.expected_sync_revision !== current.getInt("sync_revision")) {
          throw new ApiError(409, "Import record changed; read it again before retrying", {});
        }
        if (JSON.stringify(current) !== JSON.stringify(record.original())) {
          throw new ApiError(409, "Import record changed during this request", {});
        }
        const keys = ["source", ...(collection === "demo_sync_runs" ? ["snapshot_id", "generated_at"] : collection === "demo_contacts" ? ["subject"] : collection === "demo_suppressions" ? ["email"] : ["source_id"])];
        for (const key of keys) if (JSON.stringify(record.get(key)) !== JSON.stringify(current.get(key))) {
          throw new BadRequestError("Source identity is immutable");
        }
      }
      const source = record.getString("source");
      if (collection === "demo_sync_runs") {
        if (action === "delete") throw new ForbiddenError("Sync runs cannot be deleted");
        if (action === "create") {
          // Capture this count before fetching the export. An intervening run
          // invalidates it even when both snapshots share a timestamp.
          if (!Number.isSafeInteger(body.expected_run_count) || body.expected_run_count < 0) {
            throw new BadRequestError("expected_run_count must be a nonnegative safe integer captured before export");
          }
          const runCount = app.countRecords("demo_sync_runs", $dbx.exp("source = {:source}", {source}));
          if (body.expected_run_count !== runCount) {
            throw new ApiError(409, "Another sync started since the export baseline; fetch a fresh snapshot", {});
          }
          if (record.getInt("generated_at") > Math.floor(Date.now() / 1000) + 300) {
            throw new BadRequestError("Snapshot generation time is too far in the future");
          }
          if (record.getString("status") !== "running") throw new BadRequestError("A sync starts running");
          const latest = app.findRecordsByFilter(collection, "source = {:source} && status = 'complete'", "-generated_at", 1, 0, {source});
          if (latest.length && record.getInt("generated_at") < latest[0].getInt("generated_at")) {
            throw new ApiError(409, "Snapshot predates the latest completed import", {});
          }
          record.set("started_at", new Date().toISOString());
          record.set("completed_at", "");
        } else {
          if (current.getString("status") !== "running") throw new BadRequestError("Finished sync runs are immutable");
          if (actor !== current.getString("created_by") && !e.hasSuperuserAuth()) throw new ForbiddenError("Only the run owner can finish it");
          if (!["complete", "failed"].includes(record.getString("status"))) throw new BadRequestError("Finish the run as complete or failed");
          record.set("started_at", current.get("started_at"));
          record.set("completed_at", new Date().toISOString());
        }
      } else {
        const runId = action === "delete" ? body.sync_run : record.getString("sync_run");
        if (typeof runId !== "string" || !runId) throw new BadRequestError("An active sync_run is required");
        const run = app.findRecordById("demo_sync_runs", runId);
        if (run.getString("source") !== source || run.getString("status") !== "running") throw new BadRequestError("The sync run must be running for this source");
        if (actor !== run.getString("created_by") && !e.hasSuperuserAuth()) throw new ForbiddenError("Only the sync run owner can import records");
      }
      record.set("sync_revision", current ? current.getInt("sync_revision") + 1 : 1);
      record.set("created_by", current ? current.getString("created_by") : actor);
      record.set("updated_by", actor);
      e.next();
      const audit = new Record(app.findCollectionByNameOrId("audit_log"));
      audit.set("action", action);
      audit.set("collection", collection);
      audit.set("record", record.id);
      audit.set("actor_type", actor ? "agent" : "superuser");
      audit.set("actor", actor);
      audit.set("changes", {source, sync_revision: record.getInt("sync_revision"), sync_run: collection === "demo_sync_runs" ? record.id : (action === "delete" ? body.sync_run : record.getString("sync_run"))});
      app.save(audit);
    } finally { e.app = originalApp; }
  });
}
module.exports = {write};
