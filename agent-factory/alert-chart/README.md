# Kubernetes readiness event intake

Install this chart once per agent/workload in `cluster-onboarding`, where the
existing `platform` Argo Events bus runs. The caller supplies an agent ID,
digest-pinned catalog image, full source revision, concrete active Foundry
version, and the selected Kubernetes namespace/workload. Agent-specific values
belong with the agent catalog; this chart owns the EventSource, Sensor, scoped
RBAC, fixed incident input and short-lived broker Job.

The EventSource watches only Events in the selected namespace. The Sensor
accepts Warning/Unhealthy readiness-probe Events for Pods whose names start
with the selected workload. The Event UID becomes the Job name suffix, so
updates or redelivery of the same Event cannot create another Job while the
first remains. Jobs expire after 24 hours; a later repeat can run again. The
trigger is limited to two attempts per minute. Duplicate `AlreadyExists`
trigger errors are expected and do not create a second investigation.

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

Before enabling on a workload, render with the actual namespace, release
image/revision/version and run a server dry-run. Then inject a controlled
readiness failure in an isolated preview, confirm one broker Job per Event UID,
inspect its trace and diagnosis, and verify a healthy workload does not trigger.
Rendering and dry-run alone do not prove live delivery or diagnosis quality.

Argo's [resource EventSource](https://argoproj.github.io/argo-events/eventsources/setup/resource/),
[Sensor data filters](https://argoproj.github.io/argo-events/sensors/filters/data/)
and [trigger parameterization](https://argoproj.github.io/argo-events/tutorials/02-parameterization/)
define the native event envelope, filtering and UID-to-Job mapping.
