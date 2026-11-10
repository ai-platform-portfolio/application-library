"""Event-driven, scoped bridge from a completed diagnostic Pod to a fix Job."""

import json
import hashlib
import os
import re
import subprocess
import tempfile
import time
from importlib.resources import files
from pathlib import Path
from urllib.parse import quote

import httpx
import yaml


SHA = re.compile(r"^[a-f0-9]{40}$")
DNS = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REF = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")


def checked_result(raw: str, marker: str, intent: dict) -> dict:
    if len(raw.encode()) > 16384 or len(marker.encode()) > 256:
        raise ValueError("Diagnostic artifact exceeds its bound")
    notice = json.loads(marker)
    if not isinstance(notice, dict) or set(notice) != {"decision", "trace_id", "sha256"} or not isinstance(notice["sha256"], str):
        raise ValueError("Diagnostic termination marker is invalid")
    lines = [line for line in raw.splitlines() if line.strip()]
    if not lines or hashlib.sha256(lines[-1].encode()).hexdigest() != notice["sha256"]:
        raise ValueError("Diagnostic log does not match its termination marker")
    document = json.loads(lines[-1])
    if not isinstance(document, dict) or not {"agent", "target", "answer", "triage", "tool_calls", "broker_trace_id"} <= set(document):
        raise ValueError("Diagnostic log has unexpected fields")
    if document["agent"] != intent["agent"]["id"] or document["target"] != intent["target"]:
        raise ValueError("Diagnostic target does not match trusted handoff intent")
    if document["broker_trace_id"] != notice["trace_id"] or not re.fullmatch(r"[a-f0-9]{32}", notice["trace_id"]):
        raise ValueError("Diagnostic trace does not match termination marker")
    result = document["triage"]
    if not isinstance(result, dict) or set(result) != {"decision", "answer", "evidence"}:
        raise ValueError("Diagnostic result has unexpected fields")
    if result["decision"] not in {"actionable", "abstain"} or result["decision"] != notice["decision"]:
        raise ValueError("Diagnostic decision is invalid")
    if not isinstance(result["answer"], str) or not 20 <= len(result["answer"]) <= 2500 or result["answer"] != document["answer"]:
        raise ValueError("Diagnostic answer is missing or too long")
    if not isinstance(result["evidence"], list) or not all(
        isinstance(item, str) and item in document["tool_calls"] for item in result["evidence"]
    ) or len(set(result["evidence"])) != len(result["evidence"]):
        raise ValueError("Diagnostic evidence list is invalid")
    return {**result, "trace_id": notice["trace_id"]}


def checked_intent(intent: dict) -> dict:
    if not isinstance(intent, dict) or set(intent) != {"agent", "target", "github", "policy", "publish", "required_evidence", "broker_service_account"}:
        raise ValueError("Handoff intent has unexpected fields")
    policy = intent["policy"]
    if not isinstance(policy, dict) or set(policy) != {
        "repository", "base_ref", "editable_paths", "checks", "instructions"
    } or not REPO.fullmatch(policy["repository"]) or not REF.fullmatch(policy["base_ref"]):
        raise ValueError("Handoff source attribution is invalid")
    agent = intent["agent"]
    if not isinstance(agent, dict) or set(agent) != {"id", "image", "revision"} or not DNS.fullmatch(agent["id"]):
        raise ValueError("Handoff agent identity is invalid")
    if not SHA.fullmatch(agent["revision"]):
        raise ValueError("Handoff agent revision is not pinned")
    target = intent["target"]
    if not isinstance(target, dict) or set(target) != {"namespace", "workload"} or not all(
        isinstance(value, str) and DNS.fullmatch(value) for value in target.values()
    ):
        raise ValueError("Handoff target is invalid")
    if not isinstance(intent["github"], dict) or set(intent["github"]) != {"appId", "installationId"}:
        raise ValueError("Handoff GitHub identity is invalid")
    if not isinstance(intent["publish"], bool):
        raise ValueError("Handoff publish flag must be boolean")
    if not isinstance(intent["broker_service_account"], str) or not DNS.fullmatch(intent["broker_service_account"]):
        raise ValueError("Handoff broker identity is invalid")
    evidence = intent["required_evidence"]
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 4 or not all(
        isinstance(tool, str) and re.fullmatch(r"[a-z_]{3,40}", tool) for tool in evidence
    ) or len(set(evidence)) != len(evidence):
        raise ValueError("Handoff evidence gate is invalid")
    return intent


