"""Execute hosted-agent tool requests from inside AKS with read-only credentials."""

import json
import hashlib
import os
from pathlib import Path

from azure.identity import DefaultAzureCredential
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .diagnostics import KubernetesDiagnostics, TOOL_DESCRIPTIONS, target
from .manifest import load_agent
from .release import create_pinned_session, smoke


def load_incident(path: Path) -> dict:
    with path.open("rb") as source:
        payload = source.read(8193)
    if len(payload) > 8192:
        raise ValueError("Incident document exceeds the 8 KiB limit")
    incident = json.loads(payload)
    if not isinstance(incident, dict):
        raise ValueError("Incident document must be an object")
    return incident


def triage_answer(answer: str, used_tools: list[str], required_evidence: list[str]) -> dict:
    """Fail closed on an unstructured answer before an alert can start coding."""
    try:
        result = json.loads(answer)
    except json.JSONDecodeError as error:
        raise ValueError("Alert diagnosis must be structured JSON") from error
    if not isinstance(result, dict) or set(result) != {"decision", "answer", "evidence"}:
        raise ValueError("Alert diagnosis has unexpected fields")
    if result["decision"] not in {"actionable", "abstain"}:
        raise ValueError("Alert diagnosis has invalid decision")
    if not isinstance(result["answer"], str) or not 20 <= len(result["answer"]) <= 2500:
        raise ValueError("Alert diagnosis answer must be 20-2500 characters")
    if not isinstance(result["evidence"], list) or not all(
        isinstance(item, str) and item in used_tools for item in result["evidence"]
    ) or len(set(result["evidence"])) != len(result["evidence"]):
        raise ValueError("Alert diagnosis must cite used tools only")
    if result["decision"] == "actionable" and not set(required_evidence) <= set(result["evidence"]):
        raise ValueError("Actionable diagnosis lacks required evidence")
    return result


def degraded_readiness(history: list[dict]) -> bool:
    evidence = {entry["name"]: entry["result"] for entry in history}
    workload = evidence.get("workload_state", {}).get("deployment", {})
    desired, ready = workload.get("desired"), workload.get("ready")
    if not isinstance(desired, int) or desired < 1 or not isinstance(ready, int) or ready >= desired:
        return False
    events = evidence.get("recent_events", {}).get("events", [])
    return isinstance(events, list) and any(
        isinstance(event, dict) and event.get("reason") == "Unhealthy" and
        str(event.get("message", "")).startswith("Readiness probe failed:")
        for event in events
    )


def run_broker(agent, incident: dict, project_endpoint: str, namespace: str, *, version=None, credential=None, executor=None, invoker=None, session_id=None, require_triage=False, required_evidence=None, require_degraded=False) -> dict:
    """Keep the bounded tool loop in the broker; no Kubernetes token enters Foundry."""
    target(incident, namespace)
    if not agent.tools or not set(agent.tools) <= set(TOOL_DESCRIPTIONS):
        raise ValueError("Agent has no supported read-only tools")
    credential = credential or DefaultAzureCredential()
    executor = executor or KubernetesDiagnostics()
    invoker = invoker or smoke
    if version:
        session_id = session_id or create_pinned_session(project_endpoint, agent.id, version, credential)
    history = []

    for _ in range(agent.max_requests):
        kwargs = {"session_id": session_id} if session_id else {}
        result = invoker(project_endpoint, agent.id, {"incident": incident, "history": history}, credential, **kwargs)
        if result.get("status") == "final":
            if not isinstance(result.get("answer"), str) or not result["answer"].strip():
                raise ValueError("Agent returned an empty final answer")
            triage = triage_answer(result["answer"], [entry["name"] for entry in history],
                                   required_evidence or []) if require_triage else None
            if triage and triage["decision"] == "actionable" and require_degraded and not degraded_readiness(history):
                raise ValueError("Actionable triage requires current degraded readiness and a matching event")
            return {"agent": agent.id, "version": version, "target": {"namespace": namespace, "workload": incident["workload"]},
                    "answer": triage["answer"] if triage else result["answer"], "triage": triage,
                    "tool_calls": [entry["name"] for entry in history],
                    "model_requests": len(history) + 1, "trace_id": result.get("trace_id")}

        if result.get("status") != "tool_request" or not isinstance(result.get("tool"), str):
            raise ValueError("Agent returned an invalid tool step")
        name = result["tool"]
        if name not in agent.tools or name in [entry["name"] for entry in history]:
            raise ValueError("Agent requested an unknown or repeated tool")
        if len(history) >= agent.max_tool_calls:
            raise RuntimeError("Tool-call limit reached")
        with trace.get_tracer(__name__).start_as_current_span("agent.tool") as span:
            span.set_attribute("langfuse.export", "true")
            span.set_attribute("agent.id", agent.id)
            span.set_attribute("agent.tool.name", name)
            evidence = executor.execute(name, incident, namespace)
        serialized = json.dumps(evidence)
        if len(serialized.encode()) > 24576:
            raise ValueError("Tool evidence exceeded the 24 KiB cap")
        history.append({"name": name, "result": evidence})

    raise RuntimeError("Model-request limit reached")


def main(agent_path: Path, incident_path: Path, project_endpoint: str, namespace: str, version: str, *, handoff=False):
    agent = load_agent(agent_path)
    incident = load_incident(incident_path)
    provider = TracerProvider(resource=Resource.create({"service.name": "foundry-diagnostic-broker"}))
    if os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    try:
        with trace.get_tracer(__name__).start_as_current_span("agent.investigation") as span:
            span.set_attribute("langfuse.export", "true")
            required_evidence = json.loads(os.getenv("HANDOFF_REQUIRED_EVIDENCE", "[]")) if handoff else []
            if not isinstance(required_evidence, list) or not required_evidence or not set(required_evidence) <= set(agent.tools):
                if handoff:
                    raise ValueError("Handoff requires declared evidence tools")
            result = run_broker(agent, incident, project_endpoint, namespace, version=version,
                                require_triage=handoff, required_evidence=required_evidence,
                                require_degraded=os.getenv("HANDOFF_REQUIRE_DEGRADED") == "true")
            result["broker_trace_id"] = f"{span.get_span_context().trace_id:032x}"
            encoded_result = json.dumps(result, sort_keys=True, separators=(",", ":"))
            print(encoded_result)
            if handoff:
                decision = result["triage"]
                message = {"decision": decision["decision"], "trace_id": result["broker_trace_id"],
                           "sha256": hashlib.sha256(encoded_result.encode()).hexdigest()}
                encoded = json.dumps(message, separators=(",", ":"))
                Path("/dev/termination-log").write_text(encoded)
    finally:
        provider.shutdown()
