#!/usr/bin/env bash
# Acts on check_add_on.py's result for a submission issue (submit-add-on.yml):
#   - failed: comments the problems on the issue;
#   - passed: (re)creates branch submit/<owner>-<repo> from main with
#     add_ons/<owner>.<repo>.json, in a commit whose message closes the issue once
#     it reaches main, and comments a link that opens the pull request, filled in,
#     for a maintainer to create (the uaudio-labs org does not let workflows create
#     pull requests), or the pull request's own link once one is open.
# A repo whose branch was pushed for another issue that is still open is not submitted
# twice; once that issue is closed, a new submission takes the branch over.
#
# Needs: ISSUE (the issue number), RESULT (the result JSON), GH_TOKEN,
# GITHUB_REPOSITORY, and a full checkout of main with this repo as origin.
set -euo pipefail

comment="$(jq -r '.comment' "$RESULT")"

if [[ "$(jq -r '.ok' "$RESULT")" != "true" ]]; then
    gh issue comment "$ISSUE" --body "$comment"
    exit 0
fi

full_name="$(jq -r '.full_name' "$RESULT")"
slug="$(jq -r '.slug' "$RESULT")"
branch="$(jq -r '.branch' "$RESULT")"
# GitHub's own spelling of the repo, which check_add_on.py lowered; refuse anything else.
[[ "$slug" =~ ^[a-z0-9-]+\.[a-z0-9._-]+$ && "$branch" =~ ^submit/[a-z0-9-]+-[a-z0-9._-]+$ ]] || {
    echo "unexpected slug '$slug' or branch '$branch'" >&2
    exit 1
}
file="add_ons/$slug.json"
closes="Closes #$ISSUE"

# The branch's commit names the issue it was pushed for.
if git rev-parse --verify --quiet "origin/$branch" >/dev/null; then
    other="$(git log -1 --format=%B "origin/$branch" | grep -oE 'Closes #[0-9]+' | head -n 1 || true)"
    if [[ -n "$other" && "$other" != "$closes" ]] &&
       [[ "$(gh issue view "${other#Closes #}" --json state --jq .state)" == "OPEN" ]]; then
        gh issue comment "$ISSUE" --body "$full_name was already submitted in #${other#Closes #}."
        exit 0
    fi
fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git checkout -B "$branch" origin/main
jq --indent 4 '.entry' "$RESULT" > "$file"
git add "$file"
git commit -m "Add $full_name" -m "$closes"
git push --force origin "$branch"

pr_url="$(gh pr list --head "$branch" --state open --json url --jq '.[0].url // empty')"
if [[ -n "$pr_url" ]]; then
    next="Pull request: $pr_url (updated)"
else
    title="Add On: $full_name"
    body="$(printf 'Adds %s to the LUNA Add On Marketplace.\n\n%s\n\n%s\n' "$full_name" "$closes" "$comment")"
    link="https://github.com/$GITHUB_REPOSITORY/compare/main...$branch?quick_pull=1&title=$(jq -rn --arg v "$title" '$v|@uri')&body=$(jq -rn --arg v "$body" '$v|@uri')"
    next="Branch \`$branch\` is ready for review. Maintainers: [create the pull request]($link), review it and merge it to list the Add On."
fi
gh issue comment "$ISSUE" --body "$(printf '%s\n\n%s\nUniversal Audio reviews every submission before it appears in LUNA.\n' "$comment" "$next")"
