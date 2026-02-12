#!/usr/bin/env python3
"""
Generate test-to-code mapping from coverage data.

Usage:
    python .github/workflows/scripts/gen_mapping.py
"""

import ast
import glob
import json
import logging
from pathlib import Path

import coverage

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


class FunctionParser(ast.NodeVisitor):
    """AST visitor to extract top-level functions and class methods."""

    def __init__(self):
        self.functions: dict[tuple[int, int], str] = {}
        self._in_nested = False
        self._class_stack: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef):
        if not self._in_nested:
            if self._class_stack:
                sig = f"{self._class_stack[-1]}::{node.name}"
            else:
                sig = node.name
            self.functions[(node.lineno, node.end_lineno)] = sig

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self.visit_FunctionDef(node)

    def visit_ClassDef(self, node: ast.ClassDef):
        self._class_stack.append(node.name)
        # Visit class body for methods
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sig = f"{node.name}::{item.name}"
                self.functions[(item.lineno, item.end_lineno)] = sig
                # Check for nested functions inside method (should be ignored)
                for inner_item in ast.walk(item):
                    if isinstance(inner_item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self._in_nested = True
                        break
        self._class_stack.pop()

    def parse_file(self, filepath: str) -> None:
        """Parse a Python file and extract function signatures."""
        try:
            with open(filepath, encoding="utf-8") as f:
                content = f.read()
            self.parse_from_code(content)
        except (SyntaxError, FileNotFoundError, UnicodeDecodeError) as e:
            logger.warning(f"Failed to parse {filepath}: {e}")

    def parse_from_code(self, code: str) -> None:
        """Parse Python code string and extract function signatures."""
        tree = ast.parse(code)
        self.visit(tree)

    def get_line_mapping(self) -> dict[int, str]:
        """Get a mapping from line number to function signature."""
        line_to_func: dict[int, str] = {}
        for (start, end), sig in self.functions.items():
            for line in range(start, end + 1):
                line_to_func[line] = sig
        return line_to_func


def generate_mapping() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """
    Generate test-to-code mapping from coverage data.

    Returns:
        file_mapping: {filepath: {test_names}}
        func_mapping: {filepath::signature: {test_names}}
    """
    file_mapping: dict[str, set[str]] = {}
    func_mapping: dict[str, set[str]] = {}

    # Find and load coverage data
    cov_files = glob.glob(".coverage") + glob.glob(".coverage.*")
    if not cov_files:
        raise FileNotFoundError("No .coverage file found. Run tests with ENABLE_MAPPING_GEN=1 first.")

    # Load coverage data from multiple runs
    for cov_file in cov_files:
        cov = coverage.Coverage(data_file=cov_file)
        cov.load()

        for filename in cov.get_data().measured_files():
            # Only process vllm_ascend files
            if "/vllm_ascend/" not in filename and "\\vllm_ascend\\" not in filename:
                continue

            # Get test contexts for this file
            contexts = cov.get_data().contexts_by_filename(filename)

            if not contexts:
                continue

            # Collect all test names for this file
            all_tests: set[str] = set()
            for ctx_list in contexts.values():
                all_tests.update(ctx_list)

            # Determine if this is a utils/config file (file-level mapping)
            if "vllm_ascend/utils" in filename or "vllm_ascend/config" in filename:
                file_mapping[filename] = all_tests
            else:
                # Function-level mapping
                parser = FunctionParser()
                parser.parse_file(filename)
                line_to_func = parser.get_line_mapping()

                for line_num, tests in contexts.items():
                    func_sig = line_to_func.get(line_num)
                    if func_sig:
                        key = f"{filename}::{func_sig}"
                        if key not in func_mapping:
                            func_mapping[key] = set()
                        func_mapping[key].update(tests)

    return file_mapping, func_mapping


def main():
    """Main entry point."""
    logger.info("Generating test-to-code mapping...")

    file_mapping, func_mapping = generate_mapping()

    # Convert sets to lists for JSON serialization
    result = {
        "file_mapping": {k: sorted(list(v)) for k, v in file_mapping.items()},
        "func_mapping": {k: sorted(list(v)) for k, v in func_mapping.items()},
    }

    # Output file with timestamp
    timestamp = int(Path(".coverage").stat().st_mtime) if Path(".coverage").exists() else None
    output_file = f"mapping_{int(timestamp) if timestamp else 'latest'}.json"

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    logger.info(f"Mapping saved to {output_file}")
    logger.info(f"  File mappings: {len(file_mapping)} files")
    logger.info(f"  Function mappings: {len(func_mapping)} functions")


if __name__ == "__main__":
    main()
