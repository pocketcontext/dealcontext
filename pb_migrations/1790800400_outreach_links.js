/// <reference path="../pb_data/types.d.ts" />
migrate((app) => {
  const access = "@request.auth.id != '' && @request.auth.collectionName = 'agents'";
  const relation = (name, collection, required = false) => ({
    name, type: "relation", collectionId: app.findCollectionByNameOrId(collection).id,
    maxSelect: 1, required, cascadeDelete: false,
  });
  app.save(new Collection({type: "base", name: "outreach_links",
    listRule: access, viewRule: access, createRule: access, updateRule: access, deleteRule: null,
    fields: [
      relation("person", "people", true), relation("owner", "agents", true),
      {name: "token", type: "text", required: true, min: 32, max: 32, pattern: "^[a-f0-9]{32}$"},
      {name: "destination", type: "url", required: true},
      {name: "campaign", type: "text", required: true, max: 128},
      {name: "content_version", type: "text", required: true, max: 128},
      relation("created_by", "agents"), relation("updated_by", "agents"),
      {name: "created", type: "autodate", onCreate: true, onUpdate: false},
      {name: "updated", type: "autodate", onCreate: true, onUpdate: true},
    ],
    indexes: [
      "CREATE UNIQUE INDEX idx_outreach_links_token ON outreach_links (token)",
      "CREATE INDEX idx_outreach_links_person_created ON outreach_links (person, created)",
    ],
  }));
}, (app) => {
  app.delete(app.findCollectionByNameOrId("outreach_links"));
});
