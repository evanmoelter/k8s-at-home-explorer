import json

import httpx
import pytest

from k8s_explorer.embeddings import EmbeddingError, HTTPEmbeddingProvider


def install_transport(monkeypatch, payload, status=200, requests=None):
    original = httpx.Client

    def handler(request):
        if requests is not None:
            requests.append(request)
        return httpx.Response(status, content=json.dumps(payload).encode())

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=transport, **kwargs))


@pytest.mark.parametrize("protocol", ["openai", "voyage"])
def test_response_order_and_validation(monkeypatch, protocol):
    # Protocol fixtures exercise validation; these vectors are never used as semantic search results.
    install_transport(
        monkeypatch, {"data": [{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}]}
    )
    provider = HTTPEmbeddingProvider("http://localhost:9999/embed", "test", 2, protocol=protocol)
    assert provider.embed(["one", "two"]) == [[1.0, 0.0], [0.0, 1.0]]


@pytest.mark.parametrize(
    "data",
    [
        [],
        [{"index": 0, "embedding": [0, 0]}],
        [{"index": 0, "embedding": [float("nan"), 1]}],
        [{"index": 0, "embedding": [float("inf"), 1]}],
        [{"index": 0, "embedding": [True, 1]}],
        [{"index": 0, "embedding": [1]}],
        [{"index": 1, "embedding": [1, 0]}],
    ],
)
def test_invalid_response(monkeypatch, data):
    install_transport(monkeypatch, {"data": data})
    with pytest.raises(EmbeddingError):
        HTTPEmbeddingProvider("http://localhost:9999/embed", "test", 2).embed(["one"])


def test_credentials_do_not_leak(monkeypatch):
    install_transport(monkeypatch, {"error": "secret-token"}, status=401)
    with pytest.raises(EmbeddingError) as error:
        HTTPEmbeddingProvider("http://localhost/embed", "m", 2, "secret-token").embed(["one"])
    assert "secret-token" not in str(error.value)


def test_openai_defaults_alias_query_instruction_and_requested_dimensions(monkeypatch):
    requests = []
    install_transport(monkeypatch, {"data": [{"index": 0, "embedding": [1, 0]}]}, requests=requests)
    defaults = HTTPEmbeddingProvider("http://localhost/embed", "m", 2)
    assert defaults.embed(["document"]) == [[1.0, 0.0]]
    assert json.loads(requests[-1].content) == {"model": "m", "input": ["document"]}
    provider = HTTPEmbeddingProvider(
        "http://localhost/embed",
        "Qwen/example",
        2,
        requested_dimensions=2,
        query_instruction="Retrieve Kubernetes deployment patterns",
        document_prefix="passage: ",
    )
    provider.embed_documents(["app manifest"])
    assert json.loads(requests[-1].content) == {
        "model": "Qwen/example",
        "input": ["passage: app manifest"],
        "dimensions": 2,
    }
    assert provider.embed_query("backup postgres") == [1.0, 0.0]
    assert json.loads(requests[-1].content) == {
        "model": "Qwen/example",
        "input": ["Instruct: Retrieve Kubernetes deployment patterns\nQuery: backup postgres"],
        "dimensions": 2,
    }


def test_voyage_query_and_document_roles_usage_and_dimensions(monkeypatch):
    requests = []
    install_transport(
        monkeypatch,
        {"data": [{"index": 0, "embedding": [1, 0]}], "usage": {"total_tokens": 5}},
        requests=requests,
    )
    provider = HTTPEmbeddingProvider(
        "https://api.voyageai.com/v1/embeddings",
        "voyage-code-3",
        2,
        "private-token",
        protocol="voyage",
        requested_dimensions=2,
        model_revision="evaluation-version",
    )
    provider.embed_documents(["manifest"])
    provider.embed_query("question")
    document = json.loads(requests[0].content)
    query = json.loads(requests[1].content)
    assert document == {
        "model": "voyage-code-3",
        "input": ["manifest"],
        "input_type": "document",
        "truncation": False,
        "output_dtype": "float",
        "output_dimension": 2,
    }
    assert query["input_type"] == "query" and query["input"] == ["question"]
    assert requests[0].headers["Authorization"] == "Bearer private-token"
    stats = provider.usage_stats
    assert stats["requests"] == stats["successful_requests"] == 2
    assert stats["document_requests"] == stats["query_requests"] == 1
    assert stats["reported_total_tokens"] == 10 and stats["input_texts"] == 2
    assert stats["token_usage_reports"] == 2
    assert "private-token" not in json.dumps(provider.metadata)


