from k8s_explorer.extract import extract
from k8s_explorer.models import Snapshot, SourceFile, stable_id


def parse(text, path="kubernetes/apollo/apps/demo.yaml", snapshot=None):
    snapshot = snapshot or Snapshot(id="snapshot", repo_id="repo", commit="a" * 40)
    source = SourceFile(
        id=stable_id(snapshot.id, path),
        repo_id="repo",
        snapshot_id=snapshot.id,
        path=path,
        blob="b" * 40,
        size=len(text),
    )
    return extract(snapshot, [(source, text)])


def test_app_template_images_and_services_with_source_lines():
    result = parse("""apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: immich
  namespace: photos
spec:
  chart:
    spec:
      chart: app-template
  values:
    controllers:
      main:
        containers:
          app:
            image:
              repository: ghcr.io/immich-app/immich-server
---
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: immich-db
  namespace: photos
""")
    assert len(result.resources) == 2
    assert result.resources[0].app == "immich"
    assert result.resources[0].images == ["ghcr.io/immich-app/immich-server"]
    assert result.resources[1].services == ["cloudnative-pg"]
    assert result.resources[1].start_line == 18
    assert result.chunks[1].content.startswith("apiVersion: postgresql")


def test_explicit_reference_resolution_and_unknown_namespace():
    text = """apiVersion: v1
kind: Deployment
metadata:
  name: app
  namespace: default
spec:
  template:
    spec:
      containers:
        - name: app
          env:
            - name: TOKEN
              valueFrom:
                secretKeyRef:
                  name: token
                  key: value
---
apiVersion: v1
kind: Secret
metadata:
  name: token
  namespace: default
"""
    result = parse(text)
    assert result.edges[0].target_id == result.resources[1].id
    assert result.edges[0].resolution == "explicit"
    missing = parse(text.replace("  namespace: default\n", ""))
    assert missing.edges[0].target_id is None
    assert missing.edges[0].resolution == "unresolved"
    assert parse(text, path="apps/test.yaml").edges[0].target_id is None


def test_invalid_yaml_and_plain_config():
    assert parse("foo: [").skipped[0]["reason"] == "invalid_yaml"
    assert parse("hello backup", path="README.md").chunks[0].start_line == 1


def test_yaml_source_includes_last_line_with_and_without_final_newline():
    for text in (
        "kind: ConfigMap\nmetadata: {name: demo}\ndata:\n  meaningful: final-value",
        "{kind: ConfigMap, metadata: {name: demo}, data: {meaningful: final-value}}",
    ):
        for suffix in ("", "\n"):
            result = parse(text + suffix)
            assert result.chunks[0].content == text
            assert result.chunks[0].end_line == len(text.splitlines())
            assert result.resources[0].end_line == len(text.splitlines())


def test_large_resources_preserve_source_ranges_with_a_provider_neutral_byte_bound():
    from k8s_explorer.extract import MAX_CHUNK_BYTES

    text = "kind: ConfigMap\nmetadata: {name: large}\ndata:\n" + "".join(
        f"  field{index}: '{'😀' * 80}'\n" for index in range(100)
    )
    result = parse(text)
    assert len(result.resources) == 1
    assert len(result.chunks) > 1
    lines = text.splitlines()
    covered = []
    for chunk in result.chunks:
        assert len(chunk.content.encode()) <= MAX_CHUNK_BYTES
        assert chunk.resource_id == result.resources[0].id
        assert chunk.content == "\n".join(lines[chunk.start_line - 1 : chunk.end_line])
        covered.extend(range(chunk.start_line, chunk.end_line + 1))
    assert covered == list(range(1, len(lines) + 1))
    assert not result.skipped


def test_oversized_single_line_is_reported_without_discarding_adjacent_source():
    from k8s_explorer.extract import MAX_CHUNK_BYTES

    text = "before\n" + "x" * (MAX_CHUNK_BYTES + 1) + "\nafter\n"
    result = parse(text, path="README.md")
    assert [(chunk.content, chunk.start_line, chunk.end_line) for chunk in result.chunks] == [
        ("before", 1, 1),
        ("after", 3, 3),
    ]
    assert result.skipped == [
        {
            "file_id": result.chunks[0].file_id,
            "path": "README.md",
            "reason": "semantic_chunk_too_large",
            "start_line": 2,
        }
    ]


