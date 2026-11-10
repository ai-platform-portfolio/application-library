# Kubernetes readiness event intake

Install this chart once per agent/workload in `cluster-onboarding`, where the
existing `platform` Argo Events bus runs. The caller supplies an agent ID,
digest-pinned catalog image, full source revision, concrete active Foundry
version, and the selected Kubernetes namespace/workload. Agent-specific values
belong with the agent catalog; this chart owns the EventSource, Sensor, scoped
RBAC, fixed incident input and short-lived broker Job.

For persistent reconciliation, an operator renders only
`templates/argocd/application.yaml` with `registration.enabled=true`, a pinned
library Git URL/commit, and the same release inputs, then applies that Argo
Application once. Its child values set `registration.enabled=false`; child
reconciliation cannot register itself. Removing the Application prunes the
alert resources. Repository/PR-derived target namespace and immutable image
must come from the release inputs, not from a copied chart manifest.

The EventSource watches Event additions in the selected namespace. The Sensor
accepts Warning/Unhealthy readiness-probe Events for Pods whose names start
with the selected workload. Repeated updates to an existing Event are ignored
so they cannot consume the two trigger attempts per minute. Startup can replay
existing Events. The Event UID becomes the Job name suffix, so a replay cannot
create a second investigation while that Job exists. Jobs expire after 24 hours.

Only the UID enters the Job metadata. Event text is never passed to the model
as instructions. The broker reads current state with the existing narrow
Kubernetes identity and Foundry User identity. This is a read-only diagnostic
path. A readiness Event may be stale by the time the Job starts; the agent
must report current evidence and abstain when it cannot confirm an incident.

This intake uses native Kubernetes Events and the existing Argo Events bus, so
it adds no webhook credential, external route or network setting. It does not
consume Mimir Alertmanager alerts. Each additional alert producer needs its
own authenticated source and mapping; this chart is the first concrete event
source for the reusable diagnostic runtime.

## Optional coding handoff

Set `handoff.enabled=true` for a reviewed alert registration. Overlay the same
agent policy used by `fix-chart` as top-level `policy` values, and supply the
target's source branch as `policy.base_ref`. Supply the existing GitHub App
ID/installation ID as top-level `github` values. `handoff.requiredEvidence`
lists the diagnostic tools that must be cited before coding. The chart
defaults to no handoff and no publication; `handoff.publish=true` explicitly
permits a **draft** PR after validation succeeds.
`handoff.requireDegradedWorkload=true` adds a deterministic readiness gate:
desired replicas must exceed ready replicas and a recent Unhealthy readiness
Event must be present. The SRE catalog enables this gate.

The diagnostic Pod retains its read-only identity. Its termination message
contains only a decision, trace ID and SHA-256 of its final bounded JSON log.
A second Argo Events Sensor watches successful diagnostic Pods. Its separate
handoff Job checks Pod ownership, broker ServiceAccount, pinned image/revision,
target, cited evidence and the log checksum. Abstention stops before source
lookup or creation of fix resources. For an actionable result, a repository-
scoped read token resolves the declared branch to a full Git SHA. The handoff
renders the existing `fix-chart` and creates its temporary scoped token and
worker resources. Model output cannot choose the repository, branch, edit
scope, validation commands or publication mode.

The handoff watches the worker Job, removes temporary token resources and
input ConfigMap after termination, and leaves worker Pod logs for the chart's
one-hour Job TTL. Temporary token resources are also owned by the handoff
Job in `argocd`; Kubernetes garbage collection removes them if the dispatcher
dies before cleanup. The worker owns its input ConfigMap, so its TTL also
cleans that resource after an interrupted handoff. Live fault-injection
acceptance is still required. A draft PR
requires human diff and CI review. This path does not establish network egress
isolation for validation containers.

Before enabling on a workload, render with the actual namespace, release
image/revision/version and run a server dry-run. Then inject a controlled
readiness failure in an isolated preview, confirm one broker Job per Event UID,
inspect its trace and diagnosis, and verify a healthy workload does not trigger.
Rendering and dry-run alone do not prove live delivery or diagnosis quality.

Argo's [resource EventSource](https://argoproj.github.io/argo-events/eventsources/setup/resource/),
[Sensor data filters](https://argoproj.github.io/argo-events/sensors/filters/data/)
and [trigger parameterization](https://argoproj.github.io/argo-events/tutorials/02-parameterization/)
define the native event envelope, filtering and UID-to-Job mapping.
