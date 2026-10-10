"""Bounded embedding protocols with explicit query/document preprocessing."""

import hashlib
import json
import math
import threading
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Literal

import httpx


class EmbeddingError(ValueError):
    def __init__(self, message: str, *, retryable: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def retry_after_seconds(value: str | None) -> float | None:
    """Parse HTTP Retry-After without returning untrusted header text in errors."""
    if not value or len(value) > 128:
        return None
    try:
        if value.strip().isdigit():
            return float(value.strip())
        deadline = parsedate_to_datetime(value)
        if deadline.tzinfo is None:
            return None
        return max(0.0, (deadline - datetime.now(UTC)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


class HTTPEmbeddingProvider:
    """Explicit adapters for OpenAI-compatible, Voyage and TEI native endpoints.

    A revision identifies the operator's deployed model; it does not change server
    weights. Configure the server revision separately. Secrets never enter identity.
    """

    def __init__(
        self,
        url: str,
        model: str,
        dimensions: int,
        api_key: str | None = None,
        *,
        protocol: Literal["openai", "voyage", "tei"] = "openai",
        requested_dimensions: int | None = None,
        model_revision: str | None = None,
        query_instruction: str | None = None,
        document_prefix: str = "",
        query_prefix: str = "",
        query_prompt_name: str | None = None,
        document_prompt_name: str | None = None,
        request_timeout_seconds: float = 60,
        max_retries: int = 2,
        retry_max_delay_seconds: float = 10,
    ):
        if type(dimensions) is not int or not 1 <= dimensions <= 16000:
            raise ValueError("Invalid embedding dimensions")
        if requested_dimensions is not None and (
            type(requested_dimensions) is not int or requested_dimensions != dimensions
        ):
            raise ValueError("Requested dimensions must match validated output dimensions")
        if protocol not in {"openai", "voyage", "tei"}:
            raise ValueError("Unsupported embedding protocol")
        if (
            type(request_timeout_seconds) not in (int, float)
            or not math.isfinite(request_timeout_seconds)
            or not 1 <= request_timeout_seconds <= 1800
        ):
            raise ValueError("Embedding request timeout must be 1..1800 seconds")
        if type(max_retries) is not int or not 0 <= max_retries <= 5:
            raise ValueError("Embedding max retries must be 0..5")
        if (
            type(retry_max_delay_seconds) not in (int, float)
            or not math.isfinite(retry_max_delay_seconds)
            or not 1 <= retry_max_delay_seconds <= 60
        ):
            raise ValueError("Embedding retry maximum delay must be 1..60 seconds")
        try:
            parsed = httpx.URL(url)
        except (httpx.InvalidURL, TypeError):
            raise ValueError("Invalid embedding URL") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.host
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Embedding URL requires HTTP(S) without credentials, query or fragment")
        if not isinstance(model, str) or not model.strip() or len(model) > 500:
            raise ValueError("Embedding model must contain 1..500 characters")
        if not isinstance(document_prefix, str) or not isinstance(query_prefix, str):
            raise ValueError("Embedding prefixes must be text")
        for label, value in (
            ("revision", model_revision),
            ("query instruction", query_instruction),
            ("document prefix", document_prefix),
            ("query prefix", query_prefix),
            ("query prompt", query_prompt_name),
            ("document prompt", document_prompt_name),
        ):
            if value is not None and (not isinstance(value, str) or len(value.encode()) > 8192):
                raise ValueError(f"Embedding {label} exceeds its text budget")
        if query_instruction is not None and not query_instruction.strip():
            raise ValueError("Query instruction must not be empty")
        if any(
            value is not None and not value.strip() for value in (query_prompt_name, document_prompt_name)
        ):
            raise ValueError("Embedding prompt names must not be empty")
        if protocol != "tei" and (query_prompt_name is not None or document_prompt_name is not None):
            raise ValueError("Native prompt names require the TEI protocol")
        if query_prompt_name is not None and (query_instruction is not None or query_prefix):
            raise ValueError("Configure either native query prompt or client query preprocessing")
        if document_prompt_name is not None and document_prefix:
            raise ValueError("Configure either native document prompt or client document prefix")
        self.url = str(parsed)
        self.model = model
        self.dimensions = dimensions
        self.api_key = api_key
        self.protocol = protocol
        self.requested_dimensions = requested_dimensions
        self.model_revision = model_revision
        self.query_instruction = query_instruction
        self.document_prefix = document_prefix
        self.query_prefix = query_prefix
        self.query_prompt_name = query_prompt_name
        self.document_prompt_name = document_prompt_name
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.max_retries = max_retries
        self.retry_max_delay_seconds = float(retry_max_delay_seconds)
        self._configuration = {
            "url": self.url,
            "model": model,
            "dimensions": dimensions,
            "protocol": protocol,
            "requested_dimensions": requested_dimensions,
            "model_revision": model_revision,
            "query_instruction": query_instruction,
            "document_prefix": document_prefix,
            "query_prefix": query_prefix,
            "query_prompt_name": query_prompt_name,
            "document_prompt_name": document_prompt_name,
            "adapter_version": "retrieval-v2",
            "chunking_version": "resource-v2-8000b",
        }
        self.cache_key = hashlib.sha256(
            json.dumps(self._configuration, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self._stats_lock = threading.Lock()
        self._stats = {
            "requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "document_requests": 0,
            "query_requests": 0,
            "input_texts": 0,
            "input_bytes": 0,
            "response_bytes": 0,
            "reported_total_tokens": 0,
            "token_usage_reports": 0,
            "elapsed_seconds": 0.0,
            "retries": 0,
            "retry_delay_seconds": 0.0,
        }

    @property
    def metadata(self) -> dict:
        return {
            **self._configuration,
            "provider_id": self.cache_key,
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_retries": self.max_retries,
            "retry_max_delay_seconds": self.retry_max_delay_seconds,
        }

    @property
    def usage_stats(self) -> dict:
        """Per-process request measurements; token counts exist only when reported."""
        with self._stats_lock:
            return {**self._stats, "elapsed_seconds": round(self._stats["elapsed_seconds"], 6)}

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Backward-compatible document embedding alias."""
        return self.embed_documents(texts)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "document")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "query")[0]

    def _prepare(self, texts: list[str], role: str) -> list[str]:
        if not isinstance(texts, list) or len(texts) > 64:
            raise EmbeddingError("Embedding input exceeds its batch budget")
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise EmbeddingError("Embedding inputs must be nonempty text")
        if sum(len(text.encode()) for text in texts) > 1_000_000:
            raise EmbeddingError("Embedding input exceeds its byte budget")
        if role == "document":
            prepared = [self.document_prefix + text for text in texts]
        else:
            prepared = [self.query_prefix + text for text in texts]
            if self.query_instruction is not None:
                # Publisher's TEI example uses this exact retrieval instruction format:
                # https://huggingface.co/Qwen/Qwen3-Embedding-0.6B#text-embeddings-inference-tei-usage
                prepared = [f"Instruct: {self.query_instruction}\nQuery: {text}" for text in prepared]
        if sum(len(text.encode()) for text in prepared) > 1_000_000:
            raise EmbeddingError("Embedding preprocessing exceeds its byte budget")
        return prepared

    def _payload(self, texts: list[str], role: str) -> dict:
        if self.protocol == "tei":
            # Native /embed returns a matrix; prompt_name selects a deployed model prompt.
            # https://github.com/huggingface/text-embeddings-inference/blob/main/router/src/http/types.rs
            payload = {"inputs": texts, "truncate": False}
            prompt = self.document_prompt_name if role == "document" else self.query_prompt_name
            if prompt is not None:
                payload["prompt_name"] = prompt
        else:
            payload = {"model": self.model, "input": texts}
            if self.protocol == "voyage":
                # https://docs.voyageai.com/reference/embeddings-api
                payload.update(input_type=role, truncation=False, output_dtype="float")
        if self.requested_dimensions is not None:
            key = "output_dimension" if self.protocol == "voyage" else "dimensions"
            payload[key] = self.requested_dimensions
        return payload

    def _validate(self, response, count: int) -> list[list[float]]:
        if self.protocol == "tei":
            if not isinstance(response, list) or len(response) != count:
                raise EmbeddingError("Embedding response count mismatch")
            ordered = response
        else:
            if not isinstance(response, dict):
                raise EmbeddingError("Embedding response has an invalid shape")
            data = response.get("data")
            if not isinstance(data, list) or len(data) != count:
                raise EmbeddingError("Embedding response count mismatch")
            ordered = [None] * count
            for item in data:
                if not isinstance(item, dict):
                    raise EmbeddingError("Invalid embedding response item")
                index, values = item.get("index"), item.get("embedding")
                if type(index) is not int or not 0 <= index < count or ordered[index] is not None:
                    raise EmbeddingError("Invalid embedding response index")
                ordered[index] = values
        result = []
        for values in ordered:
            if not isinstance(values, list) or len(values) != self.dimensions:
                raise EmbeddingError("Embedding dimension mismatch")
            if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
                raise EmbeddingError("Embedding values must be finite numbers")
            if not any(values):
                raise EmbeddingError("Zero embedding has no cosine similarity")
            result.append([float(value) for value in values])
        return result

    def _embed(self, texts: list[str], role: str) -> list[list[float]]:
        prepared = self._prepare(texts, role)
        if not prepared:
            return []
        batches = [prepared]
        if self.protocol == "openai":
            # UTF-8 bytes conservatively bound byte-BPE tokens without downloading a
            # tokenizer. OpenAI permits at most 300,000 aggregate tokens per request;
            # compatible servers may have different per-input context limits.
            # https://developers.openai.com/api/reference/resources/embeddings/methods/create
            if any(len(text.encode()) > 300_000 for text in prepared):
                raise EmbeddingError("Embedding input exceeds its request byte budget")
            batches, batch, size = [], [], 0
            for text in prepared:
                text_size = len(text.encode())
                if batch and size + text_size > 300_000:
                    batches.append(batch)
                    batch, size = [], 0
                batch.append(text)
                size += text_size
            batches.append(batch)
        result = []
        for batch in batches:
            result.extend(self._request_with_retries(batch, role))
        return result

    def _request_with_retries(self, prepared: list[str], role: str) -> list[list[float]]:
        for attempt in range(self.max_retries + 1):
            try:
                return self._request(prepared, role)
            except EmbeddingError as exc:
                if not exc.retryable or attempt == self.max_retries:
                    raise
                delay = exc.retry_after
                if delay is None:
                    delay = min(2.0**attempt, self.retry_max_delay_seconds)
                # Do not retry early when the provider asks for a longer pause.
                # The ingestion scheduler can resume it on the next sync.
                if delay > self.retry_max_delay_seconds:
                    raise
                time.sleep(delay)
                with self._stats_lock:
                    self._stats["retries"] += 1
                    self._stats["retry_delay_seconds"] += delay
        raise AssertionError("Unreachable retry state")

    def _request(self, prepared: list[str], role: str) -> list[list[float]]:
        payload = self._payload(prepared, role)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        size, tokens, successful, tokens_reported = 0, 0, False, False
        started = time.monotonic()
        try:
            # HTTPX applies inactivity timeouts, rather than a total request deadline.
            timeout = httpx.Timeout(
                self.request_timeout_seconds, connect=min(10, self.request_timeout_seconds)
            )
            with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                with client.stream("POST", self.url, json=payload, headers=headers) as response:
                    response.raise_for_status()
                    parts = []
                    for part in response.iter_bytes():
                        size += len(part)
                        if size > 16_000_000:
                            raise EmbeddingError("Embedding response exceeds its byte budget")
                        parts.append(part)
                    parsed = json.loads(b"".join(parts))
            if isinstance(parsed, dict) and isinstance(parsed.get("usage"), dict):
                reported = parsed["usage"].get("total_tokens")
                if type(reported) is int and 0 <= reported <= 2**63 - 1:
                    tokens = reported
                    tokens_reported = True
            vectors = self._validate(parsed, len(prepared))
            successful = True
            return vectors
        except httpx.HTTPStatusError as exc:
            raise EmbeddingError(
                "Embedding request failed or returned an invalid response",
                retryable=exc.response.status_code in {408, 429, 500, 502, 503, 504},
                retry_after=retry_after_seconds(exc.response.headers.get("retry-after")),
            ) from None
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            raise EmbeddingError(
                "Embedding request failed or returned an invalid response", retryable=True
            ) from None
        except (httpx.HTTPError, KeyError, TypeError, ValueError, OverflowError):
            # Never expose provider response bodies, transport URLs, texts or credentials.
            raise EmbeddingError("Embedding request failed or returned an invalid response") from None
        finally:
            with self._stats_lock:
                self._stats["requests"] += 1
                self._stats["successful_requests" if successful else "failed_requests"] += 1
                self._stats[f"{role}_requests"] += 1
                self._stats["input_texts"] += len(prepared)
                self._stats["input_bytes"] += sum(len(text.encode()) for text in prepared)
                self._stats["response_bytes"] += size
                self._stats["reported_total_tokens"] += tokens
                self._stats["token_usage_reports"] += int(tokens_reported)
                self._stats["elapsed_seconds"] += time.monotonic() - started
