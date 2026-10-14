#!/usr/bin/env python3
"""Run the extracted scan -> fixture decision -> report/publish path in memory.

Fixture choices are fixed test inputs. This does not emulate an LLM and calls no live provider.
"""
import contextlib
import datetime as dt
import io
import json
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kura_publish as publish  # noqa: E402
import kura_scan as scan  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "decisions.json"
NOW = dt.datetime(2026, 10, 10, 2, tzinfo=dt.timezone.utc)


def uid(number):
    return "00000000-0000-4000-8000-%012d" % number


def make_page(number, title, markdown, created):
    stamp = created.isoformat().replace("+00:00", "Z")
    return {"id": uid(number), "title": title, "type": "note", "parent_id": None,
            "created_at": stamp, "updated_at": stamp, "markdown": markdown}


class OfflineRelay:
    """In-memory stand-in for the Kura MCP tools used by this run."""
    def __init__(self, pages):
        self.pages = {page["id"]: page for page in pages}
        self.calls, self.appends, self.reports = [], [], []

    def tool(self, name, args):
        self.calls.append((name, args))
        if name == "operator_workspace_list":
            return {"items": [{"slug": "portfolio-sample", "name": "Portfolio sample"}]}
        if name == "operator_page_list":
            rows = list(self.pages.values())
            start = int(args.get("cursor") or 0)
            end = start + args["limit"]
            return {"items": rows[start:end], "next_cursor": str(end) if end < len(rows) else None}
        if name == "operator_search":
            words = set(args["q"].lower().split())
            rows = [page for page in self.pages.values()
                    if page["title"] != args["q"] and words.intersection(page["title"].lower().split())]
            return {"items": rows[:args.get("limit", 8)]}
        if name == "operator_page_read":
            return self.pages[args["id"]]
        if name == "operator_page_append":
            page = self.pages[args["id"]]
            page["markdown"] += args["markdown"]
            self.appends.append(args)
            return {"id": args["id"]}
        if name == "operator_page_create":
            self.reports.append(args)
            return {"id": uid(999), "title": args["title"]}
        if name == "operator_page_update":
            self.reports.append(args)
            return {"id": args["id"], "title": "Filing report"}
        raise AssertionError("unexpected provider tool: " + name)

    def raw(self, name, args):
        self.calls.append((name, args))
        page = self.pages[args["id"]]
        return "<UNTRUSTED_WORKSPACE_POLICY>\n%s\n</UNTRUSTED_WORKSPACE_POLICY>" % json.dumps(
            {"markdown": page["markdown"]}, separators=(",", ":"))


def fixture_pages():
    old, new = NOW - dt.timedelta(days=30), NOW - dt.timedelta(hours=1)
    return [
        make_page(1, "Coastal route analysis", "A short note about coastal route analysis.", new),
        make_page(2, "Rail disruption checklist", "A short note about rail disruption planning.", new),
        make_page(10, "Coastal route project", "Existing project material.", old),
        make_page(11, "Rail disruption project", "Existing transport project material.", old),
    ]


def reset_scan_state():
    scan.STATE.clear()
    scan.REFS.clear()
    scan.ALREADY_LINKED.clear()
    scan.EXCERPTS.clear()
    scan.INCOMPLETE.clear()
    scan._candidates[0] = scan._excerpts[0] = 0
    scan.JEV.update({"mode": "off", "picks": {}, "calls": 0, "errors": 0,
                     "cost": 0.0, "note": ""})


def run():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="librarian-offline-") as tmp:
        home = Path(tmp)
        (home / "scripts" / "store").mkdir(parents=True)
        (home / "workspace").mkdir()
        with patch.dict(os.environ, {"LIBRARIAN_HOME": tmp, "LIBRARIAN_JEV": "on",
                                     "LIBRARIAN_DECISION_API_KEY": "offline-fixture-key"}, clear=False):
            reset_scan_state()
            scan.JEV["mode"] = "on"
            relay = OfflineRelay(fixture_pages())
            fixture_by_title = {item["new_page_title"]: item for item in data["decisions"]}
            def fixture_provider(request, timeout=20):
                payload = json.loads(request.data.decode("utf-8"))
                title = payload["state"]["new_page_title"]
                decision = fixture_by_title[title]
                choice = "none" if decision["candidate_title"] is None else "c1"
                response = {"answers": {"pick": {"choice": choice,
                                                   "confidence": decision["confidence"]}},
                            "usage": {"cost": 0}}
                class Reply:
                    def __enter__(self): return self
                    def __exit__(self, *args): return False
                    def read(self): return json.dumps(response).encode("utf-8")
                return Reply()
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                with patch.object(scan, "http_open", side_effect=fixture_provider):
                    scan.do_loose(relay, NOW, 2)
            scan_text = captured.getvalue()
            refs_by_title = {value["title"]: key for key, value in scan.REFS.items()}
            decisions = scan.JEV["picks"]
            # Deliberately disagree with Jev: the publisher must follow the validated pick.
            page_ref = refs_by_title["Coastal route analysis"]
            opposing_ref = refs_by_title["Rail disruption project"]
            action_lines = ["link %s -> %s :: opposing draft input" % (page_ref, opposing_ref)]
            scan.STATE.update({"refs": scan.REFS, "act": True, "existing": None,
                               "parent": uid(99), "title": "Filing report fixture",
                               "draft": str(home / "workspace" / "loose-draft.md")})
            scan.STATE["jev"] = {"mode": "on", "picks": decisions, "calls": scan.JEV["calls"],
                                 "errors": scan.JEV["errors"], "note": "synthetic fixture transport"}
            prose = (
                "## Offline demonstration\n\n"
                "All content and page names in this run are synthetic. The scan found two new pages. "
                "This deliberately conflicting draft proposes linking {{%s}} to {{%s}}. The validated fixture choice must take precedence; the fixture abstains on {{%s}}.\n\n"
                "## ACTIONS\n%s\n" % (action_lines[0].split()[1], action_lines[0].split()[3],
                                         refs_by_title["Rail disruption checklist"],
                                         "\n".join(action_lines)))
            scan.STATE["draft"] = str(home / "workspace" / "loose-draft.md")
            Path(scan.STATE["draft"]).write_text(prose, encoding="utf-8")
            (home / "scripts" / "state_loose.json").write_text(json.dumps(scan.STATE), encoding="utf-8")
            with patch.object(publish, "Mcp", return_value=relay), \
                    patch.object(sys, "argv", ["kura_publish.py", "loose"]), \
                    contextlib.redirect_stdout(io.StringIO()) as published:
                publish.main()
            outcome = {
                "scan_excerpt": scan_text.replace(tmp, "<temporary offline directory>"),
                "fake_publish_result": published.getvalue().strip(),
                "fixture_notice": data["fixture_notice"],
                "decision_inputs": len(decisions),
                "abstentions": sum(1 for item in decisions.values() if item["pick"] is None),
                "fake_kura_append_calls": len(relay.appends),
                "fake_kura_report_writes": len(relay.reports),
                "fixture_decision_calls": scan.JEV["calls"],
                "external_provider_calls": 0,
                "chosen_link_target": relay.appends[0]["markdown"] if relay.appends else None,
            }
            print(json.dumps(outcome, indent=2, ensure_ascii=False))
            if (outcome["fake_kura_append_calls"] != 1 or outcome["fake_kura_report_writes"] != 1
                    or outcome["fixture_decision_calls"] != 2
                    or outcome["external_provider_calls"] != 0
                    or "Coastal route project" not in (outcome["chosen_link_target"] or "")):
                raise SystemExit("offline workflow invariant failed")


if __name__ == "__main__":
    run()
