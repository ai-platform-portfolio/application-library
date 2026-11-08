"""Validate definitions, prepare a draft version, or serve a hosted agent."""

import argparse
import importlib
import json
from pathlib import Path

from .manifest import load_agent
from .publish import create_draft, version_request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["validate", "publish", "serve"])
    parser.add_argument("--agent", type=Path)
    parser.add_argument("--image")
    parser.add_argument("--revision")
    parser.add_argument("--project-endpoint")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if args.command == "serve":
        from .server import main as serve

        serve()
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

    if args.agent is None or not args.image or not args.revision:
        parser.error("publish requires --agent, --image and --revision")
    request = version_request(load_agent(args.agent), args.image, args.revision)
    if args.apply:
        if not args.project_endpoint:
            parser.error("--apply requires --project-endpoint")
        version = create_draft(args.project_endpoint, request)
        print(json.dumps({"name": version.name, "version": version.version, "status": version.status}, default=str))
    else:
        print(json.dumps(request, indent=2))


if __name__ == "__main__":
    main()
