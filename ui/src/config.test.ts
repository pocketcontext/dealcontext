import { it, expect } from "vitest";
import { app } from "./config";
import schema from "./schema.snapshot.json";
it("every browser field and relation target belongs to the exported SQL schema", () => {
  for (const e of app.entities) {
    const table = schema.tables.find((t) => t.name === e.table);
    expect(table, e.table).toBeDefined();
    const names = table!.columns.map((c) => c.name);
    for (const f of [
      ...e.title,
      ...e.search,
      ...(e.subtitle || []),
      ...(e.markdown || []),
      ...Object.keys(e.filters || {}),
      ...Object.keys(e.relations || {}),
    ])
      expect(names, `${e.table}.${f}`).toContain(f);
    for (const target of Object.values(e.relations || {}))
      expect(
        app.entities.some((t) => t.table === target),
        target,
      ).toBe(true);
    expect(["users", "agents", "_superusers"]).not.toContain(e.table);
  }
});
