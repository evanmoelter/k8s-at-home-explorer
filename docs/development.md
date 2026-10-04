# Development

The agent service lives in `src/k8s_explorer/`. The inherited Python catalogue
scripts and `web/` remain the human explorer. They have separate dependency and
build workflows.

Install [mise](https://mise.jdx.dev/), then run:

```sh
mise trust
mise install
mise run setup
mise run check
mise run test
```

Tool versions live in `mise.toml`; Python dependencies live in `pyproject.toml`
and `uv.lock`. After changing dependencies, run `uv lock` and commit both files.
Use `uv run` for Python commands and `mise run` for repository tasks. Docker must be
running for container, PostgreSQL integration, and kind tests.

Run `mise tasks` to list commands. Tasks are defined in `mise.toml`; script tasks
reuse the files in `scripts/`. Arguments follow `--`, for example
`mise run test -- -k deployment` or `mise run local:seed -- config/repositories.yaml`.
The seed command also accepts `EXPLORER_SEED_CATALOGUE` when no path is supplied.

`mise run test` runs offline tests. `mise run test:integration` creates an isolated Compose
project with a generated password and disposable database volume, runs the real
PostgreSQL/MCP integration tests, and removes the project and volume on exit.
Its default host port is 55433; override `EXPLORER_LOCAL_DATABASE_PORT` if needed.
The script never uses an existing database URL. Do not point integration tests
at an installed service: they create and delete test data.

For persistent local development:

```sh
export EXPLORER_LOCAL_DATABASE_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
docker compose up -d --wait postgres
export EXPLORER_DATABASE_URL="postgresql://explorer:${EXPLORER_LOCAL_DATABASE_PASSWORD}@127.0.0.1:55432/explorer"
export EXPLORER_CORPUS_DIR=.data/corpus
uv run k8s-explorer init-db
uv run k8s-explorer sync --catalogue config/repositories.yaml --limit 3
uv run k8s-explorer tools
uv run k8s-explorer call structured_repositories --args '{"limit": 5}'
mise run dev
```

Keep the generated password in the same shell while using Compose. Reusing a
database volume with a different password does not change the stored password;
remove a disposable volume with `docker compose down --volumes` to start fresh.
Persistent local data and the dedicated local kubeconfig belong under `.data/`
and are excluded from Git.

The HTTP MCP endpoint is `http://127.0.0.1:8000/mcp`. Health checks are
`/health/live` and `/health/ready`. `k8s-explorer stdio` exposes the same tools
without HTTP. Configure bearer authentication with `EXPLORER_API_TOKEN` when
exposing HTTP beyond localhost.

Semantic retrieval requires a real configured embedding provider. Set
`EXPLORER_EMBEDDING_URL` to its OpenAI-compatible embeddings endpoint,
`EXPLORER_EMBEDDING_MODEL`, and `EXPLORER_EMBEDDING_DIMENSIONS`; supply
`EXPLORER_EMBEDDING_API_KEY` through the environment if needed. Configure the
provider before syncing. Without it, the other retrieval families still work
and semantic tools report their unavailable state. Synthetic embeddings are
not a substitute for an embedding model.

`EXPLORER_GITHUB_TOKEN` optionally authenticates repository discovery and metadata
refresh. Keep tokens in the environment or Kubernetes Secrets. Production fetches
accept HTTPS GitHub/GitLab URLs; `EXPLORER_ALLOWED_HOSTS` can configure other Git
providers. Provider tokens currently authenticate metadata and embeddings, not
private Git cloning.

Source budgets default to 1MB per file, 512MiB of Git storage per repository,
20,000 files and 16MiB of decoded source per repository/indexing pass. Tune
`EXPLORER_MAX_FILE_BYTES`, `EXPLORER_MAX_REPO_BYTES`, `EXPLORER_MAX_REPO_FILES`, and
`EXPLORER_MAX_REPO_SOURCE_BYTES` after measuring memory/storage. Exclusions are
recorded in scan coverage. `EXPLORER_FETCH_WORKERS` defaults to four and
`EXPLORER_QUERY_WORKERS` to eight. The service exposes only the latest completed
scan per repository. Publishing a replacement retires superseded database rows
and source refs; a failed scan preserves the previous published scan. Cleanup
failures may temporarily retain physical files without exposing old scans to
queries. Active content-hash embedding cache entries remain reusable.
Allow storage headroom for the previous scan plus a fetched candidate and Git
repacking. Latest-only retention does not eliminate temporary overlap during
replacement; a repository at its disk cap can require a larger configured cap
before ingestion can advance.

Managed fetch timeouts clean newly created Git locks and unfinished object files
after terminating the fetch process group. A forced worker or pod termination
can bypass that cleanup. The next worker preserves pre-existing files, so stale
Git locks may require manual recovery:

1. Stop all corpus writers and verify their processes and old pods have exited.
   Back up the corpus before changing it.
2. Inspect the affected `repos/<repo-id>.git` directory. Remove only confirmed
   stale fetch locks (`shallow.lock`, `FETCH_HEAD.lock`, `packed-refs.lock`, or
   `refs/explorer/upstream.lock`) and unfinished `objects/pack/tmp_pack_*` or
   `objects/<two-hex-digits>/tmp_obj_*` files. Preserve completed objects/packs,
   `shallow`, the current published source ref, and metadata. Do not delete
   arbitrary lock files.
3. Restart the worker and rerun ingestion; the previous published scan remains
   available until a replacement completes successfully.

To validate a real model after choosing one, set `EXPLORER_TEST_EMBEDDING_URL`,
`EXPLORER_TEST_EMBEDDING_MODEL`, and `EXPLORER_TEST_EMBEDDING_DIMENSIONS` before
`mise run test:integration`; use `EXPLORER_TEST_EMBEDDING_API_KEY` when required. The
ordinary integration suite validates pgvector using explicit numeric fixtures;
the real-model test remains skipped until configured.

`mise run check` also validates the agent-explorer workflow with mise-pinned
actionlint, including the gated multi-platform GHCR publish job.

`mise run deploy:check` renders the local manifests and reusable Helm/Flux packaging
and checks deployment safety invariants. `mise run deploy:schemas` additionally
validates the local and rendered Helm resources against upstream schemas. Helm and
Kustomize versions are pinned in mise. The app-template values at
`deploy/helm/app-template/values.yaml` accept installation overlays; the optional
Flux resources live at `deploy/helm/flux/`; render the wrapper with
`kustomize build deploy/helm`. See [deployment](deployment.md) for
external PostgreSQL preparation, credential Secret keys, image overrides, and
cluster-specific customization.
