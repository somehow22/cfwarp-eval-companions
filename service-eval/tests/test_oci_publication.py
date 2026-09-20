import copy

import pytest

from cfwarp_service_eval.oci_publication import validate_image_inspect, validate_index


SOURCE = "https://github.com/somehow22/cfwarp-eval-companions"
REVISION = "a" * 40


def index():
    return {
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": "sha256:" + "1" * 64,
                "platform": {"os": "linux", "architecture": "amd64"},
            },
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": "sha256:" + "2" * 64,
                "platform": {"os": "linux", "architecture": "arm64"},
            },
        ],
    }


def image():
    return {
        "Architecture": "arm64",
        "Os": "linux",
        "Config": {
            "User": "10001:10001",
            "Entrypoint": ["cfwarp-eval-worker"],
            "Labels": {
                "io.cfwarp.component": "worker",
                "org.opencontainers.image.source": SOURCE,
                "org.opencontainers.image.revision": REVISION,
            },
        },
    }


def test_index_requires_exactly_one_runnable_manifest_per_platform():
    children = validate_index(index())
    assert children[("linux", "arm64")] == "sha256:" + "2" * 64


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["manifests"].append(copy.deepcopy(value["manifests"][0])),
        lambda value: value["manifests"][1]["platform"].update(architecture="amd64"),
        lambda value: value["manifests"][1]["platform"].update(architecture="unknown"),
        lambda value: value["manifests"][1].update(
            mediaType="application/vnd.in-toto+json",
            platform={"os": "unknown", "architecture": "unknown"},
        ),
        lambda value: value["manifests"][1].update(digest="sha256:malformed"),
    ],
)
def test_index_rejects_platform_and_attestation_confusion(mutate):
    candidate = index()
    mutate(candidate)
    with pytest.raises(ValueError):
        validate_index(candidate)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("Architecture",), "amd64"),
        (("Config", "User"), "0:0"),
        (("Config", "Entrypoint"), ["cfwarp-service-eval-api"]),
        (("Config", "Labels", "io.cfwarp.component"), "browser-worker"),
        (("Config", "Labels", "org.opencontainers.image.source"), "wrong-source"),
        (("Config", "Labels", "org.opencontainers.image.revision"), "b" * 40),
    ],
)
def test_image_config_rejects_wrong_platform_runtime_and_provenance(path, value):
    candidate = image()
    target = candidate
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        validate_image_inspect(
            candidate,
            architecture="arm64",
            component="worker",
            source=SOURCE,
            revision=REVISION,
        )


def test_image_config_accepts_arm64_worker():
    validate_image_inspect(
        image(),
        architecture="arm64",
        component="worker",
        source=SOURCE,
        revision=REVISION,
    )
