"""Verify a first hosted release and record a private-network smoke invocation."""

import os
import time

import httpx
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .publish import create_candidate_release, create_first_release


def wait_active(client, agent_name: str, version: str, *, timeout_seconds: int = 600, interval_seconds: int = 5):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        current = client.agents.get_version(agent_name=agent_name, agent_version=version)
        status = str(getattr(current.status, "value", current.status)).lower()
        if status == "active":
            return current
        if status in {"failed", "deleting", "deleted"}:
            raise RuntimeError(f"Foundry version {version} status: {status}; error: {current.error}")
        time.sleep(interval_seconds)
    raise TimeoutError(f"Foundry version {version} did not become active")


def smoke(project_endpoint: str, agent_name: str, payload: dict, credential, *, client=None, session_id=None) -> dict:
    token = credential.get_token("https://ai.azure.com/.default").token
    url = f"{project_endpoint.rstrip('/')}/agents/{agent_name}/endpoint/protocols/invocations"
    sender = client or httpx.Client(timeout=120)
    response = sender.post(
        url,
        params={"api-version": "v1", **({"agent_session_id": session_id} if session_id else {})},
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict) or not result:
        raise ValueError("Foundry invocation returned no structured result")
    return result


def require_pinned_route(project_endpoint: str, agent_name: str, expected_version: str, credential, *, client=None):
    """Fail closed unless ordinary traffic is fixed to the previous version."""
    if not expected_version.isdigit():
        raise ValueError("Base version must be a concrete release number")
    sender = client or httpx.Client(timeout=30)
    token = credential.get_token("https://ai.azure.com/.default").token
    response = sender.get(
        f"{project_endpoint.rstrip('/')}/agents/{agent_name}",
        params={"api-version": "v1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    response.raise_for_status()
    endpoint = response.json().get("agent_endpoint", {})
    rules = endpoint.get("version_selector", {}).get("version_selection_rules", [])
    if rules != [{"agent_version": expected_version, "traffic_percentage": 100, "type": "FixedRatio"}]:
        raise ValueError("Production endpoint is not fixed to the expected base version")
    if "invocations" not in endpoint.get("protocol_configuration", {}):
        raise ValueError("Production endpoint has no Invocations protocol")


def create_pinned_session(project_endpoint: str, agent_name: str, version: str, credential, *, timeout_seconds=120):
    """Bind every diagnostic turn to the same concrete hosted-agent version."""
    from azure.ai.projects.models import VersionRefIndicator

    if not version.isdigit():
        raise ValueError("A concrete release version is required")
    project = AIProjectClient(project_endpoint, credential)
    session = project.agents.create_session(
        agent_name=agent_name,
        version_indicator=VersionRefIndicator(agent_version=version),
    )
    session_id = session.agent_session_id
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = str(getattr(session.status, "value", session.status)).lower()
        if status == "active":
            return session_id
        if status in {"failed", "deleted", "deleting"}:
            raise RuntimeError(f"Pinned session status: {status}")
        time.sleep(2)
        session = project.agents.get_session(agent_name=agent_name, session_id=session_id)
    raise TimeoutError("Pinned candidate session did not become active")


def candidate_and_smoke(project_endpoint: str, request: dict, payload: dict, base_version: str, credential=None) -> dict:
    """Create and test a candidate without moving the fixed production route."""
    credential = credential or DefaultAzureCredential()
    agent_name = request["agent_name"]
    require_pinned_route(project_endpoint, agent_name, base_version, credential)
    provider = TracerProvider(resource=Resource.create({"service.name": "foundry-publisher"}))
    if os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    tracer = provider.get_tracer(__name__)
    try:
        with tracer.start_as_current_span("foundry.candidate.smoke") as span:
            span.set_attribute("langfuse.export", "true")
            span.set_attribute("agent.id", agent_name)
            span.set_attribute("agent.revision", request["metadata"]["source_revision"])
            version = create_candidate_release(project_endpoint, request, credential)
            project = AIProjectClient(project_endpoint, credential)
            active = wait_active(project, agent_name, str(version.version))
            require_pinned_route(project_endpoint, agent_name, base_version, credential)
            session_id = create_pinned_session(project_endpoint, agent_name, str(active.version), credential)
            span.set_attribute("agent.version", str(active.version))
            result = smoke(project_endpoint, agent_name, payload, credential, session_id=session_id)
            return {"agent": agent_name, "candidate_version": str(active.version),
                    "base_version": base_version, "status": str(getattr(active.status, "value", active.status)),
                    "trace_id": f"{span.get_span_context().trace_id:032x}", "response": result}
    finally:
        provider.shutdown()


def release_and_smoke(project_endpoint: str, request: dict, payload: dict, credential=None) -> dict:
    credential = credential or DefaultAzureCredential()
    provider = TracerProvider(resource=Resource.create({"service.name": "foundry-publisher"}))
    if os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))

    tracer = provider.get_tracer(__name__)
    try:
        with tracer.start_as_current_span("foundry.release.smoke") as span:
            span.set_attribute("langfuse.export", "true")
            span.set_attribute("agent.id", request["agent_name"])
            span.set_attribute("agent.revision", request["metadata"]["source_revision"])
            version = create_first_release(project_endpoint, request, credential)
            project = AIProjectClient(project_endpoint, credential)
            active = wait_active(project, request["agent_name"], str(version.version))
            span.set_attribute("agent.version", str(active.version))
            result = smoke(project_endpoint, request["agent_name"], payload, credential)
            return {
                "agent": request["agent_name"],
                "version": str(active.version),
                "status": str(getattr(active.status, "value", active.status)),
                "trace_id": f"{span.get_span_context().trace_id:032x}",
                "response": result,
            }
    finally:
        provider.shutdown()
