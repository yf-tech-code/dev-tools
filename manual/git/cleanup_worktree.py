#!/usr/bin/env python3
"""Safely remove merged Git worktrees and local branches interactively.

This is a manual, human-operated maintenance tool. It requires an interactive
TTY, explicit selections through fzf, GitHub merge verification through gh,
and human confirmation before destructive operations.
"""

from __future__ import annotations

import argparse
import dataclasses
import enum
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tomllib
from typing import Sequence


_CONFIG_PATH = pathlib.Path(
    "~/.config/dev-tools/cleanup_worktree.toml"
).expanduser()
_STATE_PATH = pathlib.Path(
    "~/.local/state/dev-tools/cleanup_worktree.json"
).expanduser()


class CleanupError(RuntimeError):
    """Raised when cleanup cannot be completed safely."""


class SelectionCancelled(Exception):
    """Raised when the user cancels an fzf selection."""


class TargetType(enum.StrEnum):
    BOTH = "BOTH"
    BRANCH_ONLY = "BRANCH_ONLY"
    WORKTREE_ONLY = "WORKTREE_ONLY"


class WorktreeStatus(enum.StrEnum):
    CLEAN = "CLEAN"
    IGNORED_ONLY = "IGNORED_ONLY"
    DIRTY = "DIRTY"
    STALE = "STALE"
    LOCKED = "LOCKED"
    UNKNOWN = "UNKNOWN"


@dataclasses.dataclass(frozen=True)
class Config:
    root_directory: pathlib.Path


@dataclasses.dataclass(frozen=True)
class AppState:
    last_repository: pathlib.Path | None = None


@dataclasses.dataclass(frozen=True)
class Repository:
    name: str
    branch: str
    path: pathlib.Path
    origin: str


@dataclasses.dataclass(frozen=True)
class Worktree:
    path: pathlib.Path
    head: str
    branch: str | None
    prunable: bool
    locked: bool


@dataclasses.dataclass(frozen=True)
class CleanupTarget:
    target_type: TargetType
    branch_display: str
    status: WorktreeStatus | None
    merge_hint: str
    path: pathlib.Path | None
    head: str

    @property
    def has_branch(self) -> bool:
        return self.target_type in {TargetType.BOTH, TargetType.BRANCH_ONLY}

    @property
    def has_worktree(self) -> bool:
        return self.target_type in {TargetType.BOTH, TargetType.WORKTREE_ONLY}

    @property
    def branch(self) -> str | None:
        return self.branch_display if self.has_branch else None

    @property
    def worktree_branch(self) -> str | None:
        if not self.has_worktree or self.branch_display == "(detached)":
            return None
        return self.branch_display


@dataclasses.dataclass(frozen=True)
class GitHubContext:
    context_type: str
    number: str
    title: str
    state: str
    url: str


@dataclasses.dataclass(frozen=True)
class MergeProof:
    proof_type: str
    pr_number: str | None = None
    pr_url: str | None = None


@dataclasses.dataclass(frozen=True)
class CurrentTargetState:
    branch_exists: bool
    branch_sha: str | None
    worktree_exists: bool
    worktree_head: str | None
    worktree_branch: str | None
    worktree_status: WorktreeStatus | None


@dataclasses.dataclass(frozen=True)
class Colors:
    reset: str
    bold: str
    dim: str
    cyan: str
    green: str
    yellow: str
    red: str


class CommandRunner:
    """Runs external commands without invoking a shell."""

    def run(
        self,
        args: Sequence[str | os.PathLike[str]],
        *,
        cwd: pathlib.Path | None = None,
        check: bool = True,
        capture_output: bool = True,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
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
        result = self.run(args, cwd=cwd, check=check)
        return result.stdout.strip()


_RUNNER = CommandRunner()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely remove merged Git worktrees and local branches."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show cleanup plans but do not modify repositories.",
    )
    parser.add_argument(
        "--config",
        type=pathlib.Path,
        default=_CONFIG_PATH,
        help=f"Configuration file (default: {_CONFIG_PATH}).",
    )
    return parser.parse_args()


def _require_interactive_terminal() -> None:
    if not (sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty()):
        raise CleanupError(
            "this script must be run manually from an interactive terminal"
        )


def _require_commands(*commands: str) -> None:
    for command in commands:
        if shutil.which(command) is None:
            raise CleanupError(f"required command not found: {command}")


def _load_colors() -> Colors:
    if not sys.stdout.isatty() or shutil.which("tput") is None:
        return Colors("", "", "", "", "", "", "")

    def tput(*args: str) -> str:
        try:
            return _RUNNER.output(["tput", *args])
        except subprocess.CalledProcessError:
            return ""

    return Colors(
        reset=tput("sgr0"),
        bold=tput("bold"),
        dim=tput("dim"),
        cyan=tput("setaf", "6"),
        green=tput("setaf", "2"),
        yellow=tput("setaf", "3"),
        red=tput("setaf", "1"),
    )


