"""Commit-pinned bare Git corpus. Source repositories are data, never executed."""

import fcntl
import json
import os
import re
import signal
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .catalogue import normalize_repository_url
from .models import Repository, Snapshot, SourceFile, stable_id


class CorpusError(ValueError):
    """Safe error suitable for returning through agent tools."""


class CorpusInterrupted(CorpusError):
    """A managed Git process group was terminated after exceeding a budget."""


class CorpusStore:
    def __init__(
        self,
        root: Path,
        allowed_hosts: list[str] | None = None,
        allow_local: bool = False,
        timeout: int = 180,
        max_file_bytes: int = 1_000_000,
        max_repo_bytes: int = 512 * 1024 * 1024,
    ):
        self.root = Path(root)
        self.allowed_hosts = allowed_hosts
        self.allow_local = allow_local
        self.timeout = timeout
        self.max_file_bytes = max_file_bytes
        self.max_repo_bytes = max_repo_bytes
        if timeout <= 0 or max_file_bytes <= 0 or max_repo_bytes <= 0:
            raise ValueError("Corpus budgets must be positive")
        for directory in ("repos", "snapshots", "metadata", "locks"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _id(value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", value):
            raise CorpusError("Invalid corpus identifier")
        return value

    @staticmethod
    def _path(path: str, *, allow_empty: bool = False) -> str:
        if path == "" and allow_empty:
            return path
        parsed = PurePosixPath(path)
        if (
            not path
            or parsed.is_absolute()
            or str(parsed) != path
            or any(part in (".", "..") for part in parsed.parts)
            or any(ord(char) < 32 for char in path)
            or "\\" in path
        ):
            raise CorpusError("Invalid source path")
        return path

    def _repo_path(self, repo_id: str) -> Path:
        return self.root / "repos" / f"{self._id(repo_id)}.git"

    @contextmanager
    def _locked(self, repo_id: str):
        with (self.root / "locks" / self._id(repo_id)).open("a") as lock:
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise CorpusError("Repository lock exceeded its time budget") from None
                    time.sleep(0.05)
            yield

    @staticmethod
    def _directory_bytes(path: Path) -> int:
        size = 0
        for directory, _, files in os.walk(path, followlinks=False):
            for file in files:
                try:
                    size += (Path(directory) / file).lstat().st_size
                except FileNotFoundError:
                    pass  # Git renames completed pack files while a fetch is running.
        return size

    def _git(
        self,
        *args: str,
        repo: Path | None = None,
        max_output: int = 32_000_000,
        enforce_repo_budget: bool = False,
        input_data: bytes | None = None,
    ) -> bytes:
        if input_data is not None and len(input_data) > 8_000_000:
            raise CorpusError("Git input exceeded its byte budget")
        if enforce_repo_budget and repo is not None and self._directory_bytes(repo) > self.max_repo_bytes:
            raise CorpusError("Repository exceeds its disk byte budget")
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_COUNT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_ASKPASS": "/usr/bin/false",
            }
        )
        # Prevent environment-provided object stores, working trees, or executable hooks.
        for key in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_INDEX_FILE",
            "GIT_CONFIG_PARAMETERS",
        ):
            environment.pop(key, None)
        command = [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.https.allow=always",
            "-c",
            "maintenance.auto=false",
            "-c",
            "gc.auto=0",
            "-c",
            "http.followRedirects=false",
            "-c",
            f"protocol.file.allow={'always' if self.allow_local else 'never'}",
        ]
        if repo is not None:
            command += ["--git-dir", str(repo)]
        command += list(args)
        # Disk-backed capture keeps clone/fetch output out of memory and never returns stderr.
        with tempfile.TemporaryFile() as input_stream, tempfile.TemporaryFile() as output:
            if input_data is not None:
                input_stream.write(input_data)
                input_stream.seek(0)
            process = subprocess.Popen(
                command,
                stdin=input_stream if input_data is not None else subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                env=environment,
                start_new_session=True,
            )
            deadline = time.monotonic() + self.timeout
            while process.poll() is None:
                oversized = (
                    enforce_repo_budget
                    and repo is not None
                    and self._directory_bytes(repo) > self.max_repo_bytes
                )
                if (
                    oversized
                    or os.fstat(output.fileno()).st_size > max_output
                    or time.monotonic() >= deadline
                ):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    raise CorpusInterrupted("Git operation exceeded its time, disk, or output budget")
                try:
                    process.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    pass
            if process.returncode:
                raise CorpusError("Git operation failed")
            if output.tell() > max_output:
                raise CorpusError("Git output exceeded its size budget")
            if enforce_repo_budget and repo is not None and self._directory_bytes(repo) > self.max_repo_bytes:
                raise CorpusError("Repository exceeds its disk byte budget")
            output.seek(0)
            return output.read(max_output + 1)

    @staticmethod
    def _atomic(path: Path, value: dict) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(value, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def sync(self, repo: Repository) -> Snapshot:
        self._id(repo.id)
        url = repo.url
        if self.allow_local and Path(url).is_absolute() and Path(url).is_dir():
            url = str(Path(url).resolve())
        else:
            try:
                url = normalize_repository_url(url, self.allowed_hosts)
            except ValueError as error:
                raise CorpusError(str(error)) from None
        if not repo.branch or repo.branch.startswith("-"):
            raise CorpusError("Invalid repository branch")
        self._git("check-ref-format", f"refs/heads/{repo.branch}")
        path = self._repo_path(repo.id)
        with self._locked(repo.id):
            if not path.exists():
                self._git("init", "--bare", str(path))
            # Explicit refspec is essential: bare clones do not configure ordinary fetch refs.
            existing_artifacts = self._fetch_artifacts(path)
            try:
                self._git(
                    "fetch",
                    "--depth=1",
                    "--no-tags",
                    "--",
                    url,
                    f"+refs/heads/{repo.branch}:refs/explorer/upstream",
                    repo=path,
                    enforce_repo_budget=True,
                )
            except CorpusInterrupted:
                # SIGKILL bypasses Git's lock/temp-file cleanup. The process group
                # has been killed and waited for; our flock still excludes fetches.
                for artifact in self._fetch_artifacts(path, regular_only=True) - existing_artifacts:
                    try:
                        artifact.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        raise CorpusError(
                            "Interrupted fetch cleanup failed; operator recovery required"
                        ) from None
                raise
            commit = self._git("rev-parse", "--verify", "refs/explorer/upstream^{commit}", repo=path)
            commit_text = commit.decode().strip()
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit_text):
                raise CorpusError("Invalid Git commit identity")
            snapshot = Snapshot(id=stable_id(repo.id, commit_text), repo_id=repo.id, commit=commit_text)
            metadata_path = self.root / "snapshots" / f"{snapshot.id}.json"
            self._git("update-ref", f"refs/explorer/snapshots/{snapshot.id}", snapshot.commit, repo=path)
            if metadata_path.exists():
                snapshot = self.snapshot(snapshot.id)
            else:
                self._atomic(metadata_path, snapshot.model_dump(mode="json"))
            self._atomic(
                self.root / "metadata" / f"{repo.id}.json",
                {
                    "repository": repo.model_copy(update={"url": url}).model_dump(mode="json"),
                    "latest_snapshot": snapshot.id,
                },
            )
            return snapshot

    def prune(self, repo_id: str, published_snapshot_id: str) -> dict:
        """Keep one successfully published snapshot; callers hold the pipeline lock.

        Publication must commit before calling this method. Failure can leave
        cleanup incomplete, but the published pin is never removed. Retrying is safe.
        """
        path = self._repo_path(repo_id)
        with self._locked(repo_id):
            published = self.snapshot(published_snapshot_id)
            if published.repo_id != repo_id:
                raise CorpusError("Published snapshot belongs to another repository")
            refs = {}
            data = self._git(
                "for-each-ref",
                "--format=%(refname)%09%(objectname)",
                "refs/explorer/snapshots/",
                repo=path,
            )
            for line in data.decode().splitlines():
                ref, commit = line.split("\t", 1)
                refs[ref] = commit
            retained_ref = f"refs/explorer/snapshots/{published.id}"
            upstream = self._git("rev-parse", "--verify", "refs/explorer/upstream^{commit}", repo=path)
            if refs.get(retained_ref) != published.commit or upstream.decode().strip() != published.commit:
                raise CorpusError("Published snapshot does not match retained pin and fetched candidate")
            removable = []
            removable_refs = []
            for metadata in sorted((self.root / "snapshots").glob("*.json")):
                try:
                    previous = self.snapshot(metadata.stem)
                except CorpusError:
                    continue  # Unknown/corrupt metadata is never silently destroyed.
                if previous.repo_id != repo_id or previous.id == published.id:
                    continue
                ref = f"refs/explorer/snapshots/{previous.id}"
                if ref in refs:
                    if refs[ref] != previous.commit:
                        continue
                    removable_refs.append((ref, previous.commit))
                elif (path / ref).exists():
                    continue  # Preserve malformed loose refs hidden from enumeration.
                removable.append(previous)
            before = self._directory_bytes(path)
            if removable_refs:
                transaction = [
                    "start",
                    f"verify {retained_ref} {published.commit}",
                    f"verify refs/explorer/upstream {published.commit}",
                    *(f"delete {ref} {commit}" for ref, commit in removable_refs),
                    "prepare",
                    "commit",
                ]
                self._git(
                    "update-ref", "--stdin", repo=path, input_data=("\n".join(transaction) + "\n").encode()
                )
            for previous in removable:
                (self.root / "snapshots" / f"{previous.id}.json").unlink(missing_ok=True)
            metadata_path = self.root / "metadata" / f"{repo_id}.json"
            metadata = json.loads(metadata_path.read_text())
            metadata["latest_snapshot"] = published.id
            self._atomic(metadata_path, metadata)
            # Historical managed reflogs cannot retain deleted snapshots. Foreign
            # refs and their reflogs remain untouched by the targeted expiration.
            logged_refs = [
                ref for ref in ("refs/explorer/upstream", retained_ref) if (path / "logs" / ref).is_file()
            ]
            if logged_refs:
                self._git(
                    "reflog",
                    "expire",
                    "--expire=now",
                    "--expire-unreachable=now",
                    *logged_refs,
                    repo=path,
                )
            artifacts = self._fetch_artifacts(path) | (
                {path / "gc.pid"} if (path / "gc.pid").exists() else set()
            )
            try:
                self._git(
                    "-c",
                    "pack.threads=1",
                    "-c",
                    "pack.windowMemory=16m",
                    "-c",
                    "pack.deltaCacheSize=16m",
                    "-c",
                    "gc.reflogExpire=never",
                    "-c",
                    "gc.reflogExpireUnreachable=never",
                    "-c",
                    "gc.writeCommitGraph=false",
                    "-c",
                    "repack.writeBitmaps=false",
                    "gc",
                    "--prune=now",
                    repo=path,
                    enforce_repo_budget=True,
                )
            except CorpusInterrupted:
                current = self._fetch_artifacts(path, regular_only=True)
                pid_file = path / "gc.pid"
                if pid_file.exists() and stat.S_ISREG(pid_file.lstat().st_mode):
                    current.add(pid_file)
                for artifact in current - artifacts:
                    artifact.unlink(missing_ok=True)
                raise
            removed_ref_names = {ref for ref, _ in removable_refs}
            return {
                "snapshot_id": published.id,
                "removed_snapshots": len(removable),
                "preserved_unknown_refs": len(set(refs) - removed_ref_names - {retained_ref}),
                "bytes_before": before,
                "bytes_after": self._directory_bytes(path),
            }

    @staticmethod
    def _fetch_artifacts(repo: Path, regular_only: bool = False) -> set[Path]:
        """Only known fetch locks and unfinished object files, never committed objects."""
        candidates = {
            repo / "shallow.lock",
            repo / "FETCH_HEAD.lock",
            repo / "packed-refs.lock",
            repo / "refs" / "explorer" / "upstream.lock",
            *repo.glob("objects/pack/tmp_pack_*"),
            *repo.glob("objects/[0-9a-f][0-9a-f]/tmp_obj_*"),
        }
        result = set()
        root = repo.resolve()
        for path in candidates:
            try:
                mode = path.lstat().st_mode
                if (not regular_only or stat.S_ISREG(mode)) and path.parent.resolve().is_relative_to(root):
                    result.add(path)
            except FileNotFoundError:
                pass
        return result

    def snapshot(self, snapshot_id: str) -> Snapshot:
        path = self.root / "snapshots" / f"{self._id(snapshot_id)}.json"
        try:
            result = Snapshot.model_validate_json(path.read_text())
        except (OSError, ValueError):
            raise CorpusError("Snapshot is not available") from None
        if result.id != snapshot_id or result.id != stable_id(result.repo_id, result.commit):
            raise CorpusError("Invalid snapshot metadata")
        self._id(result.repo_id)
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", result.commit):
            raise CorpusError("Invalid snapshot commit")
        return result

    def _validate_snapshot(self, snapshot: Snapshot) -> Snapshot:
        saved = self.snapshot(snapshot.id)
        if saved.repo_id != snapshot.repo_id or saved.commit != snapshot.commit:
            raise CorpusError("Snapshot does not match retained metadata")
        return saved

    def files(self, snapshot: Snapshot) -> list[SourceFile]:
        self._validate_snapshot(snapshot)
        tree = self._git("ls-tree", "-r", "-l", "-z", snapshot.commit, repo=self._repo_path(snapshot.repo_id))
        files = []
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            attributes, raw_path = entry.split(b"\t", 1)
            mode, kind, blob, size = attributes.split()
            if kind != b"blob" or mode not in (b"100644", b"100755") or int(size) > self.max_file_bytes:
                continue
            try:
                path = self._path(raw_path.decode("utf-8"))
            except (UnicodeDecodeError, CorpusError):
                continue
            files.append(
                SourceFile(
                    id=stable_id(snapshot.id, path),
                    repo_id=snapshot.repo_id,
                    snapshot_id=snapshot.id,
                    path=path,
                    blob=blob.decode(),
                    size=int(size),
                )
            )
        return files

    def skipped_files(self, snapshot: Snapshot) -> list[dict]:
        """Expose coverage exclusions without attempting to read unsupported objects."""
        self._validate_snapshot(snapshot)
        tree = self._git("ls-tree", "-r", "-l", "-z", snapshot.commit, repo=self._repo_path(snapshot.repo_id))
        skipped = []
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            attributes, raw_path = entry.split(b"\t", 1)
            mode, kind, _, size = attributes.split()
            reason = None
            if kind != b"blob" or mode not in (b"100644", b"100755"):
                reason = "symlink or submodule"
            elif int(size) > self.max_file_bytes:
                reason = "file exceeds byte budget"
            try:
                self._path(raw_path.decode("utf-8"))
            except (UnicodeDecodeError, CorpusError):
                reason = "invalid source path"
            if reason:
                skipped.append({"path": raw_path.decode("utf-8", errors="replace"), "reason": reason})
        return skipped

    def read(
        self,
        snapshot: Snapshot,
        path: str,
        start_line: int = 1,
        end_line: int | None = None,
        max_bytes: int = 65536,
    ) -> dict:
        self._validate_snapshot(snapshot)
        self._path(path)
        if (
            start_line < 1
            or (end_line is not None and end_line < start_line)
            or not 1 <= max_bytes <= max(65536, self.max_file_bytes)
        ):
            raise CorpusError("Invalid source read range or byte budget")
        # Look up one tree entry, avoiding a full repository listing for every indexed file.
        entries = self._git(
            "ls-tree",
            "-l",
            "-z",
            snapshot.commit,
            "--",
            path,
            repo=self._repo_path(snapshot.repo_id),
        )
        source = None
        for entry in entries.split(b"\0"):
            if not entry:
                continue
            attributes, raw_path = entry.split(b"\t", 1)
            mode, kind, blob, size = attributes.split()
            if (
                raw_path == path.encode()
                and kind == b"blob"
                and mode in (b"100644", b"100755")
                and int(size) <= self.max_file_bytes
            ):
                source = SourceFile(
                    id=stable_id(snapshot.id, path),
                    repo_id=snapshot.repo_id,
                    snapshot_id=snapshot.id,
                    path=path,
                    blob=blob.decode(),
                    size=int(size),
                )
                break
        if source is None:
            raise CorpusError("Source file unavailable, unsupported, or exceeds the file byte budget")
        return self._read_known(snapshot, source, start_line, end_line, max_bytes)

    def _read_known(
        self, snapshot: Snapshot, source: SourceFile, start_line: int, end_line: int | None, max_bytes: int
    ) -> dict:
        data = self._git(
            "cat-file",
            "blob",
            source.blob,
            repo=self._repo_path(snapshot.repo_id),
            max_output=self.max_file_bytes,
        )
        content = self._decode_text(data)
        lines = content.splitlines(keepends=True)
        selected = "".join(lines[start_line - 1 : end_line])
        encoded = selected.encode()
        truncated = len(encoded) > max_bytes
        selected = encoded[:max_bytes].decode("utf-8", errors="ignore")
        returned_lines = len(selected.splitlines())
        return {
            "file_id": source.id,
            "repo_id": snapshot.repo_id,
            "snapshot_id": snapshot.id,
            "commit": snapshot.commit,
            "path": source.path,
            "start_line": start_line,
            "end_line": start_line + returned_lines - 1,
            "total_lines": len(lines),
            "text": selected,
            "truncated": truncated,
        }

    @staticmethod
    def _decode_text(data: bytes) -> str:
        if b"\0" in data:
            raise CorpusError("Binary source files are not supported")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            raise CorpusError("Source file is not UTF-8 text") from None
        if content.startswith("version https://git-lfs.github.com/spec/v1\n"):
            raise CorpusError("Git LFS objects are not fetched")
        return content

    def read_many(self, snapshot: Snapshot, files: list[SourceFile]) -> dict:
        """Read validated snapshot files using a single cat-file batch process.

        The caller selects its operational source budget first. Hard aggregate
        caps protect this internal method independently of worker configuration.
        """
        self._validate_snapshot(snapshot)
        if len(files) > 100_000 or sum(source.size for source in files) > 268_435_456:
            raise CorpusError("Batch source read exceeded its file or byte budget")
        if not files:
            return {"texts": [], "skipped": []}
        retained = {source.id: source for source in self.files(snapshot)}
        seen = set()
        for source in files:
            self._path(source.path)
            if source.id in seen or retained.get(source.id) != source:
                raise CorpusError("Batch source does not match retained snapshot metadata")
            seen.add(source.id)
        request = "".join(f"{source.blob}\n" for source in files).encode()
        raw = self._git(
            "cat-file",
            "--batch",
            repo=self._repo_path(snapshot.repo_id),
            input_data=request,
            max_output=sum(source.size for source in files) + 256 * len(files),
        )
        texts, skipped = [], []
        offset = 0
        for source in files:
            end_header = raw.find(b"\n", offset, offset + 256)
            if end_header < 0:
                raise CorpusError("Invalid Git batch response header")
            header = raw[offset:end_header].split()
            expected = [source.blob.encode(), b"blob", str(source.size).encode()]
            if header != expected:
                raise CorpusError("Git batch response does not match retained blob metadata")
            offset = end_header + 1
            data = raw[offset : offset + source.size]
            offset += source.size
            if len(data) != source.size or raw[offset : offset + 1] != b"\n":
                raise CorpusError("Invalid Git batch response size")
            offset += 1
            try:
                texts.append((source, self._decode_text(data)))
            except CorpusError as error:
                skipped.append({"path": source.path, "reason": str(error)})
        if offset != len(raw):
            raise CorpusError("Unexpected trailing Git batch output")
        return {"texts": texts, "skipped": skipped}

    def grep(self, snapshot: Snapshot, query: str, path_prefix: str = "", limit: int = 20) -> list[dict]:
        return self.grep_result(snapshot, query, path_prefix, limit)["items"]

    def grep_result(
        self,
        snapshot: Snapshot,
        query: str,
        path_prefix: str = "",
        limit: int = 20,
        scan_budget_bytes: int = 32_000_000,
    ) -> dict:
        """Literal search with explicit coverage and bounded, match-centered excerpts.

        Incomplete means some eligible source was not searched, rather than proving
        additional matches exist. Unsupported file samples are bounded separately.
        """
        self._path(path_prefix, allow_empty=True)
        if not 1 <= len(query) <= 1000 or not 1 <= limit <= 100 or not 1 <= scan_budget_bytes <= 32_000_000:
            raise CorpusError("Invalid literal query, result limit, or scan budget")
        result = []
        scanned_bytes = 0
        scanned_files = 0
        skipped = [row for row in self.skipped_files(snapshot) if row["path"].startswith(path_prefix)]
        reasons = ["unsupported_files"] if skipped else []
        sources = [source for source in self.files(snapshot) if source.path.startswith(path_prefix)]

        def response() -> dict:
            return {
                "items": result,
                "scanned_bytes": scanned_bytes,
                "scan_budget_bytes": scan_budget_bytes,
                "scanned_files": scanned_files,
                "incomplete": bool(reasons),
                "incomplete_reasons": reasons,
                "skipped_files": skipped[:100],
                "skipped_file_count": len(skipped),
                "skipped_files_truncated": len(skipped) > 100,
            }

        for file_index, source in enumerate(sources):
            if scanned_bytes + source.size > scan_budget_bytes:
                reasons.append("scan_budget")
                break
            scanned_bytes += source.size
            scanned_files += 1
            try:
                document = self._read_known(snapshot, source, 1, None, self.max_file_bytes)
            except CorpusError as error:
                skipped.append({"path": source.path, "reason": str(error)})
                if "unsupported_files" not in reasons:
                    reasons.append("unsupported_files")
                continue
            lines = document["text"].splitlines()
            for line_number, line in enumerate(lines, 1):
                if query in line:
                    match_column = line.index(query)
                    excerpt_start = max(0, match_column - 128)
                    tail = line[excerpt_start:].encode()
                    excerpt = tail[:2048].decode("utf-8", errors="ignore")
                    result.append(
                        {
                            "file_id": source.id,
                            "repo_id": snapshot.repo_id,
                            "snapshot_id": snapshot.id,
                            "commit": snapshot.commit,
                            "path": source.path,
                            "line": line_number,
                            "text": excerpt,
                            "excerpt_truncated": excerpt_start > 0 or len(tail) > 2048,
                            "excerpt_start_column": excerpt_start + 1,
                            "match_column": match_column + 1,
                        }
                    )
                    if len(result) >= limit:
                        if line_number < len(lines) or file_index < len(sources) - 1:
                            reasons.append("result_limit")
                        return response()
        return response()
