"""Validate, publish, release, or serve a hosted agent."""

import argparse
import importlib
import json
import os
from pathlib import Path

from .manifest import load_agent
from .publish import create_draft, version_request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["validate", "validate-evals", "publish", "release", "candidate", "promote", "rollback", "serve", "broker"])
    parser.add_argument("--agent", type=Path)
    parser.add_argument("--image")
    parser.add_argument("--revision")
    parser.add_argument("--project-endpoint", default=os.getenv("FOUNDRY_PROJECT_ENDPOINT"))
    parser.add_argument("--smoke", type=Path)
    parser.add_argument("--incident", type=Path)
    parser.add_argument("--case", type=Path)
    parser.add_argument("--namespace")
    parser.add_argument("--version")
    parser.add_argument("--base-version")
    parser.add_argument("--target-version")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if args.command == "serve":
        from .server import main as serve

        serve()
        return

    if args.command == "broker":
        if not args.agent or not args.incident or not args.namespace or not args.version or not args.project_endpoint:
            parser.error("broker requires --agent, --incident, --namespace, --version and --project-endpoint")
        from .broker import main as investigate

        investigate(args.agent, args.incident, args.project_endpoint, args.namespace, args.version)
        return

    if args.command == "validate":
        manifests = [args.agent] if args.agent else sorted(Path("agents").glob("*.yaml"))
        if not manifests:
            parser.error("No agent manifests found")
        for path in manifests:
            agent = load_agent(path)
            module, function = agent.handler.split(":", 1)
            if not callable(getattr(importlib.import_module(module), function)):
                raise ValueError(f"Handler is not callable: {agent.handler}")
            print(f"Valid hosted agent: {agent.id}")
        return

    if args.command == "validate-evals":
        from .evaluation import load_case

        cases = [args.case] if args.case else sorted(Path("evals/cases").glob("*.yaml"))
        if not cases:
            parser.error("No evaluation cases found")
        for path in cases:
            case = load_case(path)
            print(f"Valid evaluation case: {case.id} v{case.version}")
        return

    if args.command in {"promote", "rollback"}:
        if not all((args.agent, args.image, args.revision, args.base_version, args.target_version, args.evidence)):
            parser.error("promote/rollback require --agent, --image, --revision, --base-version, --target-version and --evidence")
        from .release import change_route, validate_route_evidence

        request = version_request(load_agent(args.agent), args.image, args.revision, draft=False)
        with args.evidence.open("rb") as source:
            document = source.read(8193)
        if len(document) > 8192:
            raise ValueError("Route evidence exceeds the 8 KiB limit")
        evidence = json.loads(document)
        validate_route_evidence(evidence, request, args.base_version, args.target_version, args.command)
        if args.apply:
            if not args.project_endpoint:
                parser.error("--apply requires --project-endpoint")
            result = change_route(args.project_endpoint, request, args.base_version,
                                  args.target_version, evidence, args.command)
        else:
            result = {"agent": request["agent_name"], "from_version": args.base_version,
                      "to_version": args.target_version, "mode": args.command, "status": "validated_only"}
        print(json.dumps(result))
        return

    if args.agent is None or not args.image or not args.revision:
        parser.error("publish/release/candidate require --agent, --image and --revision")
    request = version_request(load_agent(args.agent), args.image, args.revision, draft=args.command == "publish")
    if args.apply:
        if not args.project_endpoint:
            parser.error("--apply requires --project-endpoint")
        if args.command == "release":
            if not args.smoke:
                parser.error("release --apply requires --smoke")
            from .release import release_and_smoke

            result = release_and_smoke(args.project_endpoint, request, json.loads(args.smoke.read_text()))
            print(json.dumps(result, default=str))
        elif args.command == "candidate":
            if not args.smoke or not args.base_version:
                parser.error("candidate --apply requires --smoke and --base-version")
            from .release import candidate_and_smoke

            result = candidate_and_smoke(args.project_endpoint, request, json.loads(args.smoke.read_text()), args.base_version)
            print(json.dumps(result, default=str))
        else:
            version = create_draft(args.project_endpoint, request)
            print(json.dumps({"name": version.name, "version": version.version, "status": version.status}, default=str))
    else:
        print(json.dumps(request, indent=2))


if __name__ == "__main__":
    main()
