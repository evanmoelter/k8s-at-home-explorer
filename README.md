# Kubernetes community agent explorer

Composable tools for researching deployment patterns and discovering applications in community Kubernetes repositories.

The service indexes the latest completed scan of each repository in a local bare Git corpus and exposes 13 read-only tools through MCP and a JSON CLI. Agents can combine exact source search, structured infrastructure filters, semantic retrieval, and resource graph traversal. Every family shares commit-pinned source identifiers.

A successful refresh replaces the previous scan; a failed refresh keeps the previous published scan available. PostgreSQL stores the derived indexes; the embedding provider is configurable.

## Start locally

```sh
mise trust
mise install
mise run setup
mise run check
mise run test
mise run test:integration
mise run local:up
mise run local:smoke
mise run local:forward
```

The MCP endpoint is `http://localhost:8000/mcp`. The dedicated kind cluster starts with an empty catalogue. Add repositories to `deploy/local/repositories.yaml` and rerun `mise run local:up` to start ingestion. The default CLI catalogue in `config/repositories.yaml` contains HCC, onedr0p, and bjw-s as a small pilot; the inherited `repos.json` catalogue is also supported.

- [Development and configuration](docs/development.md)
- [Tool reference and example workflows](docs/tools.md)
- [Local Kubernetes and reusable Helm deployment](docs/deployment.md)
- [Architecture](docs/designs/20261004-agent-explorer.md)
- [Decision log and decision owners](docs/decisions.md)
- [Pilot evidence and limitations](docs/evaluation.md)
- [Embedding provider evaluation](docs/embedding-evaluation.md)

Source tools accept a repository ID and an optional expected snapshot ID to reject stale reads during refresh. Historical browsing belongs in upstream Git; this service searches only the latest completed scan.

Semantic tools report unavailable until an HTTP embedding endpoint, model, and dimensions are configured. Graph resolution uses raw source evidence; generated resources and unresolved substitutions are reported as unresolved. Repository presence does not establish app integration.

## Published service image

CI publishes `ghcr.io/evanmoelter/k8s-at-home-explorer` for Linux AMD64 and ARM64 after validation. Main builds use `edge` and `sha-<full-commit>`; version tags add release tags. API, worker, and initializer share this image. Pin the workflow's output digest for production; see [deployment and package visibility](docs/deployment.md#image-publication).

## Inherited human explorer

The existing `web/` UI and root scanner scripts remain available. Their [original README](docs/legacy-human-explorer.md) describes the legacy workflow. The new Python service has its own locked dependencies and deployment pipeline.
