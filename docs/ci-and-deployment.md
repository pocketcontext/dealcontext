# Common validation and deployment contract

`image.yml` owns push, pull-request and manual validation. It calls `test.yml`
once per run; the test workflow has no independent push/PR trigger. Each app's
backend, authentication, client and frontend checks remain application-specific.
Both application tests and container configuration/smoke/populated recovery run
on native AMD64 and ARM64 Linux. VaultContext additionally tests its client and
memory-session behavior on macOS. Tests use isolated synthetic data only.

Publication on main requires all application and container checks to pass. Each
architecture is built on its native runner and published by digest; the manifest
uses exactly those two digests. All external actions are pinned to full commits.
VaultContext and NotifyContext retain their separate publication-enable variables.

Before promoting `latest`, the workflow queries the current main revision and
refuses stale, malformed or failed lookups. A second check runs before deployment.
The image receives full and short commit tags alongside `latest`. These checks
reject old reruns; they do not lock Git refs, and main can advance after a check.
Concurrency serializes running releases but does not guarantee commit ordering
or execution of every pending run. Do not cancel a deployment in progress.

The active deployment uses the main-only `once-v2` GitHub environment and its
existing app-specific restricted SSH key. `CONTEXT_DEPLOY_PAUSED=true` disables
deployment without disabling validation or image publication.

Deployment sends no caller-selected remote command or registry credentials.
Strict host-key checking and a dedicated forced-command key select the permitted
application. The shared `once-pocketcontext-v2` dispatcher resolves the configured
image to an immutable digest, takes the host/application deployment lock, refuses
pending recovery state, disables the old writer's restart policy, and requires a
clean stop before replacement. The maintained policy uses a 300-second grace
period and disables ONCE's independent automatic image updates. It preserves
named volumes and verifies the replacement. It does not automatically roll back.

Workflow health checks prove HTTP/database availability, not the exact deployed
source revision, data equivalence, or backup durability. The shared dispatcher
resolves `latest` when it runs; the commandless SSH contract does not carry the
workflow's digest. Record independent runtime revision evidence when releasing.
Host-local locks are not distributed fencing, and Litestream remains asynchronous.

Old `deploy/install.py`, app-local deployment wrappers and any old bootstrap
commands fail closed. They must not overwrite shared authorized keys or restart
retained source state. New deployment provisioning, storage creation and DNS
changes require a separate explicitly authorized operation. Container `init` is
only for a genuinely fresh installation, never an existing volume or replica.

After source changes, run the repository's README validation and the isolated
workflow checks:

```sh
python3 tests/deploy_workflow.py
```

Validate workflow YAML with actionlint. Actual native CI and image recovery gates
remain required before release; local validation does not replace either platform.
