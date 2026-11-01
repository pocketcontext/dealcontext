function isUser(record) {
  return record && record.collection().name === "users";
}

function request(e) {
  // PocketBase treats an invalid bearer as anonymous; make revoked sessions an
  // explicit failure rather than returning anonymous filtered collection lists.
  if (!e.auth && e.request.header.get("Authorization")) {
    throw new UnauthorizedError("The DealContext session is invalid or expired.");
  }
  if (isUser(e.auth)) {
    // Re-read so this guard also applies when an earlier handler supplied auth.
    const current = e.app.findRecordById("users", e.auth.id);
    if (current.getBool("disabled") || current.tokenKey() !== e.auth.tokenKey()) {
      throw new ForbiddenError("This DealContext account is disabled or its session was revoked.");
    }
  }
  return e.next();
}

function authenticate(e) {
  if (e.record.getBool("disabled")) {
    throw new ForbiddenError("This DealContext account is disabled.");
  }
  return e.next();
}

function update(e) {
  if (e.record.getBool("disabled") !== e.record.original().getBool("disabled")) {
    // Persisted in the same transaction. Re-enabling never revives old tokens.
    // PocketBase also clears matching realtime client auth when this key changes.
    e.record.refreshTokenKey();
  }
  return e.next();
}

function remove() {
  throw new ForbiddenError("DealContext accounts cannot be deleted; disable the account to preserve assignments and history.");
}

function realtime(e) {
  // Connection setup and OAuth callback delivery are deliberately anonymous.
  if (e.message.name === "PB_CONNECT" || e.message.name === "@oauth2") return e.next();
  const auth = e.client.get("auth");
  // All application record subscriptions require authentication. Recheck at
  // delivery as a message may have been queued before the account was disabled.
  if (!auth) return;
  if (isUser(auth)) {
    const current = e.app.findRecordById("users", auth.id);
    if (current.getBool("disabled") || current.tokenKey() !== auth.tokenKey()) return;
  }
  return e.next();
}

module.exports = {request, authenticate, update, remove, realtime};