def _load_config(path: pathlib.Path) -> Config:
    path = path.expanduser()
    if not path.exists():
        print(
            f"WARNING: config file not found: {path}\n"
            "Using the current directory as root_directory."
        )
        return Config(root_directory=pathlib.Path.cwd().resolve())

    try:
        with path.open("rb") as config_file:
            raw = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CleanupError(f"failed to read config file {path}: {exc}") from exc

    section = raw.get("cleanup_worktree")
    if not isinstance(section, dict):
        raise CleanupError(
            f"missing [cleanup_worktree] section in config file: {path}"
        )

    root = section.get("root_directory")
    if not isinstance(root, str) or not root.strip():
        raise CleanupError(
            f"root_directory must be a non-empty string in config file: {path}"
        )

    root_directory = pathlib.Path(root).expanduser().resolve()
    if not root_directory.is_dir():
        raise CleanupError(f"root_directory does not exist: {root_directory}")

    return Config(root_directory=root_directory)


def _load_state() -> AppState:
    if not _STATE_PATH.exists():
        return AppState()
    try:
        raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AppState()

    last_repository = raw.get("last_repository")
    if not isinstance(last_repository, str) or not last_repository:
        return AppState()
    return AppState(last_repository=pathlib.Path(last_repository).expanduser())


