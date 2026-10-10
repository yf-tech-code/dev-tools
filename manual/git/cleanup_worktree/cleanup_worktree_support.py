"""Shared value objects and subprocess execution for worktree cleanup.

This module defines the data passed between discovery, verification, and
interactive cleanup operations. Commands run without a shell.
"""

from __future__ import annotations

import dataclasses
import enum
import os
import pathlib
import subprocess
from typing import Sequence


class CleanupError(RuntimeError):
    """A safety failure that prevents a cleanup operation."""


class UnmergedTargetError(CleanupError):
    """A branch whose tip cannot be verified as merged."""


class SelectionCancelled(Exception):
    """An intentionally cancelled fzf selection."""


class TargetType(enum.StrEnum):
    """The combination of local branch and worktree selected for cleanup."""
    BOTH = "BOTH"
    BRANCH_ONLY = "BRANCH_ONLY"
    WORKTREE_ONLY = "WORKTREE_ONLY"


class WorktreeStatus(enum.StrEnum):
    """The safety classification of a registered Git worktree."""
    CLEAN = "CLEAN"
    IGNORED_ONLY = "IGNORED_ONLY"
    DIRTY = "DIRTY"
    STALE = "STALE"
    LOCKED = "LOCKED"
    UNKNOWN = "UNKNOWN"


@dataclasses.dataclass(frozen=True)
class Config:
    """Validated application configuration.

    Attributes:
      root_directory: Root and immediate child directories to search.
    """
    root_directory: pathlib.Path


@dataclasses.dataclass(frozen=True)
class AppState:
    """Persisted interactive selection state.

    Attributes:
      last_repository: The most recently selected repository, if known.
    """
    last_repository: pathlib.Path | None = None


@dataclasses.dataclass(frozen=True)
class Repository:
    """Display information for a primary Git repository.

    Attributes:
      name: Repository directory name.
      branch: Branch checked out by the primary worktree.
      path: Resolved path to the primary worktree.
      origin: Git origin URL, or a placeholder if unavailable.
    """
    name: str
    branch: str
    path: pathlib.Path
    origin: str


@dataclasses.dataclass(frozen=True)
class Worktree:
    """A registered Git worktree and its metadata.

    Attributes:
      path: Resolved filesystem location of the worktree.
      head: Checked-out commit object ID.
      branch: Local branch name, or None when detached.
      prunable: Whether Git considers the registration stale.
      locked: Whether Git reports the worktree as locked.
    """
    path: pathlib.Path
    head: str
    branch: str | None
    prunable: bool
    locked: bool


@dataclasses.dataclass(frozen=True)
class CleanupTarget:
    """A candidate selected for safe branch or worktree deletion.

    Attributes:
      target_type: Which resources should be removed.
      branch_display: Branch label displayed to the user.
      status: Initial worktree safety status, if applicable.
      merge_hint: Preliminary merge state for the selector.
      path: Worktree path, if one is associated with the target.
      head: Commit object ID recorded at selection time.
    """
    target_type: TargetType
    branch_display: str
    status: WorktreeStatus | None
    merge_hint: str
    path: pathlib.Path | None
    head: str

    @property
    def has_branch(self) -> bool:
        """Whether this target includes a local branch to remove."""
        return self.target_type in {TargetType.BOTH, TargetType.BRANCH_ONLY}

    @property
    def has_worktree(self) -> bool:
        """Whether this target includes a linked worktree to remove."""
        return self.target_type in {TargetType.BOTH, TargetType.WORKTREE_ONLY}

    @property
    def branch(self) -> str | None:
        """The local branch name, if the target includes one."""
        return self.branch_display if self.has_branch else None

    @property
    def worktree_branch(self) -> str | None:
        """The worktree branch name, or None for detached worktrees."""
        if not self.has_worktree or self.branch_display == "(detached)":
            return None
        return self.branch_display


@dataclasses.dataclass(frozen=True)
class GitHubContext:
    """A related GitHub item shown as informational context.

    Attributes:
      context_type: PR or Issue.
      number: GitHub item number.
      title: Display title.
      state: GitHub item state.
      url: Link to the item.
      head_sha: PR head commit ID, when available.
      is_cross_repository: Whether the PR comes from a fork.
    """
    context_type: str
    number: str
    title: str
    state: str
    url: str
    head_sha: str | None = None
    is_cross_repository: bool | None = None


@dataclasses.dataclass(frozen=True)
class MergeProof:
    """Evidence that the selected commit has already been merged.

    Attributes:
      proof_type: Git ancestry or an exact GitHub pull request match.
      pr_number: Verified pull request number, if applicable.
      pr_url: Verified pull request URL, if applicable.
    """
    proof_type: str
    pr_number: str | None = None
    pr_url: str | None = None


@dataclasses.dataclass(frozen=True)
class CurrentTargetState:
    """Live branch and worktree state checked before cleanup.

    Attributes:
      branch_exists: Whether the selected branch still exists.
      branch_sha: Current branch commit, if present.
      worktree_exists: Whether the linked worktree is still registered.
      worktree_head: Current worktree commit, if present.
      worktree_branch: Branch checked out in the linked worktree.
      worktree_status: Current safety classification, if applicable.
    """
    branch_exists: bool
    branch_sha: str | None
    worktree_exists: bool
    worktree_head: str | None
    worktree_branch: str | None
    worktree_status: WorktreeStatus | None


@dataclasses.dataclass(frozen=True)
class Colors:
    """Terminal control sequences used for informational output.

    Attributes:
      reset: Reset terminal formatting.
      bold: Emphasize text.
      dim: Deemphasize text.
      cyan: Render informational highlights.
      green: Render successful status.
      yellow: Render warnings.
      red: Render errors.
    """
    reset: str
    bold: str
    dim: str
    cyan: str
    green: str
    yellow: str
    red: str


class CommandRunner:
    """An executor for external commands that never invokes a shell."""

    def run(
        self,
        args: Sequence[str | os.PathLike[str]],
        *,
        cwd: pathlib.Path | None = None,
        check: bool = True,
        capture_output: bool = True,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run an external command without involving a shell.

        Args:
          args: Command and arguments, each supplied as a separate element.
          cwd: Optional working directory for the command.
          check: Whether to raise if the command exits unsuccessfully.
          capture_output: Whether to capture stdout and stderr.
          input_text: Optional text sent to the command's standard input.

        Returns:
          The completed process, including its exit status and output.

        Raises:
          subprocess.CalledProcessError: If check is true and the command fails.
        """
        command = [os.fspath(arg) for arg in args]
        return subprocess.run(
            command,
            cwd=cwd,
            check=check,
            capture_output=capture_output,
            input=input_text,
            text=True,
        )

    def output(
        self,
        args: Sequence[str | os.PathLike[str]],
        *,
        cwd: pathlib.Path | None = None,
        check: bool = True,
    ) -> str:
        """Run a command and return its stripped standard output.

        Args:
          args: Command and arguments, each supplied as a separate element.
          cwd: Optional working directory for the command.
          check: Whether to raise if the command exits unsuccessfully.

        Returns:
          The command's standard output with surrounding whitespace removed.

        Raises:
          subprocess.CalledProcessError: If check is true and the command fails.
        """
        result = self.run(args, cwd=cwd, check=check)
        return result.stdout.strip()


_RUNNER = CommandRunner()
