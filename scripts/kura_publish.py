#!/usr/bin/env python3
"""Publish a Librarian report to Kura from the draft the model wrote.

  kura_publish.py changes|loose|tidy

The model names pages by short reference ({{P7}}) and never types an id. This script turns
each reference into a real page link `[title](/notes/<id>)` from the map the scan saved, builds
the machine-readable `librarian-actions` block from the draft's `## ACTIONS` lines, and
creates or updates the routine's report. Filing can also append bounded Related lines; this
script does not provide exactly-once behavior across remote Kura and the local ledger.

Draft `## ACTIONS` lines (last section, one per line, removed from the published prose):
  move P12 -> P40 :: reason
  flag duplicate P3 P4 :: reason
  flag stale P9 :: reason
  flag broken-link P9 :: reason
"""
import json
import os
import re
import sys
import datetime as dt
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kura_scan import (EXCLUDED_WORKSPACES, Mcp, PROTECTED_IDS, TITLE_ONLY_WORKSPACES,  # noqa: E402
                       UUID, home, jev_key, linked_path, linked_record, protected_title)

FLAGS = {"duplicate", "stale", "broken-link"}
# {{P7}} or {{P7|short label}}: the label is the link text the reader sees.
TOKEN = r"\{\{\s*(P\d+|kura:%s)\s*(?:\|([^{}]{1,80}))?\}\}" % UUID
LABEL_CHARS = 60   # link text without a label is the page title cut to this length
ACTION_CAP = 60


