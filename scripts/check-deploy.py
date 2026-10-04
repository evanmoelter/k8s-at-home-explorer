"""Render packaging and enforce deployment safety invariants without a cluster."""

import argparse
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def render(path: str) -> list[dict]:
    result = subprocess.run(
        ["kustomize", "build", str(ROOT / path)], check=True, capture_output=True, text=True
    )
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def check_local(documents: list[dict]) -> None:
    deployments = [document for document in documents if document["kind"] == "Deployment"]
    for deployment in deployments:
        spec = deployment["spec"]
        assert spec["replicas"] == 1
        assert spec["strategy"]["type"] == "Recreate"
        pod = spec["template"]["spec"]
        assert pod["automountServiceAccountToken"] is False
        assert pod["securityContext"]["runAsNonRoot"] is True
        assert pod["securityContext"]["fsGroup"] > 0
        for container in pod["containers"] + pod.get("initContainers", []):
            security = container["securityContext"]
            assert security["allowPrivilegeEscalation"] is False
            assert security["readOnlyRootFilesystem"] is True
            assert security["capabilities"]["drop"] == ["ALL"]
            assert container["resources"]["requests"]["cpu"]
            assert container["resources"]["requests"]["memory"]
            assert container["resources"]["limits"]["memory"]
    explorer = next(document for document in deployments if document["metadata"]["name"] == "k8s-explorer")
    pod = explorer["spec"]["template"]["spec"]
    assert {container["args"][0] for container in pod["containers"]} == {"serve", "worker"}
    assert pod["initContainers"][0]["args"] == ["init-db"]
    for container in pod["containers"]:
        mounts = {mount["mountPath"] for mount in container["volumeMounts"]}
        assert {"/data", "/tmp", "/config"} <= mounts
    for secret in (document for document in documents if document["kind"] == "Secret"):
        assert not secret.get("data") and not secret.get("stringData"), "Do not commit credentials"


CHART_VERSION = "5.2.1"
CHART_URL = "oci://ghcr.io/bjw-s-labs/helm/app-template"


def render_helm(extra_args: list[str] | None = None) -> list[dict]:
    cache = ROOT / ".data" / "helm"
    cache.mkdir(parents=True, exist_ok=True)
    chart = cache / f"app-template-{CHART_VERSION}.tgz"
    if not chart.exists():
        subprocess.run(
            ["helm", "pull", CHART_URL, "--version", CHART_VERSION, "--destination", str(cache)],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    result = subprocess.run(
        [
            "helm",
            "template",
            "k8s-explorer",
            str(chart),
            "--namespace",
            "explorer",
            "--values",
            str(ROOT / "deploy/helm/app-template/values.yaml"),
            *(extra_args or []),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def check_helm(documents: list[dict]) -> None:
    check_local(documents)
    assert {document["kind"] for document in documents} <= {
        "ServiceAccount",
        "ConfigMap",
        "PersistentVolumeClaim",
        "Service",
        "Deployment",
    }, "The reusable example must not require cluster-specific controllers"
    pvc = next(document for document in documents if document["kind"] == "PersistentVolumeClaim")
    assert "storageClassName" not in pvc["spec"], "Use the installer's default storage class"
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert pvc["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    service = next(document for document in documents if document["kind"] == "Service")
    assert service["spec"]["type"] == "ClusterIP"
    deployment = next(document for document in documents if document["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    for container in pod["containers"] + pod["initContainers"]:
        env = {item["name"]: item for item in container["env"]}
        assert env["EXPLORER_DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
            "name": "explorer-credentials",
            "key": "EXPLORER_DATABASE_URL",
        }
    api = next(container for container in pod["containers"] if container["name"] == "api")
    env = {item["name"]: item for item in api["env"]}
    assert env["EXPLORER_API_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "explorer-credentials",
        "key": "EXPLORER_API_TOKEN",
    }
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    assert volumes["corpus"]["persistentVolumeClaim"]["claimName"] == pvc["metadata"]["name"]


def check_flux(documents: list[dict]) -> None:
    source = next(document for document in documents if document["kind"] == "OCIRepository")
    assert source["spec"]["url"] == CHART_URL
    assert source["spec"]["ref"]["tag"] == CHART_VERSION
    assert source["spec"]["ref"]["digest"].startswith("sha256:")
    release = next(document for document in documents if document["kind"] == "HelmRelease")
    config = next(document for document in documents if document["kind"] == "ConfigMap")
    assert release["spec"]["valuesFrom"] == [
        {"kind": "ConfigMap", "name": config["metadata"]["name"], "valuesKey": "values.yaml"}
    ]
    assert yaml.safe_load(config["data"]["values.yaml"]) == yaml.safe_load(
        (ROOT / "deploy/helm/app-template/values.yaml").read_text()
    ), "Flux and direct Helm must share the same values"


def main() -> None:
    check_local(render("deploy/local"))
    check_helm(render_helm())
    check_flux(render("deploy/helm"))
    print("Local, upstream Helm, and generic Flux rendering and safety checks passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helm-only", action="store_true", help="Print rendered Helm manifests")
    args = parser.parse_args()
    if args.helm_only:
        print(yaml.safe_dump_all(render_helm(), sort_keys=False))
    else:
        main()
