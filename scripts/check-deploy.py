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


def check_security(documents: list[dict]) -> None:
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


def check_local(documents: list[dict]) -> None:
    check_security(documents)
    deployments = [document for document in documents if document["kind"] == "Deployment"]
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


def render_helm(
    extra_args: list[str] | None = None,
    *,
    values: list[str] | None = None,
    release: str = "k8s-explorer",
    namespace: str = "explorer",
) -> list[dict]:
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
            release,
            str(chart),
            "--namespace",
            namespace,
            *[
                argument
                for value in (values or ["deploy/helm/app-template/values.yaml"])
                for argument in ("--values", str(ROOT / value))
            ],
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


EMBEDDING_MODELS = {"qwen3-0.6b": "last-token", "bge-m3": "cls"}


def render_embeddings(provider: str) -> list[dict]:
    return render_helm(
        values=[
            "deploy/embeddings/tei/cpu-values.yaml",
            f"deploy/embeddings/tei/{provider}-values.yaml",
        ],
        release=provider.replace(".", "-"),
        namespace="embedding-eval",
    )


def check_embeddings(documents: list[dict], provider: str) -> None:
    check_security(documents)
    assert sorted(document["kind"] for document in documents) == [
        "Deployment",
        "PersistentVolumeClaim",
        "Service",
        "ServiceAccount",
    ], "CPU model examples must remain private and cluster-independent"
    configured = yaml.safe_load((ROOT / "config/evaluation/providers.yaml").read_text())
    expected = next(item for item in configured["providers"] if item["id"] == provider)
    deployment = next(document for document in documents if document["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    assert pod["nodeSelector"]["kubernetes.io/arch"] == "amd64"
    assert pod["securityContext"]["runAsUser"] == pod["securityContext"]["fsGroup"] == 568
    assert len(pod["containers"]) == 1 and not pod.get("initContainers")
    container = pod["containers"][0]
    assert container["image"] == (
        "ghcr.io/huggingface/text-embeddings-inference:cpu-1.9.4@"
        "sha256:8419f533857b503ebf6ec292a95d4f1cf9c0464ac8b8abeef39518cf110e5726"
    )
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env["MODEL_ID"] == expected["model"]
    assert env["REVISION"] == expected["model_revision"]
    assert env["POOLING"] == EMBEDDING_MODELS[provider]
    assert env["DTYPE"] == "float32" and env["PORT"] == "8080"
    assert env["HUGGINGFACE_HUB_CACHE"] == "/data/hub"
    assert env["AUTO_TRUNCATE"] == "true"  # Native client overrides with truncate:false.
    assert env["MAX_BATCH_TOKENS"] == "8192" and env["MAX_BATCH_REQUESTS"] == "1"
    assert env["MAX_CLIENT_BATCH_SIZE"] == env["MAX_CONCURRENT_REQUESTS"] == "32"
    assert env["MKL_ENABLE_INSTRUCTIONS"] == "AVX2"
    assert not {"API_KEY", "HF_TOKEN"} & env.keys(), "Never commit server credentials"
    assert container["resources"]["limits"]["cpu"] == "2"
    assert container["resources"]["limits"]["memory"] == "8Gi"
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        assert container[probe]["httpGet"] == {"path": "/health", "port": 8080}
    assert {mount["mountPath"] for mount in container["volumeMounts"]} == {"/data", "/tmp"}
    pvc = next(document for document in documents if document["kind"] == "PersistentVolumeClaim")
    assert "storageClassName" not in pvc["spec"]
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert pvc["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    assert volumes["cache"]["persistentVolumeClaim"]["claimName"] == pvc["metadata"]["name"]
    assert "emptyDir" in volumes["tmp"]
    service = next(document for document in documents if document["kind"] == "Service")
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"][0]["port"] == 8080


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
    for provider in EMBEDDING_MODELS:
        check_embeddings(render_embeddings(provider), provider)
    print("Local, Helm, Flux, and CPU embedding rendering and safety checks passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helm-only", action="store_true", help="Print all rendered Helm manifests")
    args = parser.parse_args()
    if args.helm_only:
        documents = render_helm()
        for provider in EMBEDDING_MODELS:
            documents.extend(render_embeddings(provider))
        print(yaml.safe_dump_all(documents, sort_keys=False))
    else:
        main()
