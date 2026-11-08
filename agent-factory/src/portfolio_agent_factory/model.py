"""OpenAI-compatible model client with a release-supplied route."""

import os
from urllib.parse import urlparse

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import OpenAI


def model_client(project_endpoint: str):
    base_url = os.environ.get("MODEL_BASE_URL") or f"{project_endpoint.rstrip('/')}/openai/v1/"
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Model endpoint must use HTTPS")
    scope = os.environ.get("MODEL_TOKEN_SCOPE", "https://ai.azure.com/.default")
    token = get_bearer_token_provider(DefaultAzureCredential(), scope)
    return OpenAI(base_url=base_url, api_key=token, max_retries=1)
