#!/usr/bin/env python3
"""Repair rc2 A3 wheels with the build container's OpenMP runtime and validate it."""

import ctypes
import fnmatch
import hashlib
import io
import os
import platform
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from elftools.elf.elffile import ELFFile


def resolve_openmp_runtime(link_input):
    """Resolve GCC Toolset's libgomp linker script to its shared runtime."""
    library = Path(link_input).resolve(strict=True)
    data = library.read_bytes()
    if not data.startswith(b"\x7fELF"):
        # RHEL's GCC Toolset uses INPUT(/usr/lib64/libgomp.so.1), not a
        # symlink. Fail closed for other script forms rather than guessing.
        script = re.sub(r"/\*.*?\*/", "", data.decode("utf-8"), flags=re.DOTALL)
        inputs = re.findall(r"\bINPUT\s*\(\s*(/[^\s()]+/libgomp\.so\.1)\s*\)", script)
        if len(inputs) != 1:
            raise RuntimeError(f"Unsupported OpenMP linker input: {library}: {script}")
        library = Path(inputs[0]).resolve(strict=True)
        data = library.read_bytes()
    if not data.startswith(b"\x7fELF"):
        raise RuntimeError(f"OpenMP runtime is not ELF: {library}")
    elf = ELFFile(io.BytesIO(data))
    dynamic = elf.get_section_by_name(".dynamic")
    sonames = [tag.soname for tag in dynamic.iter_tags() if tag.entry.d_tag == "DT_SONAME"] if dynamic else []
    if sonames != ["libgomp.so.1"]:
        raise RuntimeError(f"Unexpected OpenMP runtime SONAME: {library}: {sonames}")
    print(f"Resolved OpenMP runtime: {link_input} -> {library}", flush=True)
    return library


def check_repaired_tags(wheel, arch):
    tags = wheel.stem.rsplit("-", 1)[1].split(".")
    target = f"manylinux_2_34_{arch}"
    if target not in tags:
        raise RuntimeError(f"Missing {target} platform tag: {wheel.name}")
    for tag in tags:
        if tag in {f"manylinux{alias}_{arch}" for alias in ("1", "2010", "2014")}:
            continue
        match = re.fullmatch(rf"manylinux_(\d+)_(\d+)_{arch}", tag)
        if not match or tuple(map(int, match.groups())) > (2, 34):
            raise RuntimeError(f"Unexpected platform tag: {tag}")


def bundled_openmp(original_files, files, expected_hash):
    added = files - original_files
    runtimes = {name for name in added if fnmatch.fnmatchcase(name, "vllm_ascend.libs/libgomp-*.so.*")}
    # auditwheel 6.8 also adds dependency provenance metadata for RPM libraries.
    metadata = {name for name in added if fnmatch.fnmatchcase(name, "*.dist-info/sboms/auditwheel.cdx.json")}
    if original_files - files or len(runtimes) != 1 or added != runtimes | metadata:
        raise RuntimeError(f"Unexpected payload changes: added={added}, removed={original_files - files}")
    bundled = runtimes.pop()
    if not Path(bundled).name.startswith(f"libgomp-{expected_hash}.so."):
        raise RuntimeError(f"auditwheel bundled a different OpenMP runtime: {bundled}")
    return bundled


def check_glibc(name, data):
    elf = ELFFile(io.BytesIO(data))
    needs = elf.get_section_by_name(".gnu.version_r")
    versions = set()
    if needs:
        for _, auxiliaries in needs.iter_versions():
            versions.update(aux.name for aux in auxiliaries if aux.name.startswith("GLIBC_"))
    for version in versions:
        parts = version.removeprefix("GLIBC_").split(".")
        if not all(part.isdigit() for part in parts) or tuple(map(int, parts)) > (2, 34, 0):
            raise RuntimeError(f"{name} requires unsupported {version}")
    print(f"GLIBC check passed: {name}: {sorted(versions)}", flush=True)


def check_openmp(library):
    runtime = ctypes.CDLL(str(library))
    runtime.omp_set_dynamic.argtypes = [ctypes.c_int]
    runtime.omp_set_dynamic.restype = None
    runtime.omp_set_dynamic(0)
    observed = []
    callback_type = ctypes.CFUNCTYPE(None, ctypes.c_void_p)

    @callback_type
    def worker(_):
        observed.append((runtime.omp_get_thread_num(), runtime.omp_get_num_threads()))

    runtime.GOMP_parallel.argtypes = [callback_type, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
    runtime.GOMP_parallel.restype = None
    runtime.GOMP_parallel(worker, None, 2, 0)
    if sorted(observed) != [(0, 2), (1, 2)]:
        raise RuntimeError(f"OpenMP parallel smoke test failed: {observed}")
    print(f"OpenMP smoke test passed: {observed}; max_threads={runtime.omp_get_max_threads()}", flush=True)


def main():
    root = Path(__file__).resolve().parents[4]
    wheel_dir = Path(sys.argv[1]).resolve()
    libc = os.confstr("CS_GNU_LIBC_VERSION")
    if libc != "glibc 2.34":
        raise RuntimeError(f"Expected the manylinux_2_34 build container, found {libc}")
    print(f"Repair and runtime validation environment: {libc}", flush=True)

    # Select the same library that CMake linked, rather than a runner's libgomp.
    libraries = set()
    for cache in (root / "build").rglob("CMakeCache.txt"):
        for line in cache.read_text().splitlines():
            if line.startswith("OpenMP_gomp_LIBRARY:FILEPATH="):
                libraries.add(line.split("=", 1)[1])
    if len(libraries) != 1:
        raise RuntimeError(f"Expected one CMake OpenMP library, found {libraries}")
    library = resolve_openmp_runtime(libraries.pop())
    check_glibc(str(library), library.read_bytes())
    runtime_hash = hashlib.sha256(library.read_bytes()).hexdigest()[:8]

    wheels = list(wheel_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"Expected one unrepaired wheel, found {wheels}")
    with zipfile.ZipFile(wheels[0]) as original:
        original_files = {name for name in original.namelist() if not name.endswith("/")}

    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = f"{library.parent}:{env.get('LD_LIBRARY_PATH', '')}"
    env["AUDITWHEEL_PLAT"] = f"manylinux_2_34_{platform.machine()}"
    subprocess.run(
        [sys.executable, str(Path(__file__).with_name("auto_exclude.py")), str(wheel_dir)], env=env, check=True
    )

    wheels = list(wheel_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"Unexpected repaired wheel tags: {wheels}")
    check_repaired_tags(wheels[0], platform.machine())
    with zipfile.ZipFile(wheels[0]) as repaired, tempfile.TemporaryDirectory() as tmp:
        files = {name for name in repaired.namelist() if not name.endswith("/")}
        bundled = bundled_openmp(original_files, files, runtime_hash)
        for name in files:
            if ".so" in Path(name).name:
                data = repaired.read(name)
                if data.startswith(b"\x7fELF"):
                    check_glibc(name, data)
        extension = next(name for name in files if "/vllm_ascend_C." in name and name.endswith(".so"))
        elf = ELFFile(io.BytesIO(repaired.read(extension)))
        needed = [
            tag.needed for tag in elf.get_section_by_name(".dynamic").iter_tags() if tag.entry.d_tag == "DT_NEEDED"
        ]
        if Path(bundled).name not in needed or "libgomp.so.1" in needed:
            raise RuntimeError(f"Extension is not linked to bundled OpenMP: {needed}")
        check_openmp(Path(repaired.extract(bundled, tmp)))
    print(f"A3 wheel validation passed: {wheels[0].name}", flush=True)


if __name__ == "__main__":
    main()
