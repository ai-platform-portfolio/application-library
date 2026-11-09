"""Prepare immutable Foundry-hosted versions and create the first release."""

import re

from .manifest import Agent


_IMAGE = re.compile(r"^[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$")
_REVISION = re.compile(r"^[a-f0-9]{40}$")


def version_request(agent: Agent, image: str, revision: str, *, draft: bool = True) -> dict:
    if not _IMAGE.fullmatch(image):
        raise ValueError("Image must be a registry-qualified sha256 digest")
    if not _REVISION.fullmatch(revision):
        raise ValueError("Revision must be a full Git commit SHA")
    return {
        "agent_name": agent.id,
        "draft": draft,
        "metadata": {"source_revision": revision, "image_digest": image},
        "definition": {
            "kind": "hosted",
            "container_configuration": {"image": image},
            "cpu": "1",
            "memory": "2Gi",
            "protocol_versions": [{"protocol": agent.protocol, "version": "2.0.0"}],
            "environment_variables": {
                "AGENT_MANIFEST": f"agents/{agent.id}.yaml",
                "MODEL_DEPLOYMENT_NAME": agent.model,
                "PYTHONNOUSERSITE": "1",
            },
        },
    }


def create_draft(project_endpoint: str, request: dict, credential=None):
    """The caller runs inside the private network with Project Manager access."""
    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import (
        ContainerConfiguration,
        HostedAgentDefinition,
        ProtocolVersionRecord,
    )
    from azure.core.exceptions import ResourceNotFoundError
    from azure.identity import DefaultAzureCredential

    if not project_endpoint.startswith("https://") or "/api/projects/" not in project_endpoint:
        raise ValueError("A Foundry project HTTPS endpoint is required")
    definition = request["definition"]
    client = AIProjectClient(project_endpoint, credential or DefaultAzureCredential())
    try:
        versions = client.agents.list_versions(request["agent_name"], include_drafts=True)
        for version in versions:
            metadata = version.metadata or {}
            if metadata.get("source_revision") == request["metadata"]["source_revision"]:
                if metadata.get("image_digest") != request["metadata"]["image_digest"]:
                    raise ValueError("Revision already exists with a different image")
                return version
    except ResourceNotFoundError:
        pass
    return client.agents.create_version(
        agent_name=request["agent_name"],
        draft=True,
        metadata=request["metadata"],
        definition=HostedAgentDefinition(
            container_configuration=ContainerConfiguration(**definition["container_configuration"]),
            cpu=definition["cpu"],
            memory=definition["memory"],
            protocol_versions=[ProtocolVersionRecord(**item) for item in definition["protocol_versions"]],
            environment_variables=definition["environment_variables"],
        ),
    )


def create_first_release(project_endpoint: str, request: dict, credential=None):
    """Reapply the same release, but never implicitly change an existing agent."""
    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import ContainerConfiguration, HostedAgentDefinition, ProtocolVersionRecord
    from azure.core.exceptions import ResourceNotFoundError
    from azure.identity import DefaultAzureCredential

    if request["draft"]:
        raise ValueError("First release request must not be a draft")
    if not project_endpoint.startswith("https://") or "/api/projects/" not in project_endpoint:
        raise ValueError("A Foundry project HTTPS endpoint is required")

    client = AIProjectClient(project_endpoint, credential or DefaultAzureCredential())
    try:
        versions = list(client.agents.list_versions(request["agent_name"], include_drafts=True))
    except ResourceNotFoundError:
        versions = []

    releases = [version for version in versions if not str(version.version).startswith("draft-")]
    for version in releases:
        metadata = version.metadata or {}
        if metadata.get("source_revision") == request["metadata"]["source_revision"]:
            if metadata.get("image_digest") != request["metadata"]["image_digest"]:
                raise ValueError("Revision already exists with a different image")
            return version
    if releases:
        raise ValueError("Existing release requires a reviewed promotion workflow")

    definition = request["definition"]
    return client.agents.create_version(
        agent_name=request["agent_name"],
        draft=False,
        metadata=request["metadata"],
        definition=HostedAgentDefinition(
            container_configuration=ContainerConfiguration(**definition["container_configuration"]),
            cpu=definition["cpu"],
            memory=definition["memory"],
            protocol_versions=[ProtocolVersionRecord(**item) for item in definition["protocol_versions"]],
            environment_variables=definition["environment_variables"],
        ),
    )