def test_tei_native_matrix_and_prompt_names(monkeypatch):
    requests = []
    payload = [[1, 0], [0, 1]]
    install_transport(monkeypatch, payload, requests=requests)
    provider = HTTPEmbeddingProvider(
        "http://localhost:8080/embed",
        "Qwen/example",
        2,
        protocol="tei",
        requested_dimensions=2,
        query_prompt_name="query",
        document_prompt_name="passage",
    )
    assert provider.embed_documents(["one", "two"]) == [[1.0, 0.0], [0.0, 1.0]]
    assert json.loads(requests[-1].content) == {
        "inputs": ["one", "two"],
        "truncate": False,
        "prompt_name": "passage",
        "dimensions": 2,
    }
    assert provider.usage_stats["token_usage_reports"] == 0
    payload[:] = [[1, 0]]
    assert provider.embed_query("question") == [1.0, 0.0]
    assert json.loads(requests[-1].content) == {
        "inputs": ["question"],
        "truncate": False,
        "prompt_name": "query",
        "dimensions": 2,
    }


@pytest.mark.parametrize("payload", [[], [1, 2], [[0, 0]], [[True, 1]], [[1]], {"data": []}])
def test_tei_invalid_native_responses(monkeypatch, payload):
    install_transport(monkeypatch, payload)
    provider = HTTPEmbeddingProvider("http://localhost/embed", "m", 2, protocol="tei")
    with pytest.raises(EmbeddingError):
        provider.embed_query("question")


def test_cache_identity_tracks_safe_semantics_and_excludes_auth():
    baseline = HTTPEmbeddingProvider("http://LOCALHOST:80/embed", "m", 2, "first")
    rotated = HTTPEmbeddingProvider("http://localhost/embed", "m", 2, "second")
    assert baseline.cache_key == rotated.cache_key
    assert baseline.metadata["chunking_version"] == "resource-v2-8000b"
    for changed in (
        {"protocol": "voyage"},
        {"protocol": "tei"},
        {"requested_dimensions": 2},
        {"model_revision": "new"},
        {"query_instruction": "Retrieve manifests"},
        {"document_prefix": "passage: "},
        {"query_prefix": "query: "},
        {"protocol": "tei", "query_prompt_name": "query"},
        {"protocol": "tei", "document_prompt_name": "passage"},
    ):
        assert (
            HTTPEmbeddingProvider("http://localhost/embed", "m", 2, **changed).cache_key != baseline.cache_key
        )
    assert "first" not in json.dumps(baseline.metadata)
    metadata = baseline.metadata
    metadata["model"] = "changed"
    assert baseline.metadata["model"] == "m"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"protocol": "unknown"},
        {"requested_dimensions": 3},
        {"protocol": "openai", "query_prompt_name": "query"},
        {"protocol": "tei", "query_prompt_name": "query", "query_instruction": "Retrieve"},
        {"protocol": "tei", "query_prompt_name": "query", "query_prefix": "query: "},
        {"protocol": "tei", "document_prompt_name": "passage", "document_prefix": "passage: "},
        {"query_instruction": " "},
    ],
)
def test_invalid_adapter_configuration(kwargs):
    with pytest.raises(ValueError):
        HTTPEmbeddingProvider("http://localhost/embed", "m", 2, **kwargs)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@localhost/embed",
        "https://localhost/embed?token=secret",
        "https://localhost/embed#secret",
        "file:///tmp/embed",
        "http:///embed",
    ],
)
def test_url_credentials_never_enter_metadata(url):
    with pytest.raises(ValueError) as error:
        HTTPEmbeddingProvider(url, "m", 2)
    assert "secret" not in str(error.value)


