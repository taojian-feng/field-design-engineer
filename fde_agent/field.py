"""Field workflows: ROW survey review, BOM vs. as-built reconciliation, field change request impact."""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from . import calcs, codes
from .pods_store import PodsStore


def _read_csv(path: str | Path) -> list[dict]:
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def _class_at(store: PodsStore, pipeline_id: str, station_ft: float) -> int | None:
    ranges = store.segments_at(pipeline_id, station_ft)["class_locations"]
    return max((r["class_location"] for r in ranges), default=None)


def review_survey(store: PodsStore, pipeline_id: str, csv_path: str) -> dict:
    """Review a ROW walkdown CSV.

    Expected columns: station_ft, observation_type, value, notes. observation_type is one
    of depth_of_cover_in, cp_potential_mv, encroachment, exposed_pipe, coating_damage, rock.
    """
    findings, stats = [], defaultdict(int)
    rows = _read_csv(csv_path)
    rock_stations = {float(r["station_ft"]) for r in rows if r["observation_type"] == "rock"}
    for r in rows:
        sta = float(r["station_ft"])
        kind = r["observation_type"].strip()
        stats[kind] += 1
        notes = r.get("notes", "")
        if kind == "depth_of_cover_in":
            cls = _class_at(store, pipeline_id, sta)
            if cls is None:
                findings.append({"station_ft": sta, "severity": "medium", "issue": "Station outside class location records", "notes": notes})
                continue
            chk = calcs.check_cover(float(r["value"]), cls, rock=sta in rock_stations)
            if not chk["compliant"]:
                findings.append({
                    "station_ft": sta, "severity": "high" if chk["deficit_in"] >= 12 else "medium",
                    "issue": f"Depth of cover {chk['measured_in']} in below 192.327 install minimum {chk['required_in']} in (Class {cls})",
                    "action": "Evaluate for cover restoration or protection; confirm with damage prevention program.",
                    "notes": notes,
                })
        elif kind == "cp_potential_mv":
            mv = float(r["value"])
            if mv > codes.CP_CRITERION_MV:  # less negative than -850 mV
                findings.append({
                    "station_ft": sta, "severity": "high",
                    "issue": f"Pipe-to-soil {mv:.0f} mV CSE is less negative than {codes.CP_CRITERION_MV} mV criterion",
                    "action": "Notify corrosion/CP specialist; remediation required under 192.465(d).",
                    "notes": notes,
                })
        elif kind in ("encroachment", "exposed_pipe", "coating_damage"):
            sev = "high" if kind == "exposed_pipe" else "medium"
            findings.append({"station_ft": sta, "severity": sev, "issue": kind.replace("_", " ").capitalize(),
                             "action": "Document with photos; open work order.", "notes": notes})
    findings.sort(key=lambda f: ({"high": 0, "medium": 1, "low": 2}[f["severity"]], f["station_ft"]))
    return {"observations": dict(stats), "finding_count": len(findings), "findings": findings}


def reconcile_bom(design_csv: str, asbuilt_csv: str) -> dict:
    """Compare design BOM to field-installed materials.

    Both CSVs: item_id, description, spec, quantity, unit. The as-built CSV may also carry
    heat_number and mtr_on_file (yes/no) for traceability.
    """
    design = {r["item_id"]: r for r in _read_csv(design_csv)}
    built = {r["item_id"]: r for r in _read_csv(asbuilt_csv)}
    issues = []
    for item_id, d in design.items():
        b = built.get(item_id)
        if b is None:
            issues.append({"item_id": item_id, "type": "not_installed", "detail": f"{d['description']} not on as-built"})
            continue
        dq, bq = float(d["quantity"]), float(b["quantity"])
        if dq and abs(bq - dq) / dq > 0.05:
            issues.append({"item_id": item_id, "type": "quantity_variance",
                           "detail": f"{d['description']}: design {dq:g} {d['unit']}, installed {bq:g} {b['unit']} ({100*(bq-dq)/dq:+.0f}%)"})
        if d["spec"].strip().lower() != b["spec"].strip().lower():
            issues.append({"item_id": item_id, "type": "substitution",
                           "detail": f"{d['description']}: design spec '{d['spec']}', installed '{b['spec']}'. Needs approved FCR and MAOP check."})
    for item_id, b in built.items():
        if item_id not in design:
            issues.append({"item_id": item_id, "type": "unplanned_item", "detail": f"{b['description']} installed but not in design BOM"})
        if (b.get("mtr_on_file") or "").strip().lower() in ("no", "n", "false", "0"):
            issues.append({"item_id": item_id, "type": "traceability",
                           "detail": f"{b['description']} (heat {b.get('heat_number') or 'unknown'}) has no MTR on file. Records would not be TVC."})
    return {"design_items": len(design), "asbuilt_items": len(built), "issue_count": len(issues), "issues": issues}


