"""Regression tests for interactive worktree cleanup.

These tests use standard-library mocks and never remove real worktrees.
"""

from __future__ import annotations

import ast
import pathlib
import sys
import unittest
from unittest import mock

TOOL_DIRECTORY = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIRECTORY))

import cleanup_worktree  # pylint: disable=wrong-import-position
import cleanup_worktree_support  # pylint: disable=wrong-import-position


class ConfirmationTest(unittest.TestCase):
    """Exercise the default-yes destructive-action prompt."""

    def test_empty_input_confirms(self):
        with mock.patch("builtins.input", return_value="") as user_input:
            self.assertTrue(cleanup_worktree._confirm("Continue cleanup?"))
        user_input.assert_called_once_with("\nContinue cleanup? [Y/n]: ")

    def test_explicit_yes_confirms(self):
        for answer in ("y", "Y", " y ", "Y  "):
            with self.subTest(answer=answer):
                with mock.patch("builtins.input", return_value=answer):
                    self.assertTrue(cleanup_worktree._confirm("Delete?"))

    def test_no_and_unknown_input_cancel(self):
        for answer in ("n", "N", "no", "yes", "anything"):
            with self.subTest(answer=answer):
                with mock.patch("builtins.input", return_value=answer):
                    self.assertFalse(cleanup_worktree._confirm("Delete?"))

    def test_eof_cancels_without_deleting(self):
        with mock.patch("builtins.input", side_effect=EOFError):
            self.assertFalse(cleanup_worktree._confirm("Delete?"))

    def test_keyboard_interrupt_is_not_swallowed(self):
        with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                cleanup_worktree._confirm("Delete?")


class DocumentationTest(unittest.TestCase):
    """Require docstrings on production functions, classes, and modules."""

    def test_production_definitions_are_documented(self):
        for module in (cleanup_worktree, cleanup_worktree_support):
            with self.subTest(module=module.__name__):
                module_path = pathlib.Path(module.__file__)
                tree = ast.parse(module_path.read_text(encoding="utf-8"))
                self.assertIsNotNone(ast.get_docstring(tree))
                for node in ast.walk(tree):
                    if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
                        with self.subTest(definition=node.name):
                            self.assertIsNotNone(ast.get_docstring(node))


if __name__ == "__main__":
    unittest.main()
