# Evaluate embedding providers

The preparation includes provider adapters, a source-verified corpus exporter,
graded judgments, BM25 and structured baselines, exact pgvector dense search,
and reciprocal rank fusion. The operator selected `voyage-4` dense retrieval for
the first working deployment on 2026-10-10; see the
[deployment profile](deployment.md#voyage-4-deployment-profile). Further quality
evaluation is deferred and does not block deployment.
The [expanded calibration findings](evaluations/20261007-expanded-calibration.md)
record the first completed hosted comparison and incomplete CPU trials, with
report fingerprints, costs, and a proposal for broader judgments.
See the [design](designs/20261004-embedding-evaluation.md) for the comparison and
the [pilot evidence](evaluation.md) for the original ingestion measurements.

## Prepare the corpus and questions

Use the service's current database and its matching retained corpus. Export reads
the database without publishing or changing indexes and verifies every chunk
against the exact Git source range. An older source directory paired with a newer
database fails verification.

```sh
mise run eval:export -- --output .data/evaluation/corpus.json
```

The export freezes one latest successful scan per repository, including commits,
extraction fingerprints, source ranges, content hashes, and exact duplicate groups.
The frozen artifact supports reproducible offline comparison after the live corpus
refreshes; it does not add history tools to the service. Keep it with reports and
judgments. It contains source text and is excluded from Git through `.data/`.

Rescan with the current extractor before export: chunks are bounded to 8,000 UTF-8
bytes, with full small resources retained and larger ones split at source lines.
Oversized individual lines are reported as exclusions. Dense runs reject older
oversized exports before any provider calls. Shared chunking keeps every provider
on the same evidence; actual model tokenizers and server limits still need checking.

`config/evaluation/pilot-judgments.json` has 50 calibration questions: 45 sparse
positive judgments and five explicit difficult negatives. Codex inspected their
source passages. These are a smoke/calibration set, with a mix of global and
repository-scoped questions, not a reviewed final holdout. Pattern groups keep
paraphrases and repeated configuration evidence together.

Rebind the starter only when its inspected passages are unchanged in a new export:

```sh
mise run eval:rebind -- \
  --corpus .data/evaluation/corpus.json \
  --judgments config/evaluation/pilot-judgments.json \
  --output .data/evaluation/judgments.json
mise run eval:validate -- \
  --corpus .data/evaluation/corpus.json \
  --judgments .data/evaluation/judgments.json \
  --providers config/evaluation/providers.yaml
```

Rebinding matches repository, path, line range, and content hash, then updates the
scan evidence and corpus fingerprint. Missing, ambiguous, or changed passages
require review; holdout judgments cannot be rebound automatically. A rebound
corpus is a different experiment, even if its judged passages have not changed.
If your corpus does not include these passages, create your own judgments using
the starter's schema and exported chunk IDs.

`config/evaluation/repositories.yaml` supplies 20 opt-in expansion candidates:
the pilot plus public topic matches ordered by stars, excluding unusually large
repositories by GitHub's reported size. Review repository purpose, infrastructure
diversity, and shared lineage before ingestion. It does not change the app's
default catalogue. To ingest it into a dedicated evaluation installation, run
`k8s-explorer sync --catalogue config/evaluation/repositories.yaml`; then export
the matching source and database. Prepare 50–100 independently reviewed questions
and separate calibration from holdout by pattern group and repository lineage.

## Run the baseline

```sh
mise run eval:lexical -- \
  --corpus .data/evaluation/corpus.json \
  --judgments .data/evaluation/judgments.json \
  --output .data/evaluation/lexical.json
```

This uses neither a database nor an embedding endpoint. BM25 scores chunk text
and source paths with identifier-preserving tokenization, `k1=1.2`, and `b=0.75`.
Optional app, service, kind, namespace, context, and repository filters apply to
the same candidate set across retrieval methods. The structured baseline ranks
only questions with explicit structured predicates; natural-language questions
are not automatically converted into filters. Its applicability is reported.

## Configure and compare providers

`config/evaluation/providers.yaml` prepares Qwen3-Embedding-0.6B, BGE-M3,
voyage-code-4, and text-embedding-3-small, plus voyage-4 for the proposed
within-provider comparison. Self-hosted URLs are placeholders. Configure each
endpoint, pin its serving image, and deploy the weight revision declared in the
file. The client cannot enforce server weights. Hosted aliases lack immutable
weight guarantees; keep run dates and metadata with reports.

The [CPU serving and HCC handoff guide](embedding-serving.md) supplies reusable
Kubernetes examples and the measurements to collect. The operator selected CPU
first on NUC11i5 nodes; the HCC agent owns deployment and can run this harness or
return endpoints. This repository does not deploy into the operator's cluster.

Provider keys come from `OPENAI_API_KEY` and `VOYAGE_API_KEY`; never put values in
YAML or artifacts. Only explicitly selected providers resolve credentials or
receive requests. Input truncation is disabled for native TEI and Voyage; an
oversized document fails rather than silently changing the source being compared.
Record actual tokenizer limits and serving precision, pooling, normalization,
hardware, and revision alongside each run.
`request_timeout_seconds` bounds HTTP inactivity; the TEI examples use 300 seconds
for CPU batches, while the default remains 60. It does not change cached vectors.

After endpoints and any paid API budget are explicitly authorized:

```sh
mise run eval:benchmark -- \
  --mode hybrid \
  --corpus .data/evaluation/corpus.json \
  --judgments .data/evaluation/judgments.json \
  --providers config/evaluation/providers.yaml \
  --provider qwen3-0.6b \
  --output .data/evaluation/qwen3.json
```

Repeat with each provider ID against identical frozen artifacts. The mise task
creates a disposable Compose database on localhost port 55434, enables pgvector,
and removes its generated volume and containers on exit. Set
`EXPLORER_EVAL_DATABASE_PORT` if that port is occupied. It never reads an existing
application database URL. Each provider uses an isolated schema and genuine
provider vectors with exact cosine retrieval. For an independently managed
disposable database, use `eval run` with `EXPLORER_EVAL_DATABASE_URL` and
`--confirm-disposable-database`; install pgvector first.

`--mode dense` compares dense retrieval with the baseline; `--mode hybrid` also
reports lexical+dense reciprocal rank fusion with candidate depth 100 and constant
60. `--verify-live-corpus` optionally checks the source database still matches the
export before and after a run. All runs check artifact fingerprints; model
comparison uses the frozen corpus even when a live worker refreshes later.

## Interpret the report

Reports record Recall@10, MRR@10, nDCG@10, retrieved negative/unjudged counts,
provider settings and usage, indexing time, and query latency. Unjudged passages
receive zero gain, so sparse judgments describe the judged set rather than
exhaustive corpus recall. Queries without positive judgments are excluded from
relevance averages and reported separately. A retrieved negative is not itself a
false integration claim: that metric requires an answer and reviewed claim labels.

For a stronger future comparison, pool candidate passages from all methods, judge them
without provider labels, review near-duplicates, and assess source-backed answers.
Measure API cost using reported usage and dated pricing; measure server memory and
latency with actual telemetry. Unavailable measurements remain unavailable.
Reranking is a separate follow-up using saved candidate lists and its own revision
and latency records. This preparation does not deploy a reranker, Qdrant, or an
approximate vector index.

## Hosted cost and budgets

Rates checked on 2026-10-04 for ordinary synchronous embedding calls:

| Provider/model | Input price per million tokens | Approximate pilot pass |
|---|---:|---:|
| OpenAI text-embedding-3-small | $0.02 | $0.01 |
| Voyage voyage-code-4 | $0.12 | $0.06–$0.12 |
| Voyage voyage-4 | $0.06 | $0.03–$0.06 |

Sources: [OpenAI model pricing](https://developers.openai.com/api/docs/models/text-embedding-3-small),
[Voyage pricing](https://docs.voyageai.com/docs/pricing).

Local `cl100k_base` tokenization of the corrected three-repository export measured
508,320 document tokens across 1,841 unique document texts plus 800 query tokens
for the 50 questions. The largest chunk has 5,513 tokens. Voyage uses
its own tokenizer, so its estimate assumes roughly 0.5–1 million tokens rather
than treating OpenAI counts as exact. These estimates assume one fresh indexing
pass per provider, omit account-specific free allowances, and exclude reranking,
answer generation, and serving infrastructure. Repeated questions cost little
relative to reindexing; each fresh benchmark database reindexes its corpus.

If 20–30 similar repositories scale roughly with the pilot, a pass through these
three hosted candidates should be on the order of $1–$2. That is a planning
estimate, not a corpus measurement. Start with a proposed $5 budget for OpenAI
and $5 for Voyage to cover several passes, then recalculate after exporting the
expanded corpus. The operator has approved paid calls in principle but has not
yet supplied a spending amount or credentials. The executor must receive both
before calls; this CLI does not enforce a dollar cap. Configure provider account
controls separately and retain reported usage with dated prices in the findings.
When providers omit token usage, cost remains unknown rather than reported as zero.