def assess_field_change(store: PodsStore, pipeline_id: str, begin_station_ft: float, end_station_ft: float,
                        proposed: dict, description: str) -> dict:
    """Impact assessment for a field change request (FCR).

    proposed may hold: nps, wall_in, grade, seam_type, component_rating_psig, depth_of_cover_in, rock.
    Compares the proposed material to the current MAOP of the affected range.
    """
    recs = store.segments_at(pipeline_id, begin_station_ft, end_station_ft)
    if not recs["pipe_segments"]:
        raise ValueError("No pipe segments found in that station range")
    cls = max(r["class_location"] for r in recs["class_locations"])
    current = current_maop(store, pipeline_id, begin_station_ft, end_station_ft)
    seg = recs["pipe_segments"][0]
    checks, verdict = [], "acceptable"
    if any(k in proposed for k in ("wall_in", "grade", "seam_type", "nps")):
        dp = calcs.design_pressure(
            wall_in=proposed.get("wall_in", seg["wall_in"]), class_location=cls,
            seam_type=proposed.get("seam_type", "erw"), grade=proposed.get("grade", seg["grade"]),
            nps=proposed.get("nps", seg["nps"]),
        )
        ok = dp["design_pressure_psig"] >= current["maop_psig"]
        checks.append({"check": "Proposed pipe design pressure vs. current MAOP", "result": dp["design_pressure_psig"],
                       "required": current["maop_psig"], "pass": ok})
        verdict = verdict if ok else "rejected"
    if "component_rating_psig" in proposed:
        ok = proposed["component_rating_psig"] >= current["maop_psig"]
        checks.append({"check": "Component pressure rating vs. current MAOP", "result": proposed["component_rating_psig"],
                       "required": current["maop_psig"], "pass": ok})
        verdict = verdict if ok else "rejected"
    if "depth_of_cover_in" in proposed:
        chk = calcs.check_cover(proposed["depth_of_cover_in"], cls, rock=bool(proposed.get("rock")))
        checks.append({"check": "192.327 depth of cover", "result": chk["measured_in"], "required": chk["required_in"], "pass": chk["compliant"]})
        if not chk["compliant"] and verdict == "acceptable":
            verdict = "conditional"
    deliverables = [
        "Redline affected alignment sheet(s) and P&ID, bump drawing revision",
        "Update BOM and material traceability (heat numbers, MTRs)",
        "Update PODS asset records after construction (as-built)",
    ]
    if any(not c["pass"] for c in checks):
        deliverables.insert(0, "Return FCR to design engineering with failed checks")
    return {
        "description": description, "station_range_ft": [begin_station_ft, end_station_ft],
        "class_location": cls, "current_maop": current, "checks": checks, "verdict": verdict,
        "deliverables": deliverables,
    }


def current_maop(store: PodsStore, pipeline_id: str, begin_station_ft: float, end_station_ft: float,
                 historical_max_psig: float | None = None) -> dict:
    """MAOP for a station range from asset records: lowest result across segments, tests and components."""
    recs = store.segments_at(pipeline_id, begin_station_ft, end_station_ft)
    if not recs["pipe_segments"]:
        raise ValueError("No pipe segments found in that station range")
    results = []
    for seg in recs["pipe_segments"]:
        lo, hi = max(seg["begin_station_ft"], begin_station_ft), min(seg["end_station_ft"], end_station_ft)
        cls = max(r["class_location"] for r in store.segments_at(pipeline_id, lo, hi)["class_locations"])
        sub = store.segments_at(pipeline_id, lo, hi)
        dp = calcs.design_pressure(wall_in=seg["wall_in"], class_location=cls, seam_type=seg["seam_type"],
                                   grade=seg["grade"], od_in=seg["od_in"])
        tests = [t["test_pressure_psig"] for t in sub["pressure_tests"]
                 if t["begin_station_ft"] <= lo and t["end_station_ft"] >= hi]
        ratings = [p["rating_psig"] for p in sub["point_features"] if p["rating_psig"]]
        m = calcs.maop(dp["design_pressure_psig"], cls,
                       test_pressure_psig=min(tests) if tests else None,
                       weakest_component_rating_psig=min(ratings) if ratings else None,
                       historical_max_psig=historical_max_psig,
                       records_tvc=bool(seg["mtr_on_file"]))
        results.append({"segment_id": seg["segment_id"], "range_ft": [lo, hi], "class_location": cls,
                        "design": dp, **m})
    governing = min(results, key=lambda r: r["maop_psig"])
    return {"maop_psig": governing["maop_psig"], "governing_segment": governing["segment_id"], "segments": results}


