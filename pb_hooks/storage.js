// Primary file storage is independent from Litestream's database replica.
function configure(app, frozen) {
  const read = name => String($os.getenv("DEALCONTEXT_S3_" + name) || "").trim();
  const required = ["BUCKET", "ENDPOINT", "REGION", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY"];
  const configured = required.some(name => read(name)) || !!read("FORCE_PATH_STYLE");
  const current = app.settings();
  if (!configured) {
    if (current.s3.enabled) throw new Error("Primary object storage requires explicit configuration");
    return;
  }
  if (required.some(name => !read(name))) throw new Error("Incomplete primary object storage configuration");
  const style = read("FORCE_PATH_STYLE") || "true";
  if (style !== "true" && style !== "false") throw new Error("Invalid primary object storage path style");
  // These values are available during container bootstrap, before the serving
  // child drops replica credentials. Provision separate bucket-scoped keys.
  const replicaBucket = String($os.getenv("LITESTREAM_BUCKET") || "").trim();
  const replicaKey = String($os.getenv("LITESTREAM_ACCESS_KEY_ID") || "").trim();
  if ((replicaBucket && replicaBucket === read("BUCKET")) ||
      (replicaKey && replicaKey === read("ACCESS_KEY_ID")))
    throw new Error("Primary files and database replicas require separate buckets and credentials");
  const desired = {enabled: true, bucket: read("BUCKET"), endpoint: read("ENDPOINT"),
    region: read("REGION"), accessKey: read("ACCESS_KEY_ID"), secret: read("SECRET_ACCESS_KEY"),
    forcePathStyle: style === "true"};
  if (!Object.keys(desired).some(key => current.s3[key] !== desired[key])) return;
  if (frozen) throw new Error("Frozen startup requires unchanged primary object storage configuration");
  try {
    Object.assign(current.s3, desired);
    app.save(current);
  } catch (_) {
    // Validation errors may contain credentials; never include the original error.
    throw new Error("Could not apply primary object storage configuration");
  }
}
module.exports = {configure};
