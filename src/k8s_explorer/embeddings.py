"""Bounded OpenAI-compatible HTTP embeddings, without logging credentials."""

import hashlib
import math

import httpx


class EmbeddingError(ValueError):
    pass


class HTTPEmbeddingProvider:
    def __init__(self, url: str, model: str, dimensions: int, api_key: str | None = None):
        if dimensions < 1 or dimensions > 16000:
            raise ValueError("Invalid embedding dimensions")
        parsed = httpx.URL(url)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            raise ValueError("Embedding URL must be HTTP(S) without credentials")
        self.url = url
        self.model = model
        self.dimensions = dimensions
        self.api_key = api_key
        self.cache_key = hashlib.sha256(f"{url}\0{model}\0{dimensions}\0resource-v1".encode()).hexdigest()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if len(texts) > 64 or sum(len(t.encode()) for t in texts) > 1_000_000:
            raise EmbeddingError("Embedding input exceeds batch budget")
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            with httpx.Client(timeout=httpx.Timeout(60, connect=10), follow_redirects=False) as client:
                with client.stream(
                    "POST", self.url, json={"model": self.model, "input": texts}, headers=headers
                ) as response:
                    response.raise_for_status()
                    parts, size = [], 0
                    for part in response.iter_bytes():
                        size += len(part)
                        if size > 16_000_000:
                            raise EmbeddingError("Embedding response exceeds budget")
                        parts.append(part)
                    import json

                    data = json.loads(b"".join(parts))["data"]
            if not isinstance(data, list) or len(data) != len(texts):
                raise EmbeddingError("Embedding response count mismatch")
            result = [None] * len(texts)
            for item in data:
                index, values = item["index"], item["embedding"]
                if type(index) is not int or not 0 <= index < len(texts) or result[index] is not None:
                    raise EmbeddingError("Invalid embedding response index")
                if not isinstance(values, list) or len(values) != self.dimensions:
                    raise EmbeddingError("Embedding dimension mismatch")
                if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
                    raise EmbeddingError("Embedding values must be finite numbers")
                if not any(values):
                    raise EmbeddingError("Zero embedding has no cosine similarity")
                result[index] = [float(v) for v in values]
            return result
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, EmbeddingError):
                raise
            raise EmbeddingError("Embedding request failed or returned an invalid response") from None
