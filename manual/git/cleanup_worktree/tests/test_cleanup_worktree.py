"""Regression tests for interactive worktree cleanup.

These tests use standard-library mocks and never remove real worktrees.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import io
import pathlib
import subprocess
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



class CleanupFlowTest(unittest.TestCase):
    """Verify confirmation and dry-run guards around destructive actions."""

    def _run_cleanup(self, answer: str, *, dry_run: bool = False):
        """Run branch cleanup with all external Git operations mocked.

        Args:
          answer: Simulated response to the cleanup confirmation prompt.
          dry_run: Whether the cleanup should stop after displaying its plan.

        Returns:
          Mocked prompt, final safety check, and branch removal calls.
        """
        target = cleanup_worktree.CleanupTarget(
            target_type=cleanup_worktree.TargetType.BRANCH_ONLY,
            branch_display="feature/merged",
            status=None,
            merge_hint="MERGED",
            path=None,
            head="abc123",
        )
        state = cleanup_worktree.CurrentTargetState(
            branch_exists=True,
            branch_sha="abc123",
            worktree_exists=False,
            worktree_head=None,
            worktree_branch=None,
            worktree_status=None,
        )
        repository = pathlib.Path("/virtual/repository")
        colors = cleanup_worktree.Colors("", "", "", "", "", "", "")

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                cleanup_worktree, "_refresh_remote_state",
                return_value=("main", "owner/repo")))
            stack.enter_context(mock.patch.object(
                cleanup_worktree, "_load_github_context", return_value=None))
            stack.enter_context(mock.patch.object(
                cleanup_worktree, "_load_worktrees", return_value=[]))
            stack.enter_context(mock.patch.object(
                cleanup_worktree, "_current_target_state", return_value=state))
            stack.enter_context(mock.patch.object(
                cleanup_worktree, "_validate_selected_identity"))
            stack.enter_context(mock.patch.object(
                cleanup_worktree, "_protect_primary_and_default"))
            stack.enter_context(mock.patch.object(
                cleanup_worktree, "_validate_worktree_status"))
            stack.enter_context(mock.patch.object(
                cleanup_worktree, "_verify_merge",
                return_value=cleanup_worktree.MergeProof("git-ancestor")))
            stack.enter_context(mock.patch.object(
                cleanup_worktree, "_show_cleanup_plan"))
            final_checks = stack.enter_context(mock.patch.object(
                cleanup_worktree, "_final_race_condition_checks"))
            remove_branch = stack.enter_context(mock.patch.object(
                cleanup_worktree, "_remove_branch"))
            user_input = stack.enter_context(mock.patch(
                "builtins.input", return_value=answer))
            with contextlib.redirect_stdout(io.StringIO()):
                cleanup_worktree._cleanup_target(
                    repository, target, dry_run=dry_run, colors=colors)
            return user_input, final_checks, remove_branch

    def test_enter_runs_final_checks_and_branch_removal(self):
        _, final_checks, remove_branch = self._run_cleanup("")
        final_checks.assert_called_once()
        remove_branch.assert_called_once_with(
            pathlib.Path("/virtual/repository"), "feature/merged", "abc123")

    def test_no_does_not_run_destructive_operations(self):
        _, final_checks, remove_branch = self._run_cleanup("n")
        final_checks.assert_not_called()
        remove_branch.assert_not_called()

    def test_dry_run_does_not_prompt_or_delete(self):
        user_input, final_checks, remove_branch = self._run_cleanup(
            "", dry_run=True)
        user_input.assert_not_called()
        final_checks.assert_not_called()
        remove_branch.assert_not_called()



class UnmergedBranchTest(unittest.TestCase):
    """Verify closed PR and remote deletion requirements."""

    def setUp(self):
        """Build an unmerged test branch without external commands."""
        self.repository = pathlib.Path("/virtual/repository")
        self.target = cleanup_worktree.CleanupTarget(
            cleanup_worktree.TargetType.BRANCH_ONLY,
            "feature/unused", None, "CHECK", None, "abc123",
        )
        self.state = cleanup_worktree.CurrentTargetState(
            True, "abc123", False, None, None, None,
        )
        self.context = cleanup_worktree.GitHubContext(
            "PR", "123", "Unused feature", "CLOSED",
            "https://github.com/owner/repo/pull/123",
            head_sha="abc123", is_cross_repository=False,
        )

    def _verify(self, context):
        """Check an unmerged branch with the supplied GitHub context.

        Args:
          context: Associated PR metadata.

        Returns:
          Verified cleanup eligibility evidence.
        """
        return cleanup_worktree._verify_cleanup_proof(
            self.repository, self.target, self.state,
            "main", "owner/repo", context,
        )

    def test_deleted_remote_closed_pr_is_eligible(self):
        with mock.patch.object(
            cleanup_worktree, "_verify_merge",
            side_effect=cleanup_worktree.UnmergedTargetError("unmerged")
        ), mock.patch.object(
            cleanup_worktree, "_remote_branch_is_deleted", return_value=True
        ):
            self.assertEqual(
                self._verify(self.context).proof_type, "unmerged-closed-pr"
            )

    def test_existing_remote_is_not_eligible(self):
        with mock.patch.object(
            cleanup_worktree, "_verify_merge",
            side_effect=cleanup_worktree.UnmergedTargetError("unmerged")
        ), mock.patch.object(
            cleanup_worktree, "_remote_branch_is_deleted", return_value=False
        ), self.assertRaises(cleanup_worktree.CleanupError):
            self._verify(self.context)

    def test_open_pr_is_not_eligible(self):
        with mock.patch.object(
            cleanup_worktree, "_verify_merge",
            side_effect=cleanup_worktree.UnmergedTargetError("unmerged")
        ), self.assertRaises(cleanup_worktree.CleanupError):
            self._verify(dataclasses.replace(self.context, state="OPEN"))

    def test_mismatched_pr_head_is_not_eligible(self):
        with mock.patch.object(
            cleanup_worktree, "_verify_merge",
            side_effect=cleanup_worktree.UnmergedTargetError("unmerged")
        ), self.assertRaises(cleanup_worktree.CleanupError):
            self._verify(dataclasses.replace(
                self.context, head_sha="different"
            ))

    def test_fork_pr_is_not_eligible(self):
        with mock.patch.object(
            cleanup_worktree, "_verify_merge",
            side_effect=cleanup_worktree.UnmergedTargetError("unmerged")
        ), self.assertRaises(cleanup_worktree.CleanupError):
            self._verify(dataclasses.replace(
                self.context, is_cross_repository=True
            ))

    def test_remote_probe_distinguishes_missing_and_errors(self):
        for code, missing in ((0, False), (2, True)):
            with self.subTest(status=code), mock.patch.object(
                cleanup_worktree._RUNNER, "run",
                return_value=subprocess.CompletedProcess(
                    [], code, stdout="", stderr=""
                ),
            ):
                self.assertEqual(
                    cleanup_worktree._remote_branch_is_deleted(
                        self.repository, "feature/unused"
                    ),
                    missing,
                )
        with mock.patch.object(
            cleanup_worktree._RUNNER, "run",
            return_value=subprocess.CompletedProcess(
                [], 128, stdout="", stderr="connection failed"
            ),
        ), self.assertRaises(cleanup_worktree.CleanupError):
            cleanup_worktree._remote_branch_is_deleted(
                self.repository, "feature/unused"
            )


class ErrorRecoveryTest(unittest.TestCase):
    """Keep the target selection loop active after a Git command fails."""

    def test_git_error_reopens_selection(self):
        repository = cleanup_worktree.Repository(
            "repo", "main", pathlib.Path("/virtual/repo"), "origin"
        )
        worktree = cleanup_worktree.Worktree(
            repository.path, "abc", "main", False, False
        )
        target = cleanup_worktree.CleanupTarget(
            cleanup_worktree.TargetType.BRANCH_ONLY,
            "feature", None, "CHECK", None, "abc",
        )
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                cleanup_worktree, "_load_worktrees", return_value=[worktree]
            ))
            stack.enter_context(mock.patch.object(
                cleanup_worktree, "_detect_local_default_branch",
                return_value="main"
            ))
            stack.enter_context(mock.patch.object(
                cleanup_worktree, "_build_targets", return_value=[target]
            ))
            select = stack.enter_context(mock.patch.object(
                cleanup_worktree, "_select_cleanup_target",
                side_effect=[target, cleanup_worktree.SelectionCancelled()]
            ))
            stack.enter_context(mock.patch.object(
                cleanup_worktree, "_cleanup_target",
                side_effect=subprocess.CalledProcessError(
                    1, ["git", "worktree", "remove"], stderr="failed"
                )
            ))
            with contextlib.redirect_stdout(
                io.StringIO()
            ), contextlib.redirect_stderr(io.StringIO()):
                cleanup_worktree._run_repository_loop(
                    repository, dry_run=False,
                    colors=cleanup_worktree.Colors(
                        "", "", "", "", "", "", ""
                    ),
                )
        self.assertEqual(select.call_count, 2)


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
