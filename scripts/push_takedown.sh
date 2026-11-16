#!/usr/bin/env bash
# Acts on check_takedown.py's result for a takedown issue (submit-take-down.yml):
#   - failed: comments the problems on the issue;
#   - passed: (re)creates branch takedown/<owner>-<repo> from main without
#     add_ons/<owner>.<repo>.json, in a commit whose message closes the issue once
#     it reaches main, and comments a link that opens the pull request, filled in,
#     for a maintainer to create (the uaudio-labs org does not let workflows create
#     pull requests), or the pull request's own link once one is open.
# A repo whose takedown branch was pushed for another issue that is still open is not
# taken down twice; once that issue is closed, a new request takes the branch over.
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
file_name="$(jq -r '.file' "$RESULT")"
branch="$(jq -r '.branch' "$RESULT")"
reason="$(jq -r '.reason' "$RESULT")"
# A file check_takedown.py found in add_ons/; refuse anything else.
[[ "$file_name" =~ ^[A-Za-z0-9._-]+\.json$ && "$branch" =~ ^takedown/[a-z0-9-]+-[a-z0-9._-]+$ ]] || {
    echo "unexpected file '$file_name' or branch '$branch'" >&2
    exit 1
}
file="add_ons/$file_name"
closes="Closes #$ISSUE"

# The branch's commit names the issue it was pushed for.
if git rev-parse --verify --quiet "origin/$branch" >/dev/null; then
    other="$(git log -1 --format=%B "origin/$branch" | grep -oE 'Closes #[0-9]+' | head -n 1 || true)"
    if [[ -n "$other" && "$other" != "$closes" ]] &&
       [[ "$(gh issue view "${other#Closes #}" --json state --jq .state)" == "OPEN" ]]; then
        gh issue comment "$ISSUE" --body "A takedown of $full_name was already requested in #${other#Closes #}."
        exit 0
    fi
fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git checkout -B "$branch" origin/main
git rm --quiet -- "$file"
git commit -m "Remove $full_name" -m "$closes"
git push --force origin "$branch"

pr_url="$(gh pr list --head "$branch" --state open --json url --jq '.[0].url // empty')"
if [[ -n "$pr_url" ]]; then
    next="Pull request: $pr_url (updated)"
else
    title="Take Down: $full_name"
    body="$(printf 'Removes %s from the LUNA Add On Marketplace.\n\n%s\n\n%s\n\n**Reason**\n\n%s\n' "$full_name" "$closes" "$comment" "$reason")"
    link="https://github.com/$GITHUB_REPOSITORY/compare/main...$branch?quick_pull=1&title=$(jq -rn --arg v "$title" '$v|@uri')&body=$(jq -rn --arg v "$body" '$v|@uri')"
    next="Branch \`$branch\` is ready for review. Maintainers: [create the pull request]($link), review it and merge it to remove the Add On."
fi
gh issue comment "$ISSUE" --body "$(printf '%s\n\n%s\nUniversal Audio reviews every takedown request before the Add On is removed from LUNA.\n' "$comment" "$next")"
