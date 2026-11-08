# Application library

Reusable application code packages for the AI platform portfolio. Frontend,
backend and AI modules live in separate directories and are consumed by
applications as versioned dependencies. Application configuration, deployments
and environment-specific resources stay in their owning repositories.

## AI modules

- [agent-factory](agent-factory/README.md): Foundry hosted-agent manifest,
  publication and protocol package. Agent-specific handlers and evaluations
  belong in consuming repositories.
