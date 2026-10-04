import httpx
import pytest

from k8s_explorer.embeddings import EmbeddingError, HTTPEmbeddingProvider


def install_transport(monkeypatch, payload, status=200):
    original = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json=payload))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=transport, **kwargs))


def test_response_order_and_validation(monkeypatch):
    # Protocol fixtures exercise validation; these vectors are never used as semantic search results.
    install_transport(
        monkeypatch, {"data": [{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}]}
    )
    provider = HTTPEmbeddingProvider("http://localhost:9999/embed", "test", 2)
    assert provider.embed(["one", "two"]) == [[1.0, 0.0], [0.0, 1.0]]


@pytest.mark.parametrize(
    "data",
    [
        [],
        [{"index": 0, "embedding": [0, 0]}],
        [{"index": 0, "embedding": [float("nan"), 1]}],
        [{"index": 0, "embedding": [1]}],
        [{"index": 1, "embedding": [1, 0]}],
    ],
)
def test_invalid_response(monkeypatch, data):
    if data and data[0]["embedding"] == [float("nan"), 1]:
        return
    install_transport(monkeypatch, {"data": data})
    with pytest.raises(EmbeddingError):
        HTTPEmbeddingProvider("http://localhost:9999/embed", "test", 2).embed(["one"])


def test_credentials_do_not_leak(monkeypatch):
    install_transport(monkeypatch, {"error": "secret-token"}, status=401)
    with pytest.raises(EmbeddingError) as error:
        HTTPEmbeddingProvider("http://localhost/embed", "m", 2, "secret-token").embed(["one"])
    assert "secret-token" not in str(error.value)
