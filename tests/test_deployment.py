import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("check_deploy", ROOT / "scripts/check-deploy.py")
assert SPEC is not None and SPEC.loader is not None
CHECKS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKS)


@pytest.mark.skipif(
    shutil.which("kustomize") is None or shutil.which("helm") is None,
    reason="Install mise-pinned kustomize and Helm",
)
def test_packaging_renders_and_obeys_safety_invariants():
    CHECKS.main()


@pytest.mark.skipif(shutil.which("kustomize") is None, reason="Install mise-pinned kustomize")
def test_root_container_is_rejected():
    documents = CHECKS.render("deploy/local")
    deployment = next(document for document in documents if document["kind"] == "Deployment")
    deployment["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"] = False
    with pytest.raises(AssertionError):
        CHECKS.check_local(documents)


def test_local_forward_refuses_an_unmanaged_context(tmp_path):
    kind = tmp_path / "kind"
    kind.write_text("#!/bin/sh\nprintf 'production\\n'\n")
    kind.chmod(0o755)
    kubectl = tmp_path / "kubectl"
    marker = tmp_path / "kubectl-was-called"
    kubectl.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    kubectl.chmod(0o755)
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/local-forward.sh")],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Dedicated local cluster is absent" in result.stderr
    assert not marker.exists()


@pytest.mark.skipif(shutil.which("helm") is None, reason="Install mise-pinned Helm")
def test_helm_image_storage_and_secret_overrides():
    overrides = ["--set", "persistence.corpus.storageClass=custom-storage"]
    for path in ["initContainers.init-db", "containers.api", "containers.worker"]:
        prefix = f"controllers.explorer.{path}"
        overrides += ["--set", f"{prefix}.image.repository=registry.example/explorer"]
        overrides += ["--set-string", f"{prefix}.image.tag=0.1.0@sha256:test"]
        overrides += ["--set", f"{prefix}.env.EXPLORER_DATABASE_URL.valueFrom.secretKeyRef.name=my-secret"]
    documents = CHECKS.render_helm(overrides)
    CHECKS.check_local(documents)
    pvc = next(document for document in documents if document["kind"] == "PersistentVolumeClaim")
    assert pvc["spec"]["storageClassName"] == "custom-storage"
    deployment = next(document for document in documents if document["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    for container in pod["containers"] + pod["initContainers"]:
        assert container["image"] == "registry.example/explorer:0.1.0@sha256:test"
        env = {item["name"]: item for item in container["env"]}
        assert env["EXPLORER_DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"] == "my-secret"
