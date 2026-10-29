#!/usr/bin/env python3
"""Checks that a GitHub repo can be listed in the LUNA Add On Marketplace.

The repo must be public, and its latest release must have at least one .lunaaddon
attached. Each .lunaaddon must be a zip holding a manifest.json that LUNA reads as
at least one Add On, with the files the manifest names ("html", "icon") inside it.
The rules follow LUNA's (Rack_AddOn::FromVar() and Rack_AddOnMarketplace in
hbsystem): the manifest is the shallowest manifest.json outside __MACOSX, and each
Add On needs "name" (or the older "title"), "id", exactly one of "html" and "url",
and, when it opens in a window (the default "target"), "width" and "height".

Nothing in the release is run: each .lunaaddon is downloaded and its zip directory
and manifest are read.

Usage:
    check_add_on.py https://github.com/<owner>/<repo>
    check_add_on.py --event "$GITHUB_EVENT_PATH" --registry add_ons --json result.json

With --event, the repo is read from the "GitHub repository" field of the issue
form (.github/ISSUE_TEMPLATE/submit-add-on.yml) in the issue event. With
--registry, a repo already listed there is refused. With --json, the result is
written for the workflow; the exit status is then 0 whether or not the repo
passed, and 1 only when the check itself could not run.

GITHUB_TOKEN, when set, is sent to api.github.com only (never with a download).
"""

import argparse
import json
import os
import posixpath
import re
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

API = "https://api.github.com"
FORM_FIELD = "GitHub repository"     # the label of the form's "repo" input
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024     # as LUNA's kMaxManifestBytes
USER_AGENT = "uaudio-labs-marketplace-check"

# GitHub's own limits on owner and repo names; "." and ".." are refused separately.
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def repo_from_string(text):
    """(owner, repo) a pasted URL names, as LUNA's RepoURLFromString() reads it: the
    scheme and "www." are optional, and a trailing "/" or ".git", a query or fragment,
    and a path into the repo are dropped. None for anything else."""
    text = (text or "").strip()
    text = re.split(r"[?#]", text, maxsplit=1)[0]
    text = re.sub(r"^https?://", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^www\.", "", text, flags=re.IGNORECASE)
    parts = text.split("/")
    if len(parts) < 3 or parts[0].lower() != "github.com":
        return None
    owner, repo = parts[1], parts[2]
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    if not OWNER_RE.match(owner) or not REPO_RE.match(repo) or repo in (".", ".."):
        return None
    return owner, repo


def slug(owner, repo):
    """The registry's name for a repo: add_ons/<slug>.json, branch submit/<owner>-<repo>."""
    return f"{owner}.{repo}".lower()


def repo_from_issue(event_path):
    """The text of the form's repo field in an issues event, or None."""
    with open(event_path, encoding="utf-8") as f:
        event = json.load(f)
    body = (event.get("issue") or {}).get("body") or ""
    # A form issue's body is "### <label>\n\n<value>\n\n### <next label>...".
    match = re.search(r"^###\s+" + re.escape(FORM_FIELD) + r"\s*$(.*?)(?=^###\s|\Z)", body, re.MULTILINE | re.DOTALL)
    if not match:
        return None
    value = match.group(1).strip()
    return None if value in ("", "_No response_") else value


def listed_repos(registry):
    """The lower-case owner.repo of every repo the registry folder lists."""
    listed = set()
    if not registry or not os.path.isdir(registry):
        return listed
    for name in os.listdir(registry):
        if not name.endswith(".json"):
            continue
        listed.add(name[:-5].lower())
        try:
            with open(os.path.join(registry, name), encoding="utf-8") as f:
                named = repo_from_string(json.load(f).get("github", ""))
            if named:
                listed.add(slug(*named))
        except (OSError, ValueError, AttributeError):
            pass    # build_manifest.py reports a bad entry
    return listed


def api_get(path):
    request = urllib.request.Request(API + path, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    })
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        # Not forwarded if GitHub redirects elsewhere.
        request.add_unredirected_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, None


