# Serve the self-hosted embedding candidates

The reusable [TEI values](../deploy/embeddings/tei/) prepare Qwen3-Embedding-0.6B
and BGE-M3 for a Linux x86_64 CPU node. They do not deploy anything by themselves.
An agent operating HCC should adapt them through that repository's own instructions
and Flux workflow, then return the endpoint handoff below. Cluster names, storage
classes, node placement, and routes belong in the deployment repository.

## What is pinned and verified

Both examples use bjw-s-labs `app-template` **5.2.1** and TEI **cpu-1.9.4**, pinned
to the Linux amd64 manifest:

```text
ghcr.io/huggingface/text-embeddings-inference:cpu-1.9.4@sha256:8419f533857b503ebf6ec292a95d4f1cf9c0464ac8b8abeef39518cf110e5726
```

The public registry manifest and image configuration were inspected on 2026-10-04.
The image reports source revision `e80ef225ed0e6cb1717ce632a6a84b6cf211bb67`.
The [official CPU guide](https://huggingface.co/docs/text-embeddings-inference/en/local_cpu)
explicitly uses Qwen3-Embedding-0.6B. The
[supported-model and hardware list](https://huggingface.co/docs/text-embeddings-inference/en/supported_models)
includes Qwen3, XLM-RoBERTa, and the x86_64 CPU image. The
[Intel guide](https://huggingface.co/docs/text-embeddings-inference/en/intel_container)
also provides an IPEX CPU variant. This preparation uses the versioned CPU image;
changing backend, precision, or thread settings creates a separate serving experiment.

| Provider ID | Model | Weight revision | Pooling | Dense dimensions |
|---|---|---|---|---|
| `qwen3-0.6b` | `Qwen/Qwen3-Embedding-0.6B` | `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3` | Last token | 1,024 |
| `bge-m3` | `BAAI/bge-m3` | `5617a9f61b028005a4858fdac845db406aefb181` | CLS | 1,024 |

These revisions match [providers.yaml](../config/evaluation/providers.yaml).
Pooling matches the publishers' pinned
[Qwen configuration](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B/blob/97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3/1_Pooling/config.json)
and [BGE configuration](https://huggingface.co/BAAI/bge-m3/blob/5617a9f61b028005a4858fdac845db406aefb181/1_Pooling/config.json).
The benchmark client adds Qwen's instruction to queries; the server applies no
default prompt. BGE's sparse and multi-vector modes are outside this dense comparison.

Helm rendering and Kubernetes schemas were checked locally. **No model weights were
downloaded, no serving pod was started, and CPU inference, memory use, and startup
time remain untested.** Runtime verification belongs to the serving agent.

## Render or install through the deployment owner

Render each model with the shared file followed by its model file:

```sh
mise exec -- helm template qwen3-0-6b \
  oci://ghcr.io/bjw-s-labs/helm/app-template --version 5.2.1 \
  --namespace embedding-eval \
  -f deploy/embeddings/tei/cpu-values.yaml \
  -f deploy/embeddings/tei/qwen3-0.6b-values.yaml > /tmp/qwen3.yaml
mise exec -- helm template bge-m3 \
  oci://ghcr.io/bjw-s-labs/helm/app-template --version 5.2.1 \
  --namespace embedding-eval \
  -f deploy/embeddings/tei/cpu-values.yaml \
  -f deploy/embeddings/tei/bge-m3-values.yaml > /tmp/bge-m3.yaml
mise exec -- kubeconform -kubernetes-version 1.35.0 -strict -summary \
  /tmp/qwen3.yaml /tmp/bge-m3.yaml
```

For an installation already authorized by its operator, the same values work with
`helm upgrade --install RELEASE oci://ghcr.io/bjw-s-labs/helm/app-template
--version 5.2.1 --kube-context "$SERVING_CONTEXT" --namespace "$SERVING_NAMESPACE"
--create-namespace -f SHARED_VALUES -f MODEL_VALUES -f SITE_OVERRIDES`.
For HCC, express those values in its Flux app instead of issuing an ad hoc Helm
installation. Always select the context explicitly.

Each release creates one Deployment, a private ClusterIP Service on **8080**, and
its own retained 10Gi RWO model-cache PVC using the cluster's default storage class.
Override `persistence.cache.storageClass` or use an existing claim as appropriate.
The pod runs as UID/GID 568 with a read-only root filesystem; `/data` and `/tmp` are
writable. Confirm the storage driver honors `fsGroup` and cache ownership. Initial
startup needs outbound access to Hugging Face for the pinned public weights.
The 30-minute startup probe allowance is adjustable for download speed and CPU warmup.

The shared resource settings request 1 CPU and 3Gi memory, with ceilings of 2 CPUs
and 8Gi per model. These are starting settings, not measured requirements. Run the
models sequentially on a constrained NUC and keep headroom for its existing workloads.
Confirm an AVX2-capable CPU; the example overrides the image's AVX512 setting with
`MKL_ENABLE_INSTRUCTIONS=AVX2` and bounds MKL, OpenMP, Rayon, and tokenizer threads.
An `amd64` label alone does not establish CPU instruction compatibility.

`MAX_BATCH_TOKENS=8192` and `MAX_BATCH_REQUESTS=1` bound inference batches;
`MAX_CLIENT_BATCH_SIZE=32` and `MAX_CONCURRENT_REQUESTS=32` admit the benchmark's
client batches while inference processes one input at a time. Qwen's model-native
context is larger than this serving cap. TEI 1.9 requires `AUTO_TRUNCATE=true` to
start with that smaller cap. Every benchmark `/embed` request explicitly supplies
**`truncate:false`**, which overrides the default and rejects oversized inputs.
Requests that omit this field can be truncated. See the pinned
[startup limit calculation](https://github.com/huggingface/text-embeddings-inference/blob/e80ef225ed0e6cb1717ce632a6a84b6cf211bb67/router/src/lib.rs)
and [HTTP request handling](https://github.com/huggingface/text-embeddings-inference/blob/e80ef225ed0e6cb1717ce632a6a84b6cf211bb67/router/src/http/server.rs).

Before a run, tokenize **all prepared documents and queries** with each deployed
model, including source/path prefixes and Qwen query instructions. Verify their
lengths against `/info`'s actual `max_input_length`; byte lengths do not establish
token lengths. Reject the run if any input exceeds that limit. Raise the cap and
reassess memory, or revise the frozen corpus consistently for every provider.
The CPU backend warms up a maximum-length input, so increasing the cap can also
raise startup memory substantially. Do not silently truncate an evaluation input.

After the endpoints are available, run the token preflight against the exact frozen
artifacts that will be benchmarked:

```sh
mise run eval:token-check -- \
  --corpus .data/evaluation/corpus.json \
  --judgments .data/evaluation/judgments.json \
  --providers config/evaluation/providers.yaml \
  --provider qwen3-0.6b --provider bge-m3 \
  --output .data/evaluation/token-preflight.json
```

The script first validates artifact fingerprints and source-bound judgments, then
uses the benchmark adapter's exact document/query preparation. It fetches `/info`,
checks the declared model/revision, and calls `/tokenize` for every unique prepared
text with special tokens included. Duplicate chunks still count toward input totals
and violations retain every affected chunk/query ID. Reports include actual token
maxima, limits, and artifact hashes; any violation or failed check exits nonzero.
It refuses to overwrite an input artifact and rechecks all input byte hashes after
the HTTP calls, preventing a report from certifying artifacts changed during the check.
The check is serial, bounded to 50,000 inputs and 256MiB prepared text per provider,
with an 8MiB response cap. Only selected TEI endpoints receive text or resolve keys.
No embeddings or model downloads are requested. It requires client-side prompts and
no server default prompt; native prompt-name configurations are rejected. The pinned
[tokenizer API schema](https://github.com/huggingface/text-embeddings-inference/blob/e80ef225ed0e6cb1717ce632a6a84b6cf211bb67/router/src/http/types.rs)
returns complete token objects, whose count includes special tokens.

## Verify and hand off endpoints

By default there is no ingress or authentication. Restrict the Service to trusted
clients with the deployment repository's network policy. For a workstation runner,
use local-only forwards in separate terminals after the pods are ready:

```sh
mise exec -- kubectl --context "$SERVING_CONTEXT" -n "$SERVING_NAMESPACE" \
  port-forward --address 127.0.0.1 service/qwen3-0-6b 8081:8080
mise exec -- kubectl --context "$SERVING_CONTEXT" -n "$SERVING_NAMESPACE" \
  port-forward --address 127.0.0.1 service/bge-m3 8082:8080
```

These release names render Services named `qwen3-0-6b` and `bge-m3`. Verify the
rendered names when changing release names. The forwards match the provider
configuration's `http://127.0.0.1:8081/embed` and `http://127.0.0.1:8082/embed`.
For an in-cluster runner, use the private Service DNS names with port 8080 and
the `/embed` path in an untracked copy of the provider file.

Check each endpoint before sending the corpus:

```sh
curl --fail --silent --show-error http://127.0.0.1:8081/health
curl --fail --silent --show-error http://127.0.0.1:8081/info
curl --fail --silent --show-error http://127.0.0.1:8081/embed \
  -H 'Content-Type: application/json' \
  --data '{"inputs":["apiVersion: v1\nkind: Service\nmetadata:\n  name: example"],"truncate":false,"normalize":true}' \
  > /tmp/qwen3-smoke.json
```

Repeat on port 8082. Confirm one finite, nonzero 1,024-dimensional vector per
input and check `/info` model ID, SHA, dtype, pooling, and limits against the
declared configuration. Smoke-test a representative client batch as well.
Return no credentials or full request logs in the handoff. Supply:

- Provider ID, reachable `/embed` URL, and any forwarding commands needed by the runner.
- Deployment context/namespace/release, node CPU model and allocatable memory,
  image digest, weight revision, actual `/info`, precision, pooling, normalization,
  thread and batch limits, and readiness/smoke/tokenizer-check results.
- An environment variable name for credentials if an external authentication
  proxy is required; provide its value through the existing secret channel.

Use an authenticated proxy for access beyond the trusted private network.
Do not place tokens in URLs, tracked values, command arguments, or artifacts.
The pinned TEI router logs its argument configuration and does not mark `api_key`
as redacted in its [argument definition](https://github.com/huggingface/text-embeddings-inference/blob/e80ef225ed0e6cb1717ce632a6a84b6cf211bb67/router/src/main.rs).
The examples therefore leave native `API_KEY` unset; re-audit before relying on
native server authentication with production credentials.

## Measure serving resources separately from relevance

Keep serving telemetry beside the frozen corpus, judgments, and benchmark report.
Collect cold download/startup time separately from a restart with cached weights;
record cache/PVC usage. During idle, indexing, and warmed queries collect memory
working set/RSS and peak, CPU usage and throttling, OOM/restarts, request failures,
queue time, inference time, request throughput, and p50/p95 latency. Sample
Kubernetes/container metrics through the serving repository's monitoring stack;
`kubectl top pod --containers` alone does not capture peaks or throttling.

TEI exposes `/metrics` on the HTTP Service. Capture counters and histograms before
and after each bounded run, and retain the interval and request count. Record
hardware, competing workloads, CPU limits, batch size, and network/forwarding
overhead. Run the same frozen inputs and cache policy for both candidates.
Keep CPU tuning or an IPEX variant in separate reports; do not attribute hardware
or batching changes to model relevance. See the
[benchmark instructions](embedding-evaluation.md) for the retrieval experiment.
