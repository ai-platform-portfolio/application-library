# Agent factory

An installable Python package for Foundry hosted agents. It owns the generic
manifest contract, Invocations protocol host, OpenAI-compatible model route and
digest-pinned draft-version publisher. An agent repository supplies its own
`agents/<id>.yaml`, importable handler, evaluations and container image.

The manifest needs `schema_version: 1`, `id`, `model`, `handler`, `eval_suite`,
`instructions`, `reasoning_effort` and bounded `limits`. `kind: hosted` and
`protocol: invocations` are the current defaults; other kinds and protocols
will require a reviewed adapter rather than silently using this one.

```sh
python -m portfolio_agent_factory validate
python -m portfolio_agent_factory publish --agent agents/sre.yaml \
  --image registry.example.test/agents/catalog@sha256:<digest> \
  --revision <full-git-sha>
```

The publisher prints the proposed draft request. Add `--project-endpoint` and
`--apply` from a private-network caller with Foundry Project Manager access to
create a version. Reapplying the same revision and image reuses its draft.
The image must include the agent manifest at `agents/<id>.yaml` relative to its
working directory and set `FOUNDRY_PROJECT_ENDPOINT`; the factory serves
`/invocations` and `/readiness` with the official protocol library. The
handler is imported from the manifest and receives the request payload,
manifest, model client, deployment name and tracer.

## First hosted release

The shared `chart/` renders a short-lived Kubernetes Job that runs the same
digest-pinned agent image from an AKS namespace with a federated publisher
identity. Its only agent inputs are `agent.id`, `agent.image` and
`agent.revision`; the Job derives the manifest and smoke paths and reads its
Foundry project endpoint and Entra identity from shared ConfigMaps. A second
agent uses the same chart unchanged. The image must include
`smoke/<id>.json` with a structured, non-sensitive Invocations request.

`python -m portfolio_agent_factory release` creates or reuses the first
non-draft version, waits until Foundry reports `active`, then invokes the
stable Invocations endpoint. It refuses a different release revision for an
existing agent, because version changes need a separate candidate test and
explicit promotion workflow. The caller exports a span to an optional
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` and marks it for Langfuse. That span proves
the private caller's release and invocation; it does not capture internal
hosted-container spans. The existing `publish` command still prepares drafts
for later candidate workflows and does not route traffic.

Only HTTPS model endpoints are accepted. `MODEL_BASE_URL` and
`MODEL_TOKEN_SCOPE` can select a later authenticated gateway route; the
default uses the Foundry project and `https://ai.azure.com/.default`.
This package does not supply diagnostic permissions, incident policies, a
knowledge store, event ingestion or coding autonomy.

## Read-only diagnostic broker

`diagnostics-chart/` runs the same digest-pinned agent image as a short-lived
AKS Job with a separate Foundry User identity. An agent declares tool names;
the hosted handler returns one tool request per invocation. The broker executes
only its fixed read-only registry, passes at most 24 KiB per result back as
explicitly untrusted evidence, and stops at the manifest's request/tool limits.
The Job receives one approved namespace and an image-bundled incident file.
Its chart creates a RoleBinding only in that namespace and removes it when the
release is uninstalled. The Kubernetes token stays in AKS. Tool results are
not persisted by this Job.

The current registry reads Deployment or Argo Rollout and Pod state, including bounded container
image and HTTP readiness-probe fields while excluding environment values;
recent target events,
bounded Pod logs and fixed Mimir CPU/restart queries. It cannot run shell
commands or accept model-generated Kubernetes paths or PromQL. GitOps owns the
broker's service account, read-only RBAC and federated Foundry identity. This
is a read-only investigation path; alert intake, durable incident state,
candidate promotion and fix PRs remain separate delivery stages.

## Candidate validation

After an operator fixes the ordinary Invocations endpoint to the current
numeric version at 100%, set `release.mode: candidate` and
`release.baseVersion` to that version in the publisher chart. The Job verifies
the route before and after creating or reusing the digest-pinned candidate,
waits for it to become active, and sends its smoke input through a Foundry
session pinned to the candidate version. It does not change endpoint routing.
Do not use a draft as the isolation mechanism: subscriptions without draft
support can create a regular version instead.

The diagnostic chart requires `agent.version` as a concrete numeric version.
Its broker creates one version-pinned session and uses it for every bounded
tool turn, so validation never falls through to the ordinary endpoint. Reuse
the candidate version and immutable image digest for repeat runs. Promotion,
rollback and deletion of old versions remain separate operations and require
evaluation evidence and review.
