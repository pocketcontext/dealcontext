## Packaged CLI and opt-in tracing — 4 October 2026

Deployed source `1d142c1f7bde4d813090efe589ddd2dfd1340e0e` at `https://crm.pocketcontext.com`.
Image `sha256:7d9db40f7858ddae087113f7fcc1c616524e7533fa61e0c8458858586b7678c7`; server pin `a92b0de5e1b66b6d3b6135b90092d2d6da5f7cc8` is unchanged.
The standalone `dealcontext` uv launcher pins package `1556a8f96b9af15d92d9072982941e8d95a16203`.
Old script entry points are removed; no compatibility wrappers are provided.

[Release CI](https://github.com/pocketcontext/dealcontext/actions/runs/37193925428) passed application, browser, container configuration,
smoke and populated recovery gates before publication. Copied remote launchers
passed isolated workflow and tracing tests. A predeployment backup was verified;
the update used the gated CI locked wrapper. Exact runtime revision, one writer,
existing resource settings and disabled automatic updates were verified.
Public health and anonymous SQL-schema rejection passed; the installed CLI's
live schema check passed. Eight source apps passed a live `SELECT 1` capture
with paired client/server traces and SQL text excluded. No business records
were created; diagnostic traces were uploaded to ObserveContext.

VaultContext was excluded from this migration. A separate VaultContext release
was observed during the window and was left untouched. Five other unrelated
containers retained their IDs, images and settings. The private scaffold records
the coordinated release matrix and verification evidence.

# Deployment record

## Google Workspace JIT — 24 September 2026

Implementation `2fbd667` deployed with image `ghcr.io/pocketcontext/dealcontext@sha256:da88e0e737294f78f37c77dea43aaaf6df4be123388f4610f7758347399e0309`.
[Release CI/CD](https://github.com/pocketcontext/dealcontext/actions/runs/35928779636) passed required CRM/skill/deployment/intake tests, OAuth/JIT and revocation tests, container smoke/restore, both architecture builds, publication, safe SSH deployment, and public health. Server pin remains `52c784106f047dd052de395f47af78fc5c084e42`.

A targeted environment update under the existing deploy lock enabled the separate Google client and `DEALCONTEXT_GOOGLE_WORKSPACE_DOMAIN=pocketcontext.com`. The old container stopped cleanly before replacement. All existing environment values, deployment settings and sibling container states were preserved. Live verification confirmed Google enablement, seven-day tokens, disabled-account auth rule, OAuth-only signup, health, and unchanged existing account IDs.

All eligible Workspace members can now join the shared CRM on first Google login. Existing accounts retain identity and attribution. Disable accounts instead of deleting them. Skill revision 2 adds `dc.py login --google` with SSH loopback callback and private token renewal. Google suspension does not revoke PocketBase sessions; disable access separately in each application. No synthetic production records were added. A real Google browser login is still required for human end-to-end confirmation.