def _save_state(repository: pathlib.Path) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _STATE_PATH.write_text(
            json.dumps(
                {"last_repository": os.fspath(repository)},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        try:
            _STATE_PATH.chmod(0o600)
        except OSError:
            pass
    except OSError as exc:
        print(f"WARNING: failed to save state: {exc}", file=sys.stderr)


def _git_output(
    repository: pathlib.Path, *args: str, check: bool = True
) -> str:
    return _RUNNER.output(["git", "-C", repository, *args], check=check)


def _find_primary_worktree(candidate: pathlib.Path) -> pathlib.Path | None:
    try:
        output = _git_output(candidate, "worktree", "list", "--porcelain")
    except subprocess.CalledProcessError:
        return None

    for line in output.splitlines():
        if line.startswith("worktree "):
            return pathlib.Path(line.removeprefix("worktree ")).resolve()
    return None


def _repository_from_candidate(candidate: pathlib.Path) -> Repository | None:
    if not candidate.is_dir():
        return None
    try:
        inside = _git_output(candidate, "rev-parse", "--is-inside-work-tree")
    except subprocess.CalledProcessError:
        return None
    if inside != "true":
        return None

    primary = _find_primary_worktree(candidate)
    if primary is None:
        return None

    try:
        branch = _git_output(
            primary, "symbolic-ref", "--quiet", "--short", "HEAD"
        )
    except subprocess.CalledProcessError:
        branch = "(detached)"
    try:
        origin = _git_output(primary, "remote", "get-url", "origin")
    except subprocess.CalledProcessError:
        origin = "-"

    return Repository(
        name=primary.name,
        branch=branch,
        path=primary,
        origin=origin,
    )


def _find_repositories(root: pathlib.Path) -> list[Repository]:
    candidates = [root]
    try:
        candidates.extend(path for path in root.iterdir() if path.is_dir())
    except OSError as exc:
        raise CleanupError(
            f"unable to scan root directory {root}: {exc}"
        ) from exc

    repositories: list[Repository] = []
    seen: set[pathlib.Path] = set()
    for candidate in candidates:
        repository = _repository_from_candidate(candidate)
        if repository is None or repository.path in seen:
            continue
        seen.add(repository.path)
        repositories.append(repository)

    return repositories


def _select_with_fzf(
    rows: Sequence[str],
    *,
    prompt: str,
    header: str,
    with_nth: str,
) -> str:
    args = [
        "fzf",
        "--delimiter=\t",
        f"--with-nth={with_nth}",
        f"--header={header}",
        f"--prompt={prompt}",
        "--height=80%",
        "--layout=reverse",
        "--border",
        "--no-multi",
    ]
    result = subprocess.run(
        args,
        check=False,
        input="\n".join(rows) + "\n",
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
    )
    if result.returncode != 0:
        raise SelectionCancelled
    return result.stdout.rstrip("\n")


def _select_repository(
    repositories: list[Repository], state: AppState
) -> Repository:
    if not repositories:
        raise CleanupError(
            "no Git repositories found under configured root directory"
        )

    if state.last_repository is not None:
        remembered = state.last_repository.resolve()
        repositories.sort(key=lambda repo: repo.path != remembered)

    rows = [
        "\t".join([repo.name, repo.branch, os.fspath(repo.path), repo.origin])
        for repo in repositories
    ]
    selected = _select_with_fzf(
        rows,
        prompt="repository> ",
        header="NAME\tBRANCH\tPATH",
        with_nth="1,2,3",
    )
    selected_path = pathlib.Path(selected.split("\t", maxsplit=3)[2]).resolve()
    for repository in repositories:
        if repository.path == selected_path:
            return repository
    raise CleanupError("selected repository could not be resolved")


def _load_worktrees(repository: pathlib.Path) -> list[Worktree]:
    output = _git_output(repository, "worktree", "list", "--porcelain")
    worktrees: list[Worktree] = []

    current: dict[str, object] = {}

    def flush() -> None:
        if "path" not in current:
            return
        worktrees.append(
            Worktree(
                path=pathlib.Path(str(current["path"])).resolve(),
                head=str(current.get("head", "")),
                branch=(
                    current.get("branch")
                    if isinstance(current.get("branch"), str)
                    else None
                ),
                prunable=bool(current.get("prunable", False)),
                locked=bool(current.get("locked", False)),
            )
        )
        current.clear()

    for line in output.splitlines() + [""]:
        if not line:
            flush()
        elif line.startswith("worktree "):
            current["path"] = line.removeprefix("worktree ")
        elif line.startswith("HEAD "):
            current["head"] = line.removeprefix("HEAD ")
        elif line.startswith("branch refs/heads/"):
            current["branch"] = line.removeprefix("branch refs/heads/")
        elif line == "detached":
            current["branch"] = None
        elif line.startswith("prunable"):
            current["prunable"] = True
        elif line.startswith("locked"):
            current["locked"] = True

    if not worktrees:
        raise CleanupError("unable to determine primary worktree")
    return worktrees


def _detect_local_default_branch(
    repository: pathlib.Path, primary_branch: str | None
) -> str | None:
    try:
        value = _git_output(
            repository,
            "symbolic-ref",
            "--quiet",
            "--short",
            "refs/remotes/origin/HEAD",
        )
        if value.startswith("origin/"):
            return value.removeprefix("origin/")
    except subprocess.CalledProcessError:
        pass

    for candidate in ("main", "master"):
        result = _RUNNER.run(
            [
                "git",
                "-C",
                repository,
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{candidate}",
            ],
            check=False,
        )
        if result.returncode == 0:
            return candidate
    return primary_branch


def _branch_exists(repository: pathlib.Path, branch: str) -> bool:
    result = _RUNNER.run(
        [
            "git",
            "-C",
            repository,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
        ],
        check=False,
    )
    return result.returncode == 0


def _branch_sha(repository: pathlib.Path, branch: str) -> str:
    return _git_output(
        repository,
        "rev-parse",
        "--verify",
        f"refs/heads/{branch}^{{commit}}",
    )


def _is_ancestor(repository: pathlib.Path, sha: str, ref: str) -> bool:
    result = _RUNNER.run(
        ["git", "-C", repository, "merge-base", "--is-ancestor", sha, ref],
        check=False,
    )
    return result.returncode == 0


def _local_merge_hint(
    repository: pathlib.Path, sha: str, default_branch: str | None
) -> str:
    if default_branch is None:
        return "CHECK"
    remote_ref = f"refs/remotes/origin/{default_branch}"
    result = _RUNNER.run(
        [
            "git",
            "-C",
            repository,
            "show-ref",
            "--verify",
            "--quiet",
            remote_ref,
        ],
        check=False,
    )
    if result.returncode == 0 and _is_ancestor(repository, sha, remote_ref):
        return "MERGED"
    return "CHECK"


def _worktree_status(worktree: Worktree) -> WorktreeStatus:
    if worktree.prunable or not worktree.path.is_dir():
        return WorktreeStatus.STALE
    if worktree.locked:
        return WorktreeStatus.LOCKED

    status_result = _RUNNER.run(
        [
            "git",
            "-C",
            worktree.path,
            "status",
            "--porcelain",
            "--untracked-files=normal",
        ],
        check=False,
    )
    if status_result.returncode != 0:
        return WorktreeStatus.UNKNOWN
    if status_result.stdout.strip():
        return WorktreeStatus.DIRTY

    ignored_result = _RUNNER.run(
        [
            "git",
            "-C",
            worktree.path,
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
        ],
        check=False,
    )
    if ignored_result.returncode != 0:
        return WorktreeStatus.UNKNOWN
    if ignored_result.stdout.strip():
        return WorktreeStatus.IGNORED_ONLY
    return WorktreeStatus.CLEAN


def _build_targets(
    repository: pathlib.Path,
    worktrees: list[Worktree],
    default_branch: str | None,
) -> list[CleanupTarget]:
    primary = worktrees[0]
    path_by_branch = {
        worktree.branch: worktree.path
        for worktree in worktrees
        if worktree.branch is not None
    }
    seen_branches: set[str] = set()
    targets: list[CleanupTarget] = []

    for worktree in worktrees[1:]:
        if not worktree.head:
            continue
        if worktree.branch is not None and worktree.branch == default_branch:
            continue

        status = _worktree_status(worktree)
        if worktree.branch and _branch_exists(repository, worktree.branch):
            branch_sha = _branch_sha(repository, worktree.branch)
            merge_hint = (
                _local_merge_hint(repository, branch_sha, default_branch)
                if branch_sha == worktree.head
                else "CHECK"
            )
            seen_branches.add(worktree.branch)
            targets.append(
                CleanupTarget(
                    TargetType.BOTH,
                    worktree.branch,
                    status,
                    merge_hint,
                    worktree.path,
                    worktree.head,
                )
            )
        else:
            targets.append(
                CleanupTarget(
                    TargetType.WORKTREE_ONLY,
                    worktree.branch or "(detached)",
                    status,
                    _local_merge_hint(
                        repository, worktree.head, default_branch
                    ),
                    worktree.path,
                    worktree.head,
                )
            )

    output = _git_output(
        repository,
        "for-each-ref",
        "--format=%(refname:short)%09%(objectname)",
        "refs/heads/",
    )
    for line in output.splitlines():
        if not line:
            continue
        branch, sha = line.split("\t", maxsplit=1)
        if branch == default_branch or branch == primary.branch:
            continue
        if branch in seen_branches or branch in path_by_branch:
            continue
        targets.append(
            CleanupTarget(
                TargetType.BRANCH_ONLY,
                branch,
                None,
                _local_merge_hint(repository, sha, default_branch),
                None,
                sha,
            )
        )

    return targets


def _select_cleanup_target(targets: list[CleanupTarget]) -> CleanupTarget:
    rows = []
    for target in targets:
        rows.append(
            "\t".join(
                [
                    target.target_type.value,
                    target.branch_display,
                    target.status.value if target.status else "-",
                    target.merge_hint,
                    os.fspath(target.path) if target.path else "-",
                    target.head,
                ]
            )
        )

    selected = _select_with_fzf(
        rows,
        prompt="cleanup target> ",
        header="TYPE\tBRANCH\tWORKTREE\tMERGE\tPATH",
        with_nth="1,2,3,4,5",
    )
    fields = selected.split("\t")
    if len(fields) != 6:
        raise CleanupError("invalid cleanup target selection")

    selected_type = TargetType(fields[0])
    selected_path = (
        None if fields[4] == "-" else pathlib.Path(fields[4]).resolve()
    )
    for target in targets:
        if (
            target.target_type == selected_type
            and target.branch_display == fields[1]
            and target.path == selected_path
            and target.head == fields[5]
        ):
            return target
    raise CleanupError("selected cleanup target could not be resolved")


def _refresh_remote_state(repository: pathlib.Path) -> tuple[str, str]:
    auth = _RUNNER.run(["gh", "auth", "status"], cwd=repository, check=False)
    if auth.returncode != 0:
        raise CleanupError("gh is not authenticated")

    default_result = _RUNNER.run(
        [
            "gh",
            "repo",
            "view",
            "--json",
            "defaultBranchRef",
            "--jq",
            ".defaultBranchRef.name",
        ],
        cwd=repository,
        check=False,
    )
    default_branch = default_result.stdout.strip()
    if default_result.returncode != 0 or not default_branch:
        raise CleanupError("unable to determine repository default branch")

    slug_result = _RUNNER.run(
        [
            "gh",
            "repo",
            "view",
            "--json",
            "nameWithOwner",
            "--jq",
            ".nameWithOwner",
        ],
        cwd=repository,
        check=False,
    )
    repo_slug = slug_result.stdout.strip()
    if slug_result.returncode != 0 or not repo_slug:
        raise CleanupError("unable to resolve GitHub repository with gh")

    _RUNNER.run(
        [
            "git",
            "-C",
            repository,
            "fetch",
            "origin",
            (
                f"+refs/heads/{default_branch}:"
                f"refs/remotes/origin/{default_branch}"
            ),
        ]
    )
    return default_branch, repo_slug


def _load_github_context(
    repository: pathlib.Path, branch: str, sha: str
) -> GitHubContext | None:
    if branch == "(detached)":
        return None

    result = _RUNNER.run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "all",
            "--limit",
            "100",
            "--json",
            "number,title,state,url,mergedAt,headRefOid",
        ],
        cwd=repository,
        check=False,
    )
    if result.returncode == 0:
        try:
            prs = json.loads(result.stdout)
        except json.JSONDecodeError:
            prs = []
        exact = next((pr for pr in prs if pr.get("headRefOid") == sha), None)
        pr = exact or (prs[0] if prs else None)
        if pr is not None:
            return GitHubContext(
                context_type="PR",
                number=str(pr.get("number", "")),
                title=str(pr.get("title", "")),
                state=(
                    "MERGED"
                    if pr.get("mergedAt")
                    else str(pr.get("state", ""))
                ),
                url=str(pr.get("url", "")),
            )

    match = re.search(r"(\d+)$", branch)
    if match is None:
        return None
    issue_number = match.group(1)
    issue_result = _RUNNER.run(
        [
            "gh",
            "issue",
            "view",
            issue_number,
            "--json",
            "number,title,state,url",
        ],
        cwd=repository,
        check=False,
    )
    if issue_result.returncode != 0:
        return None
    try:
        issue = json.loads(issue_result.stdout)
    except json.JSONDecodeError:
        return None
    return GitHubContext(
        context_type="Issue",
        number=str(issue.get("number", "")),
        title=str(issue.get("title", "")),
        state=str(issue.get("state", "")),
        url=str(issue.get("url", "")),
    )


