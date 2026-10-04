# Embedding provider evaluation

## Overview

The evaluation compares embedding providers on identical, source-backed questions and a frozen export of the latest successful scans. PostgreSQL/pgvector exact cosine search remains the dense baseline. Lexical retrieval, rank fusion, and reranking are measured as separate retrieval methods.

## Problem

The pilot has 2,020 chunks but has not measured semantic relevance. Kubernetes identifiers, YAML structure, and configuration context can change retrieval behavior. A useful comparison must distinguish model quality from preprocessing, source changes, duplicate patterns, and serving limits.

## Design

A read-only export records repository commits, extraction fingerprints, chunks, source ranges, and content hashes. Evaluation artifacts are separate from the service's latest-only tools. The runner validates the export and source-bound judgments before sending requests; benchmark artifacts retain evidence without adding historical production search.

Shared extraction now bounds chunks to 8,000 UTF-8 bytes, retaining complete small resources and splitting larger ones at source line boundaries. Oversized individual lines are explicit exclusions. This addresses a 9,778-token pilot chunk found through local tokenization; dense comparisons reject older oversized exports before provider calls. Provider-specific truncation would change the evidence under comparison and is disabled.

Each question records its category, repository filters, a pattern group, graded positive evidence, and explicit hard negatives. The initial pilot questions are calibration data with partial judgments. Final model selection uses 50–100 reviewed questions across 20–30 repositories. Shared configurations and paraphrases stay in the same split; inspect near-duplicates beyond exact content hashes. Pool candidates from all retrieval methods and judge them without model labels, reducing bias from incomplete positive lists.

The adapter separates document and query requests. Cache identity includes protocol, model, declared revision, output and requested dimensions, and preprocessing. Server revision declarations are operator attestations: they do not force an endpoint to serve particular weights. Hosted model aliases cannot guarantee immutable weights; record model names, access dates, and returned metadata, and rerun references when comparing across dates.

Provider preparation starts with these candidates; none is selected as the service default:

| Candidate | Initial configuration | Source |
|---|---|---|
| Qwen3-Embedding-0.6B | Dense 1,024 dimensions; task instruction on queries, plain documents; pin weights and serving revision. | [Publisher examples](https://github.com/QwenLM/Qwen3-Embedding), [model card](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) |
| BGE-M3 | Dense 1,024 dimensions; plain queries/documents. Sparse and multi-vector comparisons require a separate experiment. | [Model card](https://huggingface.co/BAAI/bge-m3) |
| Voyage voyage-code-4 | Dense 1,024 dimensions; native query/document input types. Add voyage-4 as a within-provider general retrieval comparison. | [Embedding guide](https://docs.voyageai.com/docs/embeddings) |
| OpenAI text-embedding-3-small | Native 1,536 dimensions; request reductions explicitly in separate runs. Add large if the comparison warrants it. | [Official OpenAI documentation](https://developers.openai.com/api/docs/guides/embeddings) |

TEI has documented Qwen3 support; serving compatibility, truncation, precision, pooling, token limits, hardware, and image revisions must be recorded with the actual endpoint. [TEI supported models](https://huggingface.co/docs/text-embeddings-inference/supported_models)

The operator selected CPU-first evaluation on NUC11i5 nodes. This repo prepares reusable serving examples and run instructions; the separate HCC agent deploys and runs them or supplies endpoints. Integrated graphics remain a possible later experiment. Paid calls are accepted in principle, with credentials and an explicit amount still required before execution.

The runner uses a disposable database with a fresh schema per provider run. It requires a distinct evaluation database setting and explicit confirmation before writing. It neither publishes scans nor marks a production provider ready. Lexical-only calibration runs require neither a provider nor a database. Dense vectors remain genuine provider outputs; synthetic protocol fixtures serve only automated correctness tests.

The lexical baseline preserves identifier tokens and records its tokenizer and scoring settings. Reciprocal rank fusion combines lexical and dense rankings in the harness with fixed candidate depth and fusion constant. Reranking consumes a saved, bounded candidate list, with its own revision, latency, and report; it does not alter the MCP tool families. HNSW, Qdrant, and model-generated sparse vectors remain separate experiments. Standard pgvector HNSW vector indexes have a 2,000-dimension limit; exact comparisons do not require that index. [pgvector documentation](https://github.com/pgvector/pgvector)

Reports include Recall@10, MRR@10, nDCG@10, query timings, index time, request/byte counts, and provider identity. Partial judgments make these judged-set metrics, not exhaustive corpus recall. Queries without positive judgments are reported separately. Source-backed answer success and false integration claims require generated answers plus reviewed claim labels; retrieval metrics cannot establish them. API cost and serving memory remain unmeasured until usage/pricing and server telemetry are supplied.

## Security

Credentials come from named environment variables and never enter artifacts. Corpus content remains untrusted reference data. Export and request budgets are bounded. Paid requests and model deployment wait for explicit endpoint, hardware, and spending decisions. Provider errors are sanitized. Tests and benchmark runs use disposable databases.

## Open questions

- What endpoints, CPU/RAM allocation, and measured serving behavior will the HCC agent provide?
- What hosted API budget and credentials are authorized for the first comparison?
- Which additional repositories and reviewed pattern groups form the final holdout?
- Does reranking improve source-backed answers enough to justify its latency?