def test_clusters_not_cross_linked():
    snapshot = Snapshot(id="snapshot", repo_id="repo", commit="a" * 40)
    docs = []
    for cluster, kind, body in (
        ("apollo", "HelmRelease", "spec:\n  dependsOn:\n    - name: database\n"),
        ("other", "HelmRelease", ""),
    ):
        path = f"kubernetes/{cluster}/apps/{kind}.yaml"
        text = f"apiVersion: v1\nkind: {kind}\nmetadata:\n  name: database\n  namespace: apps\n{body}"
        docs.append(
            (
                SourceFile(
                    id=path, repo_id="repo", snapshot_id="snapshot", path=path, blob="b", size=len(text)
                ),
                text,
            )
        )
    result = extract(snapshot, docs)
    assert all(e.target_id != result.resources[1].id for e in result.edges)


def test_cyclic_alias_and_malformed_spec_are_skipped():
    cyclic = "kind: Pod\nmetadata: {name: foo}\nspec: &cycle\n  loop: *cycle\n"
    assert parse(cyclic).skipped[0]["reason"] == "yaml_structure_budget"
    malformed = "kind: Pod\nmetadata: {name: foo}\nspec: []\n"
    assert parse(malformed).skipped[0]["reason"] == "invalid_resource"


def test_kustomization_without_metadata_includes_resources():
    snapshot = Snapshot(id="snapshot", repo_id="repo", commit="a" * 40)
    texts = {
        "kubernetes/apollo/apps/kustomization.yaml": (
            "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources:\n  - pod.yaml\n"
        ),
        "kubernetes/apollo/apps/pod.yaml": "apiVersion: v1\nkind: Pod\nmetadata: {name: pod}\n",
    }
    files = [
        (
            SourceFile(id=path, repo_id="repo", snapshot_id="snapshot", path=path, blob="b", size=len(text)),
            text,
        )
        for path, text in texts.items()
    ]
    result = extract(snapshot, files)
    assert len(result.resources) == 2
    assert result.edges[0].relation == "resources"
    assert result.edges[0].target_id == result.resources[1].id


def test_kustomize_component_without_metadata_resolves():
    snapshot = Snapshot(id="snapshot", repo_id="repo", commit="a" * 40)
    texts = {
        "kubernetes/apollo/apps/kustomization.yaml": (
            "apiVersion: kustomize.config.k8s.io/v1beta1\n"
            "kind: Kustomization\ncomponents:\n  - ../components/backup\n"
        ),
        "kubernetes/apollo/components/backup/kustomization.yaml": (
            "apiVersion: kustomize.config.k8s.io/v1alpha1\nkind: Component\nresources: []\n"
        ),
    }
    files = [
        (
            SourceFile(id=path, repo_id="repo", snapshot_id=snapshot.id, path=path, blob="b", size=len(text)),
            text,
        )
        for path, text in texts.items()
    ]
    result = extract(snapshot, files)
    assert result.resources[1].kind == "Component"
    assert result.edges[0].relation == "components"
    assert result.edges[0].target_id == result.resources[1].id


def test_merge_alias_expansion_rejected_before_construction(monkeypatch):
    from k8s_explorer.extract import BoundedSafeLoader

    # Each tiny merge duplicates the prior mapping. Thirty levels would expand
    # over a billion pairs if the constructor were allowed to flatten this tree.
    text = "\n".join(
        [
            "kind: Deployment",
            "metadata: {name: hostile}",
            "seed: &n0 {leaf: value}",
            *(f"node{i}: &n{i} {{<<: [*n{i - 1}, *n{i - 1}]}}" for i in range(1, 31)),
        ]
    )
    constructed = []
    original = BoundedSafeLoader.construct_document

    def observe(self, node):
        constructed.append(node)
        return original(self, node)

    monkeypatch.setattr(BoundedSafeLoader, "construct_document", observe)
    result = parse(text)
    assert len(text) < 1100
    assert constructed == []
    assert result.skipped[0]["reason"] == "yaml_structure_budget"
    assert result.resources == []


def test_ordinary_anchors_and_merge_keys_remain_supported():
    result = parse("""apiVersion: v1
kind: Deployment
base: &base
  image: ghcr.io/example/app:latest
metadata:
  name: example
spec:
  template:
    spec:
      containers:
        - <<: *base
          name: main
        - <<: *base
          name: second
""")
    assert not result.skipped
    assert result.resources[0].images == ["ghcr.io/example/app:latest"]