def class_location_study(store: PodsStore, pipeline_id: str, step_ft: float = 100) -> dict:
    """Sliding-mile class location study per 49 CFR 192.5 from the structure inventory.

    Each dwelling unit counts as a building; identified sites within 300 ft (100 yd) raise the unit to
    Class 3; a unit where most buildings have 4+ stories is Class 4 (prevalence is a judgment call, so the
    study uses a simple majority as a screening rule). Each station takes the highest class of any 1-mile
    unit that contains it. Results are compared with the recorded class location ranges.
    """
    line = next((p for p in store.list_pipelines() if p["pipeline_id"] == pipeline_id), None)
    if line is None:
        raise ValueError(f"Unknown pipeline {pipeline_id}")
    begin, end = line["begin_station_ft"], line["end_station_ft"]
    mile = 5280.0
    structs = store.structures(pipeline_id)
    starts = [begin] if end - begin <= mile else [begin + i * step_ft for i in range(int((end - begin - mile) // step_ft) + 1)]
    if end - begin > mile and starts[-1] < end - mile:
        starts.append(end - mile)

    def unit_class(a: float, b: float) -> tuple[int, int]:
        inside = [s for s in structs if a <= s["station_ft"] <= b]
        bldgs = [s for s in inside if s["structure_type"] != "identified_site"]
        count = sum(int(s["dwelling_units"] or 1) for s in bldgs)
        tall = sum(1 for s in bldgs if (s["stories"] or 1) >= 4)
        site = any(s["structure_type"] == "identified_site" and abs(s["offset_ft"]) <= 300 for s in inside)
        r = calcs.class_location(count, four_story_prevalent=bool(bldgs) and tall / len(bldgs) > 0.5,
                                 identified_site_within_100yd=site)
        return r["class_location"], count

    units = [(a, min(a + mile, end), *unit_class(a, min(a + mile, end))) for a in starts]
    # Station-by-station worst class, merged into ranges.
    edges = sorted({begin, end, *[u[0] for u in units], *[u[1] for u in units]})
    ranges: list[dict] = []
    for a, b in zip(edges, edges[1:]):
        mid = (a + b) / 2
        covering = [u for u in units if u[0] <= mid <= u[1]]
        cls = max(u[2] for u in covering)
        peak = max(u[3] for u in covering)
        if ranges and ranges[-1]["class_location"] == cls:
            ranges[-1]["end_station_ft"] = b
            ranges[-1]["max_building_count"] = max(ranges[-1]["max_building_count"], peak)
        else:
            ranges.append({"begin_station_ft": a, "end_station_ft": b, "class_location": cls, "max_building_count": peak})

    recorded = store.segments_at(pipeline_id, begin, end)["class_locations"]
    changes = []
    for r in ranges:
        for rec in recorded:
            lo, hi = max(r["begin_station_ft"], rec["begin_station_ft"]), min(r["end_station_ft"], rec["end_station_ft"])
            if hi > lo and r["class_location"] > rec["class_location"]:
                changes.append({"begin_station_ft": lo, "end_station_ft": hi, "recorded_class": rec["class_location"],
                                "study_class": r["class_location"],
                                "action": "Class location change: confirm or revise MAOP per 192.611 within 24 months (192.611(d))."})
    return {"pipeline_id": pipeline_id, "structures_in_corridor": len(structs), "study_ranges": ranges,
            "class_increases": changes}


def check_valve_spacing(store: PodsStore, pipeline_id: str) -> dict:
    """Check mainline valve spacing against 49 CFR 192.179(a) using the class location at each gap.

    Uses the strictest class along each gap. 192.179 measures to any point on the line (half-spacing);
    this screening check compares the full valve-to-valve gap with the listed spacing.
    """
    limits_mi = {1: 10, 2: 7.5, 3: 4, 4: 2.5}
    line = next((p for p in store.list_pipelines() if p["pipeline_id"] == pipeline_id), None)
    if line is None:
        raise ValueError(f"Unknown pipeline {pipeline_id}")
    recs = store.segments_at(pipeline_id, line["begin_station_ft"], line["end_station_ft"])
    valves = [f for f in recs["point_features"] if f["feature_type"] == "valve"]
    gaps = []
    for a, b in zip(valves, valves[1:]):
        classes = [r["class_location"] for r in recs["class_locations"]
                   if r["begin_station_ft"] < b["station_ft"] and r["end_station_ft"] > a["station_ft"]]
        cls = max(classes, default=1)
        gap_mi = (b["station_ft"] - a["station_ft"]) / 5280
        gaps.append({"from": a["feature_id"], "to": b["feature_id"], "gap_mi": round(gap_mi, 2), "class_location": cls,
                     "limit_mi": limits_mi[cls], "compliant": gap_mi <= limits_mi[cls]})
    return {"pipeline_id": pipeline_id, "valve_count": len(valves),
            "violations": [g for g in gaps if not g["compliant"]], "gaps": gaps}
