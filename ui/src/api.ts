import PocketBase, { BaseAuthStore, LocalAuthStore } from "pocketbase";
import { app, type Entity } from "./config";
const key = app.name + ".reader.auth";
// Do not import an old tab's credentials after another tab has signed out.
try {
  sessionStorage.removeItem(key);
} catch {
  /* unavailable storage */
}
const store = new LocalAuthStore(key);
if (!store.isValid || store.record?.collectionName !== app.authCollection)
  store.clear();
export const pb = new PocketBase(location.origin, store);
pb.autoCancellation(false);
let epoch = 0;
export const sessionEpoch = () => epoch;
store.onChange(() => {
  epoch++;
  // Future record subscriptions must be established again for the new session.
  void pb.realtime.unsubscribe().catch(() => {});
});

// Authenticate in an isolated SDK store: late responses cannot resurrect logout
// or overwrite an account selected in another tab.
export async function signIn(email: string, password: string, google = false) {
  const started = epoch;
  const token = store.token;
  const client = new PocketBase(location.origin, new BaseAuthStore());
  try {
    const auth = client.collection(app.authCollection);
    const result = google
      ? await auth.authWithOAuth2({ provider: "google" })
      : await auth.authWithPassword(email, password);
    if (epoch !== started || store.token !== token)
      throw Error("Session changed");
    refreshedAt = Date.now();
    store.save(result.token, result.record);
  } finally {
    void client.realtime.unsubscribe().catch(() => {});
  }
}
let refreshedAt = 0;
let refreshing: Promise<void> | undefined;
export function refreshSession(): Promise<void> {
  if (!store.isValid) {
    if (store.token) store.clear();
    return Promise.resolve();
  }
  if (refreshing) return refreshing;
  if (Date.now() - refreshedAt < 300000) return Promise.resolve();
  refreshedAt = Date.now();
  const started = epoch;
  const token = store.token;
  const client = new PocketBase(location.origin, new BaseAuthStore());
  client.authStore.save(store.token, store.record);
  refreshing = client
    .collection(app.authCollection)
    .authRefresh()
    .then((result) => {
      if (epoch === started && store.token === token)
        store.save(result.token, result.record);
    })
    .catch((error: { status?: number }) => {
      if (
        epoch === started &&
        store.token === token &&
        [401, 403].includes(error.status || 0)
      )
        store.clear();
      // Network failures keep the current session; another focus may retry later.
    })
    .finally(() => {
      refreshing = undefined;
    });
  return refreshing;
}
export type Row = Record<string, unknown>;
export const ident = (s: string) => {
  if (!/^[a-z_][a-z0-9_]*$/.test(s)) throw Error("Invalid field");
  return '"' + s + '"';
};
export const literal = (s: string) => "'" + s.replaceAll("'", "''") + "'";
export async function query(sql: string): Promise<Row[]> {
  const started = epoch;
  const token = store.token;
  try {
    const r = await pb.send<{
      columns: string[];
      rows: unknown[][];
      truncated: boolean;
    }>("/api/context/query", { method: "POST", body: { sql } });
    if (started !== epoch || store.token !== token)
      throw Error("Session changed");
    if (r.truncated) throw Error("Response limit reached. Narrow your search.");
    return r.rows.map((row) =>
      Object.fromEntries(r.columns.map((c, i) => [c, row[i]])),
    );
  } catch (e) {
    if (
      [401, 403].includes((e as { status?: number }).status || 0) &&
      started === epoch &&
      token === store.token
    )
      store.clear();
    throw e;
  }
}
export const entity = (table: string) =>
  app.entities.find((e) => e.table === table);
export const label = (e: Entity, r: Row) =>
  e.title
    .map((f) => r[f])
    .filter(Boolean)
    .join(" · ")
    .slice(0, 180) || String(r.id);
export const href = (table: string, id: unknown) =>
  `#/${encodeURIComponent(table)}/${encodeURIComponent(String(id))}`;
export function searchSQL(
  e: Entity,
  q: string,
  filters: Record<string, string>,
  offset = 0,
  relation?: [string, string],
) {
  if (!Number.isSafeInteger(offset) || offset < 0)
    throw Error("Invalid offset");
  const where: string[] = [];
  if (q) {
    const needle = literal(q.toLowerCase());
    where.push(
      "(" +
        [
          ...["id", ...e.search].map(
            (f) => `instr(lower(CAST(${ident(f)} AS TEXT)),${needle})>0`,
          ),
          ...Object.entries(e.relations || {})
            .filter(([f]) => !["created_by", "updated_by"].includes(f))
            .flatMap(([field, table]) => {
              const target = entity(table);
              if (!target) return [];
              return [
                `EXISTS (SELECT 1 FROM ${ident(table)} AS related WHERE related.id=${ident(e.table)}.${ident(field)} AND (${target.title.map((f) => `instr(lower(CAST(related.${ident(f)} AS TEXT)),${needle})>0`).join(" OR ")}))`,
              ];
            }),
        ].join(" OR ") +
        ")",
    );
  }
  for (const [f, v] of Object.entries(filters))
    if (e.filters?.[f]?.includes(v)) where.push(`${ident(f)}=${literal(v)}`);
  if (relation) where.push(`${ident(relation[0])}=${literal(relation[1])}`);
  return `SELECT * FROM ${ident(e.table)}${where.length ? " WHERE " + where.join(" AND ") : ""} ORDER BY ${q ? `CASE WHEN ${["id", ...e.title].map((f) => `${ident(f)}=${literal(q)}`).join(" OR ")} THEN 0 ELSE 1 END, ` : ""}${ident(e.title.includes("name") ? "name" : e.title.includes("key") ? "key" : e.title[0])}, id LIMIT 31 OFFSET ${offset}`;
}
export function parseRoute() {
  const [path, query = ""] = location.hash.slice(1).split("?");
  const [table, id] = path
    .split("/")
    .filter(Boolean)
    .map((part) => {
      try {
        return decodeURIComponent(part);
      } catch {
        return "";
      }
    });
  return {
    table: entity(table)?.table || app.entities[0].table,
    id: id || "",
    params: safeParams(query),
  };
}

export function safeParams(query: string) {
  const p = new URLSearchParams(query);
  const n = Number(p.get("offset"));
  if (!Number.isSafeInteger(n) || n < 0 || n > 1000000) p.delete("offset");
  return p;
}
