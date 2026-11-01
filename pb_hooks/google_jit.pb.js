onBootstrap((e) => {
  require(`${__hooks}/google_jit.js`).domain();
  e.next();
});

onRecordAuthWithOAuth2Request((e) => require(`${__hooks}/google_jit.js`).authenticate(e), "users");
