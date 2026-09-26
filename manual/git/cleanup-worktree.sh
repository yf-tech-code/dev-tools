#!/usr/bin/env bash
set -Eeuo pipefail

# MANUAL HUMAN-OPERATED MAINTENANCE TOOL.
#
# Do not run this script from an agent, CI, Git hook, scheduler,
# or any other automation.
#
# This script intentionally requires:
#   - an interactive TTY
#   - explicit fzf selection
#   - merge verification
#   - explicit human confirmation
#
# It removes at most ONE cleanup target per execution.

usage() {
  cat <<'EOF'
Usage:
  cleanup-git-worktree [--dry-run]

Safely removes ONE selected local cleanup target:

  BOTH
    linked worktree + local branch

  BRANCH_ONLY
    local branch only

  WORKTREE_ONLY
    linked/detached worktree only

The target is selected with fzf.

Dirty or unmerged targets are shown, but deletion is refused.

Run this command from the repository's primary worktree.
EOF
}

DRY_RUN=0

case "${1:-}" in
  "")
    ;;
  --dry-run)
    DRY_RUN=1
    ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 \
    || fail "required command not found: $1"
}

# ----------------------------------------------------------------------
# Human / interactive execution guard
# ----------------------------------------------------------------------

[[ -t 0 && -t 1 && -t 2 ]] \
  || fail "this script must be run manually from an interactive terminal"

require_cmd git
require_cmd fzf
require_cmd gh
require_cmd realpath

# ----------------------------------------------------------------------
# Repository
# ----------------------------------------------------------------------

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" \
  || fail "not inside a Git repository"

REPO_ROOT="$(realpath -m "$REPO_ROOT")"

cd "$REPO_ROOT"

# ----------------------------------------------------------------------
# Worktree state
# ----------------------------------------------------------------------

declare -a WT_PATHS=()

declare -A WT_HEAD=()
declare -A WT_BRANCH=()
declare -A WT_PRUNABLE=()
declare -A WT_LOCKED=()
declare -A PATH_BY_BRANCH=()

PRIMARY_PATH=""
PRIMARY_BRANCH=""