def test_invalid_kind_skipped_without_aborting_remaining_documents_or_files():
    snapshot = Snapshot(id="snapshot", repo_id="repo", commit="a" * 40)
    texts = {
        "bad.yaml": "kind: [Deployment]\nmetadata: {name: bad}\n---\nkind: Pod\nmetadata: {name: good}\n",
        "other.yaml": (
            "kind: {type: Deployment}\nmetadata: {name: bad}\n---\nkind: Service\nmetadata: {name: other}\n"
        ),
    }
    files = [
        (
            SourceFile(id=path, repo_id="repo", snapshot_id=snapshot.id, path=path, blob="b", size=len(text)),
            text,
        )
        for path, text in texts.items()
    ]
    result = extract(snapshot, files)
    assert [resource.name for resource in result.resources] == ["good", "other"]
    assert [entry["reason"] for entry in result.skipped] == ["invalid_resource_kind", "invalid_resource_kind"]


def test_composition_depth_is_bounded_before_constructor():
    result = parse("kind: Pod\nmetadata: {name: excessive}\nspec: " + "[" * 100 + "0" + "]" * 100)
    assert result.skipped[0]["reason"] == "yaml_structure_budget"


def test_repeated_nonmerge_alias_expansion_is_rejected():
    # A tiny source list repeats a100-element node1000times: no merge keys needed.
    text = (
        "kind: Pod\nmetadata: {name: excessive}\nseed: &items ["
        + ",".join(["0"] * 100)
        + "]\nspec:\n  values: ["
        + ",".join(["*items"] * 1000)
        + "]\n"
    )
    result = parse(text)
    assert len(text) < 8000
    assert result.skipped[0]["reason"] == "yaml_structure_budget"


def test_expansion_budget_is_cumulative_across_documents(monkeypatch):
    from k8s_explorer.extract import BoundedSafeLoader

    # Each document is under50k expanded nodes, but the combined file is over.
    document = (
        "kind: Pod\nmetadata: {name: example}\nseed: &items ["
        + ",".join(["0"] * 100)
        + "]\nspec:\n  values: ["
        + ",".join(["*items"] * 300)
        + "]\n"
    )
    original = BoundedSafeLoader.construct_document
    constructed = []

    def observe(self, node):
        constructed.append(node)
        return original(self, node)

    monkeypatch.setattr(BoundedSafeLoader, "construct_document", observe)
    result = parse(document + "---\n" + document)
    assert len(constructed) == 1
    assert result.skipped[0]["reason"] == "yaml_structure_budget"


def test_repeated_scalar_bytes_are_bounded_before_constructor(monkeypatch):
    from k8s_explorer.extract import BoundedSafeLoader

    # Escaped Unicode and repeated mapping keys both contribute to JSON size.
    text = (
        "kind: Pod\nmetadata: {name: excessive}\nseed: &payload '"
        + "😀" * 1000
        + "'\nspec:\n  values: ["
        + ",".join(["*payload"] * 200)
        + "]\n"
    )
    constructed = []
    original = BoundedSafeLoader.construct_document

    def observe(self, node):
        constructed.append(node)
        return original(self, node)

    monkeypatch.setattr(BoundedSafeLoader, "construct_document", observe)
    result = parse(text)
    assert len(text.encode()) < 10000
    assert constructed == []
    assert result.skipped[0]["reason"] == "yaml_structure_budget"


def test_repeated_large_mapping_keys_are_bounded():
    text = (
        "kind: Pod\nmetadata: {name: excessive}\nseed: &payload {'"
        + "x" * 1000
        + "': 0}\nspec:\n  values: ["
        + ",".join(["*payload"] * 2000)
        + "]\n"
    )
    result = parse(text)
    assert result.skipped[0]["reason"] == "yaml_structure_budget"


def test_nonfinite_yaml_values_skip_resource_before_database_publication():
    result = parse(
        "kind: Pod\nmetadata: {name: invalid}\nspec: {value: .nan}\n---\n"
        "kind: Pod\nmetadata: {name: valid}\nspec: {value: 1}\n"
    )
    assert [resource.name for resource in result.resources] == ["valid"]
    assert result.skipped[0]["reason"] == "invalid_resource"