def _find_exact_merged_pr_for_branch(
    repository: pathlib.Path, branch: str, sha: str, default_branch: str
) -> tuple[str, str] | None:
    result = _RUNNER.run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--base",
            default_branch,
            "--state",
            "merged",
            "--limit",
            "100",
            "--json",
            "number,headRefOid,url",
        ],
        cwd=repository,
        check=False,
    )
    if result.returncode != 0:
        raise CleanupError("GitHub merge state could not be verified")
    try:
        prs = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CleanupError(
            "invalid response while verifying GitHub merge state"
        ) from exc
    for pr in prs:
        if pr.get("headRefOid") == sha:
            return str(pr.get("number")), str(pr.get("url"))
    return None


def _find_exact_merged_pr_for_commit(
    repository: pathlib.Path, repo_slug: str, sha: str, default_branch: str
) -> tuple[str, str] | None:
    result = _RUNNER.run(
        ["gh", "api", f"repos/{repo_slug}/commits/{sha}/pulls"],
        cwd=repository,
        check=False,
    )
    if result.returncode != 0:
        raise CleanupError("GitHub merge state could not be verified")
    try:
        prs = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CleanupError(
            "invalid response while verifying GitHub merge state"
        ) from exc
    for pr in prs:
        if (
            pr.get("merged_at") is not None
            and pr.get("base", {}).get("ref") == default_branch
            and pr.get("head", {}).get("sha") == sha
        ):
            return str(pr.get("number")), str(pr.get("html_url"))
    return None