def download(url, path):
    """Downloads a public release asset, without any token. False when it is larger
    than MAX_DOWNLOAD_BYTES."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    total = 0
    with urllib.request.urlopen(request, timeout=120) as response, open(path, "wb") as out:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                return True
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                return False
            out.write(chunk)


def find_manifest(names):
    """The shallowest manifest.json outside __MACOSX, as LUNA's installer finds it."""
    candidates = [n for n in names
                  if posixpath.basename(n) == "manifest.json" and not n.startswith("__MACOSX/") and "/__MACOSX/" not in n]
    if not candidates:
        return None
    return min(candidates, key=lambda n: (n.count("/"), n))


def zip_path(manifest_path, relative):
    """Where a file the manifest names is in the zip, or None for an absolute path or
    one that leaves the zip (as LUNA's ZipPathFromManifest())."""
    relative = re.split(r"[?#]", relative, maxsplit=1)[0]
    if not relative or relative.startswith("/") or re.match(r"^[A-Za-z]:", relative):
        return None
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(manifest_path), relative))
    if joined == ".." or joined.startswith("../"):
        return None
    return joined


def check_entry(entry, defaults, manifest_path, names, where, problems):
    """One Add On of a manifest, as Rack_AddOn::AddEntry() reads it. Its summary, or None."""
    if not isinstance(entry, dict):
        problems.append(f"{where} is not an object.")
        return None
    name_field = "name" if "name" in entry else "title"
    missing = [f for f in (name_field, "id") if f not in entry]
    target = entry.get("target", "window")
    if target not in ("browser", "app", "page", "editor"):
        target = "window"
    if target == "window":
        missing += [f for f in ("width", "height") if f not in entry]
    if missing:
        problems.append(f"{where} is missing {', '.join(repr(f) for f in missing)}.")
        return None
    if ("html" in entry) == ("url" in entry):
        problems.append(f"{where} needs exactly one of \"html\" and \"url\".")
        return None
    for field in ("html", "icon"):
        value = entry.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            problems.append(f"{where}: \"{field}\" is not a string.")
            continue
        path = zip_path(manifest_path, value)
        if path is None:
            problems.append(f"{where}: \"{field}\" ({value}) must be a path inside the Add On.")
        elif path not in names:
            problems.append(f"{where}: \"{field}\" names {value}, which is not in the .lunaaddon.")
    return {
        "id": str(entry["id"]),
        "name": str(entry[name_field]),
        "version": str(entry.get("version", "")),
        "manufacturer": str(entry.get("manufacturer", defaults.get("manufacturer", ""))),
        "target": target,
    }


def check_file(path, file_name, problems):
    """One .lunaaddon. Its Add Ons' summaries."""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = {info.filename: info for info in archive.infolist()}
            manifest_path = find_manifest(infos)
            if manifest_path is None:
                problems.append(f"{file_name} has no manifest.json.")
                return []
            if infos[manifest_path].file_size > MAX_MANIFEST_BYTES:
                problems.append(f"{file_name}: its manifest.json is larger than 1 MB.")
                return []
            text = archive.read(manifest_path).decode("utf-8-sig")
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, UnicodeDecodeError) as error:
        problems.append(f"{file_name} could not be read as a zip with a UTF-8 manifest.json ({error}).")
        return []
    try:
        manifest = json.loads(text)
    except ValueError as error:
        problems.append(f"{file_name}: its manifest.json is not valid JSON ({error}).")
        return []
    if not isinstance(manifest, dict):
        problems.append(f"{file_name}: its manifest.json is not a JSON object.")
        return []

    names = set(infos)
    add_ons = []
    if "add_ons" in manifest:
        entries = manifest["add_ons"]
        if not isinstance(entries, list) or not entries:
            problems.append(f"{file_name}: \"add_ons\" in its manifest.json is not a list of Add Ons.")
            return []
        defaults = {"manufacturer": manifest.get("manufacturer", "")} if isinstance(manifest.get("manufacturer"), str) else {}
        for index, entry in enumerate(entries):
            summary = check_entry(entry, defaults, manifest_path, names, f"{file_name}: Add On {index + 1}", problems)
            if summary:
                add_ons.append(summary)
    else:
        summary = check_entry(manifest, {}, manifest_path, names, f"{file_name}: the manifest's Add On", problems)
        if summary:
            add_ons.append(summary)
    ids = [a["id"].lower() for a in add_ons]
    if len(set(ids)) != len(ids):
        problems.append(f"{file_name}: two Add Ons have the same \"id\".")
    return add_ons


