# First corpus pilot

The 2026-10-04 pilot uses the three repositories in `config/repositories.yaml`.
It validates ingestion and composable queries against real source; it does not
measure semantic model quality or predict full-community capacity.

## Retained source

| Repository | Commit | Files | Resources | Edges | Chunks |
|---|---|---:|---:|---:|---:|
| bjw-s-labs/home-ops | `61409ad33e656336fb7efaf32b78babbf5837c4a` | 558 | 501 | 652 | 615 |
| evanmoelter/hcc | `97aa3f0244c0052d8518a5edba293109a11f1154` | 695 | 577 | 859 | 844 |
| onedr0p/home-ops | `cd3786d20782b35311106fdaefcdf1205d199772` | 477 | 493 | 612 | 561 |
| Total | | 1,730 | 1,571 | 2,123 | 2,020 |

Eleven exclusions are reported: eight files with unsupported/invalid YAML and
three symlinks/submodules. They are coverage exclusions, not repository sync
failures. The retained bare Git stores and metadata occupy approximately
0.87MiB on this machine for 2.01MiB of decoded source. These small configurations
do not establish a storage budget for larger repositories or update-time repacking.

## Query observations

| Service combination | Matching repositories |
|---|---|
| Cilium | onedr0p, bjw-s, HCC |
| Cilium + CNPG | onedr0p, HCC |
| Cilium + CNPG + VolSync | HCC |

HCC results preserve separate `kubernetes/apollo` and `kubernetes/main`
contexts. Presence filters require all requested services and the requested app,
when supplied, in a shared context. These results establish source presence,
not that an app integrates with every listed service.

The graph has 1,598 resolved explicit edges and 525 unresolved edges. Most
resolved edges express file inclusion and dependency references; this count
does not measure confirmed application integrations. Missing injected
namespaces, generated Secrets/Services, and substitutions remain unresolved.

Application aggregates exclude generic Kustomization/Component names. The
pilot exposed misleading counts for names such as `app`, which prompted that
correction. Exact app names still reflect release/chart identities: upstream
image names can differ and require image or text lookup.

## Performance observations

On the developer machine, a repeated three-repository refresh with per-file
Git subprocess reads took 46.5 seconds. Bounded batch reads reduced a subsequent
refresh of the same commits to 4.3 seconds, including metadata requests,
fetches, parsing, and publication. Batch source reads alone took 0.37 seconds
including file listings. Three example infrastructure queries took approximately
25–63ms each against localhost PostgreSQL.

These are single-run observations, with warm retained repositories and no
embedding provider. Benchmark a larger representative corpus before choosing
worker concurrency, memory limits, cleanup cadence, or approximate indexes.

## Validation

- Automated tests cover disposable Git updates and retention, parser coverage,
  reference resolution, transactional publication, vector SQL, cache/readiness
  invalidation, overlapping syncs, response bounds, and real MCP HTTP transport.
- The first-PR suite had 86 passing tests and one optional real embedding
  provider test skipped. Exact pgvector operations use explicitly seeded numeric
  fixtures; model relevance has not been assessed.
- First-PR review added pre-construction YAML node/byte limits, malformed-resource
  isolation, and a real interrupted-Git-fetch recovery regression. Re-parsing
  all retained pilot source with the bounded loader preserves the resource,
  edge, and chunk counts above.
- Latest-only regressions cover deleted source and resources, stale evidence IDs,
  queries overlapping refreshes, failed publication preserving the previous scan,
  shared embedding-cache entries, and retryable Git cleanup.
- A dedicated kind deployment ingests all three repos with no sync failures.
  A real MCP client verifies infrastructure filters and source reads matching
  the indexed commit.
- All 13 tools advertise output schemas and return structured results. Containers
  run as UID 568 with a read-only application filesystem; health probes pass.
- Local Kubernetes schemas validate 8/8 resources. The reusable app-template example
  is checked against the actual pinned upstream chart for containers, shared
  mounts, private Service, default-class PVC, and existing Secret references.
  Production installation still needs an image, database, credentials, and
  backup configuration managed by the installer.

## Next experiments

Expand to 20–30 varied repositories and record successful source-backed answers,
false integration claims, tool calls, returned bytes, and unresolved graph
references. Then compare real embedding models on the same queries and source
snapshots. Use those measurements to prioritize namespace/context resolution,
application aliases, discovery ranking, and combined workflows.

## Benchmark preparation

The [embedding evaluation guide](embedding-evaluation.md) now documents export,
calibration rebinding, provider configuration, costs, and isolated benchmark runs.
The [CPU serving guide](embedding-serving.md) is the handoff to the HCC agent.

Re-parsing the retained pilot commits above with shared 8,000-byte chunking produces
2,053 source-verified chunks, with 1,841 unique document texts. The 50-question
calibration set has 45 sparse positive judgments and five inspected negative
passages; 15 questions include explicit kind predicates for the structured baseline.
All judged passages remained unchanged under the new chunking. Local OpenAI
tokenization measures 508,320 document tokens plus 800 query tokens, with a largest
chunk of 5,513 tokens. A lexical-only run verifies the harness; it does not select
an embedding provider or establish final retrieval quality. Corpus artifacts and
reports remain local under ignored `.data/evaluation/`.

Preparation checks pass: 151 offline tests and 25 disposable PostgreSQL integration
tests, with one optional real-provider test skipped. Lint, all three workflow
checks, and 21 rendered Kubernetes resources pass. The isolated benchmark task
also completed lexical calibration and removed its temporary database.
These checks validate protocols, provenance, retrieval mechanics, and packaging;
real provider relevance and CPU serving behavior await the HCC run.