def _verify_branch_merged(
    repository: pathlib.Path,
    branch: str,
    sha: str,
    default_branch: str,
) -> MergeProof:
    remote_ref = f"refs/remotes/origin/{default_branch}"
    if _is_ancestor(repository, sha, remote_ref):
        return MergeProof("git-ancestor")
    merged_pr = _find_exact_merged_pr_for_branch(
        repository, branch, sha, default_branch
    )
    if merged_pr is not None:
        return MergeProof("github-pr", merged_pr[0], merged_pr[1])
    raise CleanupError(
        "selected branch has not been safely verified as merged into "
        f"{default_branch}"
    )


def _verify_commit_merged(
    repository: pathlib.Path,
    repo_slug: str,
    sha: str,
    default_branch: str,
) -> MergeProof:
    remote_ref = f"refs/remotes/origin/{default_branch}"
    if _is_ancestor(repository, sha, remote_ref):
        return MergeProof("git-ancestor")
    merged_pr = _find_exact_merged_pr_for_commit(
        repository, repo_slug, sha, default_branch
    )
    if merged_pr is not None:
        return MergeProof("github-pr", merged_pr[0], merged_pr[1])
    raise CleanupError(
        "selected worktree HEAD has not been safely verified as merged into "
        f"{default_branch}"
    )


def _current_target_state(
    repository: pathlib.Path,
    target: CleanupTarget,
    worktrees: list[Worktree],
) -> CurrentTargetState:
    branch_exists = bool(
        target.branch and _branch_exists(repository, target.branch)
    )
    branch_sha = (
        _branch_sha(repository, target.branch)
        if branch_exists and target.branch
        else None
    )

    selected_worktree = None
    if target.path is not None:
        selected_worktree = next(
            (
                worktree
                for worktree in worktrees
                if worktree.path == target.path
            ),
            None,
        )
    worktree_exists = selected_worktree is not None
    return CurrentTargetState(
        branch_exists=branch_exists,
        branch_sha=branch_sha,
        worktree_exists=worktree_exists,
        worktree_head=selected_worktree.head if selected_worktree else None,
        worktree_branch=selected_worktree.branch if selected_worktree else None,
        worktree_status=(
            _worktree_status(selected_worktree)
            if selected_worktree
            else None
        ),
    )


