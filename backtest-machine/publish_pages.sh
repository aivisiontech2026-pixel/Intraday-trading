#!/usr/bin/env bash
# Publish backtest-machine/site to the gh-pages branch.        (S-30)
#
# THE DEFECT THIS REPLACES
# -----------------------
# Both intraday.yml and premarket.yml published with:
#
#     cd site && git init -q && git checkout -b gh-pages
#     git add -A && git commit && git push -f ... gh-pages
#
# Three problems, in increasing order of severity:
#
# 1. FORCE-PUSH FROM AN ORPHAN REPO. Every publish discarded the branch's
#    entire history and replaced it wholesale. Nothing detected or
#    reported a lost write, because a force-push always succeeds.
#
# 2. NO SHARED LOCK. The two workflows sit in DIFFERENT concurrency
#    groups - deliberately, so the daily pre-market cron cannot queue
#    behind the 5-minute trading loop - so their publishes can and do
#    overlap. Both generate the same dashboard, so no content was
#    actually lost; but the ordering of the two force-pushes was
#    undefined, and an older snapshot could land last and stay live.
#
# 3. PUSH VOLUME. This is the one that was actually breaking. The
#    trading loop publishes every cycle whether or not anything changed
#    - roughly 45 pushes/hour against a GitHub Pages build limit of 10
#    builds/hour. That is the direct cause of the red gh-pages
#    deployments: the pushes succeeded and the BUILDS were rejected.
#
# WHAT THIS DOES INSTEAD
# ----------------------
# Fetches the existing branch, applies the new files on top, and pushes
# ONLY IF THE CONTENT ACTUALLY CHANGED. A cycle that produces a
# byte-identical dashboard now exits without a commit and without a
# push, which removes the great majority of the builds. The push is a
# normal fast-forward, so history is preserved and a concurrent update
# is REJECTED rather than silently overwritten - and the retry below
# rebases onto whatever the other workflow published instead of
# discarding it.
#
# This changes only how the dashboard is published. It does not change
# the dashboard, the trading state, or any decision.
set -euo pipefail

REPO_URL="$1"          # https://x-access-token:TOKEN@github.com/owner/repo.git
SITE_DIR="${2:-site}"

cd "$(dirname "$0")"
[ -d "$SITE_DIR" ] || { echo "publish_pages: no $SITE_DIR - nothing to do"; exit 0; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Shallow-fetch the existing branch. A missing branch (first ever run) is
# not an error - start an empty one.
git init -q "$WORK"
git -C "$WORK" remote add origin "$REPO_URL"
if git -C "$WORK" fetch -q --depth=1 origin gh-pages 2>/dev/null; then
    git -C "$WORK" checkout -q -B gh-pages FETCH_HEAD
    echo "publish_pages: fetched existing gh-pages"
else
    git -C "$WORK" checkout -q -B gh-pages
    echo "publish_pages: gh-pages does not exist yet - creating"
fi

# Replace tracked content with the freshly generated site. Deletions in
# the generator must propagate, so clear first - but never touch .git.
find "$WORK" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
cp -r "$SITE_DIR"/. "$WORK"/

git -C "$WORK" add -A
if git -C "$WORK" diff --cached --quiet; then
    echo "publish_pages: dashboard unchanged - no commit, no push, no Pages build"
    exit 0
fi

git -C "$WORK" \
    -c user.name="intraday-ci" \
    -c user.email="ci@users.noreply.github.com" \
    commit -q -m "dashboard $(date -u +%FT%TZ)"

# Fast-forward push. If the other workflow published in between, the push
# is rejected; re-fetch, replay THIS content on top of theirs, and retry.
# Bounded so a persistent conflict fails loudly instead of looping.
for attempt in 1 2 3; do
    if git -C "$WORK" push -q origin gh-pages 2>/dev/null; then
        echo "publish_pages: published (attempt $attempt)"
        exit 0
    fi
    echo "publish_pages: push rejected - concurrent update, rebasing (attempt $attempt)"
    git -C "$WORK" fetch -q --depth=1 origin gh-pages
    git -C "$WORK" reset -q --soft FETCH_HEAD
    git -C "$WORK" add -A
    if git -C "$WORK" diff --cached --quiet; then
        echo "publish_pages: other workflow published identical content - done"
        exit 0
    fi
    git -C "$WORK" \
        -c user.name="intraday-ci" \
        -c user.email="ci@users.noreply.github.com" \
        commit -q -m "dashboard $(date -u +%FT%TZ)"
done

echo "publish_pages: FAILED to publish after 3 attempts" >&2
exit 1
