"""Verify pinned registry content in this process, then import local Docker archives."""

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile

import requests

ROOT = Path(__file__).resolve().parent
PINS = json.loads((ROOT / "images.lock.json").read_text())
MEDIA_TYPES = ", ".join(("application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json"))


def verify_file(path, digest):
    with Path(path).open("rb") as stream:
        value = hashlib.file_digest(stream, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else None
        if value is None:
            value_hash = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                value_hash.update(chunk)
            value = value_hash.hexdigest()
    if "sha256:" + value != digest:
        raise RuntimeError("Image content failed SHA-256 verification")


def local_tag(service):
    pin = PINS[service]
    return f"openmontage-auth/{service}:{pin['version']}-{pin['digest'][7:19]}"


def native_platform(server):
    platform = (server.get("Os"), server.get("Arch"))
    if platform not in {("linux", "amd64"), ("linux", "arm64")}:
        raise RuntimeError(f"Unsupported Docker engine platform {platform[0]}/{platform[1]}; require linux/amd64 or linux/arm64")
    return platform


def select_manifest(index, platform):
    matches = [descriptor for descriptor in index["manifests"]
               if (descriptor.get("platform", {}).get("os"), descriptor.get("platform", {}).get("architecture")) == platform]
    if len(matches) != 1:
        raise RuntimeError(f"Pinned image must have exactly one {platform[0]}/{platform[1]} manifest")
    return matches[0]["digest"]


def validate_manifest_config(manifest, config, platform):
    if (config.get("os"), config.get("architecture")) != platform:
        raise RuntimeError("Pinned image has the wrong platform")
    if len(config["rootfs"]["diff_ids"]) != len(manifest["layers"]):
        raise RuntimeError("Image layer count disagrees with its configuration")
    for layer, diff_id in zip(manifest["layers"], config["rootfs"]["diff_ids"]):
        if not all(re.fullmatch(r"sha256:[a-f0-9]{64}", digest) for digest in (layer["digest"], diff_id)):
            raise RuntimeError("Invalid image layer descriptor")


def stage_images(state, *, online=False):
    platform = native_platform(json.loads(subprocess.check_output(["docker", "version", "--format", "{{json .Server}}"], text=True)))
    cache = Path(state) / "images"
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    session = requests.Session()
    session.trust_env = False
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    records = {}
    for name, pin in PINS.items():
        registry, repository = pin["source"].split("/", 1)
        repository = repository.rsplit(":", 1)[0]
        base = f"https://{registry}/v2/{repository}"
        token = None

        def fetch(kind, digest):
            nonlocal token
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
                raise RuntimeError("Invalid pinned image descriptor")
            path = cache / digest[7:]
            if path.exists():
                verify_file(path, digest)
                return path
            if not (online or proxy):
                raise RuntimeError("Uncached image content requires explicit --online permission or a current-process HTTPS proxy")
            headers = {"Accept": MEDIA_TYPES}
            if token:
                headers["Authorization"] = "Bearer " + token
            response = session.get(f"{base}/{kind}/{digest}", headers=headers, stream=True, timeout=60)
            if response.status_code == 401:
                challenge = response.headers.get("WWW-Authenticate", "")
                response.close()
                fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
                realm = fields.get("realm", "")
                if not realm.startswith("https://"):
                    raise RuntimeError("Registry supplied an untrusted authentication endpoint")
                auth = session.get(realm, params={k: fields[k] for k in ("service", "scope") if k in fields}, timeout=30)
                if auth.status_code != 200:
                    raise RuntimeError(f"Registry authentication failed ({auth.status_code})")
                token = auth.json().get("token") or auth.json().get("access_token")
                if not token:
                    raise RuntimeError("Registry did not supply a download token")
                headers["Authorization"] = "Bearer " + token
                response = session.get(f"{base}/{kind}/{digest}", headers=headers, stream=True, timeout=60)
            if response.status_code != 200:
                response.close()
                raise RuntimeError(f"Registry content download failed ({response.status_code})")
            pending = path.with_suffix(".partial")
            try:
                with response, pending.open("wb") as stream:
                    for chunk in response.iter_content(1024 * 1024):
                        stream.write(chunk)
                verify_file(pending, digest)
                pending.replace(path)
            finally:
                pending.unlink(missing_ok=True)
            return path

        index = json.loads(fetch("manifests", pin["digest"]).read_bytes())
        manifest_digest = pin["digest"]
        if "manifests" in index:
            manifest_digest = select_manifest(index, platform)
            index = json.loads(fetch("manifests", manifest_digest).read_bytes())
        config_digest = index["config"]["digest"]
        config_path = fetch("blobs", config_digest)
        config = json.loads(config_path.read_bytes())
        validate_manifest_config(index, config, platform)
        tag = local_tag(name)
        existing = subprocess.run(["docker", "image", "inspect", config_digest, "--format", "{{.Id}}"],
                                  capture_output=True, text=True)
        if existing.returncode == 0:
            subprocess.run(["docker", "tag", config_digest, tag], check=True, capture_output=True)
        else:
            archive_dir = cache / (name + "-archive")
            archive_dir.mkdir(exist_ok=True)
            shutil.copyfile(config_path, archive_dir / (config_digest[7:] + ".json"))
            layer_names = []
            diff_ids = config["rootfs"]["diff_ids"]
            for layer, diff_id in zip(index["layers"], diff_ids):
                blob = fetch("blobs", layer["digest"])
                destination = archive_dir / (diff_id[7:] + ".tar")
                if not destination.exists():
                    media_type = layer["mediaType"]
                    if media_type.endswith("gzip") or media_type.endswith("+gzip"):
                        with gzip.open(blob, "rb") as source, destination.open("wb") as target:
                            shutil.copyfileobj(source, target)
                    elif media_type.endswith(".tar"):
                        shutil.copyfile(blob, destination)
                    else:
                        raise RuntimeError("Pinned image uses an unsupported layer format")
                verify_file(destination, diff_id)
                layer_names.append(destination.name)
            (archive_dir / "manifest.json").write_text(json.dumps([{
                "Config": config_digest[7:] + ".json", "RepoTags": [tag], "Layers": layer_names}]))
            archive = cache / (name + ".tar")
            with tarfile.open(archive, "w") as output:
                for path in archive_dir.iterdir():
                    output.add(path, arcname=path.name, recursive=False)
            loaded = subprocess.run(["docker", "load", "--quiet", "--input", str(archive)], capture_output=True)
            if loaded.returncode:
                raise RuntimeError("Docker rejected the verified local image archive")
            archive.unlink()
            shutil.rmtree(archive_dir)
        inspected = json.loads(subprocess.check_output(["docker", "image", "inspect", tag], text=True))[0]
        if (inspected["Os"], inspected["Architecture"]) != platform:
            raise RuntimeError("Loaded image has the wrong platform")
        if inspected["RootFS"]["Layers"] != config["rootfs"]["diff_ids"]:
            raise RuntimeError("Loaded image layers disagree with the verified configuration")
        # Docker's containerd store uses manifest IDs; verify the exported raw config instead.
        exported = cache / (name + "-verify.tar")
        try:
            subprocess.run(["docker", "image", "save", "--output", str(exported), tag], check=True, capture_output=True)
            with tarfile.open(exported) as saved:
                saved_manifest = json.load(saved.extractfile("manifest.json"))
                raw_config = saved.extractfile(saved_manifest[0]["Config"]).read()
            if "sha256:" + hashlib.sha256(raw_config).hexdigest() != config_digest:
                raise RuntimeError("Loaded image configuration failed SHA-256 verification")
        finally:
            exported.unlink(missing_ok=True)
        records[name] = {**pin, "platform_manifest": manifest_digest, "config_digest": config_digest,
                         "image_id": inspected["Id"], "local_tag": tag, "platform": "/".join(platform)}
        print(f"Verified and staged {name} {pin['version']} ({'/'.join(platform)})", flush=True)
    (cache / "loaded.json").write_text(json.dumps(records, indent=2) + "\n")
    return records


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--online", action="store_true", help="Allow uncached downloads; default is offline without a process proxy")
    arguments = parser.parse_args()
    try:
        stage_images(ROOT / ".state", online=arguments.online)
    except (RuntimeError, requests.RequestException, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error) if isinstance(error, RuntimeError) else "Image staging failed; private diagnostics required")
