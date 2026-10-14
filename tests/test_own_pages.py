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

NOW = dt.datetime(2025, 1, 9, tzinfo=dt.timezone.utc)


def uid(n):
    return "00000000-0000-4000-8000-%012d" % n


def page(n, title, parent=None, date="2025-01-09"):
    return {"id": uid(n), "title": title, "parent_id": uid(parent) if parent else None,
            "type": "note", "created_at": date + "T00:00:00Z",
            "updated_at": date + "T00:00:00Z"}


class FakeMcp:
    def __init__(self, items):
        self.items = items
        self.reads = []
        self.appends = []

    def tool(self, name, args):
        if name == "operator_workspace_list":
            return {"items": [{"slug": "portfolio-sample", "name": "Portfolio sample"}]}
        if name == "operator_page_list":
            rows = [] if args.get("only_archived") else self.items
            start = int(args.get("cursor") or 0)
            end = start + args["limit"]
            return {"items": rows[start:end],
                    "next_cursor": str(end) if end < len(rows) else None}
        if name == "operator_search":
            return {"items": self.items}
        if name == "operator_page_read":
            return {**next(p for p in self.items if p["id"] == args["id"]),
                    "updated_at": "2025-01-09T00:00:00Z"}
        if name == "operator_page_append":
            self.appends.append(args)
            return {"id": args["id"]}
        raise AssertionError(name)

    def raw(self, name, args):
        self.reads.append(args["id"])
        return '<UNTRUSTED_WORKSPACE_POLICY>\n{"markdown":"An ordinary example page with enough words for an excerpt."}'


class OwnPagesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LIBRARIAN_HOME": self.temp.name,
                                         "LIBRARIAN_JEV": "off", "LIBRARIAN_DECISION_API_KEY": "test-key"})
        self.env.start()
        (Path(self.temp.name) / "scripts").mkdir()
        scan.STATE.clear()
        scan.REFS.clear()
        scan.ALREADY_LINKED.clear()
        scan.EXCERPTS.clear()
        scan._candidates[0] = 0
        scan._excerpts[0] = 0

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_own_ids_only_top_level_hub_and_descendants(self):
        items = [page(1, "Librarian"), page(2, "Catalogue", 1),
                 page(3, "Catalogue: project", 2), page(4, "Projects"),
                 page(5, "Librarian", 4), page(6, "Catalogue", 4)]
        self.assertEqual(scan.own_ids(items), {uid(1), uid(2), uid(3)})
        self.assertEqual(scan.own_ids([page(2, "Catalogue", 1)]), set())
        self.assertEqual(scan.own_ids([page(2, "Catalogue", 1)], [uid(1)]), {uid(2)})

    def test_loose_omits_own_pages_homes_and_candidates(self):
        items = [page(1, "Librarian"), page(2, "Catalogue", 1),
                 page(3, "Project home", date="2024-01-01"),
                 page(4, "Project ideas", 3)]
        mcp = FakeMcp(items)
        cache = {"portfolio-sample": {"at": NOW.isoformat(), "own": [uid(1), uid(2)], "top": [
            {"id": uid(1), "title": "Librarian", "children": 1},
            {"id": uid(3), "title": "Project home", "children": 1}]}}
        Path(scan.cache_path()).write_text(json.dumps(cache), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            scan.do_loose(mcp, NOW, 7)
        report = output.getvalue()
        self.assertIn("Pages created in the window (1)", report)
        self.assertIn("Project ideas", report)
        self.assertIn("Project home", report)
        self.assertNotIn("Librarian", report)
        self.assertNotIn("Catalogue", report)
        self.assertNotIn(uid(1), report)
        self.assertNotIn(uid(2), report)
        self.assertNotIn(uid(1), mcp.reads)
        self.assertNotIn(uid(2), mcp.reads)
        self.assertEqual(scan.STATE["new_ids"], [uid(4)])

    def test_cached_hub_filters_child_when_hub_is_outside_window(self):
        mcp = FakeMcp([page(2, "Catalogue", 1), page(4, "Project ideas")])
        cache = {"portfolio-sample": {"at": NOW.isoformat(), "own": [uid(1)], "top": []}}
        Path(scan.cache_path()).write_text(json.dumps(cache), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            scan.do_loose(mcp, NOW, 7)
        self.assertIn("Pages created in the window (1)", output.getvalue())
        self.assertIn(uid(2), scan.STATE["own_ids"])

    def test_changes_does_not_count_cached_own_child(self):
        mcp = FakeMcp([page(2, "Catalogue", 1), page(4, "Project ideas")])
        cache = {"portfolio-sample": {"at": NOW.isoformat(), "own": [uid(1), uid(2)], "top": []}}
        Path(scan.cache_path()).write_text(json.dumps(cache), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            scan.do_changes(mcp, NOW, 24)
        self.assertIn("1 new, 0 edited, 0 archived", output.getvalue())
        self.assertNotIn("Catalogue", output.getvalue())
        self.assertIn(uid(2), scan.STATE["own_ids"])

    def test_tidy_caches_own_ids_and_skips_own_link_check(self):
        mcp = FakeMcp([page(1, "Librarian", date="2024-01-01"),
                       page(2, "Catalogue", 1, date="2024-01-01"),
                       page(3, "Ordinary", date="2024-01-01")])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            scan.do_tidy(mcp, NOW)
        cache = json.loads(Path(scan.cache_path()).read_text(encoding="utf-8"))
        self.assertEqual(cache["portfolio-sample"]["own"], [uid(1), uid(2)])
        self.assertIn("Not edited for 120+ days (1 pages", output.getvalue())
        self.assertEqual(mcp.reads, [uid(3)])

    def test_actions_reject_own_page_or_target_and_keep_ordinary_move(self):
        refs = {"P1": {"id": uid(1), "title": "Own", "ws": "portfolio-sample", "type": "note"},
                "P2": {"id": uid(2), "title": "Own child", "ws": "portfolio-sample", "type": "note"},
                "P3": {"id": uid(3), "title": "Ordinary", "ws": "portfolio-sample", "type": "note"},
                "P4": {"id": uid(4), "title": "Home", "ws": "portfolio-sample", "type": "note"}}
        lines = ["move P1 -> P4 :: own source", "move P3 -> P2 :: own target",
                 "move P3 -> P4 :: ordinary"]
        actions, rejected = publish.actions(lines, publish.Resolver(FakeMcp([]), refs),
                                            {"own_ids": [uid(1), uid(2)]})
        self.assertEqual(rejected, 2)
        self.assertEqual(len(actions), 1)
        self.assertEqual(json.loads(actions[0])["page_id"], uid(3))

    def test_link_to_own_page_is_skipped(self):
        refs = {"P1": {"id": uid(1), "title": "Ordinary", "ws": "portfolio-sample"},
                "P2": {"id": uid(2), "title": "Catalogue", "ws": "portfolio-sample"}}
        mcp = FakeMcp([page(1, "Ordinary"), page(2, "Catalogue")])
        done, skipped = publish.add_links(mcp, publish.Resolver(mcp, refs),
                                          ["link P1 -> P2 :: related"],
                                          {"new_ids": [uid(1)], "own_ids": [uid(2)]})
        self.assertEqual((done, skipped), ([], 1))
        self.assertEqual(mcp.reads, [])
        self.assertEqual(mcp.appends, [])


if __name__ == "__main__":
    unittest.main()
