"""Check every frozen evaluation input with the selected TEI server tokenizer."""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from k8s_explorer.evaluation import (
    FrozenCorpus,
    Judgments,
    Providers,
    _read_artifact,
    _write_json,
    validate_judgments,
)

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_INPUTS = 50000
MAX_PREPARED_BYTES = 256 * 1024 * 1024


class PreflightError(ValueError):
    pass


def request_json(client, method, url, payload=None):
    with client.stream(method, url, json=payload) as response:
        if response.status_code != 200:
            raise PreflightError("TEI request failed; check endpoint readiness and authorization")
        raw = bytearray()
        for part in response.iter_bytes(chunk_size=65536):
            if len(raw) + len(part) > MAX_RESPONSE_BYTES:
                raise PreflightError("TEI response exceeds the preflight byte budget")
            raw.extend(part)
    return json.loads(raw)


def token_count(response):
    # Pinned TEI TokenizeResponse is Vec<Vec<SimpleToken>>, including one outer
    # entry for a single string: router/src/http/types.rs at e80ef225ed0e6cb...
    if not isinstance(response, list) or len(response) != 1 or not isinstance(response[0], list):
        raise PreflightError("TEI tokenizer returned an invalid response shape")
    for token in response[0]:
        if (
            not isinstance(token, dict)
            or type(token.get("id")) is not int
            or token["id"] < 0
            or type(token.get("special")) is not bool
            or not isinstance(token.get("text"), str)
        ):
            raise PreflightError("TEI tokenizer returned an invalid token")
    if not response[0]:
        raise PreflightError("TEI tokenizer returned no tokens")
    return len(response[0])


def check(args):
    if Path(args.output).resolve() in {
        Path(path).resolve() for path in (args.corpus, args.judgments, args.providers)
    }:
        raise PreflightError("Preflight output must not overwrite an input artifact")
    corpus, corpus_hash = _read_artifact(args.corpus, FrozenCorpus)
    judgments, judgments_hash = _read_artifact(args.judgments, Judgments)
    validate_judgments(corpus, judgments)
    configurations, providers_hash = _read_artifact(args.providers, Providers, yaml_file=True)
    selected = [item for item in configurations.providers if item.id in args.provider]
    if len(selected) != len(set(args.provider)):
        raise PreflightError("A selected provider ID does not exist")
    chunks = [chunk for repo in corpus.repositories for chunk in repo.chunks]
    if len(chunks) + len(judgments.queries) > MAX_INPUTS:
        raise PreflightError("Preflight exceeds its input-count budget")
    prepared = []
    # Validate every selected configuration and prepared input before any HTTP.
    for configuration in selected:
        if configuration.protocol != "tei":
            raise PreflightError("Token preflight accepts explicitly selected TEI providers only")
        if configuration.query_prompt_name or configuration.document_prompt_name:
            raise PreflightError("Token preflight requires client-side prompts, not native prompt names")
        provider = configuration.provider()
        url = httpx.URL(provider.url)
        if not url.path.endswith("/embed"):
            raise PreflightError("TEI endpoint must end with /embed")
        inputs = [
            ("document", chunk.id, provider._prepare([chunk.content], "document")[0]) for chunk in chunks
        ] + [("query", query.id, provider._prepare([query.text], "query")[0]) for query in judgments.queries]
        if sum(len(text.encode()) for _, _, text in inputs) > MAX_PREPARED_BYTES:
            raise PreflightError("Prepared inputs exceed the preflight byte budget")
        prepared.append((configuration, provider, url, inputs))
    report = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "corpus_id": corpus.corpus_id,
        "corpus_hash": corpus_hash,
        "judgments_hash": judgments_hash,
        "providers_hash": providers_hash,
        "providers": [],
    }
    for configuration, provider, url, inputs in prepared:
        headers = {"Authorization": f"Bearer {provider.api_key}"} if provider.api_key else {}
        base = url.path.rsplit("/", 1)[0]
        with httpx.Client(
            headers=headers,
            timeout=httpx.Timeout(
                provider.request_timeout_seconds, connect=min(10, provider.request_timeout_seconds)
            ),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            info = request_json(client, "GET", url.copy_with(path=base + "/info"))
            if not isinstance(info, dict) or type(info.get("max_input_length")) is not int:
                raise PreflightError("TEI /info must declare an integer max_input_length")
            limit = info["max_input_length"]
            if not 1 <= limit <= 1000000:
                raise PreflightError("TEI input limit is outside the preflight budget")
            if info.get("model_id") != configuration.model or (
                configuration.model_revision and info.get("model_sha") != configuration.model_revision
            ):
                raise PreflightError("TEI model identity differs from the selected provider configuration")
            result = {
                "provider_id": configuration.id,
                "model": configuration.model,
                "model_revision": configuration.model_revision,
                "preprocessing_id": provider.cache_key,
                "max_input_length": limit,
                "max_document_tokens": 0,
                "max_query_tokens": 0,
                "document_inputs": len(chunks),
                "query_inputs": len(judgments.queries),
                "tokenize_requests": 0,
                "violations": [],
            }
            counts = {}
            for role, identity, text in inputs:
                if text not in counts:
                    counts[text] = token_count(
                        request_json(
                            client,
                            "POST",
                            url.copy_with(path=base + "/tokenize"),
                            {"inputs": text, "add_special_tokens": True},
                        )
                    )
                    result["tokenize_requests"] += 1
                count = counts[text]
                key = f"max_{role}_tokens"
                result[key] = max(result[key], count)
                if count > limit:
                    result["violations"].append({"role": role, "id": identity, "tokens": count})
            result["passed"] = not result["violations"]
            report["providers"].append(result)
    report["passed"] = all(item["passed"] for item in report["providers"])
    for path, model, yaml_file, expected in (
        (args.corpus, FrozenCorpus, False, corpus_hash),
        (args.judgments, Judgments, False, judgments_hash),
        (args.providers, Providers, True, providers_hash),
    ):
        _, actual = _read_artifact(path, model, yaml_file=yaml_file)
        if actual != expected:
            raise PreflightError("An input artifact changed during token preflight; rerun on frozen inputs")
    _write_json(Path(args.output), report)
    print(json.dumps(report))
    return 0 if report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("corpus", "judgments", "providers", "output"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--provider", action="append", required=True)
    args = parser.parse_args()
    try:
        return check(args)
    except PreflightError as error:
        print(str(error), file=sys.stderr)
    except (ValueError, OSError, httpx.HTTPError, TypeError, KeyError):
        # Endpoint responses, validation errors and transport exceptions can contain
        # corpus text, URLs or credentials. Never print their bodies or exceptions.
        print(
            "Token preflight failed: invalid artifacts, configuration, or endpoint response", file=sys.stderr
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
