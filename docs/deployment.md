# Deployment

The service has an HTTP API and a periodic ingestion worker in one pod, sharing
one corpus PVC. PostgreSQL with pgvector stores the derived indexes separately.
A schema initialization container runs before the API and worker. The API is
read-only from an agent's perspective; corpus fetching and indexing belong to
the worker or explicit administrative CLI commands.

The API and worker run as UID/GID 568 with read-only root filesystems. `/data`
and `/tmp` are writable mounts; `/config` is a read-only configuration mount.
Keep one replica and the `Recreate` strategy while using this RWO layout.
Back up the database and corpus together to restore a consistent serving state.
The indexes can be rebuilt from the current stored source; preferences and
configured catalogue metadata should remain in Git. The service queries only
the latest completed scan per repository. Successful refreshes replace prior
scans; failed refreshes keep the previous published scan available. Physical
cleanup failures may leave files temporarily, without exposing old scans to
queries. Use upstream Git for historical investigation.

## Dedicated local Kubernetes

The local manifests use kind's default StorageClass and a standalone PostgreSQL
container. No database operator or Flux installation is required.

```sh
mise install
mise run local:up
mise run local:smoke
mise run local:forward
```

The scripts manage only the kind cluster named `k8s-explorer`. They obtain its
own kubeconfig at `.data/local.kubeconfig`, pass the explicit
`kind-k8s-explorer` context to every kubectl operation, and leave the global
current context unchanged. They refuse to forward or delete when that managed
cluster is absent. The database password is generated at first installation and
passed directly into a local Kubernetes Secret without printing or saving it.

The initial local catalogue is deliberately empty. Copy selected entries from
`config/repositories.yaml` into `deploy/local/repositories.yaml`, then rerun
`mise run local:up` to publish the ConfigMap and restart the worker. Start with a
small corpus. To keep the tracked deployment empty while testing pilot data,
run `mise run local:seed -- config/repositories.yaml` instead. This creates
a separate local ConfigMap and restarts the worker; rerunning `mise run local:up`
restores the tracked catalogue. The seed task prints the explicit command for
following worker progress. `mise run local:forward` exposes MCP at `http://localhost:8000/mcp`;
health checks remain available on the same address. Local HTTP is unauthenticated
and the Service is ClusterIP. Add `EXPLORER_API_TOKEN` from a Secret before
sharing the endpoint.

Embedding settings can be added to the deployment's shared environment. Use a
Secret reference for the API key. A model running on the host must be reachable
from inside the kind node; a host-side `localhost` URL refers to the pod itself.

```sh
mise run local:down
```

Deleting this dedicated cluster removes its local PVC data. Compose integration
tests use a separate disposable database and do not depend on the cluster.

## Install with Helm

[The reusable values file](../deploy/helm/app-template/values.yaml) targets the
bjw-s app-template chart. It creates the API, worker, schema initializer,
ClusterIP Service, repository ConfigMap, and corpus PVC. It uses your cluster's
default StorageClass and exposes no ingress or Gateway route by default.
PostgreSQL is external to this chart: use an existing database service or your
preferred PostgreSQL operator with pgvector installed.

Prepare these before installation:

1. Select the CI-published service image at
   `ghcr.io/evanmoelter/k8s-at-home-explorer`. One image supplies the API, worker,
   and initializer on Linux AMD64 and ARM64. Successful pushes to `main` publish
   `edge` and `sha-<full-commit>` tags; `v*` version tags publish the full version
   (without `v`), major/minor alias, and a commit tag. Stable semantic versions
   also update `latest`; prereleases do not. The workflow summary records the
   multi-platform digest: pin it for production. Forks publish to
   `ghcr.io/<lowercase-owner>/<lowercase-repo>` and must override the example's
   image repository. `mise run image:build` still builds a local development image.
2. Create a dedicated PostgreSQL database and application role. PostgreSQL 17
   with pgvector is the locally tested combination. Have a database administrator
   run `CREATE EXTENSION IF NOT EXISTS vector;` **in the explorer database**.
   The application role must own the database/schema or have permission to
   create and update its tables and indexes. The initializer runs migrations;
   it does not provision the database or role. Preinstalling the extension
   avoids granting extension-installation privileges to the application role.
3. Create an `explorer-credentials` Kubernetes Secret in the application's
   namespace with `EXPLORER_DATABASE_URL` and `EXPLORER_API_TOKEN`. Use a
   PostgreSQL connection URI with the required TLS settings and a generated
   bearer token. Keep credentials in your secret manager or a private file
   outside this repository.
4. Select a small initial repository catalogue and storage capacity, then
   configure a real embedding provider if semantic retrieval is desired.

For example, after choosing an explicit Kubernetes context, create the namespace
and load credentials from a private environment file containing those two keys:

