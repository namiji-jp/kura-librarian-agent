# Kura Librarian: bounded agent workflow

An inspectable Python sample of an agent that helps organise a knowledge base. It finds new notes, gathers a short list of related pages, asks a decision model to choose one or abstain, and appends a `Related:` link only after deterministic checks. It also publishes a report of confirmed results and proposed human actions.

The useful boundary is between judgement and execution: a model chooses within a supplied candidate set; Python owns references, eligibility checks, write limits and failure handling. Page moves remain human decisions.

## Try it in two commands

From the repository root, using Python 3 and its standard library:

```sh
python3 scripts/offline_demo.py
python3 -m unittest discover -s tests -v
```

No package installation, account, API key or network connection is needed. The demo uses synthetic pages, fixed decision responses and an in-memory Kura relay. It creates temporary local state and removes it when the run finishes. It does not connect to a real workspace or invoke a real model.

Verified with Python 3.12.14: **41 tests pass**. The demo prints JSON including:

```json
{
  "decision_inputs": 2,
  "abstentions": 1,
  "fake_kura_append_calls": 1,
  "fake_kura_report_writes": 1,
  "fixture_decision_calls": 2,
  "external_provider_calls": 0
}
```

### What the demo shows

1. The scanner finds two new notes: **Coastal route analysis** and **Rail disruption checklist**.
2. The fixture chooses **Coastal route project** for the first note and abstains on the second.
3. A deliberately conflicting report draft proposes the rail project for the coastal note.
4. In decision-model `on` mode, the publisher follows the validated fixture choice rather than that draft proposal. It appends one Related line pointing to **Coastal route project** and writes one report to the fake relay.

This exercises the actual scanner, decision-response validation and publisher code. The responses are authored fixtures, not model evaluations: the demo establishes workflow behaviour, not semantic decision quality or production reliability.

## How it works

```text
Kura list / read / search tools
        |
        v
Scanner: new notes, bounded excerpts, candidate pages, short references
        |
        v
Decision provider: one candidate key or "none"
        |
        v
Saved scan state + report draft
        |
        v
Publisher: resolve references, check eligibility, re-read source page
        |
        +--> bounded Related append + local duplicate ledger
        +--> report with confirmed outcomes and human-action proposals
```

- **Measure first.** The scanner discovers pages and assigns references such as `P1`. Candidate search uses distinctive title words, with at most 14 new pages considered for candidate lookup and four candidates per page. Excerpts are limited to 18 pages and 420 characters each. Missing scan coverage is reported explicitly.
- **Constrain the decision.** The provider receives the new note's title, permitted opening lines and candidate titles. A response must name a supplied candidate key or `none`, with confidence in the range 0–1. Invalid responses are recorded as errors and do not create a valid pick. Confidence is recorded, not used as a minimum-quality threshold.
- **Separate report writing from execution.** The report writer uses short page references and a structured `ACTIONS` section. The publisher resolves references into links and records move/flag proposals; it does not execute moves. In `on` mode, Related links come from the decision picks. The code also retains `off` and `compare` modes, where draft link requests are used instead.
- **Check before writing.** The publisher filters protected pages, its own reports, title-only pages and non-note types; requires the source to have appeared in the new-page scan; then re-reads it, checks for an existing Related marker and sends an `expected_updated_at` version with the append.
- **Report what happened.** Confirmed appends appear under `Done (LIBRARIAN)`. Uncertain outcomes are called out for inspection. Report lookup failure blocks publication rather than being mistaken for an absent report.

The included `jobs/filing.md` is the report-writer prompt. The offline demo supplies its own draft; neither a report-writing model nor a scheduler is bundled as a runnable service.

## Code map and evidence

