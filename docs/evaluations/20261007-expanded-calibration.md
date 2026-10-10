# Expanded embedding calibration, 2026-10-07

The hosted runs support carrying `voyage-4` dense retrieval and `voyage-code-4`
hybrid retrieval into broader evaluation. Neither is a selected service default.
CPU indexing was too slow for the intended initial deployment under the tested
allocation; the incomplete trials provide no model relevance comparison.

## Evidence and scope

Experiment `expanded-20261007-v1` ran in Apollo through the HCC GitOps workflow,
using application commit `f0d54ffdfb748a145a51f6187281bd2c3148cf98`.
The HCC agent exported artifacts; Codex independently validated the frozen corpus
and source-bound judgments and verified the three hosted report hashes against
their completion records. Every hosted report has the same corpus ID and judgment
hash. The [derived summary](expanded-20261007-v1.summary.json) records exact
metrics, fingerprints, token usage, timings, and diagnostic query IDs without
copying source text, credentials, or cluster configuration.

- Corpus: 20 repositories, 17,307 chunks, 15,027 unique document texts, and 959
  exact duplicate groups.
- Corpus ID: `5aec6cac449b1f32cd73108f7c9d904ec1d3dcae86bc659f401f51d4fb0e3315`.
- Judgment file SHA-256: `169e01bd486de352636b28ba633e40555fefec3ad502a016b54c6cda2be5aa66`.
- Questions: 50 calibration queries, 45 answerable, 36 pattern groups. Each
  answerable question has one judged positive passage.
- Labels cover three repositories: HCC (33 queries), onedr0p (11), and bjw-s (6).
  The additional 17 repositories expand retrieval candidates, not label coverage.
- Thirty-five questions have filters: 27 have repository filters and 15 have
  structured predicates, with overlap. The scores are not all corpus-wide searches.

All methods use the same candidate filters. Dense search uses exact pgvector
cosine similarity. BM25 includes path and content; dense embeddings include content
only. Hybrid uses RRF with constant 60 and candidate depth 100. That input-field
difference is part of the measured retrieval systems, rather than an isolated
model-only comparison.

Raw artifacts remain under the operator's exported experiment directory. Relative
report paths and their SHA-256 digests are in the summary; keep the frozen corpus,
reviewed calibration file, and runtime telemetry together when sharing the experiment.

## Hosted results

| Provider | Job duration, reported by HCC | Dense Recall@10 | Dense nDCG@10 | Hybrid Recall@10 | Hybrid nDCG@10 |
|---|---:|---:|---:|---:|---:|
| OpenAI text-embedding-3-small | 8m37s | 0.733 | 0.515 | 0.733 | 0.504 |
| voyage-code-4 | 6m26s | 0.644 | 0.417 | 0.822 | 0.560 |
| voyage-4 | 9m18s | 0.822 | 0.611 | 0.778 | 0.563 |

BM25: Recall@10 **0.667**, nDCG@10 **0.483**. The answerable query counts behind
recall are 30/45 for BM25, 33/45 for OpenAI dense and hybrid, 29/45 for Voyage code
dense, 37/45 for Voyage code hybrid and Voyage general dense, and 35/45 for Voyage
general hybrid. Five no-positive queries are excluded from relevance averages.

| Provider | Indexing time, harness | Dense mean / p95 query latency | Hybrid mean / p95 query latency |
|---|---:|---:|---:|
| OpenAI small | 482.7s | 374 / 728ms | 407 / 835ms |
| Voyage code | 354.5s | 249 / 413ms | 283 / 520ms |
| Voyage general | 527.1s | 261 / 410ms | 293 / 506ms |

Dense latency includes query embedding, transport, and exact database retrieval.
Hybrid latency is the sequential sum of lexical, dense, and fusion work. Job
durations also include work outside the indexing timer. These are single-run
observations; provider ordering, network conditions, and competing load were not
controlled through repeated trials.

Each hosted run made 952 document requests and 50 query requests, with all 1,002
successful and zero failed requests. Query plus document input counts total
15,077 texts per run. OpenAI output dimensions were 1,536; both Voyage models used
1,024 dimensions with explicit native query/document input types. Hosted aliases
have no immutable weight revision in these reports.

### Where retrieval differs

The two shortlisted methods both hit 37 questions: 33 are shared, with four unique
hits each. Their diagnostic coverage union is 41/45; this is **not** a tested
combined retrieval method or evidence that a routing policy achieves that recall.

| Only Voyage general dense hits | Only Voyage code hybrid hits |
|---|---|
| `pilot-011`: CNPG object-storage compression/retention | `pilot-015`: Mealie database Secret |
| `pilot-019`: External Secrets controller flags | `pilot-023`: Gateway HTTP-to-HTTPS redirect |
| `pilot-039`: Home Assistant MCP service URL | `pilot-037`: Plex route header identifier |
| `pilot-041`: Jellyfin container security | `pilot-045`: Envoy compression policy |

