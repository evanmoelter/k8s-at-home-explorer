"""Native tokenizer protocol/preprocessing checks; no embedding quality claims."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from test_evaluation import artifacts  # noqa: F401 -- shared source-bound fixture

SPEC = importlib.util.spec_from_file_location(
    "check_embedding_inputs", Path(__file__).resolve().parents[1] / "scripts/check-embedding-inputs.py"
)
assert SPEC and SPEC.loader
CHECKS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKS)


@pytest.fixture
def token_setup(artifacts, tmp_path, monkeypatch):  # noqa: F811
    corpus, judgments, corpus_path, judgments_path = artifacts
    configuration = {
        "id": "local",
        "protocol": "tei",
        "url": "http://localhost:8081/proxy/embed",
        "model": "test-tokenizer",
        "model_revision": "abc123",
        "dimensions": 1024,
        "document_prefix": "Document: ",
        "query_instruction": "Find Kubernetes evidence",
        "api_key_env": "TOKENIZER_TEST_KEY",
    }
    monkeypatch.setenv("TOKENIZER_TEST_KEY", "private-fixture-token")
    providers = tmp_path / "providers.yaml"
    providers.write_text(json.dumps({"providers": [configuration]}))
    args = SimpleNamespace(
        corpus=corpus_path,
        judgments=judgments_path,
        providers=providers,
        provider=["local"],
        output=tmp_path / "token-report.json",
    )
    return corpus, judgments, configuration, args


def mock_endpoint(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(
        CHECKS.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)
    )


@pytest.mark.parametrize("limit, exit_code", [(2, 1), (3, 0)])
def test_every_input_uses_native_tokenizer_and_exact_preprocessing(
    token_setup, monkeypatch, capsys, limit, exit_code
):
    corpus, judgments, _, args = token_setup
    requests = []

    def handle(request):
        assert request.headers["Authorization"] == "Bearer private-fixture-token"
        if request.url.path == "/proxy/info":
            return httpx.Response(
                200, json={"model_id": "test-tokenizer", "model_sha": "abc123", "max_input_length": limit}
            )
        assert request.url.path == "/proxy/tokenize"
        payload = json.loads(request.content)
        assert payload["add_special_tokens"] is True
        requests.append(payload["inputs"])
        count = 3 if payload["inputs"].startswith("Instruct:") else 2
        return httpx.Response(200, json=[[{"id": i, "text": "t", "special": i == 0} for i in range(count)]])

    mock_endpoint(monkeypatch, handle)
    assert CHECKS.check(args) == exit_code
    expected = {"Document: " + chunk.content for repo in corpus.repositories for chunk in repo.chunks}
    expected |= {f"Instruct: Find Kubernetes evidence\nQuery: {q.text}" for q in judgments.queries}
    assert set(requests) == expected
    report = json.loads(args.output.read_text())
    result = report["providers"][0]
    assert result["max_document_tokens"] == 2 and result["max_query_tokens"] == 3
    expected_violations = {q.id for q in judgments.queries} if exit_code else set()
    assert {item["id"] for item in result["violations"]} == expected_violations
    assert result["document_inputs"] == sum(len(repo.chunks) for repo in corpus.repositories)
    assert "private-fixture-token" not in args.output.read_text() + capsys.readouterr().out


@pytest.mark.parametrize("failure", ["fingerprint", "non-tei", "native-prompt", "unknown-provider"])
def test_invalid_inputs_fail_before_http(token_setup, monkeypatch, failure):
    _, _, configuration, args = token_setup
    if failure == "fingerprint":
        corpus = json.loads(args.corpus.read_text())
        corpus["corpus_id"] = "wrong"
        args.corpus.write_text(json.dumps(corpus))
    elif failure == "non-tei":
        configuration["protocol"] = "openai"
    elif failure == "native-prompt":
        configuration.pop("query_instruction")
        configuration["query_prompt_name"] = "query"
    else:
        args.provider = ["missing"]
    args.providers.write_text(json.dumps({"providers": [configuration]}))
    mock_endpoint(monkeypatch, lambda request: pytest.fail("Invalid preparation made an HTTP request"))
    with pytest.raises(ValueError):
        CHECKS.check(args)


def test_bad_token_response_and_response_budget_are_rejected(monkeypatch):
    with pytest.raises(CHECKS.PreflightError):
        CHECKS.token_count({"tokens": [1, 2]})
    monkeypatch.setattr(CHECKS, "MAX_RESPONSE_BYTES", 4)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"12345"))
    ) as client:
        with pytest.raises(CHECKS.PreflightError):
            CHECKS.request_json(client, "GET", "http://localhost/info")


@pytest.mark.parametrize("input_name", ["corpus", "judgments", "providers"])
def test_output_cannot_overwrite_artifacts(token_setup, monkeypatch, input_name):
    _, _, _, args = token_setup
    args.output = getattr(args, input_name)
    original = args.output.read_bytes()
    mock_endpoint(monkeypatch, lambda request: pytest.fail("Unsafe output caused an HTTP request"))
    with pytest.raises(CHECKS.PreflightError, match="overwrite"):
        CHECKS.check(args)
    assert args.output.read_bytes() == original


@pytest.mark.parametrize("input_name", ["corpus", "judgments", "providers"])
def test_artifact_changes_during_requests_prevent_report(token_setup, monkeypatch, input_name):
    _, _, _, args = token_setup
    changed = False

    def handle(request):
        nonlocal changed
        if not changed:
            path = getattr(args, input_name)
            # Whitespace changes preserve schema/fingerprints but change exact file bytes.
            path.write_bytes(path.read_bytes() + b"\n")
            changed = True
        if request.url.path.endswith("/info"):
            return httpx.Response(
                200, json={"model_id": "test-tokenizer", "model_sha": "abc123", "max_input_length": 8192}
            )
        return httpx.Response(200, json=[[{"id": 1, "text": "test", "special": False}]])

    mock_endpoint(monkeypatch, handle)
    with pytest.raises(CHECKS.PreflightError, match="changed"):
        CHECKS.check(args)
    assert not args.output.exists()
