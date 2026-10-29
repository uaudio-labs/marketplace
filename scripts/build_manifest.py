#!/usr/bin/env python3
"""Builds manifest.json, the list LUNA reads, from the entries in add_ons/.

Each add_ons/<owner>.<repo>.json (lower case) holds one entry:
    { "github": "https://github.com/<owner>/<repo>" }
with an optional "private": true for a repo only developers signed in to the GitHub
CLI can read, and an optional "contact": the submitter's e-mail, for maintainers.
The manifest lists them in file name order, without "contact", which LUNA doesn't use:
    { "add_ons": [ <entry>, ... ] }

Usage:
    build_manifest.py [add_ons] [manifest.json]
    build_manifest.py --check [add_ons]     # only checks the entries

Exits 1, writing nothing, when an entry is not valid JSON, has fields other than
"github", "private" and "contact", names no GitHub repo, or is not named after its repo.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from check_add_on import is_email, repo_from_string, slug   # noqa: E402

FIELDS = {"github", "private", "contact"}
LISTED_FIELDS = ("github", "private")   # what goes in manifest.json


def read_entries(folder):
    entries, problems = [], []
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(folder, name)
        try:
            with open(path, encoding="utf-8") as f:
                entry = json.load(f)
        except (OSError, ValueError) as error:
            problems.append(f"{path}: not valid JSON ({error})")
            continue
        if not isinstance(entry, dict):
            problems.append(f"{path}: not a JSON object")
            continue
        extra = set(entry) - FIELDS
        if extra:
            problems.append(f"{path}: unknown field(s) {', '.join(sorted(extra))}")
        named = repo_from_string(entry.get("github", "")) if isinstance(entry.get("github"), str) else None
        if not named:
            problems.append(f"{path}: \"github\" is not https://github.com/<owner>/<repo>")
            continue
        if entry["github"] != f"https://github.com/{named[0]}/{named[1]}":
            problems.append(f"{path}: write \"github\" as https://github.com/{named[0]}/{named[1]}")
        if name[:-5] != slug(*named):
            problems.append(f"{path}: name it {slug(*named)}.json")
        if "private" in entry and not isinstance(entry["private"], bool):
            problems.append(f"{path}: \"private\" is not true or false")
        if "contact" in entry and not is_email(entry["contact"]):
            problems.append(f"{path}: \"contact\" is not an e-mail address")
        entries.append({k: entry[k] for k in LISTED_FIELDS if k in entry})
    return entries, problems


def main():
    args = sys.argv[1:]
    check_only = "--check" in args
    args = [a for a in args if a != "--check"]
    folder = args[0] if args else "add_ons"
    output = args[1] if len(args) > 1 else "manifest.json"

    entries, problems = read_entries(folder)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    if check_only:
        print(f"{len(entries)} entries OK")
        return 0
    with open(output, "w", encoding="utf-8") as f:
        json.dump({"add_ons": entries}, f, indent=4)
        f.write("\n")
    print(f"wrote {output} with {len(entries)} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
