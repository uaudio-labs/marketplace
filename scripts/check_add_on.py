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
and manifest are read. The entry records the checked release's tag as "tag".

The content checks then look for what a person should judge before the Add On is
listed: profanity and slurs (rules/blocked_words.txt) in the repo's name and
description, the release's name and notes, the Add Ons' names and the .lunaaddon's text
files; other companies' brands (rules/trademarks.txt) in the repo's and Add Ons'
names; audio, video, MIDI and sample files, bundled or embedded; and notices marking
content confidential or not for distribution. Their findings ("review") never fail a
submission; the workflow holds it for a maintainer instead of merging it. A form issue
must also tick the "Rights" box, confirming the builder may share everything in it.

Usage:
    check_add_on.py https://github.com/<owner>/<repo> [--contact <e-mail>]
    check_add_on.py --event "$GITHUB_EVENT_PATH" --registry add_ons --json result.json

With --event, the repo and the submitter's contact e-mail are read from the
"GitHub repository" and "Contact e-mail" fields of the issue form
(.github/ISSUE_TEMPLATE/submit-add-on.yml) in the issue event; the e-mail is
required then, and goes in the entry as "contact". An issue the UA Universe
Marketplace App opened for a builder (universe.uaudio.com's Publish) needs no
e-mail and puts none in the public entry: Universe already knows who they are. With
--registry, a repo already listed there is an update: it passes only when its latest
release's tag differs from the listed one, and its entry keeps the listed fields
("private", and "contact" unless a new e-mail is given). With --json, the result is
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
FORM_FIELD = "GitHub repository"     # the label of the form's add_on_repository input
CONTACT_FIELD = "Contact e-mail"     # the label of the form's contact_email input
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024     # as LUNA's kMaxManifestBytes
USER_AGENT = "uaudio-labs-marketplace-check"

# GitHub's own limits on owner and repo names; "." and ".." are refused separately.
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
# The private "UA Universe Marketplace" GitHub App (uaudio/universe-hub, backend/publish.py). GitHub sets an
# issue's performed_via_github_app itself, so nobody else can claim it.
UNIVERSE_APP_ID = 5545873
# A release tag as git allows it in a ref name, without whitespace or control characters.
TAG_RE = re.compile(r"^(?!.*\.\.)(?!.*@\{)(?![./-])[^\x00-\x20\x7f~^:?*\[\\]{1,255}(?<![./])(?<!\.lock)$")
# A plain address: no spaces, quotes, brackets or markdown, and a dotted domain.
EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$")

# The content checks. What they find never fails a submission: it holds the submission
# for a maintainer's review instead of letting it merge automatically, because a word
# list or a file type is a hint, not proof. The rules are plain text in rules/.
RIGHTS_FIELD = "Rights"              # the label of the form's rights checkboxes
RULES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules")
UA_OWNERS = {"uaudio-labs", "uaudio"}   # Universal Audio's accounts, which may use its marks
TEXT_EXTENSIONS = {".html", ".htm", ".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx", ".vue", ".css", ".json",
                   ".map", ".webmanifest", ".md", ".txt", ".svg", ".xml", ""}
MEDIA_EXTENSIONS = {
    ".wav", ".wave", ".aif", ".aiff", ".aifc", ".caf", ".mp3", ".flac", ".ogg", ".oga", ".opus",
    ".m4a", ".aac", ".wma",                                   # audio
    ".mp4", ".m4v", ".mov", ".webm", ".avi", ".mkv",          # video
    ".mid", ".midi",                                          # MIDI
    ".sf2", ".sfz", ".nki", ".nkx", ".exs", ".rex", ".rx2",   # sample instruments
}
MAX_SCAN_BYTES = 8 * 1024 * 1024     # per text file
MAX_SCAN_TOTAL = 64 * 1024 * 1024    # per .lunaaddon
MAX_PLACES = 5                       # places listed per finding in the comment
NOTICE_RE = re.compile(r"(?<![A-Za-z])(?:confidential|do not (?:re)?distribute|not for (?:re)?distribution|"
                       r"internal use only|trade secret|unauthori[sz]ed (?:copying|reproduction|distribution))(?![A-Za-z])",
                       re.IGNORECASE)
MEDIA_URI_RE = re.compile(r"data:(?:audio|video)/", re.IGNORECASE)
# How media files start, so a renamed one is found too: WAV and AIFF, MP3, FLAC, Ogg,
# MIDI, and MP4/MOV/M4A.
MEDIA_MAGIC_RE = re.compile(rb"^(?:RIFF....WAVE|FORM....AIF[FC]|ID3|fLaC|OggS|MThd|....ftyp)", re.DOTALL)
# The same, base64-encoded at the start of an embedded run.
MEDIA_BASE64_RE = re.compile(r"^(?:UklGR|Rk9STQ|SUQz|ZkxhQw|T2dnUw|TVRoZA|AAAA[A-Za-z0-9+/]{2}ZnR5c)")
# Long base64 runs (embedded images and fonts) are blanked before the word checks, where
# they would spell out short words by chance.
BASE64_RUN_RE = re.compile(r"[A-Za-z0-9+/=]{200,}")


def rule_pattern(file_name, endings=""):
    """A case-insensitive whole-word pattern for the names in rules/<file_name>, where
    a space in a name also matches "-", "_", "." or nothing ("Pro Tools", "pro_tools")."""
    names = []
    with open(os.path.join(RULES, file_name), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                names.append(r"[\s_.-]*".join(re.escape(word) for word in re.split(r"[\s-]+", line)))
    names.sort(key=len, reverse=True)
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(names) + ")" + endings + r"(?![A-Za-z0-9])", re.IGNORECASE)


PROFANITY_RE = rule_pattern("blocked_words.txt", r"(?:s|es|ed|er|ers|ing|in)?")
TRADEMARK_RE = rule_pattern("trademarks.txt", r"s?")

REVIEW_REASONS = {   # finding kind: why it needs a person to look, in the order shown
    "words": "Possible profanity or slurs (the words are masked)",
    "url": "An Add On that loads its page from the web, whose content can't be checked",
    "trademark": "Another company's brand or product name, which can suggest an endorsement",
    "media_file": "Audio, video, MIDI or sample files, which may be someone else's recordings",
    "media_uri": "Audio or video embedded in a text file",
    "notice": "A notice marking content as confidential or not for distribution",
    "unscanned": "Files that could not be scanned for these checks",
}


def is_email(text):
    return isinstance(text, str) and len(text) <= 254 and bool(EMAIL_RE.match(text))


def is_tag(text):
    return isinstance(text, str) and bool(TAG_RE.match(text))


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


def ticked(value):
    """True when a form's checkboxes field (its text in the issue) has a ticked box."""
    return bool(re.search(r"^\s*[-*]\s*\[[xX]\]", value or "", re.MULTILINE))


def field_from_issue(event_path, label):
    """The text of the form field with this label in an issues event, or None."""
    with open(event_path, encoding="utf-8") as f:
        event = json.load(f)
    body = (event.get("issue") or {}).get("body") or ""
    # A form issue's body is "### <label>\n\n<value>\n\n### <next label>...".
    match = re.search(r"^###\s+" + re.escape(label) + r"\s*$(.*?)(?=^###\s|\Z)", body, re.MULTILINE | re.DOTALL)
    if not match:
        return None
    value = match.group(1).strip()
    return None if value in ("", "_No response_") else value


def opened_by_universe(event_path):
    """True when the UA Universe Marketplace App opened the issue for a builder."""
    with open(event_path, encoding="utf-8") as f:
        event = json.load(f)
    app = (event.get("issue") or {}).get("performed_via_github_app") or {}
    return app.get("id") == UNIVERSE_APP_ID


def repo_from_issue(event_path):
    """The text of the form's repo field in an issues event, or None."""
    return field_from_issue(event_path, FORM_FIELD)


def listed_entry(registry, repo_slug):
    """The registry's entry for a repo (by file name, or by the repo its "github"
    names), {} when it is listed by an entry that cannot be read, or None when it is
    not listed."""
    if not registry or not os.path.isdir(registry):
        return None
    found = None
    for name in sorted(os.listdir(registry)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(registry, name), encoding="utf-8") as f:
                entry = json.load(f)
            named = repo_from_string(entry.get("github", ""))
        except (OSError, ValueError, AttributeError):
            entry, named = {}, None    # build_manifest.py reports a bad entry
        if name[:-5].lower() == repo_slug or (named and slug(*named) == repo_slug):
            if isinstance(entry, dict) and entry:
                return entry
            found = {}
    return found


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


def mask(word):
    return word[0] + "*" * (len(word) - 1)


def scan_text(text, where, findings, ua_owned=False, names=False):
    """Records in findings (kind -> places) what in text needs a maintainer's review.
    where names the text; a file's places get line numbers. names: text is a name, so
    other companies' marks count too."""
    numbered = "\n" in text

    def add(kind, match, detail=""):
        places = findings.setdefault(kind, [])
        if len(places) <= MAX_PLACES * 4:   # enough to say "and more"
            place = f"{where}:{text.count(chr(10), 0, match.start()) + 1}" if numbered else where
            places.append(f"{place} ({detail})" if detail else place)

    for match in BASE64_RUN_RE.finditer(text):
        if MEDIA_BASE64_RE.match(match.group()):
            add("media_uri", match)
    text = BASE64_RUN_RE.sub(lambda m: " " * len(m.group()), text)

    for match in PROFANITY_RE.finditer(text):
        add("words", match, mask(match.group()))
    for match in NOTICE_RE.finditer(text):
        add("notice", match, match.group().lower())
    for match in MEDIA_URI_RE.finditer(text):
        add("media_uri", match)
    if names and not ua_owned:
        for match in TRADEMARK_RE.finditer(text):
            add("trademark", match, match.group())


def scan_archive(archive, file_name, findings, ua_owned=False):
    """Scans a .lunaaddon's text files and lists its media files, without running anything."""
    budget = MAX_SCAN_TOTAL
    for info in archive.infolist():
        name = info.filename
        if info.is_dir() or name.startswith("__MACOSX/") or "/__MACOSX/" in name:
            continue
        extension = posixpath.splitext(name)[1].lower()
        where = f"{file_name}/{name}"
        text = extension in TEXT_EXTENSIONS
        if text and (info.file_size > MAX_SCAN_BYTES or info.file_size > budget):
            findings.setdefault("unscanned", []).append(f"{where} (too large)")
            text = False
        try:
            with archive.open(info) as member:
                # Only a text file is read whole; the zip's own size for it may be wrong.
                data = member.read(MAX_SCAN_BYTES + 1 if text else 16)
        except Exception:   # any damage or unsupported compression: hold it, never crash
            findings.setdefault("unscanned", []).append(f"{where} (unreadable)")
            continue
        if extension in MEDIA_EXTENSIONS or MEDIA_MAGIC_RE.match(data[:16]):
            findings.setdefault("media_file", []).append(where)
        if not text:
            continue
        if len(data) > MAX_SCAN_BYTES:
            findings.setdefault("unscanned", []).append(f"{where} (too large)")
            continue
        budget -= len(data)
        scan_text(data.decode("utf-8", errors="replace"), where, findings, ua_owned)


def content_review(findings):
    """Why a submission needs a maintainer's review, one line per kind of finding."""
    reasons = []
    for kind, reason in REVIEW_REASONS.items():
        places = findings.get(kind)
        if not places:
            continue
        shown = ", ".join("`" + re.sub(r"[`\r\n]", "'", p) + "`" for p in places[:MAX_PLACES])
        more = "" if len(places) <= MAX_PLACES else " and more" if len(places) > MAX_PLACES * 4 else f" and {len(places) - MAX_PLACES} more"
        reasons.append(f"{reason}: {shown}{more}.")
    return reasons


def check_entry(entry, defaults, manifest_path, names, where, problems, findings=None):
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
    if "url" in entry and findings is not None:
        findings.setdefault("url", []).append(where)
    return {
        "id": str(entry["id"]),
        "name": str(entry[name_field]),
        "version": str(entry.get("version", "")),
        "manufacturer": str(entry.get("manufacturer", defaults.get("manufacturer", ""))),
        "target": target,
    }


def check_file(path, file_name, problems, findings=None, ua_owned=False):
    """One .lunaaddon. Its Add Ons' summaries. With findings, its content is scanned
    into them too (scan_archive())."""
    try:
        with zipfile.ZipFile(path) as archive:
            if findings is not None:
                scan_archive(archive, file_name, findings, ua_owned)
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
            summary = check_entry(entry, defaults, manifest_path, names, f"{file_name}: Add On {index + 1}", problems, findings)
            if summary:
                add_ons.append(summary)
    else:
        summary = check_entry(manifest, {}, manifest_path, names, f"{file_name}: the manifest's Add On", problems, findings)
        if summary:
            add_ons.append(summary)
    ids = [a["id"].lower() for a in add_ons]
    if len(set(ids)) != len(ids):
        problems.append(f"{file_name}: two Add Ons have the same \"id\".")
    return add_ons


def check(text, registry=None, contact=None, contact_required=False, rights_confirmed=True):
    """The result for a submitted repo URL: ok, problems, review (what a maintainer must
    look at before it is listed), and what the workflow needs."""
    result = {"ok": False, "input": text or "", "problems": [], "review": [], "files": []}
    problems = result["problems"]
    findings = {}
    if not rights_confirmed:
        problems.append("Tick the box under \"Rights\" to confirm you made everything in the Add On or have permission to share it. "
                        "(If this issue has no Rights box, edit it and add a line \"### Rights\" followed by "
                        "\"- [x] I made everything in this Add On, or have permission to share it\".)")
    if contact is None and contact_required:
        problems.append("Enter a contact e-mail.")
    elif contact is not None and not is_email(contact):
        problems.append("The contact e-mail is not an e-mail address. Enter it as name@example.com.")
    named = repo_from_string(text)
    if not named:
        problems.append("This is not the address of a GitHub repository. Enter it as https://github.com/owner/repository.")
        return result

    status, info = api_get(f"/repos/{named[0]}/{named[1]}")
    if status != 200 or not info:
        problems.append(f"github.com/{named[0]}/{named[1]} was not found. Check the address and that the repository is public.")
        return result
    if info.get("private") or info.get("visibility", "public") != "public":   # private or internal
        problems.append("The repository is not public. The marketplace only lists public repositories.")
        return result
    owner, repo = info["full_name"].split("/", 1)   # GitHub's own spelling
    ua_owned = owner.lower() in UA_OWNERS
    scan_text(repo, "the repository's name", findings, ua_owned, names=True)
    scan_text(info.get("description") or "", "the repository's description", findings, ua_owned)
    result.update({
        "full_name": info["full_name"],
        "url": f"https://github.com/{owner}/{repo}",
        "slug": slug(owner, repo),
        "branch": f"submit/{owner}-{repo}".lower(),
        "entry": {"github": f"https://github.com/{owner}/{repo}"},
    })
    listed = listed_entry(registry, result["slug"])
    if listed is not None:
        result["listed_tag"] = listed.get("tag") if isinstance(listed.get("tag"), str) else ""
        if listed.get("private") is True:
            result["entry"]["private"] = True
        if isinstance(listed.get("contact"), str):
            result["entry"]["contact"] = listed["contact"]
    if is_email(contact):
        result["entry"]["contact"] = contact
    if info.get("archived"):
        problems.append("The repository is archived.")

    status, release = api_get(f"/repos/{owner}/{repo}/releases/latest")
    if status != 200 or not release:
        problems.append("The repository has no published release. Publish a release (not a draft or pre-release) with a .lunaaddon attached.")
        return result
    result["tag"] = release.get("tag_name", "")
    scan_text(release.get("name") or "", "the release's name", findings, ua_owned)
    scan_text(release.get("body") or "", "the release's notes", findings, ua_owned)
    if not is_tag(result["tag"]):
        problems.append(f"The latest release's tag ({result['tag']!r}) is not a tag name the marketplace can list.")
        return result
    if "listed_tag" in result and result["listed_tag"] == result["tag"]:
        problems.append(f"{info['full_name']} is already in the marketplace with its latest release ({result['tag']}). "
                        "Publish a new release, then submit again.")
        return result
    # The release checked here is the one the entry records; submit again for a new one.
    entry = result["entry"]
    result["entry"] = {"github": entry.pop("github"), "tag": result["tag"], **entry}
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
            add_ons = check_file(path, file_name, problems, findings, ua_owned)
            for add_on in add_ons:
                scan_text(add_on["name"], f"{file_name}: an Add On's name", findings, ua_owned, names=True)
            result["files"].append({"name": file_name, "add_ons": add_ons})

    result["review"] = content_review(findings)
    result["ok"] = not problems
    return result


def markdown(result):
    """The result as an issue comment, without the pull request's link."""
    lines = []
    if result["ok"]:
        update = f", updating {result['listed_tag'] or 'its listed entry'}" if "listed_tag" in result else ""
        lines.append(f"✅ **{result['full_name']}** ({result.get('tag', '')}{update}) passed the marketplace checks.")
    else:
        what = result.get("full_name") or result["input"] or "The submission"
        lines.append(f"❌ **{what}** cannot be added to the marketplace yet:")
        lines.append("")
        lines += [f"- {p}" for p in result["problems"]]
        lines.append("")
        lines.append("Fix the repository or its latest release, then edit this issue (any change) to check it again.")
    if result.get("review"):
        lines.append("")
        lines.append("⚠️ A maintainer will review it before it is listed, because the content checks found:"
                     if result["ok"] else "The content checks also found these, which a maintainer will review:")
        lines.append("")
        lines += [f"- {r}" for r in result["review"]]
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
    parser.add_argument("--contact", help="the submitter's e-mail, for the entry (read from the issue with --event)")
    parser.add_argument("--registry", help="the add_ons folder, to treat a repo already listed as an update")
    parser.add_argument("--json", help="write the result here")
    args = parser.parse_args()
    if bool(args.repo) == bool(args.event):
        parser.error("give a repo URL or --event")

    if args.event and args.contact:
        parser.error("--contact is read from the issue with --event")
    text = repo_from_issue(args.event) if args.event else args.repo
    universe = bool(args.event) and opened_by_universe(args.event)
    contact = None if universe else field_from_issue(args.event, CONTACT_FIELD) if args.event else args.contact
    # Universe has its builders accept its terms; a form issue asks for the Rights box.
    rights = universe or not args.event or ticked(field_from_issue(args.event, RIGHTS_FIELD))
    result = check(text, args.registry, contact, contact_required=bool(args.event) and not universe,
                   rights_confirmed=rights)
    result["comment"] = markdown(result)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        return 0
    print(result["comment"])
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