- [`scripts/kura_scan.py`](scripts/kura_scan.py): MCP transport, scan routines, privacy filters, candidate search and decision-provider seam. Also contains change-digest, tidy and count-probe routines beyond the filing demo.
- [`scripts/kura_publish.py`](scripts/kura_publish.py): reference resolution, action validation, bounded appends, ledger and report publication.
- [`scripts/offline_demo.py`](scripts/offline_demo.py) and [`fixtures/decisions.json`](fixtures/decisions.json): reproducible synthetic end-to-end example.
- [`jobs/filing.md`](jobs/filing.md): constrained report-writing instructions.
- [`tests/test_links_and_homes.py`](tests/test_links_and_homes.py): eligibility, decision precedence, append caps, duplicate ledger, concurrent-edit rejection, uncertain writes, transport guards and failed report lookup.
- [`tests/test_own_pages.py`](tests/test_own_pages.py): exclusion of the agent's own pages and descendants.
- [`tests/test_title_only_workspace.py`](tests/test_title_only_workspace.py): restricted body reads and filing actions.
- [`tests/test_scan_retries.py`](tests/test_scan_retries.py): bounded read retries and non-retried writes.
- [`tests/test_offline_demo.py`](tests/test_offline_demo.py): regression coverage for the complete fixture run.

## Safety boundaries and limitations

- **Bounded writes:** one Related line per eligible source page, at most 10 append attempts per run, and at most three targets per page. Decision-model `on` mode picks at most one target for each source.
- **Uncertain writes require inspection:** the adapter does not retry writes automatically. An ambiguous append stops further Related-link appends in that run; a report may still be written to describe the uncertainty. Inspect the affected page before retrying.
- **Duplicate protection is best-effort:** a local ledger, existing Related marker and optimistic version checks reduce duplicate risk. There is no distributed lock or atomic transaction spanning the remote page service and local ledger. Concurrent runs can race; a confirmed remote append followed by a failed local ledger write surfaces as an error.
- **Privacy requires configuration:** excluded workspaces are omitted; title-only workspaces and matching sensitive titles suppress body excerpts and filing actions. These are configured rules and title heuristics, not a general sensitive-data classifier. Real provider use transmits permitted note content and candidate titles to that provider.
- **Fail-closed transport checks:** incomplete framed reads are rejected. Decision endpoints require HTTPS without URL credentials. Authenticated cross-origin redirects are rejected. Live Kura requires HTTPS except for explicit loopback development.
- **Scope of evidence:** tests use fake services and mocked responses. They do not establish live-service compatibility, production scale, model accuracy or immunity to prompt injection. Search is title-based and coverage is deliberately bounded.

## Live integration boundary

The runnable entry point for this sample is the offline demo. The live adapter is disabled unless `LIBRARIAN_ENABLE_LIVE=1` and explicit operator configuration is present. A compatible Kura operator MCP service, authenticated access, report destinations and a separate scheduler/report-writing integration would be needed for deployment.

The adapter requires `KURA_OPERATOR_URL`, `KURA_OPERATOR_BEARER_TOKEN`, `LIBRARIAN_WORKSPACE`, `LIBRARIAN_EXCLUDED_WORKSPACES`, `LIBRARIAN_TITLE_ONLY_WORKSPACES`, `LIBRARIAN_PROTECTED_PAGE_IDS`, `LIBRARIAN_HUB_ID`, and all three `LIBRARIAN_{CHANGES,LOOSE,TIDY}_REPORT_PARENT_ID` values. Decision-provider use additionally needs an explicitly configured endpoint/key and workspace scope; see the `LIBRARIAN_DECISION_*`, `LIBRARIAN_JEV` and `LIBRARIAN_JEV_WORKSPACES` settings in the scanner. The default decision endpoint uses the reserved `.invalid` domain.

Do not treat the fixture configuration as deployment approval. Review privacy scope, provider data handling, service contracts and recovery procedures before connecting real data. No deployment credentials or live endpoint values are included.

## Attribution

Seiji Matsumura specified the product problem, workflow boundaries, review criteria and release decisions. AI agents generated and tested the implementation, including the offline fixture adapter and additional tests; Seiji reviewed the result.