def resolve_sha(repository: str, ref: str, token: str, *, client=None) -> str:
    client = client or httpx.Client(base_url="https://api.github.com", timeout=15)
    response = client.get(
        f"/repos/{quote(repository, safe='/')}/commits/{quote(ref, safe='')}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    response.raise_for_status()
    sha = response.json().get("sha")
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise ValueError("GitHub did not return a full commit SHA")
    return sha


def render_fix(intent: dict, result: dict, incident_id: str, sha: str, *, helm="helm") -> list[dict]:
    values = {
        "agent": intent["agent"], "github": intent["github"],
        "policy": {**intent["policy"], "base_sha": sha, "incident_id": incident_id},
        "diagnosis": {"answer": result["answer"], "trace_id": result["trace_id"]},
        "publish": {"enabled": intent["publish"]},
    }
    chart = files("portfolio_agent_factory").joinpath("charts/fix")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "values.yaml"
        path.write_text(yaml.safe_dump(values))
        rendered = subprocess.run(
            [helm, "template", incident_id, str(chart), "--namespace", "foundry-publisher", "--values", str(path)],
            capture_output=True, text=True, check=True, timeout=20,
            env={**os.environ, "HELM_PLUGINS": directory},
        )
    resources = [item for item in yaml.safe_load_all(rendered.stdout) if item]
    if len(resources) not in {4, 6} or {item["kind"] for item in resources} != {
        "GithubAccessToken", "ExternalSecret", "ConfigMap", "Job"
    }:
        raise ValueError("Fix chart rendered an unexpected resource set")
    return resources


class Kubernetes:
    """Small API adapter; the dispatcher SA is distinct from the broker SA."""

    def __init__(self, *, client=None):
        if client is None:
            token = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text()
            client = httpx.Client(
                base_url=f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ.get('KUBERNETES_SERVICE_PORT', '443')}",
                verify="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
                headers={"Authorization": f"Bearer {token}"}, timeout=20,
            )
        self.client = client

    @staticmethod
    def path(resource: dict) -> str:
        namespace = resource["metadata"].get("namespace", "foundry-publisher")
        kind = resource["kind"]
        group, plural = {
            "GithubAccessToken": ("/apis/generators.external-secrets.io/v1alpha1", "githubaccesstokens"),
            "ExternalSecret": ("/apis/external-secrets.io/v1", "externalsecrets"),
            "ConfigMap": ("/api/v1", "configmaps"),
            "Job": ("/apis/batch/v1", "jobs"),
        }[kind]
        return f"{group}/namespaces/{quote(namespace)}/{plural}"

    def create(self, resource: dict) -> None:
        path = self.path(resource)
        response = self.client.post(path, json=resource)
        if response.status_code == 409:
            existing = self.client.get(f"{path}/{quote(resource['metadata']['name'])}")
            existing.raise_for_status()
            old = existing.json()
            expected_owner = resource.get("metadata", {}).get("ownerReferences", [])
            if expected_owner and not all(
                any(self._contains(actual, owner) for actual in old.get("metadata", {}).get("ownerReferences", []))
                for owner in expected_owner
            ):
                raise ValueError(f"Conflicting {resource['kind']} owner already exists")
            for key in ("spec", "data"):
                if key in resource and not self._contains(old.get(key), resource[key]):
                    raise ValueError(f"Conflicting {resource['kind']} already exists")
            return
        response.raise_for_status()

    def reference(self, resource: dict) -> dict:
        path = f"{self.path(resource)}/{quote(resource['metadata']['name'])}"
        response = self.client.get(path)
        response.raise_for_status()
        uid = response.json().get("metadata", {}).get("uid")
        if not isinstance(uid, str) or not uid:
            raise ValueError("Kubernetes owner is missing its UID")
        return {"apiVersion": resource["apiVersion"], "kind": resource["kind"],
                "name": resource["metadata"]["name"], "uid": uid}

    def handoff_owner(self, name: str) -> dict:
        if not DNS.fullmatch(name):
            raise ValueError("Handoff Job name is invalid")
        return self.reference({"apiVersion": "batch/v1", "kind": "Job",
                               "metadata": {"name": name, "namespace": "argocd"}})

    @classmethod
    def _contains(cls, actual, expected) -> bool:
        if isinstance(expected, dict):
            return isinstance(actual, dict) and all(
                key in actual and cls._contains(actual[key], value) for key, value in expected.items()
            )
        if isinstance(expected, list):
            return isinstance(actual, list) and len(actual) == len(expected) and all(
                cls._contains(a, e) for a, e in zip(actual, expected)
            )
        return actual == expected

    def delete(self, resource: dict) -> None:
        path = f"{self.path(resource)}/{quote(resource['metadata']['name'])}"
        response = self.client.delete(path)
        if response.status_code != 404:
            response.raise_for_status()

    def read_log(self, pod: str, incident_id: str, intent: dict) -> str:
        if not DNS.fullmatch(pod) or not DNS.fullmatch(incident_id):
            raise ValueError("Diagnostic Pod or Job name is invalid")
        pod_response = self.client.get(f"/api/v1/namespaces/foundry-publisher/pods/{quote(pod)}")
        pod_response.raise_for_status()
        source = pod_response.json()
        job_response = self.client.get(f"/apis/batch/v1/namespaces/foundry-publisher/jobs/{quote(incident_id)}")
        job_response.raise_for_status()
        job = job_response.json()
        owners = source.get("metadata", {}).get("ownerReferences", [])
        containers = source.get("spec", {}).get("containers", [])
        if not any(owner.get("kind") == "Job" and owner.get("name") == incident_id and
                   owner.get("uid") == job.get("metadata", {}).get("uid") for owner in owners):
            raise ValueError("Diagnostic Pod is not owned by the selected Job")
        if source.get("status", {}).get("phase") != "Succeeded" or source.get("spec", {}).get("serviceAccountName") != intent["broker_service_account"]:
            raise ValueError("Diagnostic Pod state or identity is invalid")
        if len(containers) != 1 or containers[0].get("name") != "broker" or containers[0].get("image") != intent["agent"]["image"]:
            raise ValueError("Diagnostic Pod image does not match pinned agent")
        if job.get("metadata", {}).get("annotations", {}).get("agent-factory/source-revision") != intent["agent"]["revision"]:
            raise ValueError("Diagnostic Job revision does not match pinned agent")
        response = self.client.get(
            f"/api/v1/namespaces/foundry-publisher/pods/{quote(pod)}/log",
            params={"container": "broker", "limitBytes": 16384},
        )
        response.raise_for_status()
        return response.text

    def wait_job(self, resource: dict, deadline: float) -> bool:
        path = f"{self.path(resource)}/{quote(resource['metadata']['name'])}"
        while time.monotonic() < deadline:
            current = self.client.get(path)
            current.raise_for_status()
            job = current.json()
            if job.get("status", {}).get("succeeded"):
                return True
            if job.get("status", {}).get("failed"):
                return False
            version = job["metadata"]["resourceVersion"]
            with self.client.stream("GET", self.path(resource), params={
                "watch": "true", "fieldSelector": f"metadata.name={resource['metadata']['name']}",
                "resourceVersion": version, "timeoutSeconds": 60,
            }, timeout=httpx.Timeout(70)) as stream:
                stream.raise_for_status()
                for line in stream.iter_lines():
                    if not line:
                        continue
                    event = json.loads(line)
                    if event.get("object", {}).get("status", {}).get("succeeded"):
                        return True
                    if event.get("object", {}).get("status", {}).get("failed"):
                        return False
        raise TimeoutError("Fix Job did not finish before the handoff deadline")


