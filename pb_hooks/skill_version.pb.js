/// <reference path="../pb_data/types.d.ts" />

// Bump this advisory revision when an installed skill should be updated for
// workflow, permission, schema, or client changes; unrelated deploys do not bump it.
routerAdd("GET", "/api/dealcontext/skill-version", (e) => {
  e.response.header().set("Cache-Control", "no-store");
  return e.json(200, {recommendedRevision: 3});
}, $apis.requireAuth("agents"));