def test_preprocessed_budget_rejected_without_request_and_failures_count(monkeypatch):
    requests = []
    install_transport(monkeypatch, {"error": "private"}, status=401, requests=requests)
    provider = HTTPEmbeddingProvider("http://localhost/embed", "m", 2, "private", document_prefix="x" * 8192)
    assert provider.embed_documents([]) == []
    with pytest.raises(EmbeddingError, match="preprocessing"):
        provider.embed_documents(["x" * 995000])
    assert requests == [] and provider.usage_stats["requests"] == 0
    with pytest.raises(EmbeddingError) as error:
        provider.embed_documents(["manifest"])
    assert "private" not in str(error.value)
    assert provider.usage_stats["failed_requests"] == 1


def test_tei_qwen_client_instruction_preserves_plain_documents(monkeypatch):
    requests = []
    install_transport(monkeypatch, [[1, 0]], requests=requests)
    provider = HTTPEmbeddingProvider(
        "http://localhost/embed",
        "Qwen/example",
        2,
        protocol="tei",
        query_instruction="Retrieve deployment manifests",
    )
    provider.embed_documents(["kind: Deployment"])
    assert json.loads(requests[-1].content) == {"inputs": ["kind: Deployment"], "truncate": False}
    provider.embed_query("database backups")
    assert json.loads(requests[-1].content) == {
        "inputs": ["Instruct: Retrieve deployment manifests\nQuery: database backups"],
        "truncate": False,
    }


@pytest.mark.parametrize("reported", [True, -1, 1.5, 2**63, float("nan")])
def test_invalid_reported_usage_is_not_counted(monkeypatch, reported):
    install_transport(
        monkeypatch,
        {
            "data": [{"index": 0, "embedding": [1, 0]}],
            "usage": {"total_tokens": reported},
        },
    )
    provider = HTTPEmbeddingProvider("http://localhost/embed", "m", 2)
    assert provider.embed_query("query") == [1.0, 0.0]
    assert provider.usage_stats["reported_total_tokens"] == 0


def test_duplicate_response_indexes_and_oversized_body_fail(monkeypatch):
    payload = {"data": [{"index": 0, "embedding": [1, 0]}, {"index": 0, "embedding": [0, 1]}]}
    install_transport(monkeypatch, payload)
    provider = HTTPEmbeddingProvider("http://localhost/embed", "m", 2)
    with pytest.raises(EmbeddingError):
        provider.embed_documents(["one", "two"])
    payload["extra"] = "x" * 16_000_001
    with pytest.raises(EmbeddingError):
        provider.embed_documents(["one", "two"])
    assert provider.usage_stats["failed_requests"] == 2


def test_transport_error_details_do_not_escape(monkeypatch):
    original = httpx.Client

    def failure(request):
        raise httpx.ReadTimeout("request contained private-token and private-query")

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original(
            transport=httpx.MockTransport(failure),
            **kwargs,
        ),
    )
    provider = HTTPEmbeddingProvider("http://localhost/embed", "m", 2, "private-token")
    with pytest.raises(EmbeddingError) as error:
        provider.embed_query("private-query")
    assert "private" not in str(error.value)


def test_reported_usage_preserved_when_vectors_fail_validation(monkeypatch):
    install_transport(
        monkeypatch,
        {
            "data": [{"index": 0, "embedding": [1]}],
            "usage": {"total_tokens": 3},
        },
    )
    provider = HTTPEmbeddingProvider("http://localhost/embed", "m", 2)
    with pytest.raises(EmbeddingError):
        provider.embed_query("question")
    assert provider.usage_stats["failed_requests"] == 1
    assert provider.usage_stats["reported_total_tokens"] == 3