def dispatch(intent: dict, raw_result: str, marker: str, incident_id: str, handoff_job: str, read_token: str, *, api=None, github=None, helm="helm") -> dict:
    intent = checked_intent(intent)
    result = checked_result(raw_result, marker, intent)
    if result["decision"] == "abstain":
        return {"incident_id": incident_id, "status": "abstained"}
    if not set(intent["required_evidence"]) <= set(result["evidence"]):
        raise ValueError("Actionable result is missing the declared evidence")
    if not DNS.fullmatch(incident_id) or len(incident_id) > 50:
        raise ValueError("Incident ID is not a short DNS label")
    sha = resolve_sha(intent["policy"]["repository"], intent["policy"]["base_ref"], read_token, client=github)
    resources = render_fix(intent, result, incident_id, sha, helm=helm)
    api = api or Kubernetes()
    owner = api.handoff_owner(handoff_job)
    job = next(resource for resource in resources if resource["kind"] == "Job")
    input_map = next(resource for resource in resources if resource["kind"] == "ConfigMap")
    tokens = [resource for resource in resources if resource["kind"] in {"GithubAccessToken", "ExternalSecret"}]
    created = []
    try:
        for resource in [*tokens, job, input_map]:
            if resource in tokens:
                resource["metadata"]["ownerReferences"] = [owner]
            if resource is input_map:
                resource["metadata"]["ownerReferences"] = [api.reference(job)]
            api.create(resource)
            created.append(resource)
        succeeded = api.wait_job(job, time.monotonic() + 2100)
        if not succeeded:
            raise RuntimeError("Fix Job failed; inspect its retained Pod logs")
        return {"incident_id": incident_id, "status": "completed", "base_sha": sha,
                "published": intent["publish"]}
    finally:
        for resource in reversed(created):
            # The Job keeps its Pod logs until its one-hour TTL; credentials
            # and the input ConfigMap are removed after the Pod is terminal.
            if resource["kind"] != "Job":
                api.delete(resource)


def main(intent_path: Path, marker: str, pod: str, incident_id: str):
    intent = checked_intent(json.loads(intent_path.read_text()))
    api = Kubernetes()
    output = dispatch(intent, api.read_log(pod, incident_id, intent), marker, incident_id,
                      os.environ["HANDOFF_JOB_NAME"],
                      os.environ["GITHUB_READ_TOKEN"], api=api)
    print(json.dumps(output))
