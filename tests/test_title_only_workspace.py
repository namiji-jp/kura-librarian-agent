import contextlib
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import kura_publish as publish
import kura_scan as scan

P1 = "00000000-0000-4000-8000-000000000001"
P2 = "00000000-0000-4000-8000-000000000002"
MARKER = "PRIVATE-SAMPLE-MARKER"


class FakeMcp:
    def __init__(self):
        stamp = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).isoformat()
        self.items = {
            "title-only-private": [{"id": P1, "title": "personal sample page", "parent_id": None,
                            "type": "note", "created_at": stamp, "updated_at": stamp}],
            "portfolio-sample": [{"id": P2, "title": "Ordinary home", "parent_id": None,
                     "type": "note", "created_at": stamp, "updated_at": stamp}]}
        self.markdown_reads = []
        self.searches = []

    def tool(self, name, args):
        if name == "operator_workspace_list":
            return {"items": [{"slug": slug, "name": slug.title()} for slug in self.items]}
        if name == "operator_page_list":
            rows = [] if args.get("only_archived") else self.items[args["workspace"]]
            start = int(args.get("cursor") or 0)
            end = start + args["limit"]
            return {"items": rows[start:end],
                    "next_cursor": str(end) if end < len(rows) else None}
        if name == "operator_search":
            self.searches.append(args)
            return {"items": []}
        if name == "operator_page_read":
            return next(p for p in self.items[args["workspace"]] if p["id"] == args["id"])
        raise AssertionError(name)

    def raw(self, name, args):
        self.markdown_reads.append(args)
        return '<UNTRUSTED_WORKSPACE_POLICY>\n{"markdown":"' + MARKER + '"}\n</UNTRUSTED_WORKSPACE_POLICY>'


class TitleOnlyWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LIBRARIAN_HOME": self.temp.name,
                                         "LIBRARIAN_JEV": "off", "LIBRARIAN_DECISION_API_KEY": "test-key"})
        self.env.start()
        (Path(self.temp.name) / "scripts").mkdir()
        (Path(self.temp.name) / "workspace").mkdir()
        scan.JEV["mode"] = "off"

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_reports_never_read_or_expose_personal_sensitive_body(self):
        for mode in ("changes", "loose", "tidy"):
            with self.subTest(mode=mode):
                scan.STATE.clear()
                scan.REFS.clear()
                scan.ALREADY_LINKED.clear()
                scan.EXCERPTS.clear()
                scan._candidates[0] = scan._excerpts[0] = 0
                fake = FakeMcp()
                with patch.object(scan, "Mcp", return_value=fake), \
                        patch.object(sys, "argv", ["kura_scan.py", mode]), \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    scan.main()
                report = output.getvalue()
                state_text = (Path(self.temp.name) / "scripts" /
                              ("state_%s.json" % mode)).read_text(encoding="utf-8")
                state = json.loads(state_text)
                self.assertNotIn(MARKER, report)
                self.assertNotIn(MARKER, state_text)
                self.assertEqual(state["title_only_ids"], [P1])
                self.assertEqual([call for call in fake.markdown_reads
                                  if call["workspace"] == "title-only-private"], [])
                self.assertEqual([call for call in fake.searches
                                  if call["workspace"] == "title-only-private"], [])
                if mode == "changes":
                    self.assertIn("personal sample page", report)
                if mode == "loose":
                    self.assertIn("no filing proposals are made here", report)
                    self.assertNotIn("personal sample page", report)
                    self.assertNotIn(P1, state.get("new_ids", []))

    def test_publisher_rejects_moves_and_links_but_allows_flag(self):
        fake = FakeMcp()
        refs = {"P1": {"id": P1, "title": "personal sample page", "ws": "title-only-private"},
                "P2": {"id": P2, "title": "Ordinary home", "ws": "portfolio-sample"}}
        resolver = publish.Resolver(fake, refs)
        state = {"title_only_ids": [P1], "new_ids": [P1, P2]}
        actions, rejected = publish.actions(
            ["move P1 -> P2 :: file", "move P2 -> P1 :: file",
             "flag duplicate P1 P2 :: duplicate"], resolver, state)
        self.assertEqual(rejected, 2)
        self.assertEqual([json.loads(line)["kind"] for line in actions], ["flag"])
        self.assertEqual(json.loads(actions[0])["page_id"], P1)
        done, skipped = publish.add_links(
            fake, resolver, ["link P1 -> P2 :: related", "link P2 -> P1 :: related"], state)
        self.assertEqual((done, skipped), ([], 2))
        self.assertEqual(fake.markdown_reads, [])

        # The workspace label also protects a resolved reference absent from saved ids.
        no_ids = {"new_ids": [P1, P2]}
        self.assertEqual(publish.actions(["move P1 -> P2"], resolver, no_ids)[1], 1)
        self.assertEqual(publish.add_links(fake, resolver, ["link P2 -> P1"], no_ids), ([], 1))


if __name__ == "__main__":
    unittest.main()
