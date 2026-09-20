from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
INDEX_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
}
IMAGE_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
}
EXPECTED_PLATFORMS = {("linux", "amd64"), ("linux", "arm64")}


def validate_index(index: dict[str, Any]) -> dict[tuple[str, str], str]:
    if index.get("mediaType") not in INDEX_MEDIA_TYPES:
        raise ValueError("publication is not an OCI/Docker image index")
    manifests = index.get("manifests")
    if not isinstance(manifests, list) or len(manifests) != 2:
        raise ValueError("index must contain exactly two runnable manifests")

    children: dict[tuple[str, str], str] = {}
    for descriptor in manifests:
        if descriptor.get("mediaType") not in IMAGE_MEDIA_TYPES:
            raise ValueError("index contains a non-runnable descriptor")
        platform = descriptor.get("platform")
        if not isinstance(platform, dict):
            raise ValueError("runnable descriptor lacks a platform")
        key = (platform.get("os"), platform.get("architecture"))
        if key not in EXPECTED_PLATFORMS or key in children:
            raise ValueError(f"unexpected or duplicate platform: {key}")
        digest = descriptor.get("digest")
        if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
            raise ValueError("child manifest has an invalid digest")
        children[key] = digest

    if set(children) != EXPECTED_PLATFORMS:
        raise ValueError("index does not contain one amd64 and one arm64 image")
    return children


def validate_image_inspect(
    image: dict[str, Any],
    *,
    architecture: str,
    component: str,
    source: str,
    revision: str,
) -> None:
    if image.get("Architecture") != architecture or image.get("Os") != "linux":
        raise ValueError("pulled image platform does not match its index descriptor")
    config = image.get("Config")
    if not isinstance(config, dict):
        raise ValueError("image config is missing")
    labels = config.get("Labels")
    if not isinstance(labels, dict):
        raise ValueError("image labels are missing")
    expected_labels = {
        "io.cfwarp.component": component,
        "org.opencontainers.image.source": source,
        "org.opencontainers.image.revision": revision,
    }
    if any(labels.get(key) != value for key, value in expected_labels.items()):
        raise ValueError("image component/source/revision labels do not match")
    expected_entrypoint = {
        "observer": ["cfwarp-service-eval-api"],
        "worker": ["cfwarp-eval-worker"],
    }[component]
    if config.get("Entrypoint") != expected_entrypoint:
        raise ValueError("image entrypoint does not match its component")
    if config.get("User") != "10001:10001":
        raise ValueError("image is not configured for the rootless runtime user")


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="validate a candidate OCI publication")
    subparsers = parser.add_subparsers(dest="command", required=True)
    index_parser = subparsers.add_parser("index")
    index_parser.add_argument("path", type=Path)
    image_parser = subparsers.add_parser("image")
    image_parser.add_argument("path", type=Path)
    image_parser.add_argument(
        "--architecture", required=True, choices=("amd64", "arm64")
    )
    image_parser.add_argument(
        "--component", required=True, choices=("observer", "worker")
    )
    image_parser.add_argument("--source", required=True)
    image_parser.add_argument("--revision", required=True)
    args = parser.parse_args()

    if args.command == "index":
        children = validate_index(load(args.path))
        print(
            json.dumps(
                {
                    architecture: children[("linux", architecture)]
                    for architecture in ("amd64", "arm64")
                }
            )
        )
        return
    validate_image_inspect(
        load(args.path),
        architecture=args.architecture,
        component=args.component,
        source=args.source,
        revision=args.revision,
    )


if __name__ == "__main__":
    main()
