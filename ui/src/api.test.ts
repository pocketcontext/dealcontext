import { app } from "./config";
import { describe, it, expect } from "vitest";
import {
  searchSQL,
  literal,
  ident,
  entity,
  safeParams,
  parseRoute,
} from "./api";
describe("bounded authenticated search", () => {
  it("quotes text without changing query shape", () => {
    const e = app.entities[0];
    const sql = searchSQL(e, "x' OR 1=1 --", { status: "done" }, 30);
    expect(sql).toContain("'x'' or 1=1 --'");
    expect(sql).toContain("LIMIT 31 OFFSET 30");
  });
  it("rejects identifier injection and unsafe pagination", () => {
    expect(() => ident("issues; DROP TABLE users")).toThrow();
    expect(() => searchSQL(app.entities[0], "", {}, -1)).toThrow();
    expect(literal("O'Neil")).toBe("'O''Neil'");
  });
  it("ignores unconfigured filters and never searches auth tables", () => {
    const sql = searchSQL(app.entities[0], "APP-1", { password: "secret" });
    expect(sql).not.toContain("password");
    expect(entity("users")).toBeUndefined();
  });
});

it("handles malformed routes and unsafe offsets", () => {
  location.hash = "#/issues/%ZZ?offset=Infinity";
  expect(parseRoute().id).toBe("");
  expect(parseRoute().params.has("offset")).toBe(false);
  expect(safeParams("offset=1.5").has("offset")).toBe(false);
});

it("clears rejected authorization without clearing a newer session", async () => {
  const { pb, query } = await import("./api");
  const { vi } = await import("vitest");
  const spy = vi.spyOn(pb, "send").mockRejectedValue({ status: 403 });
  pb.authStore.save("old", { id: "test", collectionName: "users", collectionId: "testcollection" });
  await expect(query("SELECT id FROM projects")).rejects.toEqual({
    status: 403,
  });
  expect(pb.authStore.token).toBe("");
  spy.mockImplementation(async () => {
    pb.authStore.save("new", { id: "next", collectionName: "users", collectionId: "testcollection" });
    throw { status: 401 };
  });
  pb.authStore.save("old", { id: "test", collectionName: "users", collectionId: "testcollection" });
  await expect(query("SELECT id FROM projects")).rejects.toEqual({
    status: 401,
  });
  expect(pb.authStore.token).toBe("new");
  spy.mockRestore();
  pb.authStore.clear();
});
