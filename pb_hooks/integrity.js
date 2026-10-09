/// <reference path="../pb_data/types.d.ts" />
// Helpers for integrity.pb.js. Every hook handler runs in its own runtime, so handlers load this with require().

// Active ISO 4217 codes (SIX list one, published 2026-09-17) without XTS, XXX, precious metals and bond market units.
const CURRENCIES = ("AED AFN ALL AMD AOA ARS AUD AWG AZN BAM BBD BDT BHD BIF BMD BND BOB BOV BRL BSD BTN BWP BYN BZD CAD " +
  "CDF CHE CHF CHW CLF CLP CNY COP COU CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL " +
  "GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW " +
  "KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD " +
  "NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP " +
  "SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD USN UYI UYU UYW UZS " +
  "VED VES VND VUV WST XAD XAF XCD XCG XDR XOF XPF XSU XUA YER ZAR ZMW ZWG").split(" ");

// Custom (non-column) record key that carries the API actor from the request hook to the execute hook.
const ACTOR_KEY = "_audit_actor";
const CRM = ["organizations", "people", "pipelines", "stages", "deals", "activities", "notes", "messages", "outreach_links"];
// Collections that hold public input: only these fields reach audit_log, so the log keeps no submitted values.
const AUDITED = {enquiries: {update: ["status", "person", "deal"], delete: ["status"]}};

