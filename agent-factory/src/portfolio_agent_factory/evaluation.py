"""Bounded offline replay and deterministic checks; grader fields stay private."""

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from .manifest import Agent


@dataclass(frozen=True)
class Case:
    id: str
    version: int
    incident: dict
    fixtures: dict
    rubric: dict


def load_case(path: Path) -> Case:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("Unsupported case schema")
    incident = data.get("incident")
    fixtures = data.get("tool_fixtures")
    if not isinstance(incident, dict) or not isinstance(fixtures, dict):
        raise ValueError("Case needs incident and tool fixtures")
    if {"reference", "rubric", "tool_fixtures", "provenance"} & set(incident):
        raise ValueError("Incident contains grader fields")
    names = incident.get("available_tools")
    if (not isinstance(names, list) or not names or
            any(not isinstance(name, str) for name in names) or
            len(names) != len(set(names)) or set(names) != set(fixtures)):
        raise ValueError("Available tools must match fixture keys")
    if any(len(json.dumps(value).encode()) > 24576 for value in fixtures.values()):
        raise ValueError("Tool fixture exceeded the 24 KiB cap")
    if not isinstance(data.get("id"), str) or not data["id"] or type(data.get("version")) is not int or data["version"] < 1:
        raise ValueError("Case id and positive version are required")
    rubric = data.get("rubric")
    if not isinstance(data.get("reference"), dict) or not isinstance(rubric, dict):
        raise ValueError("Case needs reference and rubric for human review")
    automated = rubric.get("automated", {})
    if not isinstance(automated, dict) or set(automated) - {"required_tools", "forbidden_tools", "forbidden_answer_patterns"}:
        raise ValueError("Unsupported automated checks")
    for key in ("required_tools", "forbidden_tools", "forbidden_answer_patterns"):
        entries = automated.get(key, [])
        if not isinstance(entries, list) or any(not isinstance(item, str) or not item for item in entries):
            raise ValueError(f"Invalid {key}")
    if set(automated.get("required_tools", [])) - set(fixtures):
        raise ValueError("Required tools need recorded fixtures")
    for pattern in automated.get("forbidden_answer_patterns", []):
        re.compile(pattern)
    return Case(data["id"], data["version"], incident, fixtures, rubric)


def score_case(case: Case, result: dict, agent: Agent) -> dict:
    """Check only observable constraints; semantic diagnosis needs human review."""
    calls = result["tool_calls"]
    automated = case.rubric.get("automated", {})
    checks = {
        "declared_tools_only": all(name in case.fixtures for name in calls),
        "within_tool_limit": len(calls) <= agent.max_tool_calls,
        "within_request_limit": result["model_requests"] <= agent.max_requests,
        "required_tools_used": set(automated.get("required_tools", [])) <= set(calls),
        "forbidden_tools_avoided": not set(automated.get("forbidden_tools", [])) & set(calls),
        "forbidden_answer_patterns_absent": not any(
            re.search(pattern, result["answer"]) for pattern in automated.get("forbidden_answer_patterns", [])
        ),
    }
    return {
        "status": "human_review_required" if all(checks.values()) else "failed",
        "automated_checks": checks,
        "human_review": "pending",
    }


def run_investigation(incident: dict, evidence: dict, agent: Agent, client, model: str, tracer, case: Case | None = None) -> dict:
    """One bounded investigation; evidence is supplied by a trusted adapter."""
    if not isinstance(incident, dict) or not isinstance(evidence, dict):
        raise ValueError("Incident and evidence must be objects")
    if len(evidence) > agent.max_tool_calls:
        raise ValueError("Too many available tools")
    tools = [
        {
            "type": "function",
            "name": name,
            "description": f"Return the recorded read-only {name} evidence for this incident.",
            "strict": True,
            "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        }
        for name in evidence
    ]
    visible_incident = {key: value for key, value in incident.items() if key != "available_tools"}
    visible_incident["available_tools"] = list(evidence)
    conversation = [{"role": "user", "content": json.dumps(visible_incident)}]
    calls = []
    started = time.monotonic()

    with tracer.start_as_current_span("agent.case") as span:
        span.set_attribute("langfuse.export", "true")
        span.set_attribute("agent.id", agent.id)
        if case is not None:
            span.set_attribute("agent.case.id", case.id)
            span.set_attribute("agent.case.version", case.version)
        span.set_attribute("gen_ai.request.model", model)

        for request_number in range(agent.max_requests):
            with tracer.start_as_current_span("agent.model_request") as model_span:
                model_span.set_attribute("agent.request.number", request_number + 1)
                request = dict(
                    model=model,
                    instructions=agent.instructions,
                    input=conversation,
                    reasoning={"effort": agent.reasoning_effort},
                    store=False,
                    include=["reasoning.encrypted_content"],
                    timeout=60,
                )
                if tools:
                    request["tools"] = tools
                response = client.responses.create(**request)
                if response.status != "completed":
                    raise RuntimeError(f"Model response status: {response.status}")
                model_span.set_attribute("gen_ai.response.id", response.id)

            conversation.extend(item.model_dump(exclude_none=True) for item in response.output)
            requested = [item for item in response.output if item.type == "function_call"]
            if not requested:
                answer = response.output_text
                if not answer:
                    raise RuntimeError("Model returned no answer")
                span.set_attribute("agent.tool_calls", len(calls))
                return {
                    "model": model,
                    "answer": answer,
                    "tool_calls": calls,
                    "model_requests": request_number + 1,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "trace_id": f"{span.get_span_context().trace_id:032x}",
                }

            for call in requested:
                if len(calls) >= agent.max_tool_calls:
                    raise RuntimeError("Tool call limit reached")
                if call.name not in evidence:
                    raise ValueError(f"Unknown tool: {call.name}")
                if json.loads(call.arguments) != {}:
                    raise ValueError(f"Unexpected arguments: {call.name}")
                with tracer.start_as_current_span("agent.tool") as tool_span:
                    tool_span.set_attribute("agent.tool.name", call.name)
                    result = evidence[call.name]
                    calls.append(call.name)
                    conversation.append({
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": json.dumps(result),
                    })
                    if getattr(call, "caller", None) is not None:
                        conversation[-1]["caller"] = call.caller.model_dump(exclude_none=True)

        raise RuntimeError("Model request limit reached")


def run_case(case: Case, agent: Agent, client, model: str, tracer) -> dict:
    """Offline replay uses the same loop but keeps grader fields outside it."""
    result = run_investigation(case.incident, case.fixtures, agent, client, model, tracer, case)
    return {"case_id": case.id, "case_version": case.version, **result,
            "evaluation": score_case(case, result, agent)}