```sh
: "${CLUSTER_CONTEXT:?Set the target Kubernetes context}"
kubectl --context "$CLUSTER_CONTEXT" create namespace k8s-explorer --dry-run=client -o yaml \
  | kubectl --context "$CLUSTER_CONTEXT" apply -f -
kubectl --context "$CLUSTER_CONTEXT" -n k8s-explorer create secret generic explorer-credentials \
  --from-env-file=/path/to/private/explorer-credentials.env
```

Create an image overlay such as `.data/install-values.yaml`, replacing the
repository and digest with the CI-published image:

```yaml
controllers:
  explorer:
    initContainers:
      init-db:
        image: &image
          repository: ghcr.io/evanmoelter/k8s-at-home-explorer
          tag: sha-REPLACE_WITH_FULL_COMMIT@sha256:REPLACE_WITH_PUBLISHED_DIGEST
    containers:
      api:
        image: *image
      worker:
        image: *image
```

The image must be configured for all three containers. Overriding only the API's
image through `--set` does not update YAML aliases in values that Helm has
already parsed. An overlay containing all three image entries keeps their
versions consistent.

```sh
helm upgrade --install k8s-explorer oci://ghcr.io/bjw-s-labs/helm/app-template \
  --version 5.2.1 --kube-context "$CLUSTER_CONTEXT" --namespace k8s-explorer \
  --values deploy/helm/app-template/values.yaml --values .data/install-values.yaml \
  --wait --timeout 10m
kubectl --context "$CLUSTER_CONTEXT" -n k8s-explorer port-forward service/k8s-explorer 8000:8000
```

MCP is available at `http://localhost:8000/mcp`; clients send
`Authorization: Bearer <token>`. Health routes remain `/health/live` and
`/health/ready`. Review the rendered release before installing into a shared
cluster. PostgreSQL and the container registry must be reachable from its pods.

## Image publication

The `publish` job in `.github/workflows/agent-explorer.yaml` runs only after
lint, tests, integration, schema, and local Kubernetes smoke checks pass.
Pull requests run those checks without registry login or publication. Publishing
uses the repository's `GITHUB_TOKEN` with job-scoped `packages: write`; no
additional registry credential is required. Images include source/revision
labels, BuildKit provenance, and an SBOM.

After the first successful publication, set the GHCR package visibility to
**public** in its package settings so other operators can pull anonymously.
GitHub creates new packages private by default; linking the image to the
repository does not make it public. If retaining a private package, configure
Kubernetes image-pull credentials. Organization policies may also need to allow
package publishing by Actions. Publication starts on a matching CI trigger.

## Customize the installation

### Voyage-4 deployment profile

The operator selected `voyage-4` dense retrieval on 2026-10-10 based on the
[hosted calibration](evaluations/20261007-expanded-calibration.md). Broader
relevance evaluation is deferred and is not a deployment prerequisite. Provider
selection remains configurable; the base values do not enable paid calls.

Load [the Voyage overlay](../deploy/helm/app-template/voyage-4-values.yaml)
after `values.yaml` and before your installation-specific values. It configures
the native Voyage protocol and requests 1,024 dimensions for both API and worker,
matching the evaluated settings. Add `EXPLORER_EMBEDDING_API_KEY` to the
`explorer-credentials` Secret. For Flux, include the overlay in your installation's
values ConfigMap or copy its entries into HelmRelease values. Keep credentials,
the database, storage class, route, and catalogue in the installing GitOps repo.

Before calling the installation ready:

1. Pin the published service image digest for all three containers, provision a
   dedicated pgvector database, and configure corpus storage and credentials.
2. Start with the [tested 20-repository catalogue](../config/evaluation/repositories.yaml), then expand discovery after a
   successful serving check. `discover` prints a catalogue; the worker refreshes
   configured entries but does not automatically discover newly tagged repos.
3. Check `structured_repositories` and `semantic_search` readiness counts after
   ingestion. `/health/ready` verifies database/schema access, not embedding
   completeness. Sync reports `ingestion_failed` and `semantic_failed`, counts
   both in `failed`, and exits with status 1 if either is nonzero. Transient
   embedding failures receive bounded retries; exhausted failures retain published
   source/structured data and cached vector batches for the next sync.
