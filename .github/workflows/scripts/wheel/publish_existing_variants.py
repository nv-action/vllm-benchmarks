# SPDX-License-Identifier: Apache-2.0
"""Validate and publish existing rc2 device wheels without rebuilding."""

import argparse
import hashlib
import json
import os
import zipfile
from email.parser import BytesParser
from itertools import product
from pathlib import Path

from obs import ObsClient
from repair_a3_wheel import check_glibc, check_repaired_tags
from variantlib.constants import VALIDATION_WHEEL_NAME_REGEX
from variantlib.variant_dist_info import VariantDistInfo
from variantlib.variants_json import VariantsJson

NAMEVER = "vllm_ascend-0.30.0rc2"
LABELS = ("910b", "950", "310p")
PYTHONS = ("cp310", "cp311", "cp312")
ARCHITECTURES = ("x86_64", "aarch64")
BUCKET = "ascend-artifcat-packages"
PREFIX = "pypi/packages/ascend/repos/pypi/variant/vllm-ascend"


def validate(directory, labels=LABELS):
    expected = set(product(labels, PYTHONS, ARCHITECTURES))
    seen = set()
    wheels = sorted(directory.glob("*.whl"))
    for wheel in wheels:
        match = VALIDATION_WHEEL_NAME_REGEX.fullmatch(wheel.name)
        if match is None or match["namever"] != NAMEVER:
            raise ValueError(f"Unexpected release wheel: {wheel.name}")
        label, python, platform = match["variant_label"], match["pyver"], match["plat"]
        arch = next((arch for arch in ARCHITECTURES if platform.endswith(f"_{arch}")), None)
        key = (label, python, arch)
        if key not in expected or key in seen or match["abi"] != python:
            raise ValueError(f"Unexpected or duplicate release combination: {wheel.name}")
        check_repaired_tags(Path(f"{match['base_wheel_name']}.whl"), arch)
        with zipfile.ZipFile(wheel) as archive:
            info = f"{NAMEVER}.dist-info"
            metadata = BytesParser().parsebytes(archive.read(f"{info}/METADATA"))
            if metadata["Name"].replace("-", "_") != "vllm_ascend" or metadata["Version"] != "0.30.0rc2":
                raise ValueError(f"Package metadata mismatch: {wheel.name}")
            tags = BytesParser().parsebytes(archive.read(f"{info}/WHEEL")).get_all("Tag", [])
            if set(tags) != {f"{python}-{python}-{tag}" for tag in platform.split(".")}:
                raise ValueError(f"Wheel metadata tags mismatch: {wheel.name}")
            variant = VariantDistInfo(archive.read(f"{info}/variant.json"), label)
            if {prop.to_str() for prop in variant.variant_desc.properties} != {f"ascend :: npu_type :: {label}"}:
                raise ValueError(f"Variant properties mismatch: {wheel.name}")
            for name in archive.namelist():
                with archive.open(name) as source:
                    magic = source.read(4)
                    if magic == b"\x7fELF":
                        check_glibc(name, magic + source.read())
        seen.add(key)
        print(f"Validated {wheel.name}: sha256={hashlib.sha256(wheel.read_bytes()).hexdigest()}")
    if seen != expected:
        raise ValueError(f"Missing release combinations: {sorted(expected - seen)}")
    index = VariantsJson(json.loads((directory / f"{NAMEVER}-variants.json").read_text()))
    if set(index.variants) != set(labels):
        raise ValueError(f"Unexpected generated index labels: {sorted(index.variants)}")
    return wheels


def merge_index(current, previous):
    index = VariantsJson(json.loads(current))
    if previous is not None:
        index.merge(VariantsJson(json.loads(previous)))
    return index.to_str()


def publish(client, directory, wheels):
    index = directory / f"{NAMEVER}-variants.json"
    key = f"{PREFIX}/{index.name}"
    response = client.getObject(BUCKET, key, loadStreamInMemory=True)
    previous = None
    if response.status < 300:
        previous = response.body.buffer
        backup = directory / "previous-index" / index.name
        backup.parent.mkdir(exist_ok=True)
        backup.write_bytes(previous)
    elif response.status != 404:
        raise RuntimeError(f"Cannot read existing index: {response.status}: {response.errorMessage}")
    index.write_text(merge_index(index.read_text(), previous))
    # Make every wheel available before advertising variants in the shared index.
    for path in [*wheels, index]:
        key = f"{PREFIX}/{path.name}"
        response = client.putFile(BUCKET, key, str(path))
        if response.status >= 300:
            raise RuntimeError(f"Upload failed: {path.name}: {response.errorMessage}")
        response = client.getObjectMetadata(BUCKET, key)
        if response.status >= 300 or response.body.contentLength != path.stat().st_size:
            raise RuntimeError(f"Uploaded size verification failed: {path.name}")
        print(f"Uploaded and verified: {path.name}", flush=True)
    response = client.getObject(BUCKET, f"{PREFIX}/{index.name}", loadStreamInMemory=True)
    if response.status >= 300 or response.body.buffer != index.read_bytes():
        raise RuntimeError("Published index readback differs from local index")
    print(
        f"Publication complete: {len(wheels)} wheels; index labels={sorted(json.loads(index.read_text())['variants'])}"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "publish"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--devices", choices=("a2-a5-310p", "a3"), default="a2-a5-310p")
    args = parser.parse_args()
    labels = ("a3",) if args.devices == "a3" else LABELS
    wheels = validate(args.directory, labels)
    if args.action == "publish":
        client = ObsClient(
            access_key_id=os.environ["OBS_ACCESS_KEY"],
            secret_access_key=os.environ["OBS_SECRET_KEY"],
            server="https://obs.cn-north-4.myhuaweicloud.com",
        )
        try:
            publish(client, args.directory, wheels)
        finally:
            client.close()


if __name__ == "__main__":
    main()