def test_openai_batches_effective_utf8_bytes_and_preserves_vector_order(monkeypatch):
    original = httpx.Client
    requests = []

    def handler(request):
        texts = json.loads(request.content)["input"]
        requests.append(texts)
        # Reversed local indexes exercise ordering within each separate response.
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [int(text[-1]), 1]}
                    for index, text in reversed(list(enumerate(texts)))
                ],
                "usage": {"total_tokens": len(texts)},
            },
        )

    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)
    )
    provider = HTTPEmbeddingProvider("http://localhost/embed", "m", 2, document_prefix="prefix: ")
    # Each raw input is 100,000 UTF-8 bytes. Prefixes force groups of two, not three.
    inputs = ["é" * 49999 + "x" + str(index) for index in range(6)]
    assert provider.embed_documents(inputs) == [[float(index), 1.0] for index in range(6)]
    assert [len(batch) for batch in requests] == [2, 2, 2]
    assert [text for batch in requests for text in batch] == ["prefix: " + text for text in inputs]
    assert all(sum(len(text.encode()) for text in batch) <= 300_000 for batch in requests)
    stats = provider.usage_stats
    assert stats["requests"] == stats["successful_requests"] == 3
    assert stats["input_texts"] == stats["reported_total_tokens"] == 6
    assert stats["input_bytes"] == sum(len(text.encode()) for batch in requests for text in batch)


def test_openai_rejects_unbatchable_effective_input_before_any_request(monkeypatch):
    requests = []
    install_transport(monkeypatch, {}, requests=requests)
    provider = HTTPEmbeddingProvider("http://localhost/embed", "m", 2, document_prefix="prefix: ")
    with pytest.raises(EmbeddingError, match="request byte budget"):
        provider.embed_documents(["short", "x" * 300_000])
    assert requests == [] and provider.usage_stats["requests"] == 0


def test_openai_batch_failure_does_not_return_partial_vectors(monkeypatch):
    original = httpx.Client
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 2:
            return httpx.Response(400, json={"error": "private payload"})
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0]}]})

    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)
    )
    provider = HTTPEmbeddingProvider("http://localhost/embed", "m", 2)
    with pytest.raises(EmbeddingError, match="invalid response"):
        provider.embed_documents(["x" * 200_000, "y" * 200_000, "z" * 200_000])
    assert len(requests) == 2
    assert provider.usage_stats["successful_requests"] == provider.usage_stats["failed_requests"] == 1


@pytest.mark.parametrize("timeout", [0, 1801, float("nan"), float("inf"), True, "300"])
def test_request_timeout_validation(timeout):
    with pytest.raises(ValueError, match="timeout"):
        HTTPEmbeddingProvider("http://localhost/embed", "m", 2, request_timeout_seconds=timeout)


@pytest.mark.parametrize("timeout,connect", [(1, 1), (300, 10), (1800, 10)])
def test_request_timeout_transport_metadata_and_cache_identity(monkeypatch, timeout, connect):
    original = httpx.Client
    client_options = []

    def client(**kwargs):
        client_options.append(kwargs)
        return original(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0]}]})
            ),
            **kwargs,
        )

    monkeypatch.setattr(httpx, "Client", client)
    provider = HTTPEmbeddingProvider("http://localhost/embed", "m", 2, request_timeout_seconds=timeout)
    baseline = HTTPEmbeddingProvider("http://localhost/embed", "m", 2)
    assert baseline.metadata["request_timeout_seconds"] == 60
    assert provider.metadata["request_timeout_seconds"] == timeout
    assert baseline.cache_key == provider.cache_key
    assert provider.embed_query("question") == [1.0, 0.0]
    configured = client_options[0]["timeout"]
    assert configured.read == configured.write == configured.pool == timeout
    assert configured.connect == connect