def _validate_selected_identity(
    repository: pathlib.Path,
    target: CleanupTarget,
    worktrees: list[Worktree],
    state: CurrentTargetState,
) -> None:
    primary = worktrees[0]
    path_by_branch = {
        worktree.branch: worktree.path
        for worktree in worktrees
        if worktree.branch is not None
    }

    if state.branch_exists and state.branch_sha != target.head:
        raise CleanupError("selected branch changed after selection")
    if state.worktree_exists and state.worktree_head != target.head:
        raise CleanupError("selected worktree HEAD changed after selection")
    if (
        state.worktree_exists
        and state.worktree_branch != target.worktree_branch
    ):
        raise CleanupError(
            "selected worktree branch identity changed after selection"
        )
    if target.has_branch and not target.has_worktree and target.branch:
        new_path = path_by_branch.get(target.branch)
        if new_path is not None and new_path != primary.path:
            raise CleanupError(
                "selected branch gained a worktree after selection"
            )
    if not state.branch_exists and not state.worktree_exists:
        raise CleanupError("selected target is already gone")


def _protect_primary_and_default(
    repository: pathlib.Path,
    target: CleanupTarget,
    worktrees: list[Worktree],
    state: CurrentTargetState,
    default_branch: str,
) -> None:
    primary = worktrees[0]
    if state.branch_exists and target.branch:
        if target.branch in {default_branch, primary.branch}:
            raise CleanupError("refusing to delete the default/primary branch")
    if state.worktree_exists and target.path == primary.path:
        raise CleanupError("refusing to delete the primary worktree")
    if state.worktree_exists and target.path is not None:
        current_directory = pathlib.Path.cwd().resolve()
        try:
            current_directory.relative_to(target.path)
        except ValueError:
            pass
        else:
            raise CleanupError(
                "selected worktree is the current working directory"
            )


def _validate_worktree_status(state: CurrentTargetState) -> None:
    if not state.worktree_exists:
        return
    status = state.worktree_status
    if status in {WorktreeStatus.CLEAN, WorktreeStatus.IGNORED_ONLY}:
        return
    messages = {
        WorktreeStatus.DIRTY: (
            "selected worktree contains tracked changes or non-ignored "
            "untracked files"
        ),
        WorktreeStatus.STALE: (
            "selected worktree registration is stale; this tool does not run "
            "git worktree prune automatically"
        ),
        WorktreeStatus.LOCKED: "selected worktree is locked",
        WorktreeStatus.UNKNOWN: "unable to inspect selected worktree safely",
    }
    raise CleanupError(messages.get(status, f"unsafe worktree state: {status}"))


def _show_ignored_cleanup_preview(path: pathlib.Path) -> None:
    result = _RUNNER.run(
        ["git", "-C", path, "clean", "-ndX"],
        check=False,
    )
    if result.returncode != 0:
        raise CleanupError("unable to preview ignored-file cleanup")
    print("\nIgnored files/directories that Git would remove:\n")
    preview = result.stdout.strip()
    if not preview:
        print("  (none)")
        return
    for line in preview.splitlines():
        print(f"  {line}")


