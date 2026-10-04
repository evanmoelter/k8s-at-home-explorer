import fcntl
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from k8s_explorer.catalogue import GitHubCatalogue, load_repositories
from k8s_explorer.extract import extract
from k8s_explorer.models import Repository
from k8s_explorer.service import Explorer

logger = logging.getLogger(__name__)


@contextmanager
def pipeline_lock(explorer: Explorer, repo_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", repo_id):
        raise ValueError("Invalid repository identifier")
    path = explorer.settings.corpus_dir / "locks" / f"pipeline-{repo_id}"
    deadline = time.monotonic() + explorer.settings.fetch_timeout
    with path.open("a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("Repository pipeline lock exceeded its time budget") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def sync_repository(explorer: Explorer, repo: Repository) -> dict:
    with pipeline_lock(explorer, repo.id):
        return _sync_repository(explorer, repo)


def _sync_repository(explorer: Explorer, repo: Repository) -> dict:
    metadata_status = "configured"
    if explorer.settings.refresh_metadata and urlsplit(repo.url).hostname == "github.com":
        try:
            token = explorer.settings.github_token
            updated = GitHubCatalogue(token.get_secret_value() if token else None).refresh(repo)
            repo = updated.model_copy(update={"branch": repo.branch})
            metadata_status = "refreshed"
        except ValueError as exc:
            metadata_status = type(exc).__name__
            logger.warning("Metadata refresh skipped for %s (%s)", repo.name, metadata_status)
    snapshot = explorer.corpus.sync(repo)
    files = explorer.corpus.files(snapshot)
    skipped = explorer.corpus.skipped_files(snapshot)
    selected = files[: explorer.settings.max_repo_files]
    skipped.extend(
        {"path": f.path, "reason": "repository file budget exceeded"} for f in files[len(selected) :]
    )
    source_bytes = 0
    bounded_files = []
    for source in selected:
        if source_bytes + source.size > explorer.settings.max_repo_source_bytes:
            skipped.append({"path": source.path, "reason": "repository source byte budget exceeded"})
            continue
        bounded_files.append(source)
        source_bytes += source.size
    batch = explorer.corpus.read_many(snapshot, bounded_files)
    texts = batch["texts"]
    skipped.extend(batch["skipped"])
    source_bytes = sum(source.size for source, _ in texts)
    extraction = extract(snapshot, texts)
    extraction.skipped.extend(skipped)
    explorer.store.publish(repo, snapshot, files, extraction)
    result = {
        "repo_id": repo.id,
        "name": repo.name,
        "snapshot_id": snapshot.id,
        "commit": snapshot.commit,
        "files": len(files),
        "resources": len(extraction.resources),
        "edges": len(extraction.edges),
        "chunks": len(extraction.chunks),
        "skipped": len(extraction.skipped),
        "semantic": "disabled",
        "metadata": metadata_status,
        "source_bytes": source_bytes,
    }
    try:
        result["source_cleanup"] = explorer.corpus.prune(repo.id, snapshot.id)
    except Exception as exc:
        result["source_cleanup"] = {"error_type": type(exc).__name__}
        logger.warning("Source cleanup deferred for %s (%s)", repo.name, type(exc).__name__)
    if explorer.provider:
        try:
            result["semantic"] = explorer.store.index_embeddings(snapshot.id, explorer.provider)
        except Exception as exc:
            result["semantic"] = {"available": False, "error_type": type(exc).__name__}
            logger.warning("Embedding indexing failed for %s (%s)", repo.name, type(exc).__name__)
    return result


def sync_catalogue(explorer: Explorer, path: Path | None = None, limit: int | None = None) -> dict:
    if limit is not None and limit < 1:
        raise ValueError("Repository limit must be positive")
    repos = load_repositories(
        path or explorer.settings.repositories_file,
        allowed_hosts=explorer.settings.allowed_hosts,
        allow_local=explorer.settings.allow_local_repos,
    )
    if limit is not None:
        repos = repos[:limit]
    explorer.store.initialize()
    results = []
    with ThreadPoolExecutor(max_workers=explorer.settings.fetch_workers) as workers:
        futures = {workers.submit(sync_repository, explorer, repo): repo for repo in repos}
        for future in as_completed(futures):
            repo = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"repo_id": repo.id, "name": repo.name, "error_type": type(exc).__name__})
                logger.warning("Repository sync failed for %s (%s)", repo.name, type(exc).__name__)
    return {
        "items": sorted(results, key=lambda r: r["name"]),
        "failed": sum("error_type" in r for r in results),
    }
