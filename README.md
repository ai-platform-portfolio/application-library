# Application library

Reusable application code packages for the AI platform portfolio. Frontend,
backend and AI modules live in separate directories and are consumed by
applications as versioned dependencies. Application configuration, deployments
and environment-specific resources stay in their owning repositories.

## Packages

| Package | Purpose | Distribution |
| --- | --- | --- |
| [Agent observability](observability) | Optional bounded OTLP tracing and collector-side Langfuse forwarding | Versioned Python wheel on GitHub Releases |

Consumers install a released wheel pinned by URL/SHA256 in their existing
requirements.txt. The package workflow builds/tests on PRs and main; publication
is an explicit manual run on main. Package-specific tests and publication intent
live here; the flat build/release workflow lives in ops-shared. No additional
Python registry, source synchronization or runtime download is needed.

## Retired agent factory

The Foundry agent factory has been removed from this portfolio. Its historical
source is preserved in the private
[sre-agent-foundry/application-library](https://github.com/sre-agent-foundry/application-library)
repository. The old SRE runtime, publisher and Helm charts remain retired;
the observability package is independent reusable Python code.