def _show_cleanup_plan(
    repository: pathlib.Path,
    target: CleanupTarget,
    state: CurrentTargetState,
    context: GitHubContext | None,
    proof: MergeProof,
    default_branch: str,
    colors: Colors,
) -> None:
    print(f"\n{colors.bold}Selected cleanup target{colors.reset}\n")
    print(f"  Repository:     {repository}")
    print(f"  Branch:         {target.branch or '(none)'}")

    if context is not None:
        if context.context_type == "PR":
            state_color = {
                "MERGED": colors.green,
                "OPEN": colors.yellow,
                "CLOSED": colors.red,
            }.get(context.state, colors.reset)
            print(
                f"\n  {colors.bold}{colors.cyan}PR:             "
                f"#{context.number} {context.title}{colors.reset}"
            )
            print(
                f"  PR state:       {state_color}{context.state}{colors.reset}"
            )
            print(
                f"  {colors.dim}PR URL:         "
                f"{context.url}{colors.reset}\n"
            )
        else:
            state_color = {
                "OPEN": colors.yellow,
                "CLOSED": colors.green,
            }.get(context.state, colors.reset)
            print(
                f"\n  {colors.bold}{colors.cyan}Issue:          "
                f"#{context.number} {context.title}{colors.reset}"
            )
            print(
                f"  Issue state:    {state_color}{context.state}{colors.reset}"
            )
            print(
                f"  {colors.dim}Issue URL:      "
                f"{context.url}{colors.reset}\n"
            )
    else:
        print("  GitHub context: (not found)")

    worktree_display = target.path if state.worktree_exists else "(none)"
    print(f"  Worktree:       {worktree_display}")
    status = state.worktree_status.value if state.worktree_status else "-"
    status_color = {
        "CLEAN": colors.green,
        "IGNORED_ONLY": colors.yellow,
    }.get(status, colors.reset)
    print(f"  Worktree state: {status_color}{status}{colors.reset}")
    print(f"  HEAD:           {state.branch_sha or state.worktree_head}")
    if proof.proof_type == "github-pr":
        print(f"  Merge proof:    PR #{proof.pr_number} -> {default_branch}")
        print(f"  PR URL:         {proof.pr_url}")
    else:
        print(f"  Merge proof:    HEAD is contained in origin/{default_branch}")

    if state.worktree_status == WorktreeStatus.IGNORED_ONLY and target.path:
        _show_ignored_cleanup_preview(target.path)
        print(f"\n{colors.yellow}WARNING:{colors.reset}")
        print("  The worktree contains ignored files.")
        print(
            "  This may include generated files and local files such as .env."
        )
        print("  Review the list above carefully.")

    print("\nPlanned actions:")
    if state.worktree_status == WorktreeStatus.IGNORED_ONLY:
        print("  - remove ignored files with: git clean -fdX")
    if state.worktree_exists and target.path:
        print(f"  - remove worktree: {target.path}")
    if state.branch_exists and target.branch:
        print(f"  - delete local branch: {target.branch}")
    print("\nOther worktrees and branches will not be modified.")


def _confirm(message: str) -> bool:
    answer = input(f"\n{message} [y/N]: ").strip().lower()
    return answer == "y"


def _verify_merge(
    repository: pathlib.Path,
    target: CleanupTarget,
    state: CurrentTargetState,
    default_branch: str,
    repo_slug: str,
) -> MergeProof:
    if state.branch_exists and target.branch and state.branch_sha:
        return _verify_branch_merged(
            repository,
            target.branch,
            state.branch_sha,
            default_branch,
        )
    if state.worktree_head:
        return _verify_commit_merged(
            repository,
            repo_slug,
            state.worktree_head,
            default_branch,
        )
    raise CleanupError("unable to determine selected target HEAD")


def _final_race_condition_checks(
    repository: pathlib.Path,
    target: CleanupTarget,
    previous: CurrentTargetState,
    default_branch: str,
    repo_slug: str,
) -> None:
    worktrees = _load_worktrees(repository)
    latest = _current_target_state(repository, target, worktrees)

    if previous.worktree_exists:
        if (
            not latest.worktree_exists
            or latest.worktree_head != previous.worktree_head
        ):
            raise CleanupError(
                "worktree HEAD changed before deletion; cleanup aborted"
            )
        if latest.worktree_status != previous.worktree_status:
            raise CleanupError(
                "worktree state changed before deletion; cleanup aborted"
            )
        if target.path is None:
            raise CleanupError("selected worktree path is missing")
        latest_fs_head = _git_output(
            target.path, "rev-parse", "--verify", "HEAD"
        )
        if latest_fs_head != previous.worktree_head:
            raise CleanupError(
                "worktree HEAD changed before deletion; cleanup aborted"
            )

    if previous.branch_exists:
        if not latest.branch_exists or latest.branch_sha != previous.branch_sha:
            raise CleanupError(
                "branch changed before deletion; cleanup aborted"
            )

    _verify_merge(repository, target, latest, default_branch, repo_slug)


def _remove_ignored_files(path: pathlib.Path) -> None:
    print("Removing ignored files from worktree...")
    _RUNNER.run(["git", "-C", path, "clean", "-fdX"])


def _remove_worktree(repository: pathlib.Path, path: pathlib.Path) -> None:
    print("Removing worktree...")
    _RUNNER.run(["git", "-C", repository, "worktree", "remove", path])


