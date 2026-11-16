#!/usr/bin/env python3
"""Checks a request to remove a repo from the LUNA Add On Marketplace.

The repo must be listed in the registry folder (add_ons/). Nothing is downloaded:
a repo may be taken down after it was deleted or made private, so it is matched by
the address given and, when GitHub still knows the repo (it may have been renamed),
by GitHub's own name for it.

Usage:
    check_takedown.py https://github.com/<owner>/<repo> [--registry add_ons]
    check_takedown.py --event "$GITHUB_EVENT_PATH" --registry add_ons --json result.json

With --event, the repo, reason and optional contact e-mail are read from the
"GitHub repository", "Reason" and "Contact e-mail" fields of the issue form
(.github/ISSUE_TEMPLATE/submit-take-down.yml) in the issue event. With --json, the
result is written for the workflow; the exit status is then 0 whether or not the
request passed, and 1 only when the check itself could not run.

GITHUB_TOKEN, when set, is sent to api.github.com only.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from check_add_on import (CONTACT_FIELD, FORM_FIELD, api_get, field_from_issue,   # noqa: E402
                          is_email, repo_from_string, slug)

REASON_FIELD = "Reason"   # the label of the form's reason textarea


def listed_entries(registry):
    """{slug: (file name, entry)} for every entry in the registry folder."""
    entries = {}
    if not registry or not os.path.isdir(registry):
        return entries
    for name in sorted(os.listdir(registry)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(registry, name), encoding="utf-8") as f:
                entry = json.load(f)
        except (OSError, ValueError):
            continue    # build_manifest.py reports a bad entry
        named = repo_from_string(entry.get("github", "")) if isinstance(entry, dict) else None
        entries[name[:-5].lower()] = (name, entry)
        if named:
            entries.setdefault(slug(*named), (name, entry))
    return entries


def check(text, registry, reason=None, reason_required=False, contact=None, requester=None):
    """The result for a takedown request: ok, problems, and what the workflow needs."""
    result = {"ok": False, "input": text or "", "problems": []}
    problems = result["problems"]
    if reason is None and reason_required:
        problems.append("Enter a reason for the takedown.")
    if contact is not None and not is_email(contact):
        problems.append("The contact e-mail is not an e-mail address. Enter it as name@example.com, or leave it empty.")
    named = repo_from_string(text)
    if not named:
        problems.append("This is not the address of a GitHub repository. Enter it as https://github.com/owner/repository.")
        return result

    entries = listed_entries(registry)
    found = entries.get(slug(*named))
    if not found:
        status, info = api_get(f"/repos/{named[0]}/{named[1]}")
        if status == 200 and info:
            found = entries.get(slug(*info["full_name"].split("/", 1)))
    if not found:
        problems.append(f"github.com/{named[0]}/{named[1]} is not in the marketplace.")
        return result

    file_name, entry = found
    listed = repo_from_string(entry.get("github", "")) or named
    owner, repo = listed
    result.update({
        "full_name": f"{owner}/{repo}",
        "url": f"https://github.com/{owner}/{repo}",
        "file": file_name,
        "branch": f"takedown/{owner}-{repo}".lower(),
        "reason": reason or "",
        "by_owner": bool(requester) and requester.lower() == owner.lower(),
    })
    result["ok"] = not problems
    return result


def markdown(result):
    """The result as an issue comment, without the pull request's link."""
    if result["ok"]:
        lines = [f"✅ **{result['full_name']}** is in the marketplace (`add_ons/{result['file']}`)."]
        if result["by_owner"]:
            lines.append("")
            lines.append("This request was opened by the repository's owner.")
        return "\n".join(lines)
    what = result.get("full_name") or result["input"] or "The request"
    lines = [f"❌ **{what}** cannot be taken down:", ""]
    lines += [f"- {p}" for p in result["problems"]]
    lines += ["", "Fix the request, then edit this issue (any change) to check it again."]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("repo", nargs="?", help="https://github.com/<owner>/<repo>")
    parser.add_argument("--event", help="an issues event (GITHUB_EVENT_PATH) to read the request from")
    parser.add_argument("--registry", default="add_ons", help="the add_ons folder")
    parser.add_argument("--json", help="write the result here")
    args = parser.parse_args()
    if bool(args.repo) == bool(args.event):
        parser.error("give a repo URL or --event")

    if args.event:
        with open(args.event, encoding="utf-8") as f:
            requester = ((json.load(f).get("issue") or {}).get("user") or {}).get("login")
        result = check(field_from_issue(args.event, FORM_FIELD), args.registry,
                       reason=field_from_issue(args.event, REASON_FIELD), reason_required=True,
                       contact=field_from_issue(args.event, CONTACT_FIELD), requester=requester)
    else:
        result = check(args.repo, args.registry)
    result["comment"] = markdown(result)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        return 0
    print(result["comment"])
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
