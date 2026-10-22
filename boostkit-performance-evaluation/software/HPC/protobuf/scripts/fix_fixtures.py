#!/usr/bin/env python3
"""Fix each pinned AccLibBenchmark fixture's seed in the private checkout."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

GENERATORS = (
    "ScalarInt32", "ScalarUInt32", "ScalarSInt32", "ScalarInt64",
    "ScalarUInt64", "ScalarSInt64", "ScalarBool", "ScalarEnum",
    "ScalarDouble", "ScalarFloat", "RepeatedInt32", "RepeatedUInt32",
    "RepeatedSInt32", "RepeatedInt64", "RepeatedUInt64", "RepeatedSInt64",
    "RepeatedDouble", "RepeatedFloat",
)


def adapt(source: str) -> str:
    original = """inline std::uint64_t genIntRandomUniformBytes(std::uint8_t maxByteNum) {
    static std::random_device rd;
    static std::mt19937 engine(rd());"""
    replacement = """inline std::mt19937 fixtureEngine(std::uint32_t type, std::size_t count) {
    std::seed_seq seed{0x50424658U, type, static_cast<std::uint32_t>(count),
                       static_cast<std::uint32_t>(count >> 32)};
    return std::mt19937(seed);
}

inline std::uint64_t genIntRandomUniformBytes(std::mt19937 &engine,
                                               std::uint8_t maxByteNum) {"""
    if source.count(original) != 1:
        raise ValueError("pinned integer generator differs from the expected source")
    source = source.replace(original, replacement)

    for fixture_id, name in enumerate(GENERATORS, start=1):
        pattern = rf"(inline pb::test::{name} gen{name}\(std::size_t count\) \{{\n)(.*?)(\n\}})"
        match = re.search(pattern, source, flags=re.DOTALL)
        if not match:
            raise ValueError(f"pinned generator is missing: {name}")
        body = match.group(2)
        if name.endswith(("Bool", "Enum", "Double", "Float")):
            random_seed = "    std::random_device rd;\n    std::mt19937 gen(rd());"
            if body.count(random_seed) != 1:
                raise ValueError(f"unexpected random initialization: {name}")
            body = body.replace(random_seed, f"    auto gen = fixtureEngine({fixture_id}, count);")
        else:
            declaration = f"    pb::test::{name} data;"
            if body.count(declaration) != 1:
                raise ValueError(f"unexpected generator declaration: {name}")
            body = body.replace(
                declaration,
                declaration + f"\n    auto engine = fixtureEngine({fixture_id}, count);",
            )
            calls = body.count("genIntRandomUniformBytes(")
            if calls != 1:
                raise ValueError(f"unexpected integer generator call count: {name}: {calls}")
            body = body.replace("genIntRandomUniformBytes(", "genIntRandomUniformBytes(engine, ")
        source = source[: match.start(2)] + body + source[match.end(2) :]

    if "random_device" in source:
        raise ValueError("a random_device call remains in benchmark_common.h")
    return source


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("header", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    source = args.header.read_text(encoding="utf-8")
    adapted = adapt(source)
    if not args.check:
        args.header.write_text(adapted, encoding="utf-8")
    print("[protobuf-fixtures] fixed 18 random generators; String and Bytes remain seeded at 42")


if __name__ == "__main__":
    main()
