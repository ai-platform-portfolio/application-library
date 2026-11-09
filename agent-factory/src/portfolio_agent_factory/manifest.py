"""Small, reviewable contract shared by agent handlers and publication."""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml


_ID = re.compile(r"^[a-z][a-z0-9-]{1,48}$")
_HANDLER = re.compile(r"^[a-zA-Z_][\w]*(?:\.[a-zA-Z_][\w]*)*:[a-zA-Z_][\w]*$")


@dataclass(frozen=True)
class Agent:
    id: str
    kind: str
    protocol: str
    model: str
    handler: str
    eval_suite: str
    instructions: str
    reasoning_effort: str
    max_requests: int
    max_tool_calls: int
    tools: tuple[str, ...] = ()


def load_agent(path: Path) -> Agent:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("Unsupported agent schema")
    if data.get("kind", "hosted") != "hosted" or data.get("protocol", "invocations") != "invocations":
        raise ValueError("This template currently supports hosted invocations agents")
    if not isinstance(data.get("id"), str) or not _ID.fullmatch(data["id"]):
        raise ValueError("Agent id must be a lower-case DNS label")
    if path.stem != data["id"]:
        raise ValueError("Manifest filename must match agent id")
    if not isinstance(data.get("handler"), str) or not _HANDLER.fullmatch(data["handler"]):
        raise ValueError("Handler must be a Python module:function")
    for field in ("model", "eval_suite", "instructions"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValueError(f"Agent {field} is required")
    if data.get("reasoning_effort") not in {"low", "medium", "high"}:
        raise ValueError("Unsupported reasoning effort")
    limits = data.get("limits")
    if not isinstance(limits, dict):
        raise ValueError("Agent limits are required")
    if type(limits.get("model_requests")) is not int or not 1 <= limits["model_requests"] <= 20:
        raise ValueError("Invalid model request limit")
    if type(limits.get("tool_calls")) is not int or not 0 <= limits["tool_calls"] <= 30:
        raise ValueError("Invalid tool call limit")
    tools = data.get("tools", [])
    if not isinstance(tools, list) or any(not isinstance(name, str) or not _ID.fullmatch(name) for name in tools):
        raise ValueError("Agent tools must be lower-case names")
    if len(tools) != len(set(tools)) or len(tools) > limits["tool_calls"]:
        raise ValueError("Agent tools must be unique and within the tool-call limit")
    return Agent(
        id=data["id"], kind=data.get("kind", "hosted"), protocol=data.get("protocol", "invocations"),
        model=data["model"], handler=data["handler"], eval_suite=data["eval_suite"],
        instructions=data["instructions"], reasoning_effort=data["reasoning_effort"],
        max_requests=limits["model_requests"], max_tool_calls=limits["tool_calls"],
        tools=tuple(tools),
    )
