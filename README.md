# Application library

Reusable application code packages for the AI platform portfolio. Frontend,
backend and AI modules live in separate directories and are consumed by
applications as versioned dependencies. Application configuration, deployments
and environment-specific resources stay in their owning repositories.

## Packages

| Package | Purpose | Distribution |
| --- | --- | --- |
| [Agent runtime](agent-runtime) | Native Foundry durable context, reviewed knowledge and bounded coding capabilities | Versioned Python wheel on GitHub Releases |
| [Agent observability](observability) | Optional bounded OTLP tracing and collector-side Langfuse forwarding | Versioned Python wheel on GitHub Releases |

Consumers install a released wheel pinned by URL/SHA256 in their existing
requirements.txt. One [repository-wide workflow](.github/workflows/packages.yml)
finds root directories with pyproject.toml, tests installed packages and calls the
flat ops-shared build/release workflow once per package. Package tests use unittest
under each package's tests/ directory. Adding a package does not need another workflow.

PR/main runs only build and test. To publish, manually run **Python packages** on
main with `package=observability` and `publish=true`. Release tags use the directory
and wheel version, such as `observability-v0.1.0`. Native Git/GitHub CLI publish
without forcing tags or overwriting releases; no custom publisher/recovery script.

