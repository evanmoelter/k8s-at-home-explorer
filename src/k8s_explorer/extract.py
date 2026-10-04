"""Parse source as data and retain evidence; never render or execute repository code."""

import hashlib
import json
import posixpath
import re
from collections import defaultdict

import yaml

from .models import Chunk, Edge, Extraction, Resource, Snapshot, SourceFile, stable_id

ALIASES = {
    "cnpg": "cloudnative-pg",
    "cloudnative-pg": "cloudnative-pg",
    "cilium": "cilium",
    "longhorn": "longhorn",
    "volsync": "volsync",
    "envoy-gateway": "envoy-gateway",
    "cert-manager": "cert-manager",
    "external-secrets": "external-secrets",
    "external-dns": "external-dns",
    "sops": "sops",
    "rook-ceph": "rook-ceph",
    "ingress-nginx": "ingress-nginx",
    "traefik": "traefik",
    "flux": "flux",
    "argo-cd": "argo-cd",
}


class YAMLStructureBudgetError(yaml.YAMLError):
    """Reject excessive YAML structure before constructors expand merge aliases."""


class BoundedSafeLoader(yaml.SafeLoader):
    def __init__(self, stream):
        super().__init__(stream)
        self.composed_nodes = 0
        self.compose_depth = 0

    def compose_node(self, parent, index):
        self.composed_nodes += 1
        self.compose_depth += 1
        try:
            if self.composed_nodes > 50000 or self.compose_depth > 64:
                raise YAMLStructureBudgetError("YAML composition exceeds budget")
            return super().compose_node(parent, index)
        finally:
            self.compose_depth -= 1


def _bounded_documents(text):
    loader = BoundedSafeLoader(text)
    documents = []
    expanded_nodes = 0
    expanded_bytes = 0
    scalar_sizes = {}
    try:
        while loader.check_node():
            node = loader.get_node()
            # Count every alias occurrence rather than only unique node identities:
            # merge constructors duplicate pairs along every referenced path.
            stack = [(node, 0)]
            while stack:
                current, depth = stack.pop()
                expanded_nodes += 1
                expanded_bytes += 2  # Container delimiters and separators.
                if isinstance(current, yaml.ScalarNode):
                    if current not in scalar_sizes:
                        scalar_sizes[current] = len(json.dumps(current.value).encode())
                    expanded_bytes += scalar_sizes[current]
                if expanded_nodes > 50000 or depth > 64 or expanded_bytes > 2_000_000:
                    raise YAMLStructureBudgetError("Expanded YAML structure exceeds budget")
                if isinstance(current, yaml.MappingNode):
                    for key, value in current.value:
                        stack.append((key, depth + 1))
                        stack.append((value, depth + 1))
                elif isinstance(current, yaml.SequenceNode):
                    stack.extend((child, depth + 1) for child in current.value)
            documents.append((node, loader.construct_document(node)))
    finally:
        loader.dispose()
    return documents


def _context(path):
    parts = path.split("/")
    for label in ("kubernetes", "clusters"):
        if label in parts and len(parts) > parts.index(label) + 2:
            return "/".join(parts[: parts.index(label) + 2])
    return "unknown"


def _walk(value, trail=()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield trail + (str(key),), child
            yield from _walk(child, trail + (str(key),))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, trail + (str(index),))


def _json_safe(value):
    # Count the expanded tree before JSON conversion, bounding aliases and cycles.
    stack = [(value, 0)]
    count = 0
    expanded_bytes = 0
    scalar_sizes = {}
    while stack:
        current, depth = stack.pop()
        count += 1
        expanded_bytes += 2
        if not isinstance(current, (dict, list)):
            if id(current) not in scalar_sizes:
                scalar_sizes[id(current)] = len(json.dumps(current, default=str, allow_nan=False).encode())
            expanded_bytes += scalar_sizes[id(current)]
        if count > 50000 or depth > 64 or expanded_bytes > 2_000_000:
            raise ValueError("YAML structure exceeds budget")
        if isinstance(current, dict):
            for key, child in current.items():
                stack.append((key, depth + 1))
                stack.append((child, depth + 1))
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
    # Kubernetes YAML dates and non-string keys must remain serializable.
    serialized = json.dumps(value, default=str, allow_nan=False)
    if len(serialized.encode()) > 2_000_000:
        raise ValueError("Expanded YAML exceeds budget")
    return json.loads(serialized)


def _images(document):
    result = set()
    for trail, value in _walk(document):
        if trail[-1] == "image":
            if isinstance(value, str):
                result.add(value)
            elif isinstance(value, dict) and isinstance(value.get("repository"), str):
                result.add(value["repository"])
        if trail[-1] == "repository" and "image" in trail and isinstance(value, str):
            result.add(value)
    return sorted(result)


