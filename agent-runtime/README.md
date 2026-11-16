# Foundry agent runtime capabilities

The lightweight base package supplies the OpenAI-compatible model client. Hosted
consumers install `[foundry]` as well. This source proposes runtime0.1.8; publication
and live cutover are separate gates. Consumers pin the actual released wheel/hash.

## Model connections

`model_client(credential, capability="default", timeout=45)` returns the normal
OpenAI client with zero retries and no alternate route. `coding` and `embedding`
select their own credentials/models through `CODING_` and `EMBEDDING_` environment
prefixes. They cannot borrow the default key. Endpoint/mode/authentication are common.

Direct generation retains `MODEL_CONNECTION_MODE=foundry` with Entra authentication.
Gateway mode requires `MODEL_CONNECTION_MODE=gateway`, `MODEL_AUTH_METHOD=api_key`,
`MODEL_BASE_URL`, and each selected capability's `MODEL_DEPLOYMENT_NAME` and
`MODEL_API_KEY_SECRET_URI`. Inline `MODEL_API_KEY` remains supported for existing
base-client consumers, mutually exclusive with a secret reference.

Versionless `https://<vault>.vault.azure.net/secrets/<name>` references resolve
finite enabled credentials using the application's native identity. Keys are cached
for at most five minutes and refreshed before expiry; refresh failure propagates.
Credential creation, limits and revocation belong to the shared application chart.
Backend authorization and live bypass-denial evidence remain deployment requirements.

## Durable context and transition

Authenticated invocations retain the native user-isolated `agent/users-v1` state
store, checkpoints and event deduplication. Caller JSON cannot choose the identity
or memory scope. The SDK's local disk fallback is unavailable in the hosted path.
Retention is1–30 days and existing store settings must match.

Approved notes use a bounded native JSON ledger with conditional ETag writes, a
100-active-note capacity and logical expiry per record. Updating the ledger never
extends another note's visible lifetime. Remember requires explicit approval and
review attribution. Forget and checkpoint deletion remain explicit operations;
recorded grader/fixture data cannot enter persistent context.

All new/query embeddings go through the scoped gateway capability, using256
validated dimensions. `EMBEDDING_MODEL_VERSION` pins the provider version alongside
the model alias in stored notes. A model/version change requires explicit reindexing;
existing vectors are never silently mixed. Retrieval returns at most three bounded
notes and never turns historical text into current diagnostic evidence.

If `MEMORY_STORE_NAME` names the previous native memory store, the operator must
set a fixed timezone-qualified `LEGACY_MEMORY_UNTIL` no later than30 days after
cutover. Missing deadline blocks memory operations. The transition uses scoped
list/delete only, retaining original reviewed envelopes, IDs and original expiry.
It never calls managed memory creation/search, repeats legacy plaintext into new
storage, or extends retention. Expired/unreviewed/oversized corpora are not silently
migrated. After the deadline, legacy reads stop; retire the old resources through
reviewed cleanup. Forget includes matching eligible legacy records.

## Reviewed runbooks and deployment

The predeploy hook computes a manifest from1–20 regular Markdown files, bounded
at32KiB per file/128KiB total. It records exact repository/revision/path/hash links;
no managed memory, vectors, toolbox or inference is provisioned by deployment.
The consumer bundles that same directory in its image. Runtime rejects mismatched
provenance and lazily embeds bounded chunks through its own gateway identity.
Only reviewed public text/vectors enter `agent/runbooks-v1`, a non-user-isolated
native cache with30-day TTL. Private notes remain in isolated state.

`hosting.invoke` composes note commands, runbook retrieval, investigation and
checkpoints. The application supplies `embedding_client`, `knowledge_path`, its
manifest and existing project identity. Source citations use deterministic file
IDs bound to the exact publication. Public cache generations include the source
revision, content, embedding model/version and dimensions.

The hook forwards public connection references through an owner-only temporary
native azd `--file`; it is removed on success/failure. Never upload the ignored
`.azure` directory or record keys/payloads in logs. Hosting can start before secret
access is granted: deploy/read the native principal, reconcile chart credentials,
then invoke and evaluate the exact candidate. CI needs no separate inference key.

## Coding boundaries

The coding module supplies functions, not worker infrastructure. Run fetching with a read token,
proposal generation in Foundry without GitHub write credentials, validation in a
network-disabled, non-root Docker sandbox without credentials, and publication with a repository-scoped,
short-lived write token. The publisher never executes checkout code and creates
only a draft PR. An application must not call untrusted validation inside its
Foundry container or combine these credential boundaries.

The consumer's trusted coding configuration declares its handoff evidence gate:

```yaml
required_evidence:
  source: live_http_probe
  tools: [service_status]
```

`prepare` requires that source and every declared tool, an actionable diagnosis
and a trace ID before fetching source. Each consumer declares its evidence source
and tool names in its own configuration. The model cannot choose this policy.

## Build and verification

The existing repository-wide `packages.yml` discovers this package; no additional
workflow or registry is introduced. Build/release uses the existing flat
`ops-shared` capability. Consumers install a published wheel with its real SHA256
in their requirements.txt after the user merges and publishes this package.

Tests cover authenticated scope requirements, isolation/TTL mismatch rejection,
explicit review, recall/forget scope consistency, grader/size guards, curated
corpus boundaries, returned-source citations, and proposal/publication
contracts. They make no model calls. Foundry restart/recall, different-user
isolation, server TTL enforcement, toolbox ingestion and deletion remain required
live acceptance gates.

`hosting.invoke` composes these capabilities around the application's investigator
callback. It isolates operation schemas, keeps recalled history outside current
evidence and writes a bounded result checkpoint only after application validation.
The catalog binds this once in its Foundry protocol adapter. `deployment.py` is a
native azd predeploy hook, using the existing OIDC login via Azure Identity and
writing native azd environment values. It records the knowledge manifest in the
caller's existing acceptance directory. No extra workflow is required.

Microsoft's [hosted-agent CI/CD guidance](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/set-up-ci-cd-cli)
and [azure.yaml reference](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/azure-yaml-reference)
describe native `azd` deployment and prebuilt images. The bounded investigation,
evidence gate and draft-only publication rules here are application policy, not
Microsoft-prescribed orchestration.


The handoff CLI requires the immutable validation image, mounts only source as
read-only, passes no host environment and mounts no Docker socket or artifact
paths. It applies process/memory/CPU limits and disables privilege escalation.
The trusted host writes the exact-proposal report outside the sandbox. A local
trusted unit test must explicitly choose `sandbox_image=None`; production CLI
never chooses that mode. A real Docker acceptance test checks these boundaries.
Validation drains combined stdout/stderr incrementally, retains only the last
4 KiB and fails above 1 MiB total output or 300 seconds per check. On an abort,
the host kills the CLI process group and removes the uniquely named validation
container. It does not accumulate full output in host memory or on disk.

Event state expires after 24 hours and is scoped to the authenticated integration
caller and agent. Complete UIDs reuse cached results; active claims cannot run
twice before lease expiry, expired/failed claims can retry, and optimistic
concurrency protects the rate bucket. Every completion/failure write uses the acquired claim's ETag;
a replaced worker cannot overwrite its successor or return an accepted stale result.
This is bounded at-least-once delivery, not an atomic transaction
across Foundry and GitHub. One deterministic incident branch and exact-tree/base
checks prevent retry-created competing drafts. Closed PRs are not reopened.
