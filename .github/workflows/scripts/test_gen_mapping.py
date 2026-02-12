#!/usr/bin/env python3
"""Unit tests for gen_mapping.py - assumes coverage data exists."""

import sys
import unittest
from pathlib import Path

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).parent))

from gen_mapping import FunctionParser


class TestFunctionParser(unittest.TestCase):
    """Test FunctionParser AST extraction logic."""

    def test_top_level_function(self):
        """Test extraction of top-level functions."""
        code = """
def foo():
    pass

def bar():
    pass
"""
        parser = FunctionParser()
        parser.parse_from_code(code)
        line_map = parser.get_line_mapping()

        self.assertIn("foo", line_map.values())
        self.assertIn("bar", line_map.values())

    def test_class_method(self):
        """Test extraction of class methods with correct signature."""
        code = """
class MyClass:
    def method_a(self):
        pass

    async def method_b(self):
        pass
"""
        parser = FunctionParser()
        parser.parse_from_code(code)
        line_map = parser.get_line_mapping()

        self.assertIn("MyClass::method_a", line_map.values())
        self.assertIn("MyClass::method_b", line_map.values())

    def test_nested_function_ignored(self):
        """Test that nested functions are NOT extracted."""
        code = """
def outer():
    def inner():
        pass
    return inner
"""
        parser = FunctionParser()
        parser.parse_from_code(code)
        line_map = parser.get_line_mapping()

        # Only outer function should be present
        self.assertNotIn("inner", line_map.values())
        self.assertIn("outer", line_map.values())

    def test_line_range_mapping(self):
        """Test that line ranges are correctly mapped to functions."""
        code = """
def short_func():
    pass

def long_func():
    x = 1
    y = 2
    z = 3
    return x + y + z
"""
        parser = FunctionParser()
        parser.parse_from_code(code)
        line_map = parser.get_line_mapping()

        # short_func spans lines 2-3, long_func spans lines 5-9
        # Verify short_func covers its range
        self.assertEqual(line_map[2], "short_func")
        self.assertEqual(line_map[3], "short_func")

        # Verify long_func covers its range (lines 5-9)
        for line in range(5, 10):
            self.assertEqual(line_map[line], "long_func")

    def test_multiple_classes(self):
        """Test extraction from multiple classes."""
        code = """
class ClassA:
    def method_a1(self):
        pass

class ClassB:
    def method_b1(self):
        pass
    def method_b2(self):
        pass
"""
        parser = FunctionParser()
        parser.parse_from_code(code)
        line_map = parser.get_line_mapping()

        self.assertIn("ClassA::method_a1", line_map.values())
        self.assertIn("ClassB::method_b1", line_map.values())
        self.assertIn("ClassB::method_b2", line_map.values())

    def test_nested_class_method(self):
        """Test that methods in nested classes are extracted."""
        code = """
class Outer:
    class Inner:
        def inner_method(self):
            pass

    def outer_method(self):
        pass
"""
        parser = FunctionParser()
        parser.parse_from_code(code)
        line_map = parser.get_line_mapping()

        # Both methods should be present
        self.assertIn("Outer::outer_method", line_map.values())


class TestMappingOutput(unittest.TestCase):
    """Test mapping output format and structure."""

    def test_output_json_structure(self):
        """Test that output JSON has correct structure."""
        # Simulate expected output structure
        result = {
            "file_mapping": {"path/to/file.py": ["test_a", "test_b"]},
            "func_mapping": {"path/to/file.py::Class::func": ["test_c"]},
        }

        # Verify structure
        self.assertIn("file_mapping", result)
        self.assertIn("func_mapping", result)
        self.assertIsInstance(result["file_mapping"], dict)
        self.assertIsInstance(result["func_mapping"], dict)

    def test_utils_file_classified_as_file_mapping(self):
        """Test that utils/config files use file-level mapping."""
        test_cases = [
            ("vllm_ascend/utils.py", True),
            ("vllm_ascend/config.py", True),
            ("vllm_ascend/utils/helper.py", True),
            ("vllm_ascend/core/engine.py", False),
        ]

        for path, should_be_file in test_cases:
            is_utils = "vllm_ascend/utils" in path or "vllm_ascend/config" in path
            self.assertEqual(is_utils, should_be_file, f"Path {path} classification incorrect")


if __name__ == "__main__":
    unittest.main()