def _identity(doc, images):
    kind = doc.get("kind", "")
    name = str(doc.get("metadata", {}).get("name", ""))
    spec = doc.get("spec", {})
    if not isinstance(spec, dict):
        raise ValueError("Resource spec must be an object")
    chart = spec.get("chart", {})
    chart = chart.get("spec", chart) if isinstance(chart, dict) else {}
    chart_name = chart.get("chart", "") if isinstance(chart, dict) else ""
    if kind == "Application":
        source = spec.get("source", {})
        chart_name = source.get("chart", "") if isinstance(source, dict) else ""
    app = name
    if chart_name == "app-template" and images and name in {"app", "main", "app-template", "application"}:
        candidate = next((image for image in images if "sidecar" not in image), images[0])
        app = candidate.split("/")[-1].split(":")[0].split("@")[0]
    elif chart_name and chart_name != "app-template":
        app = str(chart_name).split("/")[-1]
    services = set()
    for candidate in [name, app, *images, str(doc.get("apiVersion", ""))]:
        for alias, service in ALIASES.items():
            if candidate == alias or re.search(
                r"(?:^|[/.:_-])" + re.escape(alias) + r"(?:$|[/.:_-])", candidate
            ):
                services.add(service)
    api_version = str(doc.get("apiVersion", ""))
    if "postgresql.cnpg.io" in api_version:
        services.add("cloudnative-pg")
    if "toolkit.fluxcd.io" in api_version:
        services.add("flux")
    if "argoproj.io" in api_version:
        services.add("argo-cd")
    return app, sorted(services)


def _refs(resource):
    for trail, value in _walk(resource.document):
        key = trail[-1]
        if isinstance(value, dict) and isinstance(value.get("name"), str):
            kind = None
            if key in {"sourceRef", "chartRef"}:
                kind = value.get("kind", "unknown")
            elif key == "dependsOn":
                kind = value.get("kind", resource.kind)
            elif key in {"secretKeyRef", "secretRef", "secret"}:
                kind = "Secret"
            elif key in {"configMapKeyRef", "configMapRef"}:
                kind = "ConfigMap"
            elif "backendRefs" in trail:
                kind = value.get("kind", "Service")
            if kind:
                yield key, kind, value["name"], value.get("namespace", resource.namespace), trail
        if key == "claimName" and isinstance(value, str):
            yield "claimName", "PersistentVolumeClaim", value, resource.namespace, trail
        # Handle list entries such as dependsOn and backendRefs.
        if key in {"dependsOn", "backendRefs", "valuesFrom"} and isinstance(value, list):
            for index, ref in enumerate(value):
                if isinstance(ref, dict) and isinstance(ref.get("name"), str):
                    default_kind = {
                        "backendRefs": "Service",
                        "valuesFrom": "Secret",
                        "dependsOn": resource.kind,
                    }[key]
                    kind = ref.get("kind", default_kind)
                    yield (
                        key,
                        kind,
                        ref["name"],
                        ref.get("namespace", resource.namespace),
                        trail + (str(index),),
                    )


