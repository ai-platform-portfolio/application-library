"""Bounded, read-only AKS evidence for an agent tool broker."""

import os
import re
from urllib.parse import quote

import httpx


TOOL_DESCRIPTIONS = {
    "workload_state": "Read the target Deployment and its Pods, including rollout and restart state.",
    "recent_events": "Read recent Kubernetes events in the target namespace.",
    "bounded_logs": "Read at most 8 KiB of recent logs from one target Pod.",
    "service_metrics": "Read fixed CPU and restart metrics for the target namespace.",
}

_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_REDACT = re.compile(r"(?i)(authorization|bearer|password|api[_-]?key|token)(\s*[:=]\s*|\s+)[^\s,;]+")


def target(incident: dict, allowed_namespace: str) -> tuple[str, str]:
    namespace, workload = incident.get("namespace"), incident.get("workload")
    if namespace != allowed_namespace or not isinstance(workload, str):
        raise ValueError("Incident target must match the approved namespace")
    if not _DNS_LABEL.fullmatch(namespace) or not _DNS_LABEL.fullmatch(workload):
        raise ValueError("Incident target must use Kubernetes DNS labels")
    return namespace, workload


class KubernetesDiagnostics:
    def __init__(self, *, api_client=None, metrics_client=None):
        if api_client is None:
            host = os.environ["KUBERNETES_SERVICE_HOST"]
            port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
            token = open("/var/run/secrets/kubernetes.io/serviceaccount/token", encoding="utf-8").read()
            api_client = httpx.Client(
                base_url=f"https://{host}:{port}",
                verify="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
                headers={"Authorization": f"Bearer {token}"},
                timeout=8,
            )
        self.api = api_client
        self.metrics = metrics_client or httpx.Client(
            base_url=os.environ.get("MIMIR_QUERY_URL", "http://mimir-gateway.lgtm.svc.cluster.local/prometheus"),
            timeout=8,
        )

    def _get(self, path: str, params=None):
        response = self.api.get(path, params=params)
        response.raise_for_status()
        return response.json()

    def execute(self, name: str, incident: dict, allowed_namespace: str) -> dict:
        if name not in TOOL_DESCRIPTIONS:
            raise ValueError("Tool is not in the read-only registry")
        namespace, workload = target(incident, allowed_namespace)
        prefix = f"/api/v1/namespaces/{quote(namespace)}"

        if name == "workload_state":
            deployment = self._get(f"/apis/apps/v1/namespaces/{quote(namespace)}/deployments/{quote(workload)}")
            selector = deployment.get("spec", {}).get("selector", {}).get("matchLabels", {})
            if not selector:
                raise ValueError("Deployment has no exact pod selector")
            label_selector = ",".join(f"{key}={value}" for key, value in sorted(selector.items()))
            pods = self._get(f"{prefix}/pods", {"labelSelector": label_selector, "limit": 20})
            return {
                "deployment": {
                    "name": workload,
                    "generation": deployment.get("metadata", {}).get("generation"),
                    "observed_generation": deployment.get("status", {}).get("observedGeneration"),
                    "desired": deployment.get("spec", {}).get("replicas"),
                    "ready": deployment.get("status", {}).get("readyReplicas", 0),
                    "conditions": deployment.get("status", {}).get("conditions", [])[:8],
                    "selector": selector,
                },
                "pods": [
                    {
                        "name": pod["metadata"]["name"],
                        "phase": pod.get("status", {}).get("phase"),
                        "restarts": sum(c.get("restartCount", 0) for c in pod.get("status", {}).get("containerStatuses", [])),
                        "waiting": [c.get("state", {}).get("waiting") for c in pod.get("status", {}).get("containerStatuses", []) if c.get("state", {}).get("waiting")],
                    }
                    for pod in sorted(
                        pods.get("items", []),
                        key=lambda item: (
                            sum(c.get("restartCount", 0) for c in item.get("status", {}).get("containerStatuses", [])),
                            any(c.get("state", {}).get("waiting") for c in item.get("status", {}).get("containerStatuses", [])),
                        ),
                        reverse=True,
                    )[:20]
                ],
                "truncated": len(pods.get("items", [])) > 20 or bool(pods.get("metadata", {}).get("continue")),
            }

        if name == "recent_events":
            events = self._get(f"{prefix}/events", {"limit": 100})
            items = sorted(
                (event for event in events.get("items", [])
                 if str(event.get("involvedObject", {}).get("name", "")).startswith(workload)),
                key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "", reverse=True,
            )
            return {"events": [
                {
                    "reason": event.get("reason"),
                    "message": _REDACT.sub(r"\1\2[REDACTED]", str(event.get("message", "")))[:500],
                    "kind": event.get("involvedObject", {}).get("kind"),
                    "object": event.get("involvedObject", {}).get("name"),
                    "time": event.get("lastTimestamp") or event.get("eventTime"),
                }
                for event in items[:20]
            ], "truncated": len(items) > 20 or bool(events.get("metadata", {}).get("continue"))}

        if name == "bounded_logs":
            state = self.execute("workload_state", incident, allowed_namespace)
            pods = state["pods"]
            if not pods:
                return {"pod": None, "logs": "", "reason": "No matching pods"}
            pod = pods[0]["name"]
            response = self.api.get(f"{prefix}/pods/{quote(pod)}/log", params={"tailLines": 50, "limitBytes": 8192, "timestamps": "true"})
            response.raise_for_status()
            return {"pod": pod, "logs": _REDACT.sub(r"\1\2[REDACTED]", response.text[:8192]), "truncated": len(response.content) >= 8192}

        queries = {
            "cpu_5m": f'sum(rate(container_cpu_usage_seconds_total{{namespace="{namespace}",pod=~"{workload}-.*"}}[5m]))',
            "restarts": f'sum(kube_pod_container_status_restarts_total{{namespace="{namespace}",pod=~"{workload}-.*"}})',
        }
        results = {}
        for key, query in queries.items():
            response = self.metrics.get("/api/v1/query", params={"query": query})
            response.raise_for_status()
            series = response.json().get("data", {}).get("result", [])
            results[key] = [entry.get("value") for entry in series[:5]]
        return {"metrics": results, "scope": namespace}
