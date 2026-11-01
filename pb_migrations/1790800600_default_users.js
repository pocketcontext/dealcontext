// One-way account schema conversion. CRM rows and historical attribution stay untouched.
// PocketBase runs migrations transactionally; any conflict restores the entire old schema.
migrate((app) => {
  const legacy = app.findCollectionByNameOrId('agents');
  const users = app.findCollectionByNameOrId('_pb_users_auth_');
  if (users.name !== 'users' || users.type !== 'auth' || legacy.type !== 'auth')
    throw new Error('Account conversion requires the default users auth collection.');
  if (app.findRecordsByFilter(users.id, '', '', 1, 0).length)
    throw new Error('Account conversion requires an empty default users collection; resolve existing accounts before deployment.');
  // File bytes are keyed by collection ID. Refuse unplanned custom file storage.
  for (const field of legacy.fields) {
    if (field.type() === 'file') throw new Error('Account conversion refuses legacy account file fields.');
  }
  const defaults = new Collection({type:'auth',name:'agents',id:legacy.id});
  if (JSON.stringify(Array.from(legacy.indexes).sort()) !== JSON.stringify(Array.from(defaults.indexes).sort()))
    throw new Error('Account conversion refuses unplanned legacy auth indexes.');
  const rules = ['listRule', 'viewRule', 'createRule', 'updateRule', 'deleteRule', 'authRule', 'manageRule'];
  const convert = value => value == null ? null : String(value).replace(/\bagents\b/g, 'users');
  users.fields = legacy.fields;
  for (const name of rules) users[name] = convert(legacy[name]);
  for (const name of ['passwordAuth', 'oauth2', 'otp', 'mfa', 'authAlert', 'authToken', 'passwordResetToken',
      'emailChangeToken', 'verificationToken', 'fileToken', 'verificationTemplate', 'resetPasswordTemplate', 'confirmEmailChangeTemplate'])
    users[name] = legacy[name];
  // Startup environment remains authoritative during the one-time conversion.
  const clientId = String($os.getenv('DEALCONTEXT_GOOGLE_CLIENT_ID') || '');
  const clientSecret = String($os.getenv('DEALCONTEXT_GOOGLE_CLIENT_SECRET') || '');
  if (clientId || clientSecret) {
    if (!clientId || !clientSecret || /\s/.test(clientId) || /\s/.test(clientSecret))
      throw new Error('Invalid DealContext Google OAuth configuration during account conversion.');
    const providers = JSON.parse(JSON.stringify(users.oauth2.providers || []));
    let google = providers.find(provider => provider.name === 'google');
    if (!google) { google = {name:'google'}; providers.push(google); }
    google.clientId = clientId; google.clientSecret = clientSecret;
    users.oauth2.providers = providers; users.oauth2.enabled = true;
  }
  app.save(users);
  let copied = 0;
  for (let offset = 0; ; offset += 500) {
    const records = app.findRecordsByFilter(legacy.id, '', 'id', 500, offset);
    for (const old of records) {
      const next = new Record(users);
      for (const field of legacy.fields) next.setRaw(field.name, old.getRaw(field.name));
      next.setRaw('password', old.getString('password:hash'));
      // Fresh per-record keys invalidate any previous capability even if manually replayed.
      next.set('tokenKey', $security.randomString(50));
      // The source ID still exists in the old auth collection until all relations move.
      // Bypass the cross-auth ID guard only for this transactional exact copy.
      app.unsafeWithoutHooks().saveNoValidate(next);
      copied++;
    }
    if (records.length < 500) break;
  }
  const collections = app.findAllCollections();
  for (const collection of collections) {
    if (collection.id === legacy.id || collection.id === users.id) continue;
    let changed = false;
    for (const field of collection.fields) {
      if (field.type() === 'relation' && field.collectionId === legacy.id) {
        field.collectionId = users.id;
        collection.fields.add(field);
        changed = true;
      }
    }
    for (const name of rules) {
      const value = convert(collection[name]);
      if (value !== collection[name]) { collection[name] = value; changed = true; }
    }
    // This deliberate schema conversion keeps column IDs/values and bypasses
    // the ordinary prohibition on changing a relation target.
    if (changed) app.saveNoValidate(collection);
  }
  // Preserve provider associations and remembered origins. Pending MFA/OTP flows
  // are intentionally invalidated when the legacy collection is removed.
  for (const name of ['_externalAuths', '_authOrigins']) {
    while (true) {
      const links = app.findRecordsByFilter(name, 'collectionRef = {:id}', 'id', 500, 0, {id: legacy.id});
      for (const link of links) { link.set('collectionRef', users.id); app.save(link); }
      if (links.length < 500) break;
    }
  }
  if (app.countRecords(users.id) !== copied) throw new Error('Account conversion count mismatch.');
  // All relation schema points to users before removal; no business record changes.
  app.delete(legacy);
}, () => {
  throw new Error('Account conversion rollback requires a verified complete backup restore.');
});
