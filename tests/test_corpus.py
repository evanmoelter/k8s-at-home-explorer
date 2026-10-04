import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

from k8s_explorer.corpus import CorpusError, CorpusInterrupted, CorpusStore
from k8s_explorer.models import Repository, Snapshot, stable_id


def git(path: Path, *args: str) -> str:
    output = subprocess.check_output(["git", "-C", str(path), *args], stderr=subprocess.DEVNULL)
    return output.decode().strip()


@pytest.fixture
def repository(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-b", "main")
    git(upstream, "config", "user.name", "Corpus test")
    git(upstream, "config", "user.email", "test@example.invalid")
    (upstream / "app.yaml").write_text("kind: ConfigMap\nmetadata:\n  name: app\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "initial")
    repo = Repository(id=stable_id("local", str(upstream)), name="fixture", url=str(upstream))
    return upstream, repo


def test_latest_snapshot_cleanup_reclaims_deleted_files_and_rejects_old_ids(tmp_path, repository):
    upstream, repo = repository
    (upstream / "obsolete-data").write_bytes(os.urandom(200_000))
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "old source payload")
    corpus = CorpusStore(tmp_path / "corpus", allow_local=True)
    old = corpus.sync(repo)
    corpus.prune(repo.id, old.id)
    deleted_blob = next(source.blob for source in corpus.files(old) if source.path == "obsolete-data")
    (upstream / "obsolete-data").unlink()
    (upstream / "app.yaml").rename(upstream / "renamed.yaml")
    (upstream / "other.yaml").write_text("kind: Namespace\n")
    git(upstream, "add", "-A")
    git(upstream, "commit", "-m", "rename and add")
    new = corpus.sync(repo)
    assert old.commit != new.commit
    assert corpus.snapshot(old.id) == old
    assert corpus.read(old, "app.yaml")["text"].startswith("kind: ConfigMap")
    assert {source.path for source in corpus.files(new)} == {"renamed.yaml", "other.yaml"}
    result = corpus.prune(repo.id, new.id)
    assert result["removed_snapshots"] == 1
    assert result["bytes_after"] < result["bytes_before"]
    with pytest.raises(CorpusError, match="not available"):
        corpus.snapshot(old.id)
    with pytest.raises(CorpusError, match="not available"):
        corpus.read(old, "app.yaml")
    assert corpus.read(new, "renamed.yaml")["commit"] == new.commit
    with pytest.raises(subprocess.CalledProcessError):
        git(corpus._repo_path(repo.id), "cat-file", "-e", deleted_blob)
    refs = git(corpus._repo_path(repo.id), "for-each-ref", "--format=%(refname)", "refs/explorer/snapshots/")
    assert refs == f"refs/explorer/snapshots/{new.id}"
    assert not hasattr(corpus, "diff")
    assert corpus.prune(repo.id, new.id)["removed_snapshots"] == 0
    (upstream / "other.yaml").unlink()
    git(upstream, "add", "-A")
    git(upstream, "commit", "-m", "delete")
    newest = corpus.sync(repo)
    corpus.prune(repo.id, newest.id)
    assert {source.path for source in corpus.files(newest)} == {"renamed.yaml"}
    with pytest.raises(CorpusError):
        corpus.snapshot(new.id)


def test_failed_fetch_preserves_latest_and_snapshots(tmp_path, repository):
    _, repo = repository
    corpus = CorpusStore(tmp_path / "corpus", allow_local=True)
    snapshot = corpus.sync(repo)
    metadata = (corpus.root / "metadata" / f"{repo.id}.json").read_bytes()
    with pytest.raises(CorpusError, match="Git operation failed"):
        corpus.sync(repo.model_copy(update={"branch": "missing"}))
    assert (corpus.root / "metadata" / f"{repo.id}.json").read_bytes() == metadata
    assert corpus.sync(repo) == snapshot
    assert corpus.read(snapshot, "app.yaml")["text"]


def test_invalid_inputs_and_snapshot_forgery(tmp_path, repository):
    _, repo = repository
    corpus = CorpusStore(tmp_path / "corpus", allow_local=True)
    snapshot = corpus.sync(repo)
    for path in ("../app.yaml", "/app.yaml", "app.yaml\0x", "./app.yaml", "a//b", "a\\b"):
        with pytest.raises(CorpusError, match="path"):
            corpus.read(snapshot, path)
    for url in (
        "https://user:secret@github.com/example/repo",
        "https://evil.invalid/repo.git",
        "git@github.com:example/repo",
        "https://github.com/example/repo?token=secret",
    ):
        with pytest.raises(CorpusError) as error:
            corpus.sync(repo.model_copy(update={"url": url}))
        assert "secret" not in str(error.value)
    with pytest.raises(CorpusError):
        CorpusStore(tmp_path / "production").sync(repo)
    with pytest.raises(CorpusError):
        corpus.sync(repo.model_copy(update={"branch": "main:refs/heads/bad"}))
    with pytest.raises(CorpusError):
        corpus.snapshot("../../etc/passwd")
    with pytest.raises(CorpusError):
        corpus.files(Snapshot(id=snapshot.id, repo_id=snapshot.repo_id, commit="0" * 40))


def test_read_budgets_binary_lfs_symlinks_and_literal_grep(tmp_path, repository):
    upstream, repo = repository
    (upstream / "binary").write_bytes(b"abc\0def")
    (upstream / "large").write_text("x" * 300)
    (upstream / "pointer").write_text("version https://git-lfs.github.com/spec/v1\noid sha256:123\n")
    (upstream / "link").symlink_to("app.yaml")
    (upstream / "utf8").write_text("ééé\n")
    git(upstream, "add", "-A")
    git(upstream, "commit", "-m", "special files")
    corpus = CorpusStore(tmp_path / "corpus", allow_local=True, max_file_bytes=100)
    snapshot = corpus.sync(repo)
    assert "large" not in {source.path for source in corpus.files(snapshot)}
    assert "link" not in {source.path for source in corpus.files(snapshot)}
    assert {row["path"] for row in corpus.skipped_files(snapshot)} == {"large", "link"}
    for file, match in (("binary", "Binary"), ("pointer", "LFS"), ("large", "byte budget")):
        with pytest.raises(CorpusError, match=match):
            corpus.read(snapshot, file)
    assert corpus.read(snapshot, "app.yaml", start_line=2, end_line=2)["text"] == "metadata:\n"
    truncated = corpus.read(snapshot, "utf8", max_bytes=3)
    assert truncated["truncated"] and truncated["text"] == "é"
    hit = corpus.grep(snapshot, "kind:", limit=1)[0]
    assert hit["line"] == 1 and hit["commit"] == snapshot.commit
    assert corpus.grep(snapshot, ".*", limit=1) == []
    assert corpus.grep(snapshot, "kind", path_prefix="other") == []
    with pytest.raises(CorpusError):
        corpus.grep(snapshot, "kind", limit=0)


def test_fetch_disk_budget_preserves_retained_snapshot(tmp_path, repository):
    upstream, repo = repository
    corpus = CorpusStore(tmp_path / "corpus", allow_local=True)
    original = corpus.sync(repo)
    metadata = (corpus.root / "metadata" / f"{repo.id}.json").read_bytes()
    corpus.max_repo_bytes = corpus._directory_bytes(corpus._repo_path(repo.id)) + 10_000
    (upstream / "large-random-file").write_bytes(os.urandom(200_000))
    git(upstream, "add", "-A")
    git(upstream, "commit", "-m", "oversized")
    with pytest.raises(CorpusError, match="budget"):
        corpus.sync(repo)
    assert corpus.read(original, "app.yaml")["text"].startswith("kind: ConfigMap")
    assert (corpus.root / "metadata" / f"{repo.id}.json").read_bytes() == metadata
    tiny_corpus = CorpusStore(tmp_path / "tiny", allow_local=True, max_repo_bytes=1)
    with pytest.raises(CorpusError, match="budget"):
        tiny_corpus.sync(repo)


def test_grep_result_coverage_and_match_centered_excerpt(tmp_path, repository):
    upstream, repo = repository
    (upstream / "long.txt").write_text("é" * 3000 + "needle" + "é" * 3000 + "\n")
    (upstream / "binary").write_bytes(b"binary\0content")
    git(upstream, "add", "-A")
    git(upstream, "commit", "-m", "search fixtures")
    corpus = CorpusStore(tmp_path / "corpus", allow_local=True)
    snapshot = corpus.sync(repo)
    limited = corpus.grep_result(snapshot, "needle", path_prefix="long.txt", scan_budget_bytes=1)
    assert limited["incomplete"] and limited["incomplete_reasons"] == ["scan_budget"]
    assert limited["scanned_bytes"] == 0 and limited["scanned_files"] == 0 and limited["items"] == []
    first_file_bytes = (upstream / "app.yaml").stat().st_size
    partially_scanned = corpus.grep_result(snapshot, "absent", scan_budget_bytes=first_file_bytes)
    assert partially_scanned["incomplete"] and partially_scanned["scanned_files"] == 1
    assert partially_scanned["scanned_bytes"] == first_file_bytes
    matching = corpus.grep_result(snapshot, "needle", path_prefix="long.txt", limit=1)
    assert not matching["incomplete"] and matching["scanned_files"] == 1
    hit = matching["items"][0]
    assert hit["excerpt_truncated"] and len(hit["text"].encode()) <= 2048
    assert "needle" in hit["text"] and hit["match_column"] == 3001
    assert matching["items"] == corpus.grep(snapshot, "needle", path_prefix="long.txt", limit=1)
    binary = corpus.grep_result(snapshot, "needle", path_prefix="binary")
    assert binary["incomplete_reasons"] == ["unsupported_files"] and binary["skipped_file_count"] == 1
    result_limited = corpus.grep_result(snapshot, "kind", limit=1)
    assert result_limited["incomplete"] and "result_limit" in result_limited["incomplete_reasons"]
    complete = corpus.grep_result(snapshot, "no match", path_prefix="app.yaml")
    assert not complete["incomplete"] and complete["scanned_bytes"] == (upstream / "app.yaml").stat().st_size
    with pytest.raises(CorpusError):
        corpus.grep_result(snapshot, "é" * 1001)


def test_batch_reads_validate_metadata_skip_unsupported_and_retain_sources(tmp_path, repository):
    upstream, repo = repository
    corpus = CorpusStore(tmp_path / "corpus", allow_local=True, max_file_bytes=100)
    old = corpus.sync(repo)
    (upstream / "binary").write_bytes(b"binary\0content")
    (upstream / "invalid-utf8").write_bytes(b"\xff\xfe")
    (upstream / "lfs").write_text("version https://git-lfs.github.com/spec/v1\noid sha256:123\n")
    (upstream / "large").write_text("x" * 101)
    (upstream / "empty").write_text("")
    git(upstream, "add", "-A")
    git(upstream, "commit", "-m", "batch fixtures")
    current = corpus.sync(repo)
    files = corpus.files(current)
    result = corpus.read_many(current, files)
    assert {source.path for source, _ in result["texts"]} == {"app.yaml", "empty"}
    assert {row["path"] for row in result["skipped"]} == {"binary", "invalid-utf8", "lfs"}
    assert corpus.read_many(old, corpus.files(old))["texts"][0][1].startswith("kind: ConfigMap")
    assert corpus.read_many(current, []) == {"texts": [], "skipped": []}
    source = files[0]
    for forged in (
        source.model_copy(update={"blob": "f" * 40}),
        source.model_copy(update={"size": 101}),
        source.model_copy(update={"snapshot_id": old.id}),
        source.model_copy(update={"path": "../app.yaml"}),
    ):
        with pytest.raises(CorpusError):
            corpus.read_many(current, [forged])
    with pytest.raises(CorpusError):
        corpus.read_many(current, [source, source])
    with pytest.raises(CorpusError, match="budget"):
        corpus.read_many(current, [source.model_copy(update={"size": 268_435_457})])


def test_timed_out_real_fetch_cleans_only_new_artifacts_and_retries(tmp_path, repository, monkeypatch):
    upstream, repo = repository
    corpus = CorpusStore(tmp_path / "corpus", allow_local=True, timeout=2)
    original = corpus.sync(repo)
    metadata = (corpus.root / "metadata" / f"{repo.id}.json").read_bytes()
    bare = corpus._repo_path(repo.id)
    preserved = {
        bare / "objects" / "pack" / "tmp_pack_preexisting": b"preexisting pack fixture",
        bare / "manual.lock": b"unrelated operator lock",
    }
    for path, data in preserved.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (upstream / "large-random-file").write_bytes(os.urandom(16 * 1024 * 1024))
    git(upstream, "add", "-A")
    git(upstream, "commit", "-m", "interrupted fetch fixture")
    real_popen = subprocess.Popen
    stopped = threading.Event()
    observed = []
    monitors = []
    armed = True

    def suspend_fetch(command, *args, **kwargs):
        nonlocal armed
        process = real_popen(command, *args, **kwargs)
        if "fetch" in command and armed:
            armed = False

            def monitor():
                # Stop real Git while writing a loose object so it demonstrably
                # owns shallow.lock and an unfinished object file when killed.
                while process.poll() is None:
                    temporary = list(bare.glob("objects/[0-9a-f][0-9a-f]/tmp_obj_*"))
                    if temporary:
                        try:
                            os.killpg(process.pid, signal.SIGSTOP)
                        except ProcessLookupError:
                            return
                        live = [path for path in temporary if path.exists()]
                        if live:
                            observed.extend(live)
                            stopped.set()
                            return
                        try:
                            os.killpg(process.pid, signal.SIGCONT)
                        except ProcessLookupError:
                            return
                    time.sleep(0.001)

            thread = threading.Thread(target=monitor, daemon=True)
            monitors.append(thread)
            thread.start()
        return process

    monkeypatch.setattr(subprocess, "Popen", suspend_fetch)
    with pytest.raises(CorpusInterrupted, match="budget"):
        corpus.sync(repo)
    for thread in monitors:
        thread.join(timeout=1)
        assert not thread.is_alive()
    assert stopped.is_set(), "Fixture must interrupt actual Git object writing"
    assert not (bare / "shallow.lock").exists()
    assert all(not path.exists() for path in observed)
    for path, data in preserved.items():
        assert path.read_bytes() == data
    assert (corpus.root / "metadata" / f"{repo.id}.json").read_bytes() == metadata
    assert corpus.read(original, "app.yaml")["text"].startswith("kind: ConfigMap")
    recovered = corpus.sync(repo)
    assert recovered.commit != original.commit
    assert corpus.read(original, "app.yaml")["commit"] == original.commit


def test_preexisting_fetch_lock_is_preserved(tmp_path, repository):
    _, repo = repository
    corpus = CorpusStore(tmp_path / "corpus", allow_local=True)
    original = corpus.sync(repo)
    lock = corpus._repo_path(repo.id) / "shallow.lock"
    lock.write_text("operator-owned preexisting lock")
    with pytest.raises(CorpusError):
        corpus.sync(repo)
    assert lock.read_text() == "operator-owned preexisting lock"
    assert corpus.read(original, "app.yaml")["text"].startswith("kind: ConfigMap")


def test_candidate_keeps_previous_published_source_until_prune(tmp_path, repository):
    upstream, repo = repository
    corpus = CorpusStore(tmp_path / "corpus", allow_local=True)
    published = corpus.sync(repo)
    corpus.prune(repo.id, published.id)
    (upstream / "app.yaml").write_text("kind: Namespace\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "unpublished candidate")
    candidate = corpus.sync(repo)
    # A failed database publication does not invoke prune; both sources remain.
    assert corpus.read(published, "app.yaml")["text"].startswith("kind: ConfigMap")
    assert corpus.read(candidate, "app.yaml")["text"] == "kind: Namespace\n"
    with pytest.raises(CorpusError, match="does not match"):
        corpus.prune(repo.id, published.id)
    assert corpus.snapshot(published.id) == published
    assert corpus.sync(repo) == candidate
    corpus.prune(repo.id, candidate.id)
    with pytest.raises(CorpusError, match="not available"):
        corpus.snapshot(published.id)


def test_prune_preserves_unknown_corrupt_and_foreign_refs(tmp_path, repository):
    upstream, repo = repository
    corpus = CorpusStore(tmp_path / "corpus", allow_local=True)
    old = corpus.sync(repo)
    (upstream / "app.yaml").write_text("kind: Namespace\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "middle")
    middle = corpus.sync(repo)
    (corpus.root / "snapshots" / f"{middle.id}.json").write_text("corrupt metadata")
    (upstream / "app.yaml").write_text("kind: Secret\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "latest")
    latest = corpus.sync(repo)
    foreign_repo_id = stable_id("foreign")
    foreign = Snapshot(id=stable_id(foreign_repo_id, old.commit), repo_id=foreign_repo_id, commit=old.commit)
    foreign_metadata = corpus.root / "snapshots" / f"{foreign.id}.json"
    foreign_metadata.write_text(foreign.model_dump_json())
    mismatched = Snapshot(id=stable_id(repo.id, "f" * 40), repo_id=repo.id, commit="f" * 40)
    mismatched_metadata = corpus.root / "snapshots" / f"{mismatched.id}.json"
    mismatched_metadata.write_text(mismatched.model_dump_json())
    bare = corpus._repo_path(repo.id)
    protected_refs = {
        f"refs/explorer/snapshots/{middle.id}": middle.commit,
        f"refs/explorer/snapshots/{foreign.id}": foreign.commit,
        f"refs/explorer/snapshots/{mismatched.id}": old.commit,
        "refs/explorer/snapshots/unrecognized": old.commit,
        "refs/heads/operator": old.commit,
    }
    for ref, commit in protected_refs.items():
        git(bare, "update-ref", ref, commit)
    result = corpus.prune(repo.id, latest.id)
    assert result["removed_snapshots"] == 1 and result["preserved_unknown_refs"] == 4
    for ref, commit in protected_refs.items():
        assert git(bare, "rev-parse", ref) == commit
    assert foreign_metadata.exists()
    assert mismatched_metadata.exists()
    assert (corpus.root / "snapshots" / f"{middle.id}.json").read_text() == "corrupt metadata"
    assert corpus.read(latest, "app.yaml")["text"] == "kind: Secret\n"
    with pytest.raises(CorpusError):
        corpus.prune(repo.id, foreign.id)


def test_prune_expires_managed_reflogs_and_recovers_after_gc_failure(tmp_path, repository, monkeypatch):
    upstream, repo = repository
    (upstream / "obsolete").write_bytes(os.urandom(100_000))
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "old payload")
    corpus = CorpusStore(tmp_path / "corpus", allow_local=True)
    old = corpus.sync(repo)
    obsolete_blob = next(source.blob for source in corpus.files(old) if source.path == "obsolete")
    bare = corpus._repo_path(repo.id)
    git(bare, "config", "core.logAllRefUpdates", "always")
    (upstream / "obsolete").unlink()
    git(upstream, "add", "-A")
    git(upstream, "commit", "-m", "delete payload")
    latest = corpus.sync(repo)
    reflog = bare / "logs" / "refs" / "explorer" / "upstream"
    assert old.commit in reflog.read_text()
    real_git = corpus._git

    def failed_gc(*args, **kwargs):
        if "gc" in args:
            raise CorpusInterrupted("Simulated bounded GC interruption")
        return real_git(*args, **kwargs)

    monkeypatch.setattr(corpus, "_git", failed_gc)
    with pytest.raises(CorpusInterrupted):
        corpus.prune(repo.id, latest.id)
    assert corpus.snapshot(latest.id) == latest
    assert corpus.read(latest, "app.yaml")["text"].startswith("kind: ConfigMap")
    assert (
        json.loads((corpus.root / "metadata" / f"{repo.id}.json").read_text())["latest_snapshot"] == latest.id
    )
    assert old.commit not in reflog.read_text()
    monkeypatch.setattr(corpus, "_git", real_git)
    corpus.prune(repo.id, latest.id)
    with pytest.raises(subprocess.CalledProcessError):
        git(bare, "cat-file", "-e", obsolete_blob)