def page_link(page_id, title):
    """A Markdown link Kura imports as a real page link (clickable, counted as a backlink).

    Kura exports links as [[kura:<id>|label]] but does not parse that form on import, where it
    stays literal text; `/notes/<id>` is the form its importer turns into a link.
    """
    label = re.sub(r"[\[\]|()]", " ", " ".join((title or "untitled").split())).strip()
    if len(label) > LABEL_CHARS:
        label = label[:LABEL_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:(—-") + "…"
    return "[%s](/notes/%s)" % (label or "untitled", page_id)


class Resolver:
    def __init__(self, mcp, refs):
        self.mcp, self.refs, self.found, self.dropped = mcp, refs, {}, []
        self.slugs = None

    def lookup(self, token):
        """token -> {"id", "title"} or None. A raw id is trusted only if Kura returns the page."""
        token = token.strip()
        if token in self.refs:
            return self.refs[token]
        if not token.startswith("kura:"):
            return None
        page_id = token[5:].lower()
        for known in self.refs.values():
            if known["id"].lower() == page_id:
                return known
        if page_id not in self.found:
            if self.slugs is None:
                self.slugs = [w["slug"] for w in self.mcp.tool("operator_workspace_list", {})["items"]
                              if w["slug"] not in EXCLUDED_WORKSPACES]
            self.found[page_id] = None
            for slug in self.slugs:
                try:
                    page = self.mcp.tool("operator_page_read",
                                         {"workspace": slug, "id": page_id, "view": "page"})
                    self.found[page_id] = {"id": page["id"], "title": page["title"],
                                           "ws": slug, "type": page.get("type") or ""}
                    break
                except Exception:
                    continue
        return self.found[page_id]

    def link(self, match):
        page = self.lookup(match.group(1))
        if not page:
            self.dropped.append(match.group(1))
            return "(page reference %s could not be resolved)" % match.group(1)
        label = match.group(2) if match.lastindex and match.lastindex >= 2 else None
        return page_link(page["id"], (label or "").strip() or page["title"])


def actions(lines, resolver, state):
    out, bad = [], 0
    own = set(state.get("own_ids", []))
    title_only = set(state.get("title_only_ids", []))
    for line in lines:
        line = line.strip().lstrip("-* ").strip()
        if not line:
            continue
        body, _, reason = line.partition("::")
        words = body.replace("->", " ").split()
        if words[:1] == ["link"]:
            continue  # handled by add_links
        pages = [resolver.lookup(w.strip("{}")) for w in words[1:] if re.match(r"\{*(P\d+|kura:)", w)]
        reason = " ".join(reason.split())[:200]
        item = None
        if words[:1] == ["move"] and len(pages) == 2 and all(pages):
            item = {"kind": "move", "page_id": pages[0]["id"], "to_parent_id": pages[1]["id"],
                    "reason": reason}
        elif words[:1] == ["flag"] and len(words) > 2 and words[1] in FLAGS and pages and pages[0]:
            item = {"kind": "flag", "flag": words[1], "page_id": pages[0]["id"], "reason": reason}
            if len(pages) > 1 and pages[1]:
                item["other_page_id"] = pages[1]["id"]
        if item and item["kind"] == "move" and any(p.get("type") != "note" for p in pages[:2]):
            item = None
        if item and pages[0].get("ws"):
            item["workspace"] = pages[0]["ws"]  # a label for the inbox; Kura stays the authority
        # Neither end of an action may be a protected page: not the page, not the target.
        if item and not (item["kind"] == "move" and any(
                pg and (pg["id"] in title_only or pg.get("ws") in TITLE_ONLY_WORKSPACES)
                for pg in pages[:2])) and not any(
                pg and (pg["id"] in PROTECTED_IDS or pg["id"] in own
                                    or protected_title(pg["title"]))
                            for pg in pages[:2]):
            out.append(json.dumps(item, ensure_ascii=False))
        else:
            bad += 1
    return out[:ACTION_CAP], bad


LINK_PAGE_CAP = 10   # append attempts per run, including uncertain outcomes
LINK_PER_PAGE = 3


def save_linked(record):
    path = linked_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".linked-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def seed_linked():
    record = linked_record()
    added = 0
    for line in sys.stdin:
        ids = line.split()
        if len(ids) != 2 or any(not re.fullmatch(UUID, value) for value in ids):
            raise SystemExit("seed-linked: expected '<page id> <target id>' per line")
        page_id, target_id = ids
        entry = record.setdefault(page_id, {"to": [], "on": dt.date.today().isoformat()})
        if target_id not in entry["to"]:
            entry["to"].append(target_id)
            added += 1
    save_linked(record)
    print("seed-linked: %d added" % added)


def add_links(mcp, resolver, lines, state):
    """Append one `Related:` line to new pages. Returns report lines for what was really done.

    Only pages the scan listed as new, never a protected or own-report page, never a page
    that already has a Related line, at most LINK_PAGE_CAP pages a run. Append only.
    """
    wanted, done, skipped = {}, [], 0
    attempted = 0
    uncertain = []
    record = linked_record()
    jev = state.get("jev") or {}
    mode = jev.get("mode", "off")
    own = set(state.get("own_ids", []))
    title_only = set(state.get("title_only_ids", []))
    if mode == "on":
        requests = ([("link %s -> %s" % (item["page"], item["pick"]), item.get("confidence", 0))
                     for item in jev.get("picks", {}).values() if item.get("pick")]
                    if jev_key() and not (jev.get("errors") and not jev.get("calls")) else [])
    else:
        requests = [(line, None) for line in lines]
    for line, confidence in requests:
        line = line.strip().lstrip("-* ").strip()
        body, _, reason = line.partition("::")
        words = body.replace("->", " ").split()
        if words[:1] != ["link"]:
            continue
        pages = [resolver.lookup(w.strip("{}")) for w in words[1:] if re.match(r"\{*(P\d+|kura:)", w)]
        if (len(pages) != 2 or not all(pages) or pages[0]["id"] == pages[1]["id"]
                or any(p.get("type") != "note" for p in pages)
                or any(p["id"] in own or p["id"] in title_only
                       or p["id"] in PROTECTED_IDS or protected_title(p.get("title"))
                       or p.get("ws") in TITLE_ONLY_WORKSPACES for p in pages)
                or pages[0]["id"] in record):
            skipped += 1
            continue
        wanted.setdefault(pages[0]["id"], (pages[0], []))[1].append((pages[1], confidence))
    new_ids = set(state.get("new_ids", []))
    for page_id, (page, targets) in wanted.items():
        if attempted >= LINK_PAGE_CAP or page_id not in new_ids or page_id in record:
            skipped += len(targets)
            continue
        appended = False
        issued = False
        try:
            slug = page.get("ws") or "portfolio-sample"
            current = mcp.tool("operator_page_read", {"workspace": slug, "id": page_id, "view": "page"})
            if (current.get("type") != "note" or current.get("archived_at")
                    or current.get("id") in PROTECTED_IDS or current.get("id") in own
                    or protected_title(current.get("title"))
                    or current.get("id") in title_only
                    or current.get("parent_id") in own
                    or current.get("workspace") in TITLE_ONLY_WORKSPACES):
                skipped += len(targets)
                continue
            text = mcp.raw("operator_page_read", {"workspace": slug, "id": page_id, "view": "markdown"})
            if "\n</UNTRUSTED_WORKSPACE_POLICY>" not in text:
                raise ValueError("incomplete markdown read; refusing append")
            if "Related:" in text:
                skipped += len(targets)
                continue
            links, chosen = [], []
            for target, confidence in targets:
                if len(links) >= LINK_PER_PAGE or target["id"] in [p["id"] for p, _ in chosen] \
                        or page_id in (record.get(target["id"]) or {}).get("to", []):
                    skipped += 1
                    continue
                links.append(page_link(target["id"], target["title"]))
                chosen.append((target, confidence))
            if not links:
                continue
            attempted += 1
            issued = True
            mcp.tool("operator_page_append", {
                "id": page_id, "markdown": "\n\nRelated: " + ", ".join(links),
                "expected_updated_at": current["updated_at"]})
            appended = True
            record[page_id] = {"to": [target["id"] for target, _ in chosen],
                               "on": dt.date.today().isoformat()}
            save_linked(record)
            suffix = (" (picked by the decision model, confidence %.2f)" % chosen[0][1]
                      if mode == "on" else "")
            done.append("- %s: added Related: %s%s" % (
                page_link(page_id, page["title"]), ", ".join(links), suffix))
        except Exception:
            if appended:
                raise  # the write succeeded; surface any local record failure immediately
            if issued:
                uncertain.append(page_id)
                skipped += len(targets)
                break
            skipped += len(targets)
    state["link_uncertain"] = uncertain
    return done, skipped


HUB = os.environ.get("LIBRARIAN_HUB_ID", "")


def side_by_side(mcp, resolver, lines, state):
    """Write tonight's two sets of picks (the Librarian's model and Jev) to one page. Trial only."""
    jev = state.get("jev") or {}
    if jev.get("mode") != "compare" or not jev.get("picks"):
        return ""
    model = {}
    for line in lines:
        words = line.strip().lstrip("-* ").partition("::")[0].replace("->", " ").split()
        if words[:1] == ["link"] and len(words) >= 3:
            model.setdefault(words[1].strip("{}"), words[2].strip("{}"))

    def show(token):
        if not token:
            return "no link"
        page = resolver.lookup(token)
        if not page:
            return "(unknown)"
        return page_link(page["id"], page["title"])

    rows, agree = [], 0
    for item in jev["picks"].values():
        ours = model.get(item["page"])
        if ours and ours not in item["candidates"]:
            ours_note = show(ours) + " (not one of the candidates)"
        else:
            ours_note = show(ours)
        same = (ours or None) == (item["pick"] or None)
        agree += same
        rows.append("- %s\n    - Librarian's model: %s\n    - Jev: %s (confidence %.2f)\n    - %s" % (
            show(item["page"]), ours_note, show(item["pick"]), item["confidence"],
            "SAME" if same else "DIFFERENT"))
    day = state["title"][-10:]
    title = "Jev side-by-side %s" % day
    body = (
        "Trial page. For each new page, the script gathered a few candidate pages; the Librarian's "
        "own model and Jev each picked one (or none). Tonight's report and Related links followed "
        "the Librarian's model only.\n\n"
        "## Result\n\n- Pages compared: %d\n- Same pick: %d\n- Different: %d\n"
        "- Decision calls: %d, failed: %d, provider-reported cost: US$%.5f\n\n"
        "## What was sent to the decision provider\n\n- The new page's title, its opening lines (up to 420 "
        "characters) and the candidate pages' titles.\n- Only from these workspaces: %s.\n"
        "- Never sent: the client workspace, personal sample. private ledger pages are sent by title only.\n\n"
        "## Page by page\n\n%s\n" % (
            len(rows), agree, len(rows) - agree, jev.get("calls", 0), jev.get("errors", 0),
            jev.get("cost", 0.0), ", ".join(jev.get("workspaces", [])), "\n".join(rows)))
    try:
        hits = mcp.tool("operator_search", {"workspace": os.environ.get("LIBRARIAN_WORKSPACE", "portfolio-sample"), "q": title, "limit": 10})["items"]
        same_page = [h["id"] for h in hits if h["title"] == title and h.get("parent_id") == HUB]
        if same_page:
            current = mcp.tool("operator_page_read", {"workspace": os.environ.get("LIBRARIAN_WORKSPACE", "portfolio-sample"), "id": same_page[0], "view": "page"})
            done = mcp.tool("operator_page_update", {
                "id": same_page[0], "markdown": body, "expected_updated_at": current["updated_at"]})
        else:
            done = mcp.tool("operator_page_create", {
                "workspace": os.environ.get("LIBRARIAN_WORKSPACE", "portfolio-sample"), "parent_id": HUB, "title": title, "markdown": body})
        return " | Jev side-by-side page %s: %d compared, %d same" % (done["id"], len(rows), agree)
    except Exception:
        return " | Jev side-by-side page could not be written"


def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else ""
    if kind == "seed-linked":
        seed_linked()
        return
    path = os.path.join(home(), "scripts", "state_%s.json" % kind)
    if not os.path.exists(path):
        raise SystemExit("kura_publish: no scan state for '%s' (use changes, loose or tidy)" % kind)
    with open(path, encoding="utf-8") as fh:
        state = json.load(fh)
    if state.get("report_lookup_error"):
        raise SystemExit("kura_publish: report lookup failed during scan; refusing a possibly duplicate report")
    try:
        with open(state["draft"], encoding="utf-8") as fh:
            draft = fh.read()
    except OSError:
        raise SystemExit("kura_publish: draft not found at %s" % state["draft"])
    if len(draft.strip()) < 80:
        raise SystemExit("kura_publish: the draft is nearly empty; nothing published")

    mcp = Mcp()
    resolver = Resolver(mcp, state["refs"])
    parts = re.compile(r"^## ACTIONS\s*$", re.M).split(draft, maxsplit=1)
    prose = parts[0]
    lines, bad = actions(parts[1].splitlines() if len(parts) > 1 else [], resolver, state)
    # A link the model typed by hand is not trusted: re-resolve it by id.
    prose = re.sub(r"\[\[kura:(%s)\|[^\]]*\]\]" % UUID, r"{{kura:\1}}", prose)
    prose = re.sub(TOKEN, resolver.link, prose).rstrip() + "\n"
    links = len(re.findall(r"\]\(/notes/", prose))
    body = prose
    linked, link_skipped = [], 0
    if kind == "loose" and state.get("act"):
        linked, link_skipped = add_links(mcp, resolver, parts[1].splitlines() if len(parts) > 1 else [], state)
        jev = state.get("jev") or {}
        if jev.get("mode") == "on" and not linked:
            message = ("- Nothing confirmed: decision model unavailable."
                       if not jev_key() or (jev.get("calls", 0) == 0 and jev.get("errors", 0))
                       else "- Nothing confirmed: no eligible decision model picks.")
        else:
            message = "- No links confirmed in this run."
        body += "\n## Done (LIBRARIAN)\n\n%s\n" % (
            "\n".join(linked) if linked else message)
        if state.get("link_uncertain"):
            body += "\nAppend outcome uncertain for %d page(s); inspect the Related line before retrying.\n" % len(state["link_uncertain"])
    if kind in ("loose", "tidy"):
        body += "\n```librarian-actions\n%s\n```\n" % "\n".join(lines)

    page_id = state.get("existing")
    if page_id:
        current = mcp.tool("operator_page_read", {"workspace": os.environ.get("LIBRARIAN_WORKSPACE", "portfolio-sample"), "id": page_id, "view": "page"})
        done = mcp.tool("operator_page_update", {
            "id": page_id, "markdown": body, "expected_updated_at": current["updated_at"]})
    else:
        done = mcp.tool("operator_page_create", {
            "workspace": os.environ.get("LIBRARIAN_WORKSPACE", "portfolio-sample"), "parent_id": state["parent"], "title": state["title"],
            "markdown": body})
        state["existing"] = done["id"]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    trial = ""
    if kind == "loose":
        trial = side_by_side(mcp, resolver, parts[1].splitlines() if len(parts) > 1 else [], state)
        note = (state.get("jev") or {}).get("note")
        if note and not trial and (state.get("jev") or {}).get("mode") == "compare":
            trial = " | Jev: " + note
    print("PUBLISHED \"%s\" id %s | %d links | %d references could not be resolved%s | "
          "%d action lines (%d rejected) | %d pages given a Related line (%d link requests skipped)" % (
              state["title"], done["id"], links, len(resolver.dropped),
              (": " + ", ".join(resolver.dropped[:8])) if resolver.dropped else "",
              len(lines), bad, len(linked), link_skipped) + trial)


if __name__ == "__main__":
    main()
