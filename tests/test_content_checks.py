"""Tests for check_add_on.py's content checks, on .lunaaddon files built here (no network).

Run from the repo root: python3 -I -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import check_add_on as c   # noqa: E402

MANIFEST = {"name": "Tidy Tracks", "id": "com.example.tidy", "html": "index.html", "width": 400, "height": 300}


def lunaaddon(files, manifest=MANIFEST):
    """A .lunaaddon holding manifest and files ({name: text or bytes}); its path."""
    folder = tempfile.mkdtemp()
    path = os.path.join(folder, "test.lunaaddon")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("index.html", "<p>Tidy Tracks</p>")
        for name, data in files.items():
            archive.writestr(name, data)
    return path


def scan(files, manifest=MANIFEST, ua_owned=False):
    problems, findings = [], {}
    c.check_file(lunaaddon(files, manifest), "test.lunaaddon", problems, findings, ua_owned)
    return problems, findings


class Profanity(unittest.TestCase):
    def test_a_clean_add_on_needs_no_review(self):
        problems, findings = scan({"main.js": "class Mixer { assert(x) { return 'Scunthorpe cocktail'; } }"})
        self.assertEqual(problems, [])
        self.assertEqual(c.content_review(findings), [])

    def test_a_word_in_a_file_is_found_with_its_line_and_masked(self):
        _, findings = scan({"main.js": "const a = 1;\n// this is shit\n"})
        self.assertEqual(findings["words"], ["test.lunaaddon/main.js:2 (s***)"])
        [reason] = c.content_review(findings)
        self.assertNotIn("shit", reason)

    def test_endings_case_and_separators(self):
        for text in ("FUCKING", "fuck_this", "bitches"):
            findings = {}
            c.scan_text(text, "x", findings)
            self.assertIn("words", findings, text)

    def test_words_inside_other_words_and_base64_are_ignored(self):
        findings = {}
        c.scan_text("cockpit cocked raccoon spices Dick " + "QUJDRGshitRUZH" * 30, "x", findings)
        self.assertEqual(findings, {})

    def test_an_add_on_name_is_checked(self):
        _, findings = scan({}, {**MANIFEST, "name": "Bullshit Detector"})
        self.assertIn("words", findings)


class Copyright(unittest.TestCase):
    def test_media_files_need_review(self):
        _, findings = scan({"sounds/hit.WAV": b"RIFF", "song.mid": b"MThd", "logo.png": b"\x89PNG"})
        self.assertEqual(findings["media_file"], ["test.lunaaddon/sounds/hit.WAV", "test.lunaaddon/song.mid"])

    def test_embedded_media_needs_review(self):
        _, findings = scan({"main.js": "new Audio('data:audio/wav;base64,UklGR')"})
        self.assertIn("media_uri", findings)

    def test_base64_media_without_a_data_uri_needs_review(self):
        _, findings = scan({"main.js": "const s = 'UklGRiQAAABXQVZF" + "A" * 300 + "';"})
        self.assertIn("media_uri", findings)

    def test_renamed_media_files_are_found_by_their_contents(self):
        _, findings = scan({"assets/blob.bin": b"RIFF\x24\x00\x00\x00WAVEfmt ", "data": b"ID3\x04rest",
                            "clip.dat": b"\x00\x00\x00\x18ftypmp42"})
        self.assertEqual(sorted(findings["media_file"]),
                         ["test.lunaaddon/assets/blob.bin", "test.lunaaddon/clip.dat", "test.lunaaddon/data"])

    def test_a_damaged_member_is_held_not_crashed_on(self):
        path = lunaaddon({})
        with zipfile.ZipFile(path, "a", compression=zipfile.ZIP_LZMA) as archive:
            archive.writestr("broken.js", "".join(f"{i * 7919 % 10007:05d} " for i in range(4000)))
        with open(path, "r+b") as f:   # corrupt the LZMA stream of the last member
            data = f.read()
            at = data.index(b"broken.js") + len(b"broken.js") + 200   # into its data, after the local header
            f.seek(at)
            f.write(b"\xff" * 64)
        problems, findings = [], {}
        c.check_file(path, "test.lunaaddon", problems, findings)
        self.assertIn("test.lunaaddon/broken.js (unreadable)", findings["unscanned"])

    def test_an_add_on_that_loads_a_web_page_needs_review(self):
        manifest = {k: v for k, v in MANIFEST.items() if k != "html"}
        _, findings = scan({}, {**manifest, "url": "https://example.com"})
        self.assertIn("url", findings)

    def test_confidential_notices_need_review(self):
        for notice in ("CONFIDENTIAL", "Do not distribute", "For internal use only"):
            _, findings = scan({"lib.js": f"/* Acme Inc. {notice} */"})
            self.assertIn("notice", findings, notice)

    def test_an_apache_notice_is_fine(self):
        _, findings = scan({"NOTICE.txt": "LUNA Builder SDK\nCopyright 2026 Universal Audio, Inc.\nLicensed under the Apache License, Version 2.0"})
        self.assertEqual(findings, {})

    def test_other_companies_marks_in_a_name_need_review(self):
        for name in ("Pro Tools Keys", "ProTools Keys", "SSL Bus Comp", "My 1176", "LA2A Helper", "AutoTune Lite",
                     "Apollo Twin Remote"):
            findings = {}
            c.scan_text(name, "name", findings, names=True)
            self.assertIn("trademark", findings, name)

    def test_marks_are_only_checked_in_names_and_not_for_ua(self):
        findings = {}
        c.scan_text("works like Pro Tools", "notes", findings)
        c.scan_text("Apollo Twin Monitor", "name", findings, ua_owned=True, names=True)
        c.scan_text("Apollo GraphQL Mixer", "name", findings, names=True)
        self.assertEqual(findings, {})

    def test_large_files_are_reported_not_read(self):
        old = c.MAX_SCAN_BYTES
        c.MAX_SCAN_BYTES = 10
        try:
            _, findings = scan({"big.js": "x" * 11})
        finally:
            c.MAX_SCAN_BYTES = old
        self.assertIn("test.lunaaddon/big.js (too large)", findings["unscanned"])


class Rights(unittest.TestCase):
    def test_ticked(self):
        self.assertTrue(c.ticked("- [X] I made everything in this Add On"))
        self.assertTrue(c.ticked("- [x] I made everything in this Add On"))
        self.assertFalse(c.ticked("- [ ] I made everything in this Add On"))
        self.assertFalse(c.ticked(None))

    def test_an_unticked_box_fails_before_anything_is_fetched(self):
        result = c.check("not a url", rights_confirmed=False)
        self.assertIn("Rights", result["problems"][0])


class Comment(unittest.TestCase):
    def test_a_passing_submission_with_findings_says_it_will_be_reviewed(self):
        result = {"ok": True, "full_name": "a/b", "tag": "v1", "problems": [], "files": [],
                  "review": ["Audio, video, MIDI or sample files, which may be someone else's recordings: `x.wav`."]}
        text = c.markdown(result)
        self.assertIn("✅", text)
        self.assertIn("A maintainer will review it", text)

    def test_names_cannot_break_out_of_code_spans(self):
        [reason] = c.content_review({"media_file": ["a`@someone`.wav"]})
        self.assertNotIn("`@someone`", reason)


if __name__ == "__main__":
    unittest.main()
