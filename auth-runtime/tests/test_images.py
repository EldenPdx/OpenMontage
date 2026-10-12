"""Public OCI staging boundaries, independent of identity service lifecycle."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("auth_images", Path(__file__).resolve().parents[1] / "images.py")
images = importlib.util.module_from_spec(spec)
spec.loader.exec_module(images)


@pytest.mark.parametrize("architecture,digest", [("amd64", "sha256:" + "a" * 64), ("arm64", "sha256:" + "b" * 64)])
def test_native_engine_selects_only_its_linux_descriptor(architecture, digest):
    platform = images.native_platform({"Os": "linux", "Arch": architecture})
    index = {"manifests": [
        {"digest": "sha256:" + "c" * 64, "platform": {"os": "unknown", "architecture": "unknown"}},
        {"digest": "sha256:" + "a" * 64, "platform": {"os": "linux", "architecture": "amd64"}},
        {"digest": "sha256:" + "b" * 64, "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}},
    ]}
    assert images.select_manifest(index, platform) == digest


@pytest.mark.parametrize("os_name,architecture", [("windows", "amd64"), ("linux", "ppc64le"), ("linux", "386"), ("linux", "unknown")])
def test_unsupported_native_engine_fails_explicitly(os_name, architecture):
    with pytest.raises(RuntimeError, match="Unsupported Docker engine platform"):
        images.native_platform({"Os": os_name, "Arch": architecture})


def test_missing_or_ambiguous_platform_descriptor_is_rejected():
    descriptor = {"digest": "sha256:" + "a" * 64, "platform": {"os": "linux", "architecture": "amd64"}}
    for manifests in ([], [descriptor, descriptor]):
        with pytest.raises(RuntimeError, match="exactly one linux/amd64 manifest"):
            images.select_manifest({"manifests": manifests}, ("linux", "amd64"))


@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
def test_configuration_and_layer_metadata_match_the_selected_platform(architecture):
    manifest = {"layers": [{"digest": "sha256:" + "d" * 64, "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"}]}
    config = {"os": "linux", "architecture": architecture, "rootfs": {"diff_ids": ["sha256:" + "e" * 64]}}
    images.validate_manifest_config(manifest, config, ("linux", architecture))
    with pytest.raises(RuntimeError, match="wrong platform"):
        images.validate_manifest_config(manifest, config, ("linux", "arm64" if architecture == "amd64" else "amd64"))
    with pytest.raises(RuntimeError, match="layer count"):
        images.validate_manifest_config({"layers": []}, config, ("linux", architecture))
    with pytest.raises(RuntimeError, match="layer descriptor"):
        images.validate_manifest_config({"layers": [{"digest": "invalid", "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"}]}, config, ("linux", architecture))
