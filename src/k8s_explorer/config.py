from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EXPLORER_", env_file=None)

    database_url: SecretStr = SecretStr("postgresql://explorer:explorer@localhost:5432/explorer")
    corpus_dir: Path = Path(".data/corpus")
    repositories_file: Path = Path("config/repositories.yaml")
    allowed_hosts: list[str] = ["github.com", "gitlab.com"]
    allow_local_repos: bool = False
    fetch_workers: int = Field(default=4, ge=1, le=16)
    fetch_timeout: int = Field(default=180, ge=1, le=1800)
    max_file_bytes: int = Field(default=1_000_000, ge=1, le=10_000_000)
    max_repo_bytes: int = Field(default=536_870_912, ge=1)
    max_repo_files: int = Field(default=20_000, ge=1, le=100_000)
    max_repo_source_bytes: int = Field(default=16_777_216, ge=1, le=268_435_456)
    query_workers: int = Field(default=8, ge=1, le=32)
    sync_interval: int = Field(default=3600, ge=10)
    github_token: SecretStr | None = None
    refresh_metadata: bool = True
    embedding_url: str | None = None
    embedding_model: str | None = None
    embedding_dimensions: int | None = Field(default=None, ge=1, le=16000)
    embedding_api_key: SecretStr | None = None
    embedding_protocol: Literal["openai", "voyage", "tei"] = "openai"
    embedding_requested_dimensions: int | None = Field(default=None, ge=1, le=16000)
    embedding_model_revision: str | None = Field(default=None, max_length=500)
    embedding_query_instruction: str | None = Field(default=None, max_length=8192)
    embedding_document_prefix: str = Field(default="", max_length=8192)
    embedding_query_prefix: str = Field(default="", max_length=8192)
    embedding_query_prompt_name: str | None = Field(default=None, max_length=500)
    embedding_document_prompt_name: str | None = Field(default=None, max_length=500)
    embedding_request_timeout_seconds: float = Field(default=60, ge=1, le=1800, allow_inf_nan=False)
    api_token: SecretStr | None = None
    allowed_http_hosts: list[str] = ["localhost", "127.0.0.1", "testserver", "k8s-explorer"]
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)

    @model_validator(mode="after")
    def embedding_configuration(self):
        configured = (self.embedding_url, self.embedding_model, self.embedding_dimensions)
        if any(value is not None for value in configured) and not all(configured):
            raise ValueError("Embedding URL, model, and dimensions must be configured together")
        if (
            self.embedding_requested_dimensions is not None
            and self.embedding_requested_dimensions != self.embedding_dimensions
        ):
            raise ValueError("Requested embedding dimensions must match validated output dimensions")
        return self
