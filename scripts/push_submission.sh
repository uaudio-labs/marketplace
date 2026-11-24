#!/usr/bin/env bash
# Acts on check_add_on.py's result for a submission issue (submit-add-on.yml):
#   - failed: comments the problems on the issue;
#   - passed: (re)creates branch submit/<owner>-<repo> from main with
#     add_ons/<owner>.<repo>.json (with the checked release's tag; for a repo already
#     listed, its entry updated to that tag), in a commit whose
#     message closes the issue once it reaches main, and comments a link that opens the pull request, filled in,
#     for a maintainer to create (the uaudio-labs org does not let workflows create
#     pull requests), or the pull request's own link once one is open; and, through
#     $GITHUB_OUTPUT, gives the publish job the branch, the pull request's title and body,
#     and review=true when the content checks found something a maintainer must look at.
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
tag="$(jq -r '.tag' "$RESULT")"
listed_tag="$(jq -r '.listed_tag // empty' "$RESULT")"
updating="$(jq -r 'has("listed_tag")' "$RESULT")"
slug="$(jq -r '.slug' "$RESULT")"
review="$(jq -r '(.review // []) | length > 0' "$RESULT")"
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
if [[ "$updating" == "true" ]]; then
    subject="Update $full_name to $tag"
    title="Add On: $full_name $tag"
    summary="Updates $full_name in the LUNA Add On Marketplace from ${listed_tag:-its listed entry} to $tag."
else
    subject="Add $full_name $tag"
    title="Add On: $full_name"
    summary="Adds $full_name to the LUNA Add On Marketplace."
fi
git commit -m "$subject" -m "$closes"
git push --force origin "$branch"

body="$(printf '%s\n\n%s\n\n%s\n' "$summary" "$closes" "$comment")"
# For the publish job, which opens and merges the pull request as the org's GitHub App.
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    {
        echo "branch=$branch"
        echo "title=$title"
        echo "review=$review"
        delim="EOF_$(openssl rand -hex 16)"
        printf 'body<<%s\n%s\n%s\n' "$delim" "$body" "$delim"
    } >> "$GITHUB_OUTPUT"
fi

pr_url="$(gh pr list --head "$branch" --state open --json url --jq '.[0].url // empty')"
if [[ -n "$pr_url" ]]; then
    next="Pull request: $pr_url (updated)"
else
    link="https://github.com/$GITHUB_REPOSITORY/compare/main...$branch?quick_pull=1&title=$(jq -rn --arg v "$title" '$v|@uri')&body=$(jq -rn --arg v "$body" '$v|@uri')"
    listing="the Add On"
    if [[ "$updating" == "true" ]]; then
        listing="$tag"
    fi
    next="Branch \`$branch\` is ready for review. Maintainers: [create the pull request]($link), review it and merge it to list $listing."
fi
gh issue comment "$ISSUE" --body "$(printf '%s\n\n%s\nUniversal Audio reviews every submission before it appears in LUNA.\n' "$comment" "$next")"