load_worktrees() {
  WT_PATHS=()

  WT_HEAD=()
  WT_BRANCH=()
  WT_PRUNABLE=()
  WT_LOCKED=()
  PATH_BY_BRANCH=()

  PRIMARY_PATH=""
  PRIMARY_BRANCH=""

  local path=""
  local head=""
  local branch=""
  local prunable=0
  local locked=0
  local first=1

  flush_entry() {
    [[ -n "$path" ]] || return 0

    path="$(realpath -m "$path")"

    WT_PATHS+=("$path")
    WT_HEAD["$path"]="$head"
    WT_BRANCH["$path"]="$branch"
    WT_PRUNABLE["$path"]="$prunable"
    WT_LOCKED["$path"]="$locked"

    if [[ -n "$branch" ]]; then
      PATH_BY_BRANCH["$branch"]="$path"
    fi

    if (( first )); then
      PRIMARY_PATH="$path"
      PRIMARY_BRANCH="$branch"
      first=0
    fi

    path=""
    head=""
    branch=""
    prunable=0
    locked=0
  }

  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ -z "$line" ]]; then
      flush_entry
      continue
    fi

    case "$line" in
      worktree\ *)
        path="${line#worktree }"
        ;;

      HEAD\ *)
        head="${line#HEAD }"
        ;;

      branch\ refs/heads/*)
        branch="${line#branch refs/heads/}"
        ;;

      detached)
        branch=""
        ;;

      prunable*)
        prunable=1
        ;;

      locked*)
        locked=1
        ;;
    esac

  done < <(git worktree list --porcelain)

  flush_entry
}

load_worktrees

[[ -n "$PRIMARY_PATH" ]] \
  || fail "unable to determine primary worktree"

[[ "$REPO_ROOT" == "$PRIMARY_PATH" ]] \
  || fail "run this tool from the repository's primary worktree: $PRIMARY_PATH"

# ----------------------------------------------------------------------
# Default branch / GitHub repository
# ----------------------------------------------------------------------

DEFAULT_BRANCH="$(
  git symbolic-ref \
    --quiet \
    --short \
    refs/remotes/origin/HEAD \
    2>/dev/null \
    | sed 's#^origin/##' \
    || true
)"

if [[ -z "$DEFAULT_BRANCH" ]]; then
  DEFAULT_BRANCH="$(
    gh repo view \
      --json defaultBranchRef \
      --jq '.defaultBranchRef.name' \
      2>/dev/null \
      || true
  )"
fi

[[ -n "$DEFAULT_BRANCH" ]] \
  || fail "unable to determine repository default branch"

REPO_SLUG="$(
  gh repo view \
    --json nameWithOwner \
    --jq '.nameWithOwner' \
    2>/dev/null \
    || true
)"

[[ -n "$REPO_SLUG" ]] \
  || fail "unable to resolve GitHub repository with gh"

gh auth status >/dev/null 2>&1 \
  || fail "gh is not authenticated"

# Refresh only the remote-tracking ref used by safety checks.
git fetch \
  --quiet \
  origin \
  "+refs/heads/${DEFAULT_BRANCH}:refs/remotes/origin/${DEFAULT_BRANCH}" \
  || fail "failed to fetch origin/$DEFAULT_BRANCH"

# ----------------------------------------------------------------------
# Git helpers
# ----------------------------------------------------------------------

branch_exists() {
  git show-ref \
    --verify \
    --quiet \
    "refs/heads/$1"
}

branch_sha() {
  git rev-parse \
    --verify \
    "refs/heads/$1^{commit}" \
    2>/dev/null
}

# ----------------------------------------------------------------------
# GitHub merge verification
# ----------------------------------------------------------------------

# Return:
#   0 = exact merged PR found
#   1 = not found
#   2 = GitHub query failed
find_exact_merged_pr_for_branch() {
  local branch="$1"
  local sha="$2"

  local output
  local number
  local oid
  local url

  if ! output="$(
    gh pr list \
      --head "$branch" \
      --base "$DEFAULT_BRANCH" \
      --state merged \
      --limit 100 \
      --json number,headRefOid,url \
      --jq '.[] | [.number, .headRefOid, .url] | @tsv' \
      2>/dev/null
  )"; then
    return 2
  fi

  while IFS=$'\t' read -r number oid url; do
    [[ -n "$number" ]] || continue

    if [[ "$oid" == "$sha" ]]; then
      printf '%s\t%s\n' "$number" "$url"
      return 0
    fi
  done <<< "$output"

  return 1
}

# Used for detached / branchless worktrees.
#
# Return:
#   0 = exact HEAD belongs to merged PR
#   1 = not found
#   2 = GitHub query failed
find_exact_merged_pr_for_commit() {
  local sha="$1"

  local output
  local number
  local base
  local head
  local url

  if ! output="$(
    gh api \
      "repos/${REPO_SLUG}/commits/${sha}/pulls" \
      --jq \
      '.[] |
       select(.merged_at != null) |
       [.number, .base.ref, .head.sha, .html_url] |
       @tsv' \
      2>/dev/null
  )"; then
    return 2
  fi

  while IFS=$'\t' read -r number base head url; do
    [[ -n "$number" ]] || continue

    if [[ "$base" == "$DEFAULT_BRANCH" && "$head" == "$sha" ]]; then
      printf '%s\t%s\n' "$number" "$url"
      return 0
    fi
  done <<< "$output"

  return 1
}

MERGE_PROOF=""
MERGED_PR=""
MERGED_PR_URL=""

verify_branch_merged() {
  local branch="$1"
  local sha="$2"

  local result
  local rc

  MERGE_PROOF=""
  MERGED_PR=""
  MERGED_PR_URL=""

  # Standard merge / rebase merge.
  if git merge-base \
    --is-ancestor \
    "$sha" \
    "refs/remotes/origin/$DEFAULT_BRANCH" \
    2>/dev/null; then

    MERGE_PROOF="git-ancestor"
    return 0
  fi

  # Squash Merge etc.
  set +e
  result="$(find_exact_merged_pr_for_branch "$branch" "$sha")"
  rc=$?
  set -e

  if (( rc == 0 )); then
    IFS=$'\t' read -r MERGED_PR MERGED_PR_URL <<< "$result"

    MERGE_PROOF="github-pr"
    return 0
  fi

  (( rc == 2 )) && return 2

  return 1
}

verify_commit_merged() {
  local sha="$1"

  local result
  local rc

  MERGE_PROOF=""
  MERGED_PR=""
  MERGED_PR_URL=""

  if git merge-base \
    --is-ancestor \
    "$sha" \
    "refs/remotes/origin/$DEFAULT_BRANCH" \
    2>/dev/null; then

    MERGE_PROOF="git-ancestor"
    return 0
  fi

  set +e
  result="$(find_exact_merged_pr_for_commit "$sha")"
  rc=$?
  set -e

  if (( rc == 0 )); then
    IFS=$'\t' read -r MERGED_PR MERGED_PR_URL <<< "$result"

    MERGE_PROOF="github-pr"
    return 0
  fi

  (( rc == 2 )) && return 2

  return 1
}

merge_label_for_branch() {
  local branch="$1"
  local sha="$2"
  local rc

  if verify_branch_merged "$branch" "$sha"; then
    if [[ "$MERGE_PROOF" == "github-pr" ]]; then
      printf 'MERGED#%s' "$MERGED_PR"
    else
      printf 'MERGED'
    fi
    return
  else
    rc=$?
  fi

  if (( rc == 2 )); then
    printf 'UNKNOWN'
  else
    printf 'UNMERGED'
  fi
}

merge_label_for_commit() {
  local sha="$1"
  local rc

  if verify_commit_merged "$sha"; then
    if [[ "$MERGE_PROOF" == "github-pr" ]]; then
      printf 'MERGED#%s' "$MERGED_PR"
    else
      printf 'MERGED'
    fi
    return
  else
    rc=$?
  fi

  if (( rc == 2 )); then
    printf 'UNKNOWN'
  else
    printf 'UNMERGED'
  fi
}

# ----------------------------------------------------------------------
# Worktree safety
# ----------------------------------------------------------------------

worktree_status() {
  local path="$1"

  if [[ "${WT_LOCKED[$path]:-0}" == "1" ]]; then
    printf 'LOCKED'
    return
  fi

  # Registration exists, but directory is already gone.
  if [[ ! -d "$path" ]]; then
    printf 'MISSING'
    return
  fi

  local status
  local ignored

  if ! status="$(
    git -C "$path" \
      status \
      --porcelain \
      --untracked-files=normal \
      2>/dev/null
  )"; then
    printf 'UNKNOWN'
    return
  fi

  if [[ -n "$status" ]]; then
    printf 'DIRTY'
    return
  fi

  # git worktree remove can delete ignored files.
  # Therefore ignored local files are treated as local data.
  if ! ignored="$(
    git -C "$path" \
      ls-files \
      --others \
      --ignored \
      --exclude-standard \
      2>/dev/null
  )"; then
    printf 'UNKNOWN'
    return
  fi

  if [[ -n "$ignored" ]]; then
    printf 'DIRTY'
    return
  fi

  printf 'CLEAN'
}

# ----------------------------------------------------------------------
# Candidate generation
# ----------------------------------------------------------------------

TMP_CANDIDATES="$(mktemp)"

trap 'rm -f "$TMP_CANDIDATES"' EXIT

declare -A SEEN_BRANCH=()

# First: linked worktrees.
for path in "${WT_PATHS[@]}"; do
  [[ "$path" == "$PRIMARY_PATH" ]] && continue

  branch="${WT_BRANCH[$path]:-}"
  head="${WT_HEAD[$path]:-}"

  [[ -n "$head" ]] || continue

  # Never offer the default branch.
  [[ -n "$branch" && "$branch" == "$DEFAULT_BRANCH" ]] \
    && continue

  status="$(worktree_status "$path")"

  if [[ -n "$branch" ]] && branch_exists "$branch"; then
    type="BOTH"

    bsha="$(branch_sha "$branch")"

    if [[ "$bsha" != "$head" ]]; then
      merge="UNKNOWN"
    else
      merge="$(merge_label_for_branch "$branch" "$bsha")"
    fi

    SEEN_BRANCH["$branch"]=1

    printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$type" \
      "$branch" \
      "$status" \
      "$merge" \
      "$path" \
      "$head" \
      >> "$TMP_CANDIDATES"

  else
    type="WORKTREE_ONLY"

    display_branch="${branch:-'(detached)'}"

    merge="$(merge_label_for_commit "$head")"

    printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$type" \
      "$display_branch" \
      "$status" \
      "$merge" \
      "$path" \
      "$head" \
      >> "$TMP_CANDIDATES"
  fi
done

# Second: local branches without worktrees.
while IFS=$'\t' read -r branch sha; do
  [[ -n "$branch" ]] || continue

  [[ "$branch" == "$DEFAULT_BRANCH" ]] && continue
  [[ "$branch" == "$PRIMARY_BRANCH" ]] && continue

  [[ -n "${SEEN_BRANCH[$branch]:-}" ]] && continue

  # Do not incorrectly call a checked-out branch BRANCH_ONLY.
  [[ -n "${PATH_BY_BRANCH[$branch]:-}" ]] && continue

  merge="$(merge_label_for_branch "$branch" "$sha")"

  printf 'BRANCH_ONLY\t%s\t-\t%s\t-\t%s\n' \
    "$branch" \
    "$merge" \
    "$sha" \
    >> "$TMP_CANDIDATES"

done < <(
  git for-each-ref \
    --format='%(refname:short)%09%(objectname)' \
    refs/heads/
)

if [[ ! -s "$TMP_CANDIDATES" ]]; then
  printf 'No cleanup candidates found.\n'
  exit 0
fi

# ----------------------------------------------------------------------
# fzf
# ----------------------------------------------------------------------

FZF_HEADER=$'TYPE\tBRANCH\tWORKTREE\tMERGE\tPATH'

if ! SELECTED="$(
  fzf \
    --delimiter=$'\t' \
    --with-nth=1,2,3,4,5 \
    --header="$FZF_HEADER" \
    --prompt='cleanup target> ' \
    --height='80%' \
    --layout=reverse \
    --border \
    --no-multi \
    --preview-window='down,8,wrap' \
    --preview='
      printf "Type: %s\nBranch: %s\nWorktree state: %s\nMerge: %s\nPath: %s\nHEAD: %s\n" \
        {1} {2} {3} {4} {5} {6}
    ' \
    < "$TMP_CANDIDATES"
)"; then

  printf 'Cleanup cancelled.\n'
  exit 0
fi

IFS=$'\t' read -r \
  SELECTED_TYPE \
  SELECTED_BRANCH_DISPLAY \
  SELECTED_STATUS \
  SELECTED_MERGE \
  SELECTED_PATH \
  SELECTED_HEAD \
  <<< "$SELECTED"

# ----------------------------------------------------------------------
# Remember selection identity
# ----------------------------------------------------------------------

SELECTED_HAD_WORKTREE=0
SELECTED_HAD_BRANCH=0

case "$SELECTED_TYPE" in
  BOTH)
    SELECTED_HAD_WORKTREE=1
    SELECTED_HAD_BRANCH=1
    ;;

  BRANCH_ONLY)
    SELECTED_HAD_BRANCH=1
    ;;

  WORKTREE_ONLY)
    SELECTED_HAD_WORKTREE=1
    ;;

  *)
    fail "unexpected candidate type: $SELECTED_TYPE"
    ;;
esac

SELECTED_BRANCH=""

if (( SELECTED_HAD_BRANCH )); then
  SELECTED_BRANCH="$SELECTED_BRANCH_DISPLAY"
fi

SELECTED_WORKTREE_BRANCH=""

if (( SELECTED_HAD_WORKTREE )) \
  && [[ "$SELECTED_BRANCH_DISPLAY" != "(detached)" ]]; then

  SELECTED_WORKTREE_BRANCH="$SELECTED_BRANCH_DISPLAY"
fi

# ----------------------------------------------------------------------
# Re-read state after fzf
# ----------------------------------------------------------------------

load_worktrees

CURRENT_BRANCH_EXISTS=0
CURRENT_WORKTREE_EXISTS=0

CURRENT_BRANCH_SHA=""
CURRENT_WORKTREE_HEAD=""
CURRENT_WORKTREE_BRANCH=""

if [[ -n "$SELECTED_BRANCH" ]] \
  && branch_exists "$SELECTED_BRANCH"; then

  CURRENT_BRANCH_EXISTS=1
  CURRENT_BRANCH_SHA="$(branch_sha "$SELECTED_BRANCH")"
fi

if [[ "$SELECTED_PATH" != "-" ]]; then
  for path in "${WT_PATHS[@]}"; do
    if [[ "$path" == "$SELECTED_PATH" ]]; then
      CURRENT_WORKTREE_EXISTS=1

      CURRENT_WORKTREE_HEAD="${WT_HEAD[$path]:-}"
      CURRENT_WORKTREE_BRANCH="${WT_BRANCH[$path]:-}"

      break
    fi
  done
fi

# A resource may disappear between fzf and confirmation.
# That is fine.
#
# But it must not change identity.

if (( CURRENT_BRANCH_EXISTS )) \
  && [[ "$CURRENT_BRANCH_SHA" != "$SELECTED_HEAD" ]]; then

  fail "selected branch changed after selection; run the script again"
fi

if (( CURRENT_WORKTREE_EXISTS )) \
  && [[ "$CURRENT_WORKTREE_HEAD" != "$SELECTED_HEAD" ]]; then

  fail "selected worktree HEAD changed after selection; run the script again"
fi

if (( CURRENT_WORKTREE_EXISTS )) \
  && [[ "$CURRENT_WORKTREE_BRANCH" != "$SELECTED_WORKTREE_BRANCH" ]]; then

  fail "selected worktree branch identity changed after selection; run the script again"
fi

# A BRANCH_ONLY target must not suddenly gain a new worktree.
if (( ! SELECTED_HAD_WORKTREE && CURRENT_BRANCH_EXISTS )); then
  new_path="${PATH_BY_BRANCH[$SELECTED_BRANCH]:-}"

  if [[ -n "$new_path" && "$new_path" != "$PRIMARY_PATH" ]]; then
    fail "selected branch gained a worktree after selection; run the script again"
  fi
fi

if (( ! CURRENT_BRANCH_EXISTS && ! CURRENT_WORKTREE_EXISTS )); then
  printf 'Selected target is already gone. Nothing to do.\n'
  exit 0
fi

# ----------------------------------------------------------------------
# Primary/default protection
# ----------------------------------------------------------------------

if (( CURRENT_BRANCH_EXISTS )); then
  if [[ "$SELECTED_BRANCH" == "$DEFAULT_BRANCH" \
        || "$SELECTED_BRANCH" == "$PRIMARY_BRANCH" ]]; then

    fail "refusing to delete the default/primary branch"
  fi
fi

if (( CURRENT_WORKTREE_EXISTS )) \
  && [[ "$SELECTED_PATH" == "$PRIMARY_PATH" ]]; then

  fail "refusing to delete the primary worktree"
fi

# ----------------------------------------------------------------------
# Worktree clean check
# ----------------------------------------------------------------------

if (( CURRENT_WORKTREE_EXISTS )); then
  if [[ "${WT_LOCKED[$SELECTED_PATH]:-0}" == "1" ]]; then
    fail "selected worktree is locked"
  fi

  CURRENT_STATUS="$(worktree_status "$SELECTED_PATH")"

  case "$CURRENT_STATUS" in
    CLEAN|MISSING)
      ;;

    DIRTY)
      fail "selected worktree contains tracked, untracked, or ignored local files"
      ;;

    LOCKED)
      fail "selected worktree is locked"
      ;;

    UNKNOWN)
      fail "unable to inspect selected worktree safely"
      ;;

    *)
      fail "unable to verify selected worktree state: $CURRENT_STATUS"
      ;;
  esac
else
  CURRENT_STATUS="-"
fi

# ----------------------------------------------------------------------
# Merge verification
# ----------------------------------------------------------------------

if (( CURRENT_BRANCH_EXISTS )); then
  if verify_branch_merged \
    "$SELECTED_BRANCH" \
    "$CURRENT_BRANCH_SHA"; then

    :
  else
    rc=$?

    if (( rc == 2 )); then
      fail "GitHub merge state could not be verified"
    fi

    fail "selected branch has not been safely verified as merged into $DEFAULT_BRANCH"
  fi

else
  if verify_commit_merged "$CURRENT_WORKTREE_HEAD"; then
    :
  else
    rc=$?

    if (( rc == 2 )); then
      fail "GitHub merge state could not be verified"
    fi

    fail "selected worktree HEAD has not been safely verified as merged into $DEFAULT_BRANCH"
  fi
fi

# ----------------------------------------------------------------------
# Plan
# ----------------------------------------------------------------------

printf '\nSelected cleanup target\n\n'

printf '  Branch:         %s\n' \
  "${SELECTED_BRANCH:-'(none)'}"

if (( CURRENT_WORKTREE_EXISTS )); then
  printf '  Worktree:       %s\n' "$SELECTED_PATH"
else
  printf '  Worktree:       (none)\n'
fi

printf '  Worktree state: %s\n' "$CURRENT_STATUS"

printf '  HEAD:           %s\n' \
  "${CURRENT_BRANCH_SHA:-$CURRENT_WORKTREE_HEAD}"

if [[ "$MERGE_PROOF" == "github-pr" ]]; then
  printf '  Merge proof:    PR #%s -> %s\n' \
    "$MERGED_PR" \
    "$DEFAULT_BRANCH"

  printf '  PR URL:         %s\n' \
    "$MERGED_PR_URL"
else
  printf '  Merge proof:    HEAD is contained in origin/%s\n' \
    "$DEFAULT_BRANCH"
fi

printf '\nPlanned actions:\n'

if (( CURRENT_WORKTREE_EXISTS )); then
  printf '  - remove worktree: %s\n' \
    "$SELECTED_PATH"
fi

if (( CURRENT_BRANCH_EXISTS )); then
  printf '  - delete local branch: %s\n' \
    "$SELECTED_BRANCH"
fi

printf '\nOther worktrees and branches will not be modified.\n'

if (( DRY_RUN )); then
  printf '\nDry run: no changes made.\n'
  exit 0
fi

# ----------------------------------------------------------------------
# Human confirmation
# ----------------------------------------------------------------------

printf '\nType "cleanup" to continue: '

IFS= read -r CONFIRMATION

if [[ "$CONFIRMATION" != "cleanup" ]]; then
  printf 'Cleanup cancelled.\n'
  exit 0
fi

# ----------------------------------------------------------------------
# Final race-condition checks
# ----------------------------------------------------------------------

load_worktrees

if (( CURRENT_WORKTREE_EXISTS )); then
  latest_registered_head="${WT_HEAD[$SELECTED_PATH]:-}"

  [[ "$latest_registered_head" == "$CURRENT_WORKTREE_HEAD" ]] \
    || fail "worktree HEAD changed before deletion; cleanup aborted"

  if [[ -d "$SELECTED_PATH" ]]; then
    [[ "$(worktree_status "$SELECTED_PATH")" == "CLEAN" ]] \
      || fail "worktree state changed before deletion; cleanup aborted"

    latest_fs_head="$(
      git -C "$SELECTED_PATH" \
        rev-parse \
        --verify \
        HEAD \
        2>/dev/null \
        || true
    )"

    [[ "$latest_fs_head" == "$CURRENT_WORKTREE_HEAD" ]] \
      || fail "worktree HEAD changed before deletion; cleanup aborted"
  fi
fi

if (( CURRENT_BRANCH_EXISTS )); then
  latest_branch_sha="$(
    branch_sha "$SELECTED_BRANCH" \
      || true
  )"

  [[ "$latest_branch_sha" == "$CURRENT_BRANCH_SHA" ]] \
    || fail "branch changed before deletion; cleanup aborted"
fi

# Re-check merge state one final time.
if (( CURRENT_BRANCH_EXISTS )); then
  verify_branch_merged \
    "$SELECTED_BRANCH" \
    "$CURRENT_BRANCH_SHA" \
    || fail "branch merge state changed or could not be verified"
else
  verify_commit_merged \
    "$CURRENT_WORKTREE_HEAD" \
    || fail "worktree merge state changed or could not be verified"
fi

# ----------------------------------------------------------------------
# Remove worktree
# ----------------------------------------------------------------------

if (( CURRENT_WORKTREE_EXISTS )); then
  printf 'Removing worktree...\n'

  git worktree remove "$SELECTED_PATH" \
    || fail "git worktree remove failed; branch was not deleted"
fi

# ----------------------------------------------------------------------
# Remove branch
# ----------------------------------------------------------------------

if (( CURRENT_BRANCH_EXISTS )); then
  printf 'Removing local branch...\n'

  # Standard merged branch.
  if ! git branch \
    -d \
    "$SELECTED_BRANCH" \
    >/dev/null 2>&1; then

    # Squash-merged branches are not ancestors of main, so `git branch -d`
    # can legitimately reject them.
    #
    # Before deleting the exact ref, make sure it is not checked out anywhere.

    load_worktrees

    [[ -z "${PATH_BY_BRANCH[$SELECTED_BRANCH]:-}" ]] \
      || fail "branch is checked out in another worktree; refusing to delete it"

    latest_branch_sha="$(
      branch_sha "$SELECTED_BRANCH" \
        || true
    )"

    [[ "$latest_branch_sha" == "$CURRENT_BRANCH_SHA" ]] \
      || fail "branch changed before deletion; cleanup aborted"

    # Delete ONLY the exact SHA already verified as merged.
    git update-ref \
      -d \
      "refs/heads/$SELECTED_BRANCH" \
      "$CURRENT_BRANCH_SHA" \
      || fail "branch changed or could not be deleted safely"
  fi
fi

printf '\nCleanup completed.\n'
printf 'Other worktrees and branches were not modified.\n'