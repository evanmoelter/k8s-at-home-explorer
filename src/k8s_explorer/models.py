import hashlib
from datetime import UTC, datetime

from pydantic import BaseModel, Field


def stable_id(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:32]


class Repository(BaseModel):
    id: str
    name: str
    url: str
    branch: str = "main"
    stars: int = Field(default=0, ge=0)
    preference: float = 0


class Snapshot(BaseModel):
    id: str
    repo_id: str
    commit: str
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SourceFile(BaseModel):
    id: str
    repo_id: str
    snapshot_id: str
    path: str
    blob: str
    size: int


class Resource(BaseModel):
    id: str
    repo_id: str
    snapshot_id: str
    file_id: str
    path: str
    start_line: int
    end_line: int
    context: str
    api_version: str
    kind: str
    name: str
    namespace: str = ""
    app: str = ""
    services: list[str] = Field(default_factory=list)
    images: list[str] = Field(default_factory=list)
    document: dict = Field(default_factory=dict)


class Edge(BaseModel):
    id: str
    repo_id: str
    snapshot_id: str
    source_id: str
    target_id: str | None = None
    relation: str
    evidence: dict = Field(default_factory=dict)
    resolution: str = "explicit"


class Chunk(BaseModel):
    id: str
    repo_id: str
    snapshot_id: str
    file_id: str
    path: str
    start_line: int
    end_line: int
    content: str
    content_hash: str
    resource_id: str | None = None


class Extraction(BaseModel):
    resources: list[Resource] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    chunks: list[Chunk] = Field(default_factory=list)
    skipped: list[dict] = Field(default_factory=list)
