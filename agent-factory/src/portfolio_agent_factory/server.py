"""Foundry's Invocations protocol adapter; agent logic stays importable locally."""

import asyncio
import importlib
import os
from pathlib import Path

from .manifest import load_agent

from .model import model_client


def build_app():
    from azure.ai.agentserver.invocations import InvocationAgentServerHost
    from opentelemetry import trace
    from starlette.responses import JSONResponse

    manifest = load_agent(Path(os.environ["PORTFOLIO_MANIFEST_PATH"]))
    module_name, function_name = manifest.handler.split(":", 1)
    handler = getattr(importlib.import_module(module_name), function_name)
    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/")
    model = os.environ.get("MODEL_DEPLOYMENT_NAME", manifest.model)
    client = model_client(endpoint)
    tracer = trace.get_tracer(__name__)

    app = InvocationAgentServerHost()

    @app.invoke_handler
    async def invoke(request):
        try:
            payload = await request.json()
            result = await asyncio.to_thread(handler, payload, manifest, client, model, tracer)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(result)

    return app


def main():
    build_app().run()
