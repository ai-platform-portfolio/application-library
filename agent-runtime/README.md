# Foundry agent runtime capabilities

Reusable Python capabilities for agents hosted in Microsoft Foundry. The consumer
owns instructions, tool allowlists, diagnostic validation, reviewed knowledge,
evaluation cases and delivery composition. This package does not deploy an agent,
create infrastructure or run an AKS worker.

| Component | Responsibility |
| --- | --- |
| `context.py` | Native per-user state, explicit reviewed memory writes, bounded recall and forget; native file-search toolbox queries. |
| `knowledge.py` | Reviewed Markdown ingestion, source hashes and commit links, managed-memory definition, revision-specific toolbox publication and ordered cleanup. |
| `coding.py` | Restored archive's exact-base fetch, bounded proposal, validation binding and draft-only GitHub publication. Each phase needs an isolated execution boundary supplied by its caller. |

## Durable context

The hosted server opens `user_context(agent, project, memory_store,
retention_days=30)` inside the authenticated invocation. It requires Foundry's
hosting environment and native call/user context. It never accepts user identity,
memory scope or a delegated identity from request JSON. The state store enables
native user isolation and verifies existing isolation/TTL before use. A random,
atomically created handle maps that user's state to a managed-memory scope.

`checkpoint` / `resume` retain bounded investigation state, not arbitrary logs,
credentials or hidden reasoning. `remember` is an explicit authenticated-owner
command, outside the model's tool allowlist; it requires approved content and an
HTTPS review reference. That URL records the owner's attribution: it does not
verify an external review system. Generated diagnoses are never automatically
remembered as facts. `recall` returns at most three memories (8 KiB total), each
with a stable ID and update time. `forget` clears that user's managed-memory scope;
`forget_investigation` separately deletes a checkpoint.

Configure the memory store with `ensure_memory`, the chat deployment, embedding
deployment and retention. Profile/automatic chat-summary extraction is disabled.
Existing incompatible definitions fail rather than resetting users' memories.
Native memory is preview. State expires 30 days after its last write by default;
managed memories expire according to the store's explicit TTL. Reads do not renew
checkpoint retention. Native stores survive container restarts and AKS outages,
not deletion of their owning Foundry project/account.

## Reviewed knowledge

`publish` takes the corpus directory, agent identity, exact commit, repository and
repository-relative source path. Only top-level Markdown is uploaded: 20 documents,
32 KiB each and 128 KiB total. Symlinks, recursive datasets and other file types
are excluded. The caller must supply a reviewed knowledge directory; byte limits
and filename filtering do not establish that arbitrary text is safe/public.

Foundry files carry source paths, SHA256 hashes and immutable GitHub commit links.
A toolbox name includes the source revision and pins its vector store. Retrying a
completed publication checks provenance and sources before reusing it. The caller
retains the manifest with the release record. `knowledge_query` permits only the
native file-search operation and returns bounded output with matching source IDs.
The consumer must require citations before presenting retrieved facts as grounded.
This is historical context; it cannot establish current readiness or authorize a
coding handoff.

**File-search toolboxes are project-shared.** This corpus is reviewed public
portfolio material, not private per-user documents. User memory stays in separate
scopes. Keep evaluator rubrics, reference answers and fixtures outside both paths;
recorded evaluation invocations must bypass persistence and retrieval entirely.

`cleanup` checks toolbox ownership and discovers its attached files before deleting
the toolbox, vector store and those files, in that order. It does not delete the
shared managed-memory store or any other agent's resources. Retain a publication
until all versions using it have been retired. Interrupted publication/cleanup
still requires live acceptance; this package is not a claim of an atomic Azure
transaction.

## Coding boundaries

The restored coding module is adapted from
[sre-agent-foundry](https://github.com/sre-agent-foundry/application-library/tree/main/agent-factory).
It supplies functions, not worker infrastructure. Run fetching with a read token,
proposal generation in Foundry without GitHub write credentials, validation in an
isolated runner without credentials, and publication with a repository-scoped,
short-lived write token. The publisher never executes checkout code and creates
only a draft PR. An application must not call untrusted validation inside its
Foundry container or combine these credential boundaries.

## Build and verification

The existing repository-wide `packages.yml` discovers this package; no additional
workflow or registry is introduced. Build/release uses the existing flat
`ops-shared` capability. Consumers install a published wheel with its real SHA256
in their requirements.txt after the user merges and publishes this package.

Tests cover authenticated scope requirements, isolation/TTL mismatch rejection,
explicit review, recall/forget scope consistency, grader/size guards, curated
corpus boundaries, returned-source citations, and archived proposal/publication
contracts. They make no model calls. Foundry restart/recall, different-user
isolation, server TTL enforcement, toolbox ingestion and deletion remain required
live acceptance gates.

Primary references: [native state store](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/agent-state-store),
[managed memory](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/memory-usage),
[file search/toolboxes](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/file-search).

`hosting.invoke` composes these capabilities around the application's investigator
callback. It isolates operation schemas, keeps recalled history outside current
evidence and writes a bounded result checkpoint only after application validation.
The catalog binds this once in its Foundry protocol adapter. `deployment.py` is a
native azd predeploy hook, using the existing OIDC login via Azure Identity and
writing native azd environment values. It records the knowledge manifest in the
caller's existing acceptance directory. No extra workflow is required.
