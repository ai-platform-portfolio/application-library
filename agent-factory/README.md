# Agent factory

An installable Python package for Foundry hosted agents. It owns the generic
manifest contract, Invocations protocol host, OpenAI-compatible model route and
digest-pinned draft-version publisher. An agent repository supplies its own
`agents/<id>.yaml`, importable handler, evaluations and container image.

The manifest needs `schema_version: 1`, `id`, `model`, `handler`, `eval_suite`,
`instructions`, `reasoning_effort` and bounded `limits`. `kind: hosted` and
`protocol: invocations` are the current defaults; other kinds and protocols
will require a reviewed adapter rather than silently using this one.

```sh
python -m portfolio_agent_factory validate
python -m portfolio_agent_factory publish --agent agents/sre.yaml \
  --image registry.example.test/agents/catalog@sha256:<digest> \
  --revision <full-git-sha>
```

The publisher prints the proposed draft request. Add `--project-endpoint` and
`--apply` from a private-network caller with Foundry Project Manager access to
create a version. Reapplying the same revision and image reuses its draft.
The image must include the agent manifest at `agents/<id>.yaml` relative to its
working directory and set `FOUNDRY_PROJECT_ENDPOINT`; the factory serves
`/invocations` and `/readiness` with the official protocol library. The
handler is imported from the manifest and receives the request payload,
manifest, model client, deployment name and tracer.

Only HTTPS model endpoints are accepted. `MODEL_BASE_URL` and
`MODEL_TOKEN_SCOPE` can select a later authenticated gateway route; the
default uses the Foundry project and `https://ai.azure.com/.default`.
This package does not supply diagnostic permissions, incident policies, a
knowledge store, event ingestion or coding autonomy.