def extract(snapshot: Snapshot, files: list[tuple[SourceFile, str]]) -> Extraction:
    result = Extraction()
    for source, text in files:
        if source.snapshot_id != snapshot.id or source.repo_id != snapshot.repo_id:
            raise ValueError("Source file does not belong to snapshot")
        lines = text.splitlines()
        if source.path.endswith((".yaml", ".yml", "/Kustomization")):
            try:
                documents = _bounded_documents(text)
            except YAMLStructureBudgetError:
                result.skipped.append(
                    {"file_id": source.id, "path": source.path, "reason": "yaml_structure_budget"}
                )
                continue
            except (yaml.YAMLError, RecursionError, ValueError, TypeError):
                result.skipped.append({"file_id": source.id, "path": source.path, "reason": "invalid_yaml"})
                continue
            for index, (node, raw_doc) in enumerate(documents):
                if not isinstance(raw_doc, dict):
                    continue
                if "kind" in raw_doc and not isinstance(raw_doc["kind"], str):
                    result.skipped.append(
                        {"file_id": source.id, "path": source.path, "reason": "invalid_resource_kind"}
                    )
                    continue
                if (
                    isinstance(raw_doc, dict)
                    and raw_doc.get("kind") in {"Kustomization", "Component"}
                    and not raw_doc.get("metadata")
                ):
                    raw_doc["metadata"] = {
                        "name": posixpath.basename(posixpath.dirname(source.path)) or "root"
                    }
                if (
                    not isinstance(raw_doc, dict)
                    or not raw_doc.get("kind")
                    or not isinstance(raw_doc.get("metadata"), dict)
                ):
                    continue
                if not node or not isinstance(raw_doc["metadata"].get("name"), str):
                    continue
                try:
                    doc = _json_safe(raw_doc)
                    images = _images(doc)
                    app, services = _identity(doc, images)
                except (TypeError, ValueError, RecursionError):
                    result.skipped.append(
                        {"file_id": source.id, "path": source.path, "reason": "invalid_resource"}
                    )
                    continue
                start, end = node.start_mark.line + 1, node.end_mark.line
                resource = Resource(
                    id=stable_id(source.id, "resource", str(index)),
                    repo_id=snapshot.repo_id,
                    snapshot_id=snapshot.id,
                    file_id=source.id,
                    path=source.path,
                    start_line=start,
                    end_line=max(start, end),
                    context=_context(source.path),
                    api_version=str(doc.get("apiVersion", "")),
                    kind=str(doc["kind"]),
                    name=doc["metadata"]["name"],
                    namespace=str(doc["metadata"].get("namespace") or ""),
                    app=app,
                    services=services,
                    images=images,
                    document=doc,
                )
                result.resources.append(resource)
                content = "\n".join(lines[start - 1 : end])
                if len(content.encode()) <= 32_000:
                    result.chunks.append(_chunk(source, content, start, max(start, end), resource.id))
                else:
                    for offset in range(start - 1, end, 40):
                        piece = "\n".join(lines[offset : min(offset + 40, end)])
                        if len(piece.encode()) <= 32_000:
                            result.chunks.append(
                                _chunk(source, piece, offset + 1, min(offset + 40, end), resource.id)
                            )
                        else:
                            result.skipped.append(
                                {
                                    "file_id": source.id,
                                    "path": source.path,
                                    "reason": "semantic_chunk_too_large",
                                    "start_line": offset + 1,
                                }
                            )
        # Text/config chunks also cover Helm values without kind/metadata.
        if not any(r.file_id == source.id for r in result.resources):
            for offset in range(0, len(lines), 80):
                content = "\n".join(lines[offset : offset + 80])
                if content.strip() and len(content.encode()) <= 32_000:
                    result.chunks.append(_chunk(source, content, offset + 1, min(offset + 80, len(lines))))
    lookup = defaultdict(list)
    by_path = defaultdict(list)
    for resource in result.resources:
        lookup[(resource.context, resource.kind, resource.name, resource.namespace)].append(resource)
        by_path[resource.path].append(resource)
    seen = set()
    for resource in result.resources:
        for relation, kind, name, namespace, trail in _refs(resource):
            if not isinstance(kind, str):
                kind = "unknown"
            if not isinstance(namespace, str):
                namespace = ""
            signature = (resource.id, relation, kind, name, namespace, trail)
            if signature in seen:
                continue
            seen.add(signature)
            candidates = lookup.get((resource.context, kind, name, namespace), [])
            # Unknown context or namespace cannot establish a deployment integration.
            target = (
                candidates[0].id
                if len(candidates) == 1 and namespace and resource.context != "unknown"
                else None
            )
            if not isinstance(namespace, str):
                namespace = ""
                target = None
            if "${" in name or "${" in namespace:
                target = None
            result.edges.append(
                Edge(
                    id=stable_id(resource.id, relation, str(trail)),
                    repo_id=snapshot.repo_id,
                    snapshot_id=snapshot.id,
                    source_id=resource.id,
                    target_id=target,
                    relation=relation,
                    resolution="explicit" if target else "unresolved",
                    evidence={
                        "file_id": resource.file_id,
                        "path": resource.path,
                        "start_line": resource.start_line,
                        "field": ".".join(trail),
                        "target_kind": kind,
                        "target_name": name,
                        "target_namespace": namespace,
                        "context": resource.context,
                    },
                )
            )
        for field in ("resources", "components"):
            references = resource.document.get(field, [])
            if not isinstance(references, list):
                continue
            for index, ref in enumerate(references):
                if not isinstance(ref, str):
                    continue
                target_path = posixpath.normpath(posixpath.join(posixpath.dirname(resource.path), ref))
                candidates = by_path.get(target_path, []) if not target_path.startswith("../") else []
                if not candidates and not target_path.startswith("../"):
                    for suffix in ("kustomization.yaml", "kustomization.yml", "Kustomization"):
                        candidates.extend(by_path.get(posixpath.join(target_path, suffix), []))
                targets = candidates or [None]
                for target_resource in targets:
                    result.edges.append(
                        Edge(
                            id=stable_id(
                                resource.id, field, str(index), target_resource.id if target_resource else ""
                            ),
                            repo_id=snapshot.repo_id,
                            snapshot_id=snapshot.id,
                            source_id=resource.id,
                            target_id=target_resource.id if target_resource else None,
                            relation=field,
                            resolution="explicit" if target_resource else "unresolved",
                            evidence={
                                "file_id": resource.file_id,
                                "path": resource.path,
                                "start_line": resource.start_line,
                                "target_path": target_path,
                            },
                        )
                    )
    return result


def _chunk(source, content, start, end, resource_id=None):
    return Chunk(
        id=stable_id(source.id, "chunk", str(start)),
        repo_id=source.repo_id,
        snapshot_id=source.snapshot_id,
        file_id=source.id,
        path=source.path,
        start_line=start,
        end_line=end,
        content=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        resource_id=resource_id,
    )
