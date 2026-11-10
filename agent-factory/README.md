# Agent factory

An installable Python package for Foundry hosted agents. It owns the generic
manifest contract, Invocations protocol host, OpenAI-compatible model route and
digest-pinned draft-version publisher. An agent repository supplies its own
`agents/<id>.yaml`, importable handler, evaluations and container image.

## Reviewed fix PRs

`fix-chart/` runs four short-lived phases against one exact GitHub base commit:
fetch source with a repository-scoped read token, propose a bounded change with
Foundry, validate it without cloud credentials, then publish a draft PR with a
separate repository-scoped write token. Its operator-supplied policy fixes the
repository, editable directories and checks; the model cannot expand them. The
chart defaults to validation-only mode. See [fix-chart/README.md](fix-chart/README.md)
for inputs, credential custody and acceptance limits. The SRE catalog supplies
one consumer policy; another agent can use the same worker unchanged.

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
The Job receives one approved namespace and either an image-bundled incident
file or a bounded runtime `incident.payload`. The latter creates a content-
addressed ConfigMap mounted read-only into the Job. Use a distinct Helm release
name for each incident identity so concurrent Jobs retain their own input;
upgrading one release to a different incident can remove its old ConfigMap.
The chart creates a RoleBinding
only in the approved namespace and removes the Job, ConfigMap and RoleBinding
when the release is uninstalled. The Kubernetes token stays in AKS. Tool
results are not persisted by this Job.

For a runtime incident, supply metadata only; Helm retains values in its
release Secret and the chart stores the document in a temporary ConfigMap.
The payload must contain a matching namespace, a workload DNS label and a
summary; it is capped at 8 KiB. Fetch logs, events and metrics through the
broker's read-only tools rather than putting them in the incident document.
For example:

```yaml
incident:
  namespace: preview-example-pr-12
  payload:
    namespace: preview-example-pr-12
    workload: web
    summary: Check the current preview workload health.
```

The existing `incident.path: incidents/<name>.json` remains available for
recorded cases. Runtime input removes the image-rebuild dependency for a new
incident; authenticated alert intake and automatic Job creation are separate
capabilities that are not implemented by this chart change.

The current registry reads Deployment or Argo Rollout and Pod state, including bounded container
image and HTTP readiness-probe fields while excluding environment values;
recent target events,
bounded Pod logs and fixed Mimir CPU/restart queries. It cannot run shell
commands or accept model-generated Kubernetes paths or PromQL. GitOps owns the
broker's service account, read-only RBAC and federated Foundry identity. This
is a read-only investigation path; alert intake, durable incident state,
candidate promotion and fix PRs remain separate delivery stages.

## Offline evaluation

`portfolio_agent_factory.evaluation` loads versioned case files and replays
recorded tool fixtures through a bounded model loop. The model receives only
the incident and requested fixtures; provenance, reference and rubric stay
with the grader. The reusable report records tool and request counts plus
deterministic checks. Passing these checks yields `human_review_required`,
never an automatic diagnosis pass: a reviewer must score the case's semantic
criteria and hard failures. A failed check yields `failed`.

Agent repositories own their cases and may add `rubric.automated` with
`required_tools`, `forbidden_tools` and `forbidden_answer_patterns`. These
literal regex checks catch specific observable mistakes, not all unsafe claims.
Each fixture is limited to 24 KiB, and the agent manifest sets the request and
tool-call limits. A CI caller can load a case, run it against a chosen model
client and store the resulting JSON report; live model calls are deliberately
separate from offline deterministic unit tests.

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
the candidate version and immutable image digest for repeat runs. Promotion
and rollback use separate reviewed route-change Jobs; old-version deletion is
not automated.

## Reviewed route changes

The publisher chart supports `release.mode: promote` and `rollback` after a
candidate is Active. It verifies the current endpoint is fixed at the declared
`baseVersion`, the target version is Active with the declared image digest and
source revision, and an operator-supplied evidence document matches those
inputs. Promotion requires all six standard factory automated checks to pass
and a separate reviewed HTTPS approval reference. Rollback requires that
reference and a reason. Evidence is metadata only; Helm stores it in its
release Secret and the Job mounts a short-lived ConfigMap. Use one Helm release
name per route-change identity, then uninstall it after verifying the Job.

```yaml
agent:
  id: example
  image: registry.example.test/agents/example@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  revision: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb

release:
  mode: promote
  baseVersion: "1"
  targetVersion: "2"
  evidence:
    agent: example
    base_version: "1"
    target_version: "2"
    image_digest: registry.example.test/agents/example@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    source_revision: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
    evaluation:
      status: human_review_required
      human_review: pending
      automated_checks:
        declared_tools_only: true
        within_tool_limit: true
        within_request_limit: true
        required_tools_used: true
        forbidden_tools_avoided: true
        forbidden_answer_patterns_absent: true
    review: {status: approved, url: https://github.com/example/agent/pull/2}

publisher:
  # The runner must contain this factory command and agents/example.yaml.
  image: registry.example.test/agents/example@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
```

The route Job sends a merge PATCH containing only `version_selector`, then
reads the endpoint back and checks that the Invocations protocol and
authorization settings were preserved. It uses `If-Match` when Foundry returns
an ETag. A read-only GET of the current portfolio account returned no ETag,
so Azure cannot provide an atomic compare-and-swap precondition there; the
expected-route check and readback detect drift but cannot eliminate a race
with another operator. Serialize changes through a reviewed release process.
Reapplying the same intent after a verified route change is a no-op.
The review URL and evaluation fields are operator attestations, not an
independent verification of reviewer identity or candidate-specific diagnostic
quality. The offline evaluator currently tests a model route and recorded
fixtures; it does not prove a hosted candidate passed those cases. Live route
changes require separate operator approval. The current rebuilt SRE endpoint
still needs an explicitly reviewed first pin from `@latest` before the existing
candidate and route-change paths can be used.
