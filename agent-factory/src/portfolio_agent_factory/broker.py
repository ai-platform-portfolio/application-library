"""Execute hosted-agent tool requests from inside AKS with read-only credentials."""

import json
import os
from pathlib import Path

from azure.identity import DefaultAzureCredential
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .diagnostics import KubernetesDiagnostics, TOOL_DESCRIPTIONS, target
from .manifest import load_agent
from .release import smoke


def run_broker(agent, incident: dict, project_endpoint: str, namespace: str, *, credential=None, executor=None, invoker=None) -> dict:
    """Keep the bounded tool loop in the broker; no Kubernetes token enters Foundry."""
    target(incident, namespace)
    if not agent.tools or not set(agent.tools) <= set(TOOL_DESCRIPTIONS):
        raise ValueError("Agent has no supported read-only tools")
    credential = credential or DefaultAzureCredential()
    executor = executor or KubernetesDiagnostics()
    invoker = invoker or smoke
    history = []

    for _ in range(agent.max_requests):
        result = invoker(project_endpoint, agent.id, {"incident": incident, "history": history}, credential)
        if result.get("status") == "final":
            if not isinstance(result.get("answer"), str) or not result["answer"].strip():
                raise ValueError("Agent returned an empty final answer")
            return {"agent": agent.id, "target": {"namespace": namespace, "workload": incident["workload"]},
                    "answer": result["answer"], "tool_calls": [entry["name"] for entry in history],
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


def main(agent_path: Path, incident_path: Path, project_endpoint: str, namespace: str):
    agent = load_agent(agent_path)
    incident = json.loads(incident_path.read_text())
    provider = TracerProvider(resource=Resource.create({"service.name": "foundry-diagnostic-broker"}))
    if os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    try:
        with trace.get_tracer(__name__).start_as_current_span("agent.investigation") as span:
            span.set_attribute("langfuse.export", "true")
            result = run_broker(agent, incident, project_endpoint, namespace)
            result["broker_trace_id"] = f"{span.get_span_context().trace_id:032x}"
            print(json.dumps(result))
    finally:
        provider.shutdown()
