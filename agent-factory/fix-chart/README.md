# Reviewed draft fix PR worker

This chart runs one incident against one declared repository and exact base
commit. `fix-fetch` downloads that source with a **read-only** GitHub App
installation token. `fix-propose` uses the selected Foundry model and a
separate coding workload identity to read files and propose at most four
complete-file edits inside declared directories. `fix-validate` applies those
edits and runs the declared checks with no Azure or GitHub token. Only the
final `fix-publish` container receives a repository-scoped Contents/Pull
requests **write** token. It checks the exact base and validation record,
creates a branch through the GitHub Git Data API and opens a draft PR. It
never executes repository code. The Job cannot merge or deploy.

The existing GitHub App key stays in `argocd`; ESO requests separate one-hour
read and write tokens for the selected repository, renewing them every 15
minutes. Reflector copies only those tokens into `foundry-publisher`. The
chart does not create a new long-lived secret. `publish.enabled` defaults
false and suppresses the write token resource until a reviewed release turns
it on. The shared coding identity and Foundry User role are GitOps-owned.

Agent-specific repository, editable directories, validation commands and
coding instructions live in the catalog. The operator supplies the incident
diagnosis, exact base ref/SHA, immutable catalog image/revision, and existing
App IDs. These are inputs to one bounded Job; different incident/repository
inputs yield different names. The model cannot select the repository, base
revision, editable directories or validation commands.

Do not claim a fix merely because the model returned JSON or a draft PR was
created. Inspect the diff, run the full repository CI against the PR, compare
the diagnosis with source evidence, and review test quality. The local check
phase is a gate for the declared checks, not proof of production recovery.
This chart currently starts from a reviewed diagnosis document; automatic
diagnosis-to-worker dispatch is a separate integration and is not implied by
the existence of the Job.
