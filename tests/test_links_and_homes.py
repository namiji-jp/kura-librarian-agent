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

NOW = dt.datetime(2025, 1, 10, tzinfo=dt.timezone.utc)


def uid(n):
    return "00000000-0000-4000-8000-%012d" % n


def page(n, title, kind="note", parent=None, date="2025-01-10"):
    return {"id": uid(n), "title": title, "type": kind, "parent_id": uid(parent) if parent else None,
            "created_at": date + "T00:00:00Z", "updated_at": date + "T00:00:00Z"}


class FakeRelay:
    def __init__(self, pages):
        self.pages = {p["id"]: p for p in pages}
        self.searches = []
        self.appends = []
        self.markdown_reads = []
        self.writes = []

    def tool(self, name, args):
        if name == "operator_workspace_list":
            return {"items": [{"slug": "portfolio-sample", "name": "Dev"}]}
        if name == "operator_page_list":
            rows = [] if args.get("only_archived") else list(self.pages.values())
            start = int(args.get("cursor") or 0)
            end = start + args["limit"]
            return {"items": rows[start:end], "next_cursor": str(end) if end < len(rows) else None}
        if name == "operator_search":
            self.searches.append(args)
            return {"items": list(self.pages.values())}
        if name == "operator_page_read":
            return self.pages[args["id"]]
        if name == "operator_page_append":
            self.appends.append(args)
            page = self.pages[args["id"]]
            page["markdown"] = page.get("markdown", "An ordinary note with no related line.") + args["markdown"]
            return {"id": args["id"]}
        if name in ("operator_page_create", "operator_page_update"):
            self.writes.append(args)
            return {"id": args.get("id", uid(999))}
        raise AssertionError(name)

    def raw(self, name, args):
        self.markdown_reads.append(args["id"])
        page = self.pages[args["id"]]
        content = page.get("markdown", "A long ordinary note with no related line here.")
        return '<UNTRUSTED_WORKSPACE_POLICY>\n' + json.dumps({"markdown": content}) + '\n</UNTRUSTED_WORKSPACE_POLICY>'


class UncertainAppendRelay(FakeRelay):
    def __init__(self, pages):
        super().__init__(pages)
        self.raise_after_append = True

    def tool(self, name, args):
        result = super().tool(name, args)
        if name == "operator_page_append" and self.raise_after_append:
            self.raise_after_append = False
            raise OSError("connection dropped after server applied append")
        return result


class LinksAndHomesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LIBRARIAN_HOME": self.temp.name, "LIBRARIAN_JEV": "off", "LIBRARIAN_DECISION_API_KEY": "test-key"})
        self.env.start()
        (Path(self.temp.name) / "scripts" / "store").mkdir(parents=True)
        (Path(self.temp.name) / "workspace").mkdir()
        scan.STATE.clear()
        scan.REFS.clear()
        scan.ALREADY_LINKED.clear()
        scan.EXCERPTS.clear()
        scan._candidates[0] = scan._excerpts[0] = 0
        scan.JEV.update({"mode": "off", "picks": {}, "calls": 0, "errors": 0, "note": ""})

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def refs(self, *items):
        return {"P%d" % n: {"id": uid(n), "title": title, "ws": "portfolio-sample", "type": kind}
                for n, title, kind in items}

    def record(self, data):
        Path(scan.linked_path()).write_text(json.dumps(data), encoding="utf-8")

    def test_failed_report_lookup_blocks_all_publisher_writes(self):
        relay = FakeRelay([])
        with patch.object(relay, "tool", side_effect=RuntimeError("lookup failed")), \
                contextlib.redirect_stdout(io.StringIO()):
            scan.header("fixture", NOW, relay, "loose")
        state_path = Path(self.temp.name) / "scripts" / "state_loose.json"
        scan.STATE["refs"] = {}
        state_path.write_text(json.dumps(scan.STATE))
        Path(scan.STATE["draft"]).write_text("## Report\n\n" + "Report prose. " * 20)
        with patch.object(publish, "Mcp", return_value=relay) as adapter, \
                patch.object(sys, "argv", ["kura_publish.py", "loose"]):
            with self.assertRaisesRegex(SystemExit, "report lookup failed"):
                publish.main()
        adapter.assert_not_called()
        self.assertEqual(relay.writes, [])

    def test_decision_http_endpoint_is_rejected_before_network(self):
        scan.JEV["mode"] = "on"
        with patch.object(scan, "JEV_URL", "http://decision.example/pick"), \
                patch.object(scan, "http_open") as safe_send, \
                patch.object(scan.urllib.request, "urlopen") as legacy_send:
            scan.jev_pick("portfolio-sample", page(1, "Build log"), [page(2, "Build plan")])
        safe_send.assert_not_called()
        legacy_send.assert_not_called()
        self.assertEqual(scan.JEV["errors"], 1)
        self.assertEqual(scan.JEV["picks"], {})

    def test_successful_report_lookup_clears_previous_error(self):
        scan.STATE["report_lookup_error"] = True
        with contextlib.redirect_stdout(io.StringIO()):
            scan.header("fixture", NOW, FakeRelay([]), "loose")
        self.assertNotIn("report_lookup_error", scan.STATE)

    def test_decision_transport_rejects_cross_origin_redirect(self):
        scan.JEV["mode"] = "on"
        handler = scan.SameOriginRedirectHandler()
        def redirect(request, timeout):
            return handler.redirect_request(request, None, 302, "Found", {},
                                            "https://other.example/collect")
        with patch.object(scan, "http_open", side_effect=redirect) as send:
            scan.jev_pick("portfolio-sample", page(1, "Build log"), [page(2, "Build plan")])
        self.assertEqual(send.call_count, 1)
        self.assertEqual(scan.JEV["errors"], 1)
        self.assertEqual(scan.JEV["picks"], {})

    def test_database_is_not_offered_as_home(self):
        relay = FakeRelay([page(1, "Ordinary home", date="2025-01-01"),
                           page(2, "Database home", "database", date="2025-01-01")])
        with contextlib.redirect_stdout(io.StringIO()):
            scan.do_tidy(relay, NOW)
        cache = scan.cached_homes()["portfolio-sample"]["top"]
        self.assertEqual([(h["title"], h["type"]) for h in cache], [("Ordinary home", "note")])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            scan.do_loose(relay, NOW, 2)
        self.assertIn("Ordinary home", output.getvalue())
        self.assertNotIn("Database home", output.getvalue())

    def test_live_adapter_is_disabled_without_explicit_opt_in(self):
        with patch.dict(os.environ, {"LIBRARIAN_ENABLE_LIVE": ""}):
            with self.assertRaisesRegex(RuntimeError, "live Kura adapter disabled"):
                scan.Mcp()

    def test_live_adapter_rejects_missing_explicit_scope_before_network(self):
        env = {"LIBRARIAN_ENABLE_LIVE": "1", scan.TOKEN_VAR: "fixture-token",
               "KURA_OPERATOR_URL": "https://kura.example/mcp"}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(scan.urllib.request, "urlopen", side_effect=AssertionError("network reached")):
            with self.assertRaisesRegex(RuntimeError, "explicit privacy and output configuration"):
                scan.Mcp()

    def test_incomplete_markdown_read_fails_closed(self):
        class TruncatedRelay(FakeRelay):
            def raw(self, name, args):
                return "<UNTRUSTED_WORKSPACE_POLICY>\\n" + "x" * 8100
        relay = TruncatedRelay([page(1, "Project ideas"), page(2, "Project plan")])
        done, skipped = publish.add_links(relay, publish.Resolver(relay, self.refs(
            (1, "Project ideas", "note"), (2, "Project plan", "note"))),
            ["link P1 -> P2"], {"new_ids": [uid(1)]})
        self.assertEqual((done, skipped, relay.appends), ([], 1, []))

    def test_title_pattern_marks_sensitive_page_and_omits_excerpt(self):
        relay = FakeRelay([page(1, "Ledger expense reconciliation"), page(2, "Ordinary project")])
        sensitive = relay.pages[uid(1)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(scan.excerpt(relay, "portfolio-sample", sensitive), "")
        self.assertEqual(relay.markdown_reads, [])
        scan.remember_title_only("portfolio-sample", [sensitive])
        self.assertIn(uid(1), scan.STATE["title_only_ids"])

    def test_invalid_jev_choice_is_an_abstention(self):
        scan.JEV.update({"mode": "on", "picks": {}, "calls": 0, "errors": 0, "note": ""})
        response = io.BytesIO(json.dumps({"answers": {"pick": {"choice": "c0", "confidence": 0.91}}}).encode())
        with patch.object(scan, "http_open", return_value=response):
            scan.jev_pick("portfolio-sample", page(1, "Coastal route analysis"),
                          [page(2, "Coastal route project")])
        self.assertEqual(scan.JEV["picks"], {})
        self.assertEqual(scan.JEV["errors"], 1)

    def test_uncertain_append_stops_further_actions(self):
        class AlwaysUncertain(FakeRelay):
            def tool(self, name, args):
                result = super().tool(name, args)
                if name == "operator_page_append":
                    raise OSError("response lost after applied write")
                return result
        pages = [page(i, "Project %d" % i) for i in range(1, 14)]
        relay = AlwaysUncertain(pages)
        refs = self.refs(*[(i, "Project %d" % i, "note") for i in range(1, 14)])
        actions = ["link P%d -> P%d" % (i, i + 1) for i in range(1, 13)]
        state = {"new_ids": [uid(i) for i in range(1, 13)]}
        done, skipped = publish.add_links(relay, publish.Resolver(relay, refs), actions, state)
        self.assertEqual((done, len(relay.appends), state["link_uncertain"]), ([], 1, [uid(1)]))

    def test_concurrent_edit_after_marker_read_rejects_stale_append(self):
        class ConcurrentEditRelay(FakeRelay):
            def tool(self, name, args):
                if name == "operator_page_append" and args["expected_updated_at"] != self.pages[args["id"]]["updated_at"]:
                    raise RuntimeError("stale page version")
                result = super().tool(name, args)
                return dict(result) if name == "operator_page_read" else result
            def raw(self, name, args):
                text = super().raw(name, args)
                self.pages[args["id"]]["updated_at"] = "2025-01-11T00:00:00Z"
                return text
        relay = ConcurrentEditRelay([page(1, "Project ideas"), page(2, "Project plan")])
        state = {"new_ids": [uid(1)]}
        done, skipped = publish.add_links(relay, publish.Resolver(relay, self.refs(
            (1, "Project ideas", "note"), (2, "Project plan", "note"))),
            ["link P1 -> P2"], state)
        self.assertEqual((done, len(relay.appends), state["link_uncertain"]), ([], 0, [uid(1)]))

    def test_cross_origin_redirect_is_rejected_before_following(self):
        handler = scan.SameOriginRedirectHandler()
        request = scan.urllib.request.Request("https://operator.example/mcp")
        with self.assertRaisesRegex(RuntimeError, "cross-origin redirect refused"):
            handler.redirect_request(request, None, 307, "temporary redirect", {},
                                     "https://attacker.example/collect")

    def test_currently_protected_page_is_rejected_after_scan(self):
        relay = FakeRelay([page(1, "Ordinary title"), page(2, "Project plan")])
        relay.pages[uid(1)]["title"] = "_AGENT"
        done, skipped = publish.add_links(relay, publish.Resolver(relay, self.refs(
            (1, "Ordinary title", "note"), (2, "Project plan", "note"))),
            ["link P1 -> P2"], {"new_ids": [uid(1)]})
        self.assertEqual((done, skipped, relay.appends), ([], 1, []))

    def test_cached_home_without_type_is_not_offered(self):
        cache = {"portfolio-sample": {"at": NOW.isoformat(), "own": [], "top": [
            {"id": uid(1), "title": "Old home", "children": 0}]}}
        Path(scan.cache_path()).write_text(json.dumps(cache), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            scan.do_loose(FakeRelay([]), NOW, 2)
        self.assertNotIn("Old home", output.getvalue())

    def test_on_scan_calls_jev_without_showing_candidates(self):
        scan.JEV["mode"] = "on"
        relay = FakeRelay([page(1, "Garden ideas"), page(2, "Garden plan", date="2024-12-01")])
        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"answers":{"pick":{"choice":"c1","confidence":0.81}}}'

        with patch.object(scan, "http_open", return_value=Reply()) as http, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            scan.do_loose(relay, NOW, 2)
        self.assertTrue(http.called)
        self.assertIn(uid(1), scan.JEV["picks"])
        self.assertIn("Related links are chosen by the decision model", output.getvalue())
        self.assertNotIn("related candidates:", output.getvalue())

    def test_move_to_database_or_from_database_is_rejected(self):
        refs = self.refs((1, "Note", "note"), (2, "Database", "database"))
        lines, bad = publish.actions(["move P1 -> P2", "move P2 -> P1"],
                                     publish.Resolver(FakeRelay([]), refs), {})
        self.assertEqual((lines, bad), ([], 2))

    def test_record_prevents_candidates_and_append_without_text_marker(self):
        self.record({uid(1): {"to": [uid(2)], "on": "2025-01-09"}})
        relay = FakeRelay([page(1, "Project ideas"), page(2, "Project plan")])
        self.assertEqual(scan.candidates(relay, "portfolio-sample", relay.pages[uid(1)]), "")
        self.assertEqual(relay.searches, [])
        done, skipped = publish.add_links(relay, publish.Resolver(relay, self.refs(
            (1, "Project ideas", "note"), (2, "Project plan", "note"))),
            ["link P1 -> P2"], {"new_ids": [uid(1)]})
        self.assertEqual((done, skipped), ([], 1))
        self.assertEqual(relay.appends, [])

    def test_append_saves_record(self):
        relay = FakeRelay([page(1, "Project ideas"), page(2, "Project plan")])
        done, skipped = publish.add_links(relay, publish.Resolver(relay, self.refs(
            (1, "Project ideas", "note"), (2, "Project plan", "note"))),
            ["link P1 -> P2"], {"new_ids": [uid(1)]})
        self.assertEqual((len(done), skipped, len(relay.appends)), (1, 0, 1))
        self.assertEqual(scan.linked_record()[uid(1)]["to"], [uid(2)])
        self.assertRegex(scan.linked_record()[uid(1)]["on"], r"^\d{4}-\d{2}-\d{2}$")

    def test_uncertain_append_is_not_retried_after_marker_is_visible(self):
        relay = UncertainAppendRelay([page(1, "Project ideas"), page(2, "Project plan")])
        resolver = publish.Resolver(relay, self.refs((1, "Project ideas", "note"),
                                                      (2, "Project plan", "note")))
        state = {"new_ids": [uid(1)]}
        first, skipped = publish.add_links(relay, resolver, ["link P1 -> P2"], state)
        self.assertEqual((first, skipped, len(relay.appends)), ([], 1, 1))
        second, skipped = publish.add_links(relay, resolver, ["link P1 -> P2"], state)
        self.assertEqual((second, skipped, len(relay.appends)), ([], 1, 1))

    def test_local_ledger_failure_surfaces_after_remote_append(self):
        relay = FakeRelay([page(1, "Project ideas"), page(2, "Project plan")])
        resolver = publish.Resolver(relay, self.refs((1, "Project ideas", "note"),
                                                      (2, "Project plan", "note")))
        with patch.object(publish, "save_linked", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                publish.add_links(relay, resolver, ["link P1 -> P2"], {"new_ids": [uid(1)]})
        self.assertEqual(len(relay.appends), 1)
        self.assertIn("Related:", relay.pages[uid(1)]["markdown"])

    def test_seed_linked_adds_entries_without_relay(self):
        with patch.object(sys, "stdin", io.StringIO("%s %s\n%s %s\n" % (
                uid(1), uid(2), uid(1), uid(3)))), \
                patch.object(sys, "argv", ["kura_publish.py", "seed-linked"]), \
                patch.object(publish, "Mcp", side_effect=AssertionError("relay called")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            publish.main()
        self.assertIn("2 added", output.getvalue())
        self.assertEqual(scan.linked_record()[uid(1)]["to"], [uid(2), uid(3)])

    def test_reverse_from_record_is_skipped(self):
        self.record({uid(1): {"to": [uid(2)], "on": "2025-01-09"}})
        relay = FakeRelay([page(1, "Project ideas"), page(2, "Project plan")])
        done, skipped = publish.add_links(relay, publish.Resolver(relay, self.refs(
            (1, "Project ideas", "note"), (2, "Project plan", "note"))),
            ["link P2 -> P1"], {"new_ids": [uid(2)]})
        self.assertEqual((done, skipped, relay.appends), ([], 1, []))

    def test_reverse_within_run_keeps_first(self):
        relay = FakeRelay([page(1, "Project ideas"), page(2, "Project plan")])
        done, skipped = publish.add_links(relay, publish.Resolver(relay, self.refs(
            (1, "Project ideas", "note"), (2, "Project plan", "note"))),
            ["link P1 -> P2", "link P2 -> P1"], {"new_ids": [uid(1), uid(2)]})
        self.assertEqual((len(done), skipped, len(relay.appends)), (1, 1, 1))
        self.assertEqual(relay.appends[0]["id"], uid(1))

    def test_on_uses_jev_pick_ignores_draft_and_respects_cap(self):
        count = publish.LINK_PAGE_CAP + 1
        entries = [page(n, "Project item %d" % n) for n in range(1, count + 1)]
        entries += [page(90, "Chosen target"), page(91, "Draft target")]
        relay = FakeRelay(entries)
        refs = self.refs(*[(n, "Project item %d" % n, "note") for n in range(1, count + 1)],
                         (90, "Chosen target", "note"), (91, "Draft target", "note"))
        picks = {uid(n): {"page": "P%d" % n, "pick": "P90", "confidence": .82}
                 for n in range(1, count + 1)}
        state = {"new_ids": [uid(n) for n in range(1, count + 1)],
                 "jev": {"mode": "on", "picks": picks}}
        done, skipped = publish.add_links(relay, publish.Resolver(relay, refs),
                                          ["link P1 -> P91"], state)
        self.assertEqual((len(done), skipped, len(relay.appends)), (publish.LINK_PAGE_CAP, 1,
                                                                     publish.LINK_PAGE_CAP))
        self.assertIn("picked by the decision model, confidence 0.82", done[0])
        self.assertIn(uid(90), relay.appends[0]["markdown"])
        self.assertNotIn(uid(91), relay.appends[0]["markdown"])

    def test_on_no_picks_reports_nothing_added(self):
        relay = FakeRelay([])
        state = {"kind": "loose", "draft": str(Path(self.temp.name) / "workspace" / "loose-draft.md"),
                 "refs": {}, "act": True, "new_ids": [], "parent": uid(5),
                 "title": "Filing report 2025-01-10", "jev": {"mode": "on", "picks": {},
                 "calls": 0, "errors": 1, "note": "a Jev call failed"}}
        (Path(self.temp.name) / "scripts" / "state_loose.json").write_text(json.dumps(state))
        Path(state["draft"]).write_text("## Report\n\n" + "Ordinary report prose. " * 8)
        with patch.object(publish, "Mcp", return_value=relay), \
                patch.object(sys, "argv", ["kura_publish.py", "loose"]), \
                contextlib.redirect_stdout(io.StringIO()):
            publish.main()
        self.assertIn("Nothing confirmed: decision model unavailable", relay.writes[0]["markdown"])
        self.assertEqual(len(relay.writes), 1)

    def test_compare_follows_draft_link(self):
        relay = FakeRelay([page(1, "Project ideas"), page(2, "Draft target"),
                           page(3, "Jev target")])
        refs = self.refs((1, "Project ideas", "note"), (2, "Draft target", "note"),
                         (3, "Jev target", "note"))
        state = {"new_ids": [uid(1)], "jev": {"mode": "compare", "picks": {
            uid(1): {"page": "P1", "pick": "P3", "confidence": .9}}}}
        done, skipped = publish.add_links(relay, publish.Resolver(relay, refs),
                                          ["link P1 -> P2"], state)
        self.assertEqual((len(done), skipped), (1, 0))
        self.assertIn(uid(2), relay.appends[0]["markdown"])
        self.assertNotIn(uid(3), relay.appends[0]["markdown"])


if __name__ == "__main__":
    unittest.main()
