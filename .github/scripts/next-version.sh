#!/usr/bin/env bash
#
# Decide the next version for one tag line from the commits since its last tag.
#
# Usage: next-version.sh <tag-prefix> <bump-or-auto> <path>...
# Prints KEY=VALUE lines for the caller to append to $GITHUB_OUTPUT.
#
# Conventional Commits, with the deliberate default that ANYTHING unrecognised
# is a patch. This repo had no commit convention when this was written, so a
# subject like "Add CI" must keep releasing exactly as it did before — the
# convention is opt-in per commit, and adopting it never becomes a prerequisite
# for shipping.
#
#   feat!: / feat(scope)!:  or  BREAKING CHANGE: in the body  ->  major
#   feat: / feat(scope):                                      ->  minor
#   fix:, perf:, refactor:, docs:, chore:, no prefix at all   ->  patch
#
# Only commits touching <path>... are read, so a chart-only `feat:` does not bump
# the operator's minor. Those paths MUST be the same set the workflow triggers
# on: a commit that can cut a release but is invisible to this scan releases
# with the wrong bump and with empty release notes.
set -euo pipefail

if [ "$#" -lt 3 ]; then
  echo "usage: next-version.sh <tag-prefix> <bump-or-auto> <path>..." >&2
  exit 1
fi
prefix=$1
# EMPTY, not just unset: on a push `${{ inputs.bump }}` expands to '', and the
# workflow passes it positionally. Empty means the same as `auto` here.
explicit=${2:-auto}
shift 2
paths=("$@")

prev=$(git tag -l "$prefix-v*" \
  | sed -nE "s/^$prefix-v([0-9]+\.[0-9]+\.[0-9]+)\$/\1/p" \
  | sort -V | tail -n1)

# No previous tag means every commit under the path is in scope.
if [ -n "$prev" ]; then range="$prefix-v$prev..HEAD"; else range="HEAD"; fi

# `auto` is the workflow_dispatch default, meaning "decide from the commits" —
# so a manual run is not forced to also make a versioning decision.
if [ "$explicit" = auto ]; then explicit=; fi

if [ -n "$explicit" ]; then
  bump=$explicit
  reason="requested explicitly via workflow_dispatch"
else
  subjects=$(git log --no-merges --format='%s' "$range" -- "${paths[@]}" || true)
  bodies=$(git log --no-merges --format='%b' "$range" -- "${paths[@]}" || true)

  # `!` before the colon, per the spec, and the BREAKING CHANGE footer, which
  # the spec requires in caps. BREAKING-CHANGE is the accepted synonym.
  if printf '%s\n' "$subjects" | grep -qE '^[a-zA-Z]+(\([^)]*\))?!:' \
     || printf '%s\n' "$bodies" | grep -qE '^BREAKING[ -]CHANGE:'; then
    bump=major
    reason="a commit declares a breaking change"
  elif printf '%s\n' "$subjects" | grep -qE '^feat(\([^)]*\))?:'; then
    bump=minor
    reason="a commit is a feat:"
  else
    bump=patch
    reason="no feat: or breaking change since ${prev:-the first commit}"
  fi
fi

case "$bump" in
  major|minor|patch) ;;
  *) echo "unknown bump '$bump' — want major, minor or patch" >&2; exit 1 ;;
esac

if [ -z "$prev" ]; then
  # A first release matches the chart's appVersion rather than starting at
  # 0.0.1, so the two do not disagree from day one.
  case "$bump" in
    major) version=1.0.0 ;;
    *)     version=0.1.0 ;;
  esac
else
  IFS=. read -r major minor patch <<EOF
$prev
EOF
  case "$bump" in
    major) version="$((major + 1)).0.0" ;;
    minor) version="$major.$((minor + 1)).0" ;;
    patch) version="$major.$minor.$((patch + 1))" ;;
  esac
fi

if git rev-parse -q --verify "refs/tags/$prefix-v$version" >/dev/null; then
  echo "$prefix-v$version already exists — refusing to overwrite" >&2
  exit 1
fi

echo "previous=${prev:-none}"
echo "version=$version"
echo "tag=$prefix-v$version"
echo "bump=$bump"
echo "reason=$reason"
