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

from .publish import create_first_release


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


def smoke(project_endpoint: str, agent_name: str, payload: dict, credential, *, client=None) -> dict:
    token = credential.get_token("https://ai.azure.com/.default").token
    url = f"{project_endpoint.rstrip('/')}/agents/{agent_name}/endpoint/protocols/invocations"
    sender = client or httpx.Client(timeout=120)
    response = sender.post(
        url,
        params={"api-version": "v1"},
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict) or not result:
        raise ValueError("Foundry invocation returned no structured result")
    return result


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