// Runs after the built-in field validation, so relation ids and formats are already known to be valid.
function validate(app, record) {
  const name = record.collection().name, errors = {}, messages = [];
  const fail = (fields, code, message) => {
    messages.push(message);
    for (const field of fields) errors[field] = errors[field] || new ValidationError(code, message);
  };
  const empty = (field) => record.getString(field) === "";
  if (name === "outreach_links") {
    const destination = record.getString("destination");
    if (destination.length > 2048 || !/^https:\/\/[^\s/?#@\\]+(?:\/[^\s?#\\]*)?$/.test(destination)) {
      fail(["destination"], "validation_destination", "destination must be HTTPS without credentials, query, fragment or whitespace (maximum 2048 characters)");
    }
    if (!record.isNew()) {
      for (const field of ["token", "person", "destination", "content_version"]) {
        if (record.getString(field) !== record.original().getString(field)) {
          fail([field], "validation_immutable", field + " is immutable; create a new outreach link");
        }
      }
    }
  }
  if (name === "deals") {
    const status = record.getString("status");
    if (status === "open") {
      if (!empty("closed_at")) fail(["closed_at"], "validation_must_be_empty", "closed_at must be empty when status is open");
      if (!empty("lost_reason")) fail(["lost_reason"], "validation_must_be_empty", "lost_reason must be empty when status is open");
    } else if (empty("closed_at")) {
      fail(["closed_at"], "validation_required", "closed_at must be set when status is " + status);
    }
    if (status === "won" && !empty("lost_reason")) {
      fail(["lost_reason"], "validation_must_be_empty", "lost_reason must be empty when status is won");
    }
    if (!CURRENCIES.includes(record.getString("currency"))) {
      fail(["currency"], "validation_invalid_currency", "currency must be an active ISO 4217 code");
    }
  }
  if (name === "activities") {
    if (record.getBool("done") && empty("completed_at")) {
      fail(["completed_at"], "validation_required", "completed_at must be set when done is true");
    }
    if (!record.getBool("done") && !empty("completed_at")) {
      fail(["completed_at"], "validation_must_be_empty", "completed_at must be empty when done is false");
    }
  }
  if (name === "notes" && empty("deal") && empty("person") && empty("organization")) {
    fail(["deal", "person", "organization"], "validation_required", "at least one of deal, person, organization must be set");
  }
  if (name === "enquiries" && record.getString("status") === "qualified" && empty("person")) {
    fail(["person"], "validation_required", "person must be set when status is qualified");
  }
  if (!empty("person") && !empty("organization")) {
    const expected = app.findRecordById("people", record.getString("person")).getString("organization");
    if (expected !== "" && expected !== record.getString("organization")) {
      fail(["organization"], "validation_organization_mismatch", "organization must equal the person's organization (" + expected + ")");
    }
  }
  if (messages.length) throw new BadRequestError(messages.join("; "), errors);
}

// Router middleware. PocketBase casts an unparseable date to empty while loading the record, so by the time the record
// hooks run the sent value is gone. Check date fields on the still-raw body here, for single writes and for every
// request of a batch, and reject a bad value instead of storing empty or filling the current time.
function dates(e) {
  const method = e.request.method, path = e.request.url.path, writes = [];
  if (method === "POST" && path === "/api/batch") {
    for (const item of e.requestInfo().body.requests || []) writes.push([item.url, item.body]);
  } else if ((method === "POST" || method === "PATCH") && path.startsWith("/api/collections/")) {
    writes.push([path, e.requestInfo().body]);
  }
  for (const [url, body] of writes) {
    const match = /^\/api\/collections\/([^\/?]+)\/records/.exec(String(url || ""));
    if (!match || !body) continue;
    let collection;
    try {
      collection = e.app.findCachedCollectionByNameOrId(match[1]);
    } catch (_) {
      // Let PocketBase report unknown collections through its normal API error handling.
      continue;
    }
    if (!CRM.includes(collection.name)) continue;
    for (const field of collection.fields) {
      const value = body[field.name];
      if (field.type() !== "date" || value === undefined || value === null || String(value).trim() === "") continue;
      if (new DateTime(String(value)).isZero()) {
        const message = field.name + " is not a valid date";
        throw new BadRequestError(message, {[field.name]: new ValidationError("validation_invalid_date", message)});
      }
    }
  }
  return e.next();
}

// Create and update request hook: attribution, the two documented auto-fills, then the save in an audited transaction.
function write(e) {
  const record = e.record, name = record.collection().name, agent = agentId(e);
  if (agent) {
    if (record.collection().fields.getByName("created_by")) {
      record.set("created_by", record.isNew() ? agent : record.original().getString("created_by"));
    }
    record.set("updated_by", agent);
  }
  const now = new Date().toISOString().replace("T", " ");
  const body = e.requestInfo().body;
  const sent = (field) => Object.prototype.hasOwnProperty.call(body, field);
  if (name === "deals" && sent("status") && record.getString("status") !== "open" && record.getString("closed_at") === "") {
    record.set("closed_at", now);
  }
  if (name === "activities" && record.getBool("done") && record.getString("completed_at") === "") {
    record.set("completed_at", now);
  }
  audited(e);
}

// Runs the rest of the request in one transaction. The execute hooks see ACTOR_KEY and write the audit row with the
// same transaction app right after the record statement, so a failure on either side rolls back both and is
// reported before any response is written.
function audited(e) {
  const agent = agentId(e), app = e.app;
  e.record.set(ACTOR_KEY, agent ? "agent:" + agent : e.hasSuperuserAuth() ? "superuser:" : ":");
  app.runInTransaction((txApp) => {
    e.app = txApp;
    try {
      // The record was loaded before this transaction and the save writes the whole row, so a request that
      // queued behind another write would overwrite it and log a diff against stale values. Reject it instead.
      if (!e.record.isNew()) {
        const current = txApp.findRecordById(e.record.collection().name, e.record.id);
        if (JSON.stringify(current) !== JSON.stringify(e.record.original())) {
          throw new ApiError(409, "The record was changed by another request. Read it again and retry.", {});
        }
      }
      e.next();
    } finally {
      e.app = app;
    }
  });
}

function agentId(e) {
  return e.auth && e.auth.collection().name === "agents" ? e.auth.id : "";
}

function snapshot(record, skip, action) {
  const all = JSON.parse(JSON.stringify(record)), out = {};
  const only = (AUDITED[record.collection().name] || {})[action];
  for (const field of record.collection().fields.fieldNames()) {
    if (!skip.includes(field) && (!only || only.includes(field))) out[field] = all[field];
  }
  return out;
}

// Execute hook: no-op unless the record comes from an audited API request.
function audit(e, action) {
  const actor = e.record.getString(ACTOR_KEY);
  if (actor === "") return e.next();
  e.record.set(ACTOR_KEY, "");
  let changes;
  if (action === "update") {
    const before = snapshot(e.record.original(), [], action), after = snapshot(e.record, [], action);
    changes = {before: {}, after: {}};
    for (const field in after) {
      if (field === "updated" || field === "updated_by" || JSON.stringify(before[field]) === JSON.stringify(after[field])) continue;
      changes.before[field] = before[field];
      changes.after[field] = after[field];
    }
    if (Object.keys(changes.after).length === 0) return e.next();
  }
  if (action === "delete") changes = {before: snapshot(e.record, [], action)};
  e.next();
  if (action === "create") changes = {after: snapshot(e.record, ["id", "created", "updated"])};
  const row = new Record(e.app.findCollectionByNameOrId("audit_log"));
  row.set("action", action);
  row.set("collection", e.record.collection().name);
  row.set("record", e.record.id);
  row.set("actor_type", actor.split(":")[0]);
  row.set("actor", actor.split(":")[1]);
  row.set("changes", changes);
  e.app.save(row);
}

module.exports = {validate, dates, write, audited, audit};
