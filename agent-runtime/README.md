# Foundry agent runtime capabilities

Reusable Python capabilities for agents hosted in Microsoft Foundry. The consumer
owns instructions, tool allowlists, diagnostic validation, reviewed knowledge,
evaluation cases and delivery composition. This package does not deploy an agent,
create infrastructure or run an AKS worker.

| Component | Responsibility |
| --- | --- |
| `agent.py` | Shared model injection and bounded model/tool loop; the consumer supplies instructions and tools. |
| `evaluation.py` | Dataset/reference separation and six contract checks; no model or state operations. |
| `context.py` | Native per-user state, explicit reviewed memory writes, bounded recall and forget; native file-search toolbox queries. |
| `knowledge.py` | Reviewed Markdown ingestion, source hashes and commit links, managed-memory definition, revision-specific toolbox publication and ordered cleanup. |
| `intake.py` | Native per-caller Event UID deduplication, retry leases and a two-per-minute bound; fixed incident text. |
| `hosting.py` | Composes explicit memory commands and the investigator without mixing history with current evidence. |
| `handoff.py` | CLI bindings for isolated CI fetch, validation and draft publication. |
| `deployment.py` | Native azd hook for reviewed knowledge, memory definition and explicit optional diagnostic inputs. |
| `coding.py` | Exact-base fetch, bounded proposal, validation binding and draft-only GitHub publication. The caller supplies execution and credential boundaries. |

## Durable context

The hosted server opens `user_context(agent, project, memory_store,
retention_days=30)` inside the authenticated invocation. It requires Foundry's
hosting environment and native call/user context. It never accepts user identity,
memory scope or a delegated identity from request JSON. The state store enables
native user isolation and verifies existing isolation/TTL before use. A stable hash of the agent name and Foundry-authenticated user identity supplies
the managed-memory scope without another mapping resource. It stays stable across
agent versions; callers cannot supply another scope.

`checkpoint` / `resume` retain bounded investigation state, not arbitrary logs,
credentials or hidden reasoning. `remember` is an explicit authenticated-owner
command, outside the model's tool allowlist; it requires approved content and an
HTTPS review reference. That URL records the owner's attribution: it does not
verify an external review system. Generated diagnoses are never automatically
remembered as facts. Approved free-form notes are direct `user_profile` memory
items, retaining their content and review attribution. Procedural items require
structured procedure content and are not used for these notes. `recall` returns
at most three memories (8 KiB total), each with a stable ID and update time. `forget` clears that user's managed-memory scope;
`forget_investigation` separately deletes a checkpoint.

Configure the memory store with `ensure_memory`, the chat deployment, embedding
deployment and retention. Profile/automatic chat-summary extraction is disabled.
Existing incompatible definitions fail rather than resetting users' memories.
Native memory is preview. State expires 30 days after its last write by default;
managed memories expire according to the store's explicit TTL. Reads do not renew
checkpoint retention. Native stores survive container restarts and AKS outages,
not deletion of their owning Foundry project/account.

Microsoft guidance: [durable hosted state](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/agent-state-store)
describes per-user partitioning, retention and optimistic concurrency;
[the state-store SDK](https://learn.microsoft.com/en-us/python/api/azure-ai-agentserver-core/azure.ai.agentserver.core.storage.foundrystatestore?view=azure-python)
defines item ETags and conditional `if_match` writes. [Managed memory](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/memory-usage)
documents explicit scopes, retention and remember/forget operations. Managed memory
is preview; these references describe APIs, not evidence of this consumer's live acceptance.

## Reviewed knowledge

`publish` takes the corpus directory, agent identity, exact commit, repository and
repository-relative source path. Only top-level Markdown is uploaded: 20 documents,
32 KiB each and 128 KiB total. Symlinks, recursive datasets and other file types
are excluded. The caller must supply a reviewed knowledge directory; byte limits
and filename filtering do not establish that arbitrary text is safe/public.

Foundry files carry source paths, SHA256 hashes and immutable GitHub commit links.
A toolbox name includes the source revision and pins its vector store. Retrying a
completed publication checks provenance and sources before reusing it. The caller
retains the manifest with the release record. `knowledge_query` uses the project's
OpenAI client to search that manifest's existing vector store directly. It requests
at most three results and returns at most 16 KiB, keeping only native result file
IDs present in the pinned source manifest. Text mentioning a file ID cannot create
a citation.
The consumer must require citations before presenting retrieved facts as grounded.
This is historical context; it cannot establish current readiness or authorize a
coding handoff.

**File-search toolboxes are project-shared.** This corpus is reviewed public
portfolio material, not private per-user documents. User memory stays in separate
scopes. Keep evaluator rubrics, reference answers and fixtures outside both paths;
recorded evaluation invocations must bypass persistence and retrieval entirely.

The [native search API](https://developers.openai.com/api/reference/python/resources/vector_stores/methods/search)
documents the SDK request and result file IDs.
Microsoft's [file-search/toolbox guidance](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/file-search)
documents ingestion, pinned vector stores and the lack of user isolation for
hosted-agent toolboxes. The public-only corpus rule above is this application's
response to that sharing boundary.

`cleanup` checks toolbox ownership and discovers its attached files before deleting
the toolbox, vector store and those files, in that order. It does not delete the
shared managed-memory store or any other agent's resources. Retain a publication
until all versions using it have been retired. Interrupted publication/cleanup
still requires live acceptance; this package is not a claim of an atomic Azure
transaction.

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
