#!/usr/bin/env python3
"""Repair rc2 A3 wheels with the build container's OpenMP runtime and validate it."""

import ctypes
import fnmatch
import io
import os
import platform
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from elftools.elf.elffile import ELFFile


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
    library = Path(libraries.pop()).resolve(strict=True)
    print(f"Build-time OpenMP library: {library}", flush=True)
    check_glibc(str(library), library.read_bytes())

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
    if len(wheels) != 1 or f"-{env['AUDITWHEEL_PLAT']}.whl" not in wheels[0].name:
        raise RuntimeError(f"Unexpected repaired wheel tags: {wheels}")
    with zipfile.ZipFile(wheels[0]) as repaired, tempfile.TemporaryDirectory() as tmp:
        files = {name for name in repaired.namelist() if not name.endswith("/")}
        added = files - original_files
        if original_files - files or len(added) != 1:
            raise RuntimeError(f"Unexpected payload changes: added={added}, removed={original_files - files}")
        bundled = added.pop()
        if not fnmatch.fnmatchcase(bundled, "vllm_ascend.libs/libgomp-*.so.*"):
            raise RuntimeError(f"Unexpected bundled dependency: {bundled}")
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