def _remove_branch(
    repository: pathlib.Path,
    branch: str,
    expected_sha: str,
) -> None:
    print("Removing local branch...")
    delete_result = _RUNNER.run(
        ["git", "-C", repository, "branch", "-d", branch],
        check=False,
    )
    if delete_result.returncode == 0:
        return

    worktrees = _load_worktrees(repository)
    if any(worktree.branch == branch for worktree in worktrees):
        raise CleanupError(
            "branch is checked out in another worktree; refusing to delete it"
        )
    if not _branch_exists(repository, branch):
        return
    latest_sha = _branch_sha(repository, branch)
    if latest_sha != expected_sha:
        raise CleanupError("branch changed before deletion; cleanup aborted")

    _RUNNER.run(
        [
            "git",
            "-C",
            repository,
            "update-ref",
            "-d",
            f"refs/heads/{branch}",
            expected_sha,
        ]
    )


def _cleanup_target(
    repository: pathlib.Path,
    target: CleanupTarget,
    *,
    dry_run: bool,
    colors: Colors,
) -> None:
    print("\nSelected cleanup candidate\n")
    print(f"  Repository: {repository}")
    print(f"  Type:       {target.target_type.value}")
    print(f"  Branch:     {target.branch_display}")
    print(f"  Path:       {target.path or '-'}")
    print(f"  HEAD:       {target.head}")
    print("\nRefreshing remote state for safety checks...")

    default_branch, repo_slug = _refresh_remote_state(repository)
    context = _load_github_context(
        repository, target.branch_display, target.head
    )
    worktrees = _load_worktrees(repository)
    state = _current_target_state(repository, target, worktrees)

    _validate_selected_identity(repository, target, worktrees, state)
    _protect_primary_and_default(
        repository, target, worktrees, state, default_branch
    )
    _validate_worktree_status(state)
    proof = _verify_merge(
        repository, target, state, default_branch, repo_slug
    )
    _show_cleanup_plan(
        repository,
        target,
        state,
        context,
        proof,
        default_branch,
        colors,
    )

    if dry_run:
        print("\nDry run: no changes made.")
        return

    confirmation = (
        "Ignored files will be deleted. Continue?"
        if state.worktree_status == WorktreeStatus.IGNORED_ONLY
        else "Continue cleanup?"
    )
    if not _confirm(confirmation):
        print("Cleanup cancelled.")
        return

    _final_race_condition_checks(
        repository, target, state, default_branch, repo_slug
    )

    if state.worktree_exists and target.path:
        if state.worktree_status == WorktreeStatus.IGNORED_ONLY:
            _remove_ignored_files(target.path)
            refreshed = _load_worktrees(repository)
            selected = next(
                (
                    worktree
                    for worktree in refreshed
                    if worktree.path == target.path
                ),
                None,
            )
            if (
                selected is None
                or _worktree_status(selected) != WorktreeStatus.CLEAN
            ):
                raise CleanupError(
                    "worktree is not clean after removing ignored files; "
                    "cleanup aborted"
                )
        _remove_worktree(repository, target.path)

    if state.branch_exists and target.branch and state.branch_sha:
        _remove_branch(repository, target.branch, state.branch_sha)

    print(f"\n{colors.green}Cleanup completed.{colors.reset}")
    print("Other worktrees and branches were not modified.")


def _run_repository_loop(
    repository: Repository, *, dry_run: bool, colors: Colors
) -> None:
    print("\nSelected repository\n")
    print(f"  Name:   {repository.name}")
    print(f"  Path:   {repository.path}")
    print(f"  Branch: {repository.branch}")
    print(f"  Origin: {repository.origin}\n")

    while True:
        worktrees = _load_worktrees(repository.path)
        primary = worktrees[0]
        if repository.path != primary.path:
            raise CleanupError(
                "selected repository is not the primary worktree: "
                f"{primary.path}"
            )
        default_branch = _detect_local_default_branch(
            repository.path, primary.branch
        )
        targets = _build_targets(repository.path, worktrees, default_branch)
        if not targets:
            print(f"No cleanup candidates found for {repository.path}.")
            return

        try:
            target = _select_cleanup_target(targets)
        except SelectionCancelled:
            print("Cleanup target selection cancelled.")
            return

        try:
            _cleanup_target(
                repository.path,
                target,
                dry_run=dry_run,
                colors=colors,
            )
        except CleanupError as exc:
            print(f"\nERROR: {exc}", file=sys.stderr)
            print("Returning to cleanup target selection.")


def main() -> int:
    args = _parse_args()
    try:
        _require_interactive_terminal()
        _require_commands("git", "fzf", "gh")
        config = _load_config(args.config)
        state = _load_state()
        colors = _load_colors()
        repositories = _find_repositories(config.root_directory)
        try:
            repository = _select_repository(repositories, state)
        except SelectionCancelled:
            print("Repository selection cancelled.")
            return 0
        _save_state(repository.path)
        _run_repository_loop(
            repository,
            dry_run=args.dry_run,
            colors=colors,
        )
        return 0
    except CleanupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130


if __name__ == "__main__":
    sys.exit(main())