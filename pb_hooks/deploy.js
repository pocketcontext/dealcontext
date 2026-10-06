/// <reference path="../pb_data/types.d.ts" />
// Helpers for deploy.pb.js. Every hook handler runs in its own runtime, so handlers load this with require().

// First match wins among prefix labels, so "/api/context/" and "/api/intake/" stay before "/api/". "/up" matches no rule.
const RULES = [
  {label: "*:auth", audience: "", duration: 60, maxRequests: 10},
  {label: "/api/batch", audience: "", duration: 10, maxRequests: 10},
  {label: "/api/context/", audience: "", duration: 10, maxRequests: 60},
  {label: "/api/intake/", audience: "", duration: 60, maxRequests: 5},
  {label: "/api/", audience: "", duration: 10, maxRequests: 300},
];

// An empty value counts as unset.
function env(name) {
  return String($os.getenv(name) || "").trim();
}

// GET /up: success only when the database answers a query.
function up(e) {
  try {
    const row = new DynamicModel({n: 0});
    e.app.db().newQuery("SELECT count(*) AS n FROM _collections").one(row);
  } catch (error) {
    throw new ApiError(503, "The database is not available.", {});
  }
  return e.string(200, "OK");
}

// Copies the environment contract into the settings. Every group is saved on its own, and only when a value
// differs from the stored one. A group with no variables set changes nothing. Log lines carry no secret values.
function settings(app) {
  require(`${__hooks}/storage.js`).configure(app, false);
  const group = (name, detail, change) => {
    const current = app.settings(), changed = [];
    const set = (section, key, value) => {
      if (JSON.stringify(current[section][key]) === JSON.stringify(value)) return;
      current[section][key] = value;
      changed.push(section + "." + key);
    };
    try {
      change(set, current);
      if (changed.length === 0) return;
      app.save(current);
      console.log("deploy: applied " + name + " from the environment (" + detail + "); changed: " + changed.join(", "));
    } catch (error) {
      console.log("deploy: could not apply " + name + " from the environment; stored settings kept: " + error);
      app.reloadSettings();
    }
  };

  // Separate groups: a sender that PocketBase rejects must not keep the application URL from being applied.
  const url = env("BASE_URL"), sender = env("MAILER_FROM_ADDRESS");
  if (url) {
    group("application URL", "BASE_URL " + url, (set) => set("meta", "appURL", url.replace(/\/+$/, "")));
  }
  if (sender) {
    // ONCE passes the value as given; the Colors package sends "Name <address>", PocketBase stores the two apart.
    const named = /^\s*"?([^"<]*?)"?\s*<([^<>\s]+)>\s*$/.exec(sender);
    group("sender", "MAILER_FROM_ADDRESS set", (set) => {
      set("meta", "senderAddress", named ? named[2] : sender.trim());
      if (named && named[1]) set("meta", "senderName", named[1]);
    });
  }

  const host = env("SMTP_ADDRESS");
  if (host) {
    const port = env("SMTP_PORT") ? parseInt(env("SMTP_PORT"), 10) : 587;
    const username = env("SMTP_USERNAME"), password = env("SMTP_PASSWORD");
    const detail = "host " + host + ", port " + port + ", username " + (username ? "set" : "empty") + ", password " + (password ? "set" : "empty");
    group("SMTP settings", detail, (set) => {
      if (!(port > 0 && port < 65536)) throw new Error("SMTP_PORT is not a port number");
      set("smtp", "enabled", true);
      set("smtp", "host", host);
      set("smtp", "port", port);
      set("smtp", "username", username);
      set("smtp", "password", password);
      set("smtp", "tls", port === 465); // implicit TLS on 465; STARTTLS when the server offers it on other ports
    });
  }

  const header = env("DEALCONTEXT_TRUSTED_PROXY_HEADER");
  if (header) {
    group("trusted proxy header", header, (set) => {
      set("trustedProxy", "headers", [header]);
      set("trustedProxy", "useLeftmostIP", false);
    });
  }

  const limits = env("DEALCONTEXT_RATE_LIMITS");
  if (limits === "true") {
    group("rate limits", "enabled, " + RULES.length + " rules", (set) => {
      set("rateLimits", "rules", RULES);
      set("rateLimits", "enabled", true);
    });
  } else if (limits === "false") {
    group("rate limits", "disabled", (set) => set("rateLimits", "enabled", false));
  } else if (limits) {
    console.log("deploy: DEALCONTEXT_RATE_LIMITS must be true or false; rate limit settings left alone");
  }
}

function googleOAuth(app) {
  const clientId = String($os.getenv("DEALCONTEXT_GOOGLE_CLIENT_ID") || "");
  const clientSecret = String($os.getenv("DEALCONTEXT_GOOGLE_CLIENT_SECRET") || "");
  if (!clientId && !clientSecret) return;
  if (!clientId.trim() || !clientSecret.trim()) {
    throw new Error("DEALCONTEXT_GOOGLE_CLIENT_ID and DEALCONTEXT_GOOGLE_CLIENT_SECRET must be set together");
  }
  if (/\s/.test(clientId) || /\s/.test(clientSecret)) {
    throw new Error("DealContext Google OAuth credentials must not contain whitespace");
  }
  // A fresh database receives its custom agents collection in app migrations.
  // The OAuth migration applies these same settings during that first pass.
  try { app.findCollectionByNameOrId("agents"); } catch (_) { return; }
  try {
    // Do not run app migrations here: maintenance commands control their own passes.
    const agents = app.findCollectionByNameOrId("agents");
    // Clone the native provider slice before changing it; retain all other providers,
    // custom Google options, field mappings, password settings, and access rules.
    const providers = JSON.parse(JSON.stringify(agents.oauth2.providers || []));
    let google = providers.find(provider => provider.name === "google");
    if (google && agents.oauth2.enabled && google.clientId === clientId && google.clientSecret === clientSecret) return;
    if (!google) {
      google = {name: "google"};
      providers.push(google);
    }
    google.clientId = clientId;
    google.clientSecret = clientSecret;
    agents.oauth2.providers = providers;
    agents.oauth2.enabled = true;
    app.save(agents);
    console.log("deploy: applied Google OAuth from the environment");
  } catch (_) {
    // Collection validation errors can include provider values. Never expose them.
    throw new Error("Could not apply DealContext Google OAuth configuration; server startup stopped");
  }
}

module.exports = {up, settings, googleOAuth, RULES};
