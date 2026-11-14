# Agent observability

A small Python library for optional OTLP HTTP trace export. It owns provider
initialization, bounded batching, payload-capture defaults and shutdown. It has
no Foundry SDK, SRE tool, Kubernetes client, collector address or Langfuse key.
Agent-specific spans and evaluation attributes stay with the consuming code.

```python
from functools import partial
from portfolio_agent_observability import configure, shutdown

# Pass this as the hosting adapter's configure_observability callback.
configure_tracing = partial(
    configure,
    service_name="my-agent",
    service_version="source-commit",
    langfuse=True,
)
```

For a CLI/evaluator, call `configure_tracing()` before work, then `shutdown()`
in a finally block. For a host, call shutdown from its shutdown hook. Importing
the package does not configure anything. Repeating configuration with identical
settings reuses the provider; conflicting settings or an existing foreign
provider fail explicitly. Configure before another SDK installs its provider.
The package accepts the hosting callback's extra arguments but intentionally owns
only the declared OTLP trace sink, not automatic Azure Monitor/log/metric export.

Set standard `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` to the exact traces URL. Empty
disables export while retaining valid trace identifiers. Export runs in a
background processor with a512-span queue,128-span batches and two-second HTTP
timeout. Flush is best-effort; traces may be dropped during an outage. Startup
and inference do not wait for the receiver. Message capture is forced off; the
package does not add automatic HTTP/OpenAI instrumentation or record payloads.
Consumers remain responsible for the attributes/errors they add to spans.

Langfuse forwarding defaults off. Explicit `langfuse=True` adds resource marker
`langfuse.export="true"` to every span so the existing collector can forward the
whole trace. The marker itself does not configure a collector or authenticate
to Langfuse. Credentials and routing remain collector-owned in GitOps.

## Build, test and consume

```sh
python3 -m pip wheel --no-deps --wheel-dir dist .
python3 -m pip install dist/*.whl
python3 -m unittest discover -s tests -v
```

The repository-wide packages.yml calls ops-shared's scalar Python-package workflow.
PR/main builds test the package without publishing. After merging, manually run
the workflow on main with `package=observability` and `publish=true`; it derives the release tag from the
wheel version (`observability-v0.1.0`). The version lives only in pyproject.toml.
Publication uses GitHub Releases, not GHCR or a new Python registry. The source
commit and wheel SHA256 accompany the release. Native Git/GitHub CLI refuse
conflicting tags or existing releases; no automatic overwrite/recovery is added.
Consumer adoption must use the actual released
wheel URL and SHA256 in the existing requirements.txt; never commit a placeholder.

Keep complete pinned/hashed dependencies in the consuming application's existing
requirements.txt. Install with `--require-hashes --only-binary=:all:`. Package
metadata declares compatible OTel1.x constraints; current acceptance uses Python3.12
and OTel1.45.2, including the agent's pinned hosting SDK. Do not copy this module
or download mutable source during container startup.
