"""Opt-in real-model configuration shared by disposable integration tests."""

import os

import pytest

from k8s_explorer.config import Settings
from k8s_explorer.embeddings import HTTPEmbeddingProvider


@pytest.fixture
def real_embedding_provider(tmp_path):
    required = [os.environ.get("EXPLORER_TEST_EMBEDDING_" + key) for key in ("URL", "MODEL", "DIMENSIONS")]
    if not any(required):
        pytest.skip("Configure a real embedding provider for the opt-in semantic serving tests")
    if not all(required):
        pytest.fail("Real embedding test URL, model and dimensions must all be configured", pytrace=False)
    try:
        settings = Settings(_env_prefix="EXPLORER_TEST_", corpus_dir=tmp_path / "test-provider")
        kwargs = {
            name.removeprefix("embedding_"): getattr(settings, name)
            for name in Settings.model_fields
            if name.startswith("embedding_")
        }
        key = kwargs["api_key"]
        kwargs["api_key"] = key.get_secret_value() if key else None
        return HTTPEmbeddingProvider(**kwargs)
    except ValueError:
        pytest.fail("Invalid real embedding test configuration", pytrace=False)
