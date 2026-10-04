# Community Kubernetes agent explorer

## Overview

Agents research deployment patterns and discover applications through separate source, structured, semantic, and relationship tools. Each family searches the latest completed scan of community repositories from a local bare Git corpus and returns commit-pinned evidence.

## Problem

The inherited explorer indexes Helm releases for human browsing. Agents also need arbitrary combinations of infrastructure services, related manifests, exact source evidence, and control over retrieval and ranking strategies.

## Functionality

An agent searches exact source or indexed resources, filters repositories by service combinations, searches semantically, and traverses resource relationships. Results expose native scores, source locations, indexing coverage, and snapshot identity. Stars and operator repository preferences are available as explicit ranking inputs.

## Design

The Python service exposes 13 read-only tools through a JSON CLI and the MCP Streamable HTTP transport. A bounded worker fetches configured repositories into bare Git stores and publishes one current scan per repository. A successful replacement retires the superseded database rows and source refs; a failed scan leaves the previous published scan available. Physical cleanup failures can temporarily retain files that are unavailable to queries. Active content-hash embedding cache entries are preserved.

PostgreSQL stores repository metadata, resource documents, graph edges, text-search data, and pgvector embeddings. An HTTP embedding provider is configured explicitly; structured and graph indexing work without it.

Repository IDs identify provider and repository; snapshot IDs include commit identity and remain in result evidence. Source tools take a repository ID and optional expected snapshot ID to reject stale reads during refresh. Structured, graph, and semantic queries search current scans without historical snapshot selectors. Files and resources carry deterministic IDs and source ranges. Graph resolution is restricted to one current scan and cluster context. Extractors recognize Flux, Argo, ordinary Kubernetes resources, Helm values, images, and common infrastructure identities. Unresolved and inferred connections remain distinguishable from explicit references.

The initial deployment uses a single corpus-owning pod with a worker and API sharing a PVC. PostgreSQL is separate. Local Kubernetes uses a disposable database and storage; the reusable app-template values use an existing PostgreSQL database and Secret, a default-class PVC, and a private Service. Optional Flux resources reuse the same values file.

## Security

Ingestion reads source without executing it. Production repository URLs are restricted to configured HTTPS providers, and source operations validate repository identity, commit, paths, and byte budgets. Agent tools are read-only; corpus refresh is a CLI/worker operation. The network endpoint supports an optional bearer credential and is intended for private cluster or LAN access. Credentials are not persisted in repository URLs or returned in errors.

## Validation

Disposable Git repositories test incremental fetches, renamed/deleted files, latest-scan replacement, failed-refresh preservation, cleanup, and guarded source reads. PostgreSQL tests verify replacement transactions, current-only queries, graph resolution, and vector search with explicit numeric fixtures. MCP transport tests verify discovery and tool composition. Deployment validation renders local Kubernetes, upstream Helm, and optional Flux examples; a disposable local cluster exercises the container when available.

## Open decisions

- The operator's embedding provider and model remain configurable; selection does not block other tool families.
- Cross-repository object deduplication, specialized graph databases, and combined search workflows depend on measurements from the first corpus experiment.

Historical browsing, diffs, and trend analysis are outside this service's scope. Agents can use upstream Git when a result's commit warrants historical investigation.

See [the decision log](../decisions.md) for decision owners and rationale.