4. Exercise authenticated HTTP MCP with a real query, follow a returned result to
   exact source evidence, and confirm a repeat sync reuses unchanged embeddings.
   The local smoke checks transport/tool availability; use `mise run deploy:smoke`
   for real semantic serving. The opt-in real-model integration tests support
   Voyage and verify authenticated MCP, source evidence, and repeat-sync cache reuse
   in a disposable database; see [development](development.md#real-provider-serving-validation).
5. Connect the agent client to `/mcp` with its bearer token. Observe exclusions,
   ingestion failures, memory/storage usage, and exact-search latency before
   expanding to the full discovered catalogue; adjust capacity from measurements.

The separate HCC agent owns cluster-specific GitOps configuration and execution.
Benchmark databases are disposable evaluation targets, not the serving database;
their vector cache is not automatically imported into a new installation.

For an installed endpoint, supply `EXPLORER_SMOKE_URL` (the full `/mcp` URL) and
`EXPLORER_SMOKE_TOKEN` through your private environment, then run:

```sh
mise run deploy:smoke -- --min-ready-repositories 20
```

This read-only check rejects an endpoint that accepts unauthenticated requests,
checks the model/dimensions, requires every selected scan to be ready, and follows
up to three real semantic results to their guarded source ranges. It compares
source lines using the extractor's newline normalization and checks commit and
content hash. It prints only a summary. Set the minimum to the expected catalogue
size to detect repositories that never published a scan. Run after ingestion is
idle; a concurrent refresh can deliberately invalidate a source guard. The query
embedding is a paid provider call unless covered by the account's allowance.

`EXPLORER_EMBEDDING_MAX_RETRIES` defaults to 2 additional attempts (0–5 allowed).
Retries apply to HTTP 408/429/500/502/503/504 and transient network/timeout errors,
with 1-second exponential backoff. `Retry-After` seconds and HTTP dates take
precedence. `EXPLORER_EMBEDDING_RETRY_MAX_DELAY_SECONDS` defaults to 10 (1–60
allowed); a longer requested pause fails immediately for the scheduler to revisit,
instead of retrying before the provider permits it. Authentication, malformed
responses, and invalid vectors fail without retry. Settings affect both ingestion
and query requests and do not invalidate cached vectors. Usage statistics count
every attempt, with separate retry and delay counters; ambiguous transport retries
may have been processed by the provider, so reported usage is not a billing ledger.

Add these settings to your overlay rather than editing the reusable defaults:

- **Storage:** set `persistence.corpus.size` and, if needed,
  `persistence.corpus.storageClass`. Keep one replica and `Recreate` with the
  shared RWO corpus. Choose a backup and retention policy for both corpus and
  database. Backups recover the current serving state; the service does not
  provide historical snapshot browsing.
- **Catalogue:** populate `configMaps.config.data.repositories.yaml` with entries
  in the format shown in `config/repositories.yaml`. Preferences and stars
  remain separate ranking signals. For authenticated metadata refresh, add
  `EXPLORER_GITHUB_TOKEN` to a Secret and a matching `secretKeyRef` in the
  worker's environment.
- **Embeddings:** configure `EXPLORER_EMBEDDING_URL`,
  `EXPLORER_EMBEDDING_MODEL`, and `EXPLORER_EMBEDDING_DIMENSIONS` for the API and
  worker; supply `EXPLORER_EMBEDDING_API_KEY` through a Secret when required.
  Both containers need the same provider configuration for ingestion and
  queries to use compatible vectors. Without a provider, source, structured,
  and graph tools work while semantic tools report that they are unavailable.
- **Networking:** configure app-template ingress/route values or a separate
  Ingress/HTTPRoute for your cluster. Add every advertised hostname to the API's
  `EXPLORER_ALLOWED_HTTP_HOSTS` JSON list. If you change the release or Service
  name, include its hostname when accessing MCP through Service DNS. Keep
  bearer authentication enabled when exposing the endpoint. TLS termination
  belongs at your ingress or Gateway.

Environment overrides belong under `controllers.explorer.containers.api.env`
and `controllers.explorer.containers.worker.env`. If a setting is also needed
by the initializer, add it under
`controllers.explorer.initContainers.init-db.env`. Helm merges values maps;
YAML aliases do not propagate later overrides between containers. Extra keys
in `explorer-credentials` are not loaded automatically: wire optional token and
embedding-key environment variables through explicit `valueFrom.secretKeyRef`
entries for the containers that need them.

## Flux installation

[The optional Flux resources](../deploy/helm/flux/) contain an app-template
OCIRepository pinned by chart version and digest, plus a HelmRelease. The root
`deploy/helm/kustomization.yaml` creates the values ConfigMap from the reusable
values file; the HelmRelease reads it through `valuesFrom`. Render the complete
wrapper with `kustomize build deploy/helm`, rather than building the `flux/`
subdirectory alone. It has no cluster-specific storage, database, secret-manager,
or Gateway dependencies.

Integrate the `deploy/helm/` directory into your own GitOps tree, select the target namespace,
and supply the same image and configuration overrides through the HelmRelease
or your own overlay. Provision the external database and `explorer-credentials`
Secret through your cluster's normal workflow before reconciling the release.
Declare the appropriate dependencies in the Flux Kustomization that installs
it. Validate Kustomize and Helm rendering before publishing those GitOps
changes; use Flux reconciliation rather than applying Helm resources over an
existing Flux-managed release.

Upstream references:

- [GitHub container registry permissions and visibility](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
- [app-template documentation](https://bjw-s-labs.github.io/helm-charts/docs/app-template/)
- [pgvector installation and Docker images](https://github.com/pgvector/pgvector)
- [kind release and node image pins](https://github.com/kubernetes-sigs/kind/releases/tag/v0.31.0)