Fusion changes which questions succeed. OpenAI fusion gains five hits and loses
five; Voyage code gains nine and loses one; Voyage general gains one and loses
three. Keeping fusion configurable is justified by these observations. No
query-category routing rule is established by this small calibration set.

Sparse labels also produce source-specific misses with identical text. Voyage general dense and
hybrid both retrieve unjudged passages with the **same content hash** as positive
evidence on `pilot-031` (GatewayClass) and `pilot-045` (Envoy compression). Voyage
code dense/hybrid and OpenAI hybrid have that issue on `pilot-031`. Source IDs
differ, so the harness correctly applies the supplied source-specific judgments.
The summary lists those alternative chunk IDs for review. Published scores remain
unchanged; resolving exact duplicate labels is a separate judgment revision.

### Cost estimate

| Model | Reported tokens | Standard price per million tokens | Estimated list-price cost |
|---|---:|---:|---:|
| OpenAI small | 4,566,334 | $0.02 | $0.0913 |
| Voyage code | 4,705,885 | $0.12 | $0.5647 |
| Voyage general | 4,705,885 | $0.06 | $0.2824 |
| Total | | | $0.9384 |

Rates checked on 2026-10-07: [OpenAI pricing](https://developers.openai.com/api/docs/models/text-embedding-3-small)
and [Voyage pricing](https://docs.voyageai.com/docs/pricing). These estimates use
reported usage and ordinary synchronous list prices, before credits, discounts,
taxes, or additional attempts outside the completed reports. Actual account charges
were not verified. A fresh full Voyage general pass on this corpus is roughly
$0.28 before those adjustments; unchanged document-cache reuse can reduce later
service indexing work. Fresh disposable benchmark runs reindex their corpus.

## CPU findings

The HCC trials used a two-core allowance and 12Gi memory limit, with TEI 1.9.4,
float32 weights, and the pinned model revisions in the serving examples. Actual
batch settings differed from the bundled examples: Qwen's final attempt used
`max_batch_tokens=5760` and `max_batch_requests=4`; BGE used 8192 and 8. These are
measurements of those configurations, not a bound on optimized CPU/GPU serving.

- Qwen3-Embedding-0.6B was cancelled at operator request after approximately four
  hours, with 1,966/15,027 unique documents embedded (13.1%). No quality report.
  Earlier attempts and the final cancelled attempt are distinct artifacts.
- BGE-M3 completed 144 documents in a 600-second throughput trial. The window,
  including unfinished batch time, projects 17.3 hours for embeddings alone.
  It excludes database indexing and depends on the trial's byte-length strata.
  Ten query probes had median 203ms and p95 446ms embedding latency, without
  database retrieval. No quality report.
- BGE's sampled peak working set was 11.07Gi; sampled peak RSS was 10.06Gi.
  Fifteen-second samples do not establish an absolute peak. No restarts were
  observed in the measurement window; the exported OOM metric had no samples.

The CPU workloads were stopped and artifacts retained, as reported by the HCC
agent. This review made no cluster calls or deployments. Query latency alone does
not settle feasibility: these providers still need their own matching document
embeddings. Hosted document vectors cannot be paired with a different model's
CPU query vectors.

## Proposed next evaluation

Update 2026-10-10: the operator chose `voyage-4` dense retrieval for deployment
and deferred this evaluation plan. The recommendations below describe the
2026-10-07 review; they do not block shipping or reverse the provider choice.

Keep the current questions as calibration. Construct 50–100 new, source-backed
questions across at least eight additional repositories and several infrastructure
combinations; group shared lineage, near-duplicate patterns, and paraphrases before
assigning a holdout. Include new-app examples, CNPG/Cilium/Gateway/backup patterns,
identifier-heavy searches, and cases where co-presence does not prove integration.
Separate unconstrained searches from explicit infrastructure/repository filters.

Pool top candidates from BM25, Voyage general dense, Voyage code hybrid, and the
low-cost OpenAI reference. Have a reviewer judge source passages without method
labels, reviewing exact duplicate groups as well as similar configurations. Keep
these pooled calibration repairs separate from the independently constructed
holdout. A final holdout cannot be rebound automatically or tuned against.

Report query and pattern-group results, paired differences, uncertainty, and
performance across repeated comparable runs. Add reviewed source-backed answers
and integration-claim labels for complete agent workflows: chunk recall alone
does not evaluate choosing a repository with matching infrastructure or discovering
adjacent apps. Keep lexical, semantic, and graph tool families separate while
testing those workflows.

Defer reranking, additional models, GPU/IPEX tuning, approximate indexes, and
automatic fusion/routing defaults until broader labels identify a specific gap.
New paid runs require a separately explicit spending limit and executor credentials;
this analysis made no provider calls. The recommendation remains provisional and
does not change the service default.