def check(text, registry=None):
    """The result for a submitted repo URL: ok, problems, and what the workflow needs."""
    result = {"ok": False, "input": text or "", "problems": [], "files": []}
    problems = result["problems"]
    named = repo_from_string(text)
    if not named:
        problems.append("This is not the address of a GitHub repository. Enter it as https://github.com/owner/repository.")
        return result

    status, info = api_get(f"/repos/{named[0]}/{named[1]}")
    if status != 200 or not info:
        problems.append(f"github.com/{named[0]}/{named[1]} was not found. Check the address and that the repository is public.")
        return result
    if info.get("private"):
        problems.append("The repository is private. The marketplace only lists public repositories.")
        return result
    owner, repo = info["full_name"].split("/", 1)   # GitHub's own spelling
    result.update({
        "full_name": info["full_name"],
        "url": f"https://github.com/{owner}/{repo}",
        "slug": slug(owner, repo),
        "branch": f"submit/{owner}-{repo}".lower(),
        "entry": {"github": f"https://github.com/{owner}/{repo}"},
    })
    if result["slug"] in listed_repos(registry):
        problems.append(f"{info['full_name']} is already in the marketplace. A new release is listed without submitting it again.")
        return result
    if info.get("archived"):
        problems.append("The repository is archived.")

    status, release = api_get(f"/repos/{owner}/{repo}/releases/latest")
    if status != 200 or not release:
        problems.append("The repository has no published release. Publish a release (not a draft or pre-release) with a .lunaaddon attached.")
        return result
    result["tag"] = release.get("tag_name", "")
    assets = [a for a in release.get("assets", []) if a.get("name", "").lower().endswith(".lunaaddon")]
    if not assets:
        problems.append(f"The latest release ({result['tag']}) has no .lunaaddon file attached.")
        return result

    with tempfile.TemporaryDirectory() as folder:
        for index, asset in enumerate(assets):
            file_name = asset["name"]
            if asset.get("size", 0) > MAX_DOWNLOAD_BYTES:
                problems.append(f"{file_name} is larger than {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB.")
                continue
            path = os.path.join(folder, f"{index}.zip")
            try:
                if not download(asset["browser_download_url"], path):
                    problems.append(f"{file_name} is larger than {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB.")
                    continue
            except (urllib.error.URLError, OSError) as error:
                problems.append(f"{file_name} could not be downloaded ({error}).")
                continue
            add_ons = check_file(path, file_name, problems)
            result["files"].append({"name": file_name, "add_ons": add_ons})

    result["ok"] = not problems
    return result


def markdown(result):
    """The result as an issue comment, without the pull request's link."""
    lines = []
    if result["ok"]:
        lines.append(f"✅ **{result['full_name']}** ({result.get('tag', '')}) passed the marketplace checks.")
    else:
        what = result.get("full_name") or result["input"] or "The submission"
        lines.append(f"❌ **{what}** cannot be added to the marketplace yet:")
        lines.append("")
        lines += [f"- {p}" for p in result["problems"]]
        lines.append("")
        lines.append("Fix the repository or its latest release, then edit this issue (any change) to check it again.")
    for file in result["files"]:
        if not file["add_ons"]:
            continue
        lines.append("")
        lines.append(f"`{file['name']}`:")
        lines.append("")
        lines.append("| Name | ID | Version | Manufacturer |")
        lines.append("| --- | --- | --- | --- |")
        for a in file["add_ons"]:
            cells = [a["name"], f"`{a['id']}`", a["version"], a["manufacturer"]]
            lines.append("| " + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in cells) + " |")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("repo", nargs="?", help="https://github.com/<owner>/<repo>")
    parser.add_argument("--event", help="an issues event (GITHUB_EVENT_PATH) to read the repo from")
    parser.add_argument("--registry", help="the add_ons folder, to refuse a repo already listed")
    parser.add_argument("--json", help="write the result here")
    args = parser.parse_args()
    if bool(args.repo) == bool(args.event):
        parser.error("give a repo URL or --event")

    text = repo_from_issue(args.event) if args.event else args.repo
    result = check(text, args.registry)
    result["comment"] = markdown(result)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        return 0
    print(result["comment"])
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
