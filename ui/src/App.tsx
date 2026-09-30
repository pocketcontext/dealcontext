import { useEffect, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { app, type Entity } from "./config";
import {
  pb,
  sessionEpoch,
  refreshSession,
  signIn,
  query,
  entity,
  label,
  href,
  ident,
  literal,
  searchSQL,
  parseRoute,
  type Row,
} from "./api";
const metadata = [
  "created",
  "updated",
  "created_by",
  "updated_by",
  "revision",
  "sha256",
  "content_hash",
];
const human = (s: string) =>
  s.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());
function RecordLink({ table, id }: { table: string; id: unknown }) {
  const e = entity(table);
  const [row, setRow] = useState<Row>();
  useEffect(() => {
    let active = true;
    setRow(undefined);
    if (e && id)
      query(
        `SELECT * FROM ${ident(table)} WHERE id=${literal(String(id))} LIMIT 1`,
      )
        .then((r) => {
          if (active) setRow(r[0]);
        })
        .catch(() => {});
    return () => {
      active = false;
    };
  }, [table, id, e]);
  return row && e ? (
    <a href={href(table, id)}>{label(e, row)}</a>
  ) : (
    <span className="muted">Unavailable</span>
  );
}
function Related({ current, id }: { current: Entity; id: string }) {
  return (
    <aside className="related">
      <h2>Related records</h2>
      {app.entities.flatMap((e) =>
        Object.entries(e.relations || {})
          .filter(([, target]) => target === current.table)
          .filter(([field]) => !["created_by", "updated_by"].includes(field))
          .map(([field]) => (
            <RelatedList key={e.table + field} e={e} field={field} id={id} />
          )),
      )}
    </aside>
  );
}
function RelatedList({
  e,
  field,
  id,
}: {
  e: Entity;
  field: string;
  id: string;
}) {
  const [rows, setRows] = useState<Row[]>([]);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState("");
  useEffect(() => {
    setOffset(0);
  }, [id]);
  useEffect(() => {
    let active = true;
    setRows([]);
    setError("");
    query(searchSQL(e, "", {}, offset, [field, id]))
      .then((r) => {
        if (active) setRows(r);
      })
      .catch(() => {
        if (active) setError("Could not load related records.");
      });
    return () => {
      active = false;
    };
  }, [e, field, id, offset]);
  return (
    <section>
      <h3>
        {e.reverseLabels?.[field] || e.label}{" "}
        {!e.reverseLabels?.[field] && (
          <small>by {e.relationLabels?.[field] || human(field)}</small>
        )}
      </h3>
      {error ? (
        <p role="alert">{error}</p>
      ) : rows.length ? (
        rows.slice(0, 30).map((r) => (
          <a
            className="related-link"
            key={String(r.id)}
            href={href(e.table, r.id)}
          >
            {label(e, r)}
          </a>
        ))
      ) : (
        <p className="muted">No visible records</p>
      )}
      {offset > 0 && (
        <button onClick={() => setOffset(offset - 30)}>Previous</button>
      )}
      {rows.length > 30 && (
        <button onClick={() => setOffset(offset + 30)}>More</button>
      )}
    </section>
  );
}
function Value({
  e,
  field,
  value,
  currency,
}: {
  e: Entity;
  field: string;
  value: unknown;
  currency?: unknown;
}) {
  if (e.relations?.[field])
    return <RecordLink table={e.relations[field]} id={value} />;
  if (
    field.endsWith("_minor") &&
    typeof value === "number" &&
    typeof currency === "string"
  ) {
    try {
      return (
        <span>
          {new Intl.NumberFormat(undefined, {
            style: "currency",
            currency,
          }).format(
            value /
              10 **
                new Intl.NumberFormat(undefined, {
                  style: "currency",
                  currency,
                }).resolvedOptions().maximumFractionDigits!,
          )}{" "}
          <small className="muted">({value} minor units)</small>
        </span>
      );
    } catch {
      /* display original below */
    }
  }
  const text =
    typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
  if (e.markdown?.includes(field))
    return (
      <div className="markdown">
        <Markdown
          remarkPlugins={[remarkGfm]}
          components={{
            img: () => null,
            a: ({ href, children }) => (
              <a href={href} rel="noopener noreferrer">
                {children}
              </a>
            ),
          }}
        >
          {text}
        </Markdown>
      </div>
    );
  if (/^https?:\/\//i.test(text))
    return (
      <a href={text} rel="noopener noreferrer">
        {text}
      </a>
    );
  return <span className="text-value">{text}</span>;
}
function Detail({ e, id }: { e: Entity; id: string }) {
  const [row, setRow] = useState<Row>();
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    let active = true;
    setRow(undefined);
    setError("");
    setCopied(false);
    query(`SELECT * FROM ${ident(e.table)} WHERE id=${literal(id)} LIMIT 1`)
      .then((r) => {
        if (active) {
          setRow(r[0]);
          if (!r[0])
            setError("This record is unavailable or you do not have access.");
        }
      })
      .catch(() => {
        if (active)
          setError("Could not load this record. Try again or sign in.");
      });
    return () => {
      active = false;
    };
  }, [e, id]);
  if (error)
    return (
      <main className="detail">
        <p role="alert">{error}</p>
      </main>
    );
  if (!row)
    return (
      <main className="detail" aria-busy="true">
        Loading record…
      </main>
    );
  return (
    <>
      <main className="detail">
        <div className="eyebrow">{e.label}</div>
        <h1>{label(e, row)}</h1>
        <button
          onClick={() =>
            navigator.clipboard
              .writeText(location.origin + "/" + href(e.table, id))
              .then(() => setCopied(true))
              .catch(() =>
                setError(
                  "Clipboard unavailable. Copy the address from your browser.",
                ),
              )
          }
        >
          {copied ? "Link copied" : "Copy record link"}
        </button>
        <p className="muted record-id">{id} · Current record</p>
        <dl>
          {Object.entries(row)
            .filter(
              ([f, v]) =>
                f !== "id" &&
                !metadata.includes(f) &&
                !e.hidden?.includes(f) &&
                v !== "" &&
                v !== null,
            )
            .map(([field, value]) => (
              <div
                className={e.markdown?.includes(field) ? "field wide" : "field"}
                key={field}
              >
                <dt>{e.relationLabels?.[field] || human(field)}</dt>
                <dd>
                  <Value
                    e={e}
                    field={field}
                    value={value}
                    currency={row.currency}
                  />
                </dd>
              </div>
            ))}
        </dl>
        <details className="metadata">
          <summary>Record metadata</summary>
          <dl>
            {Object.entries(row)
              .filter(
                ([f, v]) =>
                  metadata.includes(f) &&
                  v !== "" &&
                  v !== null &&
                  !e.hidden?.includes(f),
              )
              .map(([field, value]) => (
                <div className="field" key={field}>
                  <dt>{human(field)}</dt>
                  <dd>
                    <Value e={e} field={field} value={value} />
                  </dd>
                </div>
              ))}
          </dl>
        </details>
      </main>
      <Related current={e} id={id} />
    </>
  );
}
export default function App() {
  const [session, setSession] = useState(sessionEpoch);
  useEffect(() => pb.authStore.onChange(() => setSession(sessionEpoch())), []);
  useEffect(() => {
    void refreshSession();
    const focus = () => { void refreshSession(); };
    window.addEventListener("focus", focus);
    return () => window.removeEventListener("focus", focus);
  }, []);
  // Remount every private view, including relationship labels, on auth changes.
  // The destination remains in the URL and is restored after authentication.
  return <Reader key={session} />;
}
function Reader() {
  const [authenticated, setAuthenticated] = useState(pb.authStore.isValid);
  const [route, setRoute] = useState(parseRoute);
  const [generation, setGeneration] = useState(0);
  function refresh() {
    setRoute(parseRoute());
    setGeneration((g) => g + 1);
  }
  useEffect(() => {
    window.addEventListener("focus", refresh);
    return () => window.removeEventListener("focus", refresh);
  }, []);
  const [q, setQ] = useState(route.params.get("q") || "");
  const [rows, setRows] = useState<Row[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const e = entity(route.table)!;
  const offset = Math.max(0, Number(route.params.get("offset")) || 0);
  const filters = Object.fromEntries(
    Object.keys(e.filters || {}).map((f) => [f, route.params.get(f) || ""]),
  );
  useEffect(
    () =>
      pb.authStore.onChange(() => {
        setAuthenticated(pb.authStore.isValid);
        setRows([]);
      }),
    [],
  );
  useEffect(() => {
    const listener = () => {
      const r = parseRoute();
      setRoute(r);
      setQ(r.params.get("q") || "");
    };
    window.addEventListener("hashchange", listener);
    return () => window.removeEventListener("hashchange", listener);
  }, []);
  function navigate(table: string, id: string, params: URLSearchParams) {
    const hash = "#/" + table + (id ? "/" + id : "") + (params.size ? "?" + params.toString() : "");
    // Update controlled inputs synchronously: a delayed hashchange must not erase new typing.
    history.pushState(null, "", hash);
    const next = parseRoute();
    setRoute(next);
    setQ(next.params.get("q") || "");
  }
  useEffect(() => {
    if (q === (route.params.get("q") || "")) return;
    const timer = setTimeout(() => {
      const p = new URLSearchParams(route.params);
      q ? p.set("q", q) : p.delete("q");
      p.delete("offset");
      navigate(e.table, route.id, p);
    }, 250);
    return () => clearTimeout(timer);
  }, [q, e.table, route]);
  useEffect(() => {
    if (!authenticated) return;
    let active = true;
    setLoading(true);
    setRows([]);
    setError("");
    query(searchSQL(e, route.params.get("q") || "", filters, offset))
      .then((r) => {
        if (active) setRows(r);
      })
      .catch(() => {
        if (active) setError("Search failed. Try again or sign in.");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [authenticated, route]);
  async function login(google = false) {
    setBusy(true);
    setError("");
    try {
      await signIn(email, password, google);
      setPassword("");
    } catch {
      setError("Sign in failed. Check your account or try again.");
    } finally {
      setBusy(false);
    }
  }
  if (!authenticated)
    return (
      <div className="login">
        <div className="brand">◈ {app.name}</div>
        <h1>Your workspace, connected.</h1>
        <p>Sign in to browse records and follow their relationships.</p>
        {app.google !== false && (
          <button disabled={busy} onClick={() => login(true)}>
            Continue with Google
          </button>
        )}
        <form
          onSubmit={(ev) => {
            ev.preventDefault();
            login();
          }}
        >
          <label>
            Email
            <input
              type="email"
              autoComplete="username"
              required
              value={email}
              onChange={(ev) => setEmail(ev.target.value)}
            />
          </label>
          <label>
            Password
            <input
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(ev) => setPassword(ev.target.value)}
            />
          </label>
          <button disabled={busy} type="submit">
            Sign in
          </button>
        </form>
        {error && <p role="alert">{error}</p>}
        <p className="muted">Your destination will open after sign in.</p>
      </div>
    );
  return (
    <>
      <header>
        <button
          className="menu"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
        >
          Browse
        </button>
        <a className="brand" href={"#/" + app.entities[0].table}>
          ◈ {app.name}
        </a>
        <span className="header-note">Workspace reader</span>
        <button onClick={refresh}>Refresh</button>
        <button
          onClick={() => {
            pb.authStore.clear();
            setPassword("");
          }}
        >
          Sign out
        </button>
      </header>
      <div className="layout">
        <nav
          aria-label="Record browser"
          className={open ? "browser open" : "browser"}
        >
          <label>
            Collection
            <select
              value={e.table}
              onChange={(ev) => {
                setQ("");
                navigate(ev.target.value, "", new URLSearchParams());
              }}
            >
              {app.entities
                .filter((x) => x.menu !== false || x.table === e.table)
                .map((x) => (
                  <option key={x.table} value={x.table}>
                    {x.label}
                  </option>
                ))}
            </select>
          </label>
          <label>
            Search {e.label.toLowerCase()}
            <input
              type="search"
              placeholder={"Search " + e.label.toLowerCase() + "…"}
              value={q}
              onChange={(ev) => setQ(ev.target.value)}
            />
          </label>
          {Object.entries(e.filters || {}).map(([f, values]) => (
            <label key={f}>
              {human(f)}
              <select
                value={filters[f]}
                onChange={(ev) => {
                  const p = new URLSearchParams(route.params);
                  ev.target.value ? p.set(f, ev.target.value) : p.delete(f);
                  p.delete("offset");
                  navigate(e.table, route.id, p);
                }}
              >
                <option value="">All</option>
                {values.map((v) => (
                  <option key={v} value={v}>
                    {human(v)}
                  </option>
                ))}
              </select>
            </label>
          ))}
          <div className="results" aria-live="polite" aria-busy={loading}>
            {loading ? (
              <p>Searching…</p>
            ) : error ? (
              <p role="alert">{error}</p>
            ) : !rows.length ? (
              <p className="muted">
                {q ? "No matching records." : "No visible records."}
              </p>
            ) : (
              rows.slice(0, 30).map((r) => (
                <a
                  className={route.id === r.id ? "result selected" : "result"}
                  aria-current={route.id === r.id ? "page" : undefined}
                  key={String(r.id)}
                  href={
                    href(e.table, r.id) +
                    (route.params.size ? "?" + route.params : "")
                  }
                  onClick={() => setOpen(false)}
                >
                  <strong>{label(e, r)}</strong>
                  <small>
                    {(e.subtitle || [])
                      .map((f) => r[f])
                      .filter((v) => v !== null && v !== "")
                      .join(" · ")}
                  </small>
                </a>
              ))
            )}
          </div>
          <button
            onClick={() =>
              navigator.clipboard
                .writeText(
                  location.origin +
                    "/#/" +
                    e.table +
                    (route.params.size ? "?" + route.params : ""),
                )
                .then(() => setNotice("Search link copied."))
                .catch(() =>
                  setError("Copy the browser address to share this search."),
                )
            }
          >
            Copy search link
          </button>
          <p role="status" className="muted">
            {notice}
          </p>
          <div className="pagination">
            {offset > 0 && (
              <button
                onClick={() => {
                  const p = new URLSearchParams(route.params);
                  p.set("offset", String(offset - 30));
                  navigate(e.table, route.id, p);
                }}
              >
                Previous
              </button>
            )}
            {rows.length > 30 && (
              <button
                onClick={() => {
                  const p = new URLSearchParams(route.params);
                  p.set("offset", String(offset + 30));
                  navigate(e.table, route.id, p);
                }}
              >
                Next
              </button>
            )}
          </div>
        </nav>
        {route.id ? (
          <Detail key={e.table + route.id + generation} e={e} id={route.id} />
        ) : (
          <main className="detail empty">
            <div className="eyebrow">{e.label}</div>
            <h1>Find your next connection.</h1>
            <p>
              Choose a record to read its details, explore related records, or
              copy a permanent link for your wiki.
            </p>
          </main>
        )}
      </div>
    </>
  );
}
