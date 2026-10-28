# Pipeline Field Design Engineer (FDE) agent

A prototype AI agent that works like a pipeline Field Design Engineer. Claude (Opus 5.5) handles the
reasoning and writing. Every number with regulatory weight comes from deterministic Python tools that you can
test and audit.

> Prototype only. The code tables come from 49 CFR Part 192 and common industry references, but a qualified
> engineer must verify every result before it is used for design, construction or operation.

## What it does

| FDE responsibility | Agent tools |
|---|---|
| Compliance and MAOP validation | `design_pressure` (192.105, with F/E/T factors from 192.111/113/115), `maop` (192.619(a)), `current_maop` (computed from asset records), `required_test_pressure`, `hoop_stress`, `class_location` (192.5), `class_location_study` (sliding-mile study from the structure inventory), `check_cover` (192.327), `check_valve_spacing` (192.179) |
| Field data and surveys | `review_survey`: reads a ROW walkdown CSV and flags shallow cover, CP readings that miss the -850 mV criterion, encroachments, exposed pipe and coating damage |
| As-built and change management | `assess_field_change` (FCR impact against current MAOP and cover), `reconcile_bom` (missing items, quantity variance, spec substitutions, unplanned items, missing MTRs), `update_asset_record` (approval-gated, change-logged) |
| CAD/GIS and PODS records | `list_pipelines`, `get_assets`: a SQLite store modeled on PODS Lite (stationed pipe segments, point features with lat/long, class location ranges, pressure tests, change log) |
| Finding and verifying drawings | `search_drawings`, `get_drawing`, `compare_drawings`, `verify_drawings`, `drawing_package`: one index over document control, vendor submittals, GIS links and shared drives; works out the current revision and flags superseded links, uncontrolled or conflicting copies, design vs vendor mismatches (pressure class, part number, approval status) and drawings that disagree with asset records. See [docs/drawing-control.md](docs/drawing-control.md) |
| Cross-functional coordination | `save_document`: drafts FCR responses, survey reports and memos to `output/`. The agent never sends anything |

## Architecture

```
engineer ──► cli.py ──► FDEAgent (agent.py)
                          │  Claude Messages API: tool-use loop, adaptive thinking,
                          │  prompt caching, server-side refusal fallback
                          ▼
                       ToolBox (tools.py) ──► calcs.py      49 CFR 192 calculations
                          │                ├─► field.py      survey / BOM / FCR workflows
                          │                ├─► pods_store.py PODS-style SQLite store
                          │                └─► drawings/     drawing index + cross-checks over EDMS,
                          │                                  vendor, GIS and shared-drive adapters
                          └─ approval hook: every database write needs a "yes" from the engineer
```

`pods_store.PodsStore` is intentionally small, so it can be replaced with a real PODS or ArcGIS Utility
Network backend, Civil 3D or alignment sheet exports, or GIS feature services without touching the agent.

## Run it

```bash
pip install -r requirements.txt   # plus requirements-dev.txt for tests
export ANTHROPIC_API_KEY=...            # or: ant auth login
python -m fde_agent.cli                 # interactive, uses the fictional "Line 100" sample data
python -m fde_agent.cli -p "Review data/sample/l100_walkdown.csv for L100 and draft a memo to the CP specialist"
python -m fde_agent.cli --data data/synthetic   # the 6-line synthetic system
python -m fde_agent.cli --db assets.db  # persist the asset database between runs
python -m fde_agent.drawings data/synthetic/drawings --data data/synthetic   # drawing check report, no LLM
```

Example requests:

- "What's the MAOP of L100 from station 0 to 26400, and what governs each segment?"
- "FCR-0042: the crew wants to swap the 12-inch ANSI 600 valve at station 0 for an ANSI 300. Assess it."
- "Reconcile data/sample/l100_bom_design.csv against data/sample/l100_asbuilt.csv and write up the open items."
- "We counted 52 houses within 220 yards near station 20000. What does that do to class location and MAOP?"
- With `--data data/synthetic`: "Run an integrity records review of SYN-104: class location changes, valve spacing, MAOP and records gaps."
- With `--data data/synthetic`: "Find every drawing for valve SYN-101-V02 and tell me which copy is current."
- With `--data data/synthetic`: "Build the drawing package for SYN-105 station 0 to 30000. Anything I shouldn't hand the crew?"
- With `--data data/synthetic`: "Does the vendor GA for SYN-106-V02 match our valve detail?"

## Tests

```bash
python -m pytest -q
```

The tests check the calculations against hand calculations, run the survey, BOM and FCR workflows on the
sample data, confirm that writes are blocked without approval, run the agent loop offline against a scripted
client, and check that the tools find every problem seeded in the synthetic data. The drawing checks must match the
planted drawing discrepancies exactly: every one found, nothing else flagged.

## Data

- `data/sample/`: a small fictional 12-inch, 5-mile gas lateral ("Line 100", built into `seed_sample()`), with a
  walkdown CSV and a BOM / as-built pair. Used by the quick tests.
- `data/synthetic/`: a generated six-line system with structures, valves, CP test stations, walkdowns, replacement
  projects, GeoJSON, drawing sources (`drawings/`) and an answer key of seeded problems. See [its README](data/synthetic/README.md). Regenerate
  or vary it with `python -m fde_agent.synth --out data/synthetic --seed 7`.
- Public reference data: [docs/public-data-sources.md](docs/public-data-sources.md) lists PHMSA, EIA, NPMS,
  building footprint, eCFR and PODS sources, and `scripts/fetch_public_data.py` downloads the scriptable ones
  into `data/public/`.
