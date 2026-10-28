"""Synthetic pipeline dataset generator.

Builds a reproducible, fictional gas transmission system with the records an FDE works from: stationed pipe
segments, class location ranges, pressure tests, valves and CP test stations, a structure inventory for class
location studies, ROW walkdown CSVs, replacement-project BOM / as-built pairs, GeoJSON for GIS viewers, and an
answer key listing every seeded problem so agent output can be scored.

Pipe vintage follows the share of U.S. gas mains miles by install decade in PHMSA's 2025 annual reports (via
Catalyst Cooperative's PUDL tables). The rest is invented. Run:

    python -m fde_agent.synth --out data/synthetic --seed 7
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
from pathlib import Path

from . import calcs, codes
from .drawings.synth import generate_drawings
from .pods_store import PodsStore

# Share of U.S. gas distribution mains miles by install decade, PHMSA annual reports for 2025 (PUDL table
# core_phmsagas__yearly_distribution_by_install_decade). "unknown" means the operator reported no decade.
VINTAGE_SHARE = {
    "pre_1940": 2.0, "1940s": 1.4, "1950s": 6.4, "1960s": 12.8, "1970s": 8.8, "1980s": 11.2,
    "1990s": 16.9, "2000s": 14.6, "2010s": 12.3, "2020s": 8.0, "unknown": 5.6,
}

REGIONS = [  # fictional line names, real-ish geography for map context
    ("SYN-101", "Coastal Prairie Lateral", 29.62, -95.62, 110),
    ("SYN-102", "Red River Header", 34.05, -97.10, 60),
    ("SYN-103", "Allegheny Gathering Trunk", 40.35, -80.10, 20),
    ("SYN-104", "Bayou Crossing Line", 30.25, -91.95, 80),
    ("SYN-105", "Front Range Supply Line", 39.95, -104.85, 350),
    ("SYN-106", "Ohio Valley Loop", 39.85, -82.95, 170),
]

ANSI_RATING_PSIG = {150: 285, 300: 740, 600: 1480, 900: 2220}  # ASME B16.5 group 1.1 at 100 F
VALVE_SPACING_MI = {1: 10, 2: 7.5, 3: 4, 4: 2.5}  # 49 CFR 192.179(a)
WALLS = [0.188, 0.219, 0.250, 0.281, 0.312, 0.344, 0.375, 0.406, 0.438, 0.500]
FT_PER_DEG_LAT = 364_000.0


def _vintage_year(rng: random.Random) -> int | None:
    decade = rng.choices(list(VINTAGE_SHARE), weights=list(VINTAGE_SHARE.values()))[0]
    if decade == "unknown":
        return None
    if decade == "pre_1940":
        return rng.randint(1925, 1939)
    return int(decade[:4]) + rng.randint(0, 9 if decade != "2020s" else 5)


def _pipe_props(rng: random.Random, year: int | None, nps: int) -> dict:
    y = year or 1955
    if y < 1970:
        grade = rng.choice(["B", "X42", "X42", "X46", "X52"])
        seam = rng.choice(["erw", "erw", "seamless", "lap_welded", "unknown"] + (["furnace_butt"] if nps <= 4 else []))
        coating = rng.choice(["Coal tar enamel", "Asphalt", "Bare", "Coal tar enamel"])
        mtr, tested = rng.random() < 0.3, rng.random() < 0.55
    elif y < 1990:
        grade = rng.choice(["X52", "X52", "X60"])
        seam = rng.choice(["erw", "dsaw"])
        coating = rng.choice(["Coal tar enamel", "Polyethylene tape", "FBE"])
        mtr, tested = rng.random() < 0.65, rng.random() < 0.9
    else:
        grade = rng.choice(["X52", "X60", "X65", "X70"])
        seam = rng.choice(["erw", "dsaw"])
        coating = rng.choice(["FBE", "FBE", "3LPE", "Dual-layer FBE"])
        mtr, tested = rng.random() < 0.95, rng.random() < 0.98
    od = codes.NPS_OD_IN[nps]
    wall = rng.choice([w for w in WALLS if 0.012 <= w / od <= 0.045] or [WALLS[-1]])
    return {"grade": grade, "seam_type": seam, "coating": coating, "wall_in": wall, "od_in": od,
            "mtr_on_file": int(mtr), "tested": tested}


class _Route:
    """Random-walk centerline so features can carry plausible lat/long."""

    def __init__(self, rng: random.Random, lat: float, lon: float, heading_deg: float, length_ft: float):
        self.pts = [(0.0, lat, lon, heading_deg)]
        s, h = 0.0, heading_deg
        while s < length_ft:
            step = min(500.0, length_ft - s)
            h += rng.gauss(0, 4)
            lat2 = self.pts[-1][1] + step * math.cos(math.radians(h)) / FT_PER_DEG_LAT
            lon2 = self.pts[-1][2] + step * math.sin(math.radians(h)) / (FT_PER_DEG_LAT * math.cos(math.radians(lat2)))
            s += step
            self.pts.append((s, lat2, lon2, h))

    def locate(self, station: float, offset: float = 0.0) -> tuple[float, float]:
        i = min(range(len(self.pts)), key=lambda k: abs(self.pts[k][0] - station))
        s, lat, lon, h = self.pts[i]
        d = station - s
        lat += (d * math.cos(math.radians(h)) - offset * math.sin(math.radians(h))) / FT_PER_DEG_LAT
        lon += (d * math.sin(math.radians(h)) + offset * math.cos(math.radians(h))) / (FT_PER_DEG_LAT * math.cos(math.radians(lat)))
        return round(lat, 6), round(lon, 6)


def generate(out: str | Path, seed: int = 7, n_pipelines: int = 6) -> dict:
    rng = random.Random(seed)
    out = Path(out)
    for sub in ("", "walkdowns", "projects", "geojson"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    T = {k: [] for k in ("pipeline", "pipe_segment", "point_feature", "class_location_range", "pressure_test", "structure")}
    key: dict[str, list] = {k: [] for k in (
        "missing_pressure_test", "non_tvc_records", "underrated_valve", "class_location_increase",
        "valve_spacing_exceeded", "low_cp", "shallow_cover", "exposed_pipe", "encroachment", "bom_discrepancy")}
    routes: dict[str, _Route] = {}
    spacing_violations_left = 2  # seed two 192.179 valve spacing violations across the system

    for idx, (pid, name, lat, lon, heading) in enumerate(REGIONS[:n_pipelines]):
        length = round(rng.uniform(4, 22) * 5280 / 100) * 100
        nps = rng.choice([8, 10, 12, 16, 20, 24])
        routes[pid] = route = _Route(rng, lat, lon, heading, length)
        T["pipeline"].append({"pipeline_id": pid, "name": name, "operator": "Synthetic Gas Transmission Co.",
                              "product": "Natural gas", "begin_station_ft": 0, "end_station_ft": length})

        # Structures: rural scatter plus a few development clusters, some built after the last class study.
        clusters = [(rng.uniform(0, length), rng.uniform(1500, 6000), rng.randint(20, 140)) for _ in range(rng.randint(1, 3))]
        sid = 0
        def add_structure(sta, kind="house", stories=1, units=1, year=None):
            nonlocal sid
            sid += 1
            off = rng.choice([-1, 1]) * rng.uniform(30, 650)
            la, lo = route.locate(sta, off)
            T["structure"].append({"structure_id": f"{pid}-B{sid:04d}", "pipeline_id": pid, "station_ft": round(sta),
                                   "offset_ft": round(off), "structure_type": kind, "stories": stories,
                                   "dwelling_units": units, "year_built": year or rng.randint(1950, 2012),
                                   "latitude": la, "longitude": lo})
        for _ in range(int(length / 5280 * rng.uniform(2, 6))):
            add_structure(rng.uniform(0, length))
        for center, spread, count in clusters:
            new_dev = idx % 3 == 0 and rng.random() < 0.7  # post-2015 development on every third line
            for _ in range(count):
                sta = min(length, max(0, rng.gauss(center, spread / 2)))
                apt = rng.random() < 0.08
                add_structure(sta, "apartment" if apt else "house", rng.choice([2, 3, 4]) if apt else rng.choice([1, 1, 2]),
                              rng.randint(4, 12) if apt else 1, rng.randint(2018, 2025) if new_dev else None)
        if rng.random() < 0.5:
            sta = rng.uniform(0, length)
            add_structure(sta, "identified_site", 1, 0)
            T["structure"][-1]["offset_ft"] = round(rng.uniform(60, 280))

        # Recorded class locations: the last study (2015) only saw structures built by then.
        tmp = PodsStore()
        tmp.conn.execute("INSERT INTO pipeline VALUES (?,?,?,?,?,?)", tuple(T["pipeline"][-1].values()))
        old = [s for s in T["structure"] if s["pipeline_id"] == pid and s["year_built"] <= 2015]
        cur = [s for s in T["structure"] if s["pipeline_id"] == pid]
        def study(structs):
            tmp.conn.execute("DELETE FROM structure")
            tmp.conn.executemany("INSERT INTO structure VALUES (?,?,?,?,?,?,?,?,?,?)", [tuple(s.values()) for s in structs])
            from .field import class_location_study
            return class_location_study(tmp, pid, step_ft=200)["study_ranges"]
        recorded = study(old)
        for r in recorded:
            T["class_location_range"].append({"pipeline_id": pid, "begin_station_ft": r["begin_station_ft"],
                                              "end_station_ft": r["end_station_ft"], "class_location": r["class_location"],
                                              "building_count": r["max_building_count"]})
        for now in study(cur):
            for r in recorded:
                lo, hi = max(now["begin_station_ft"], r["begin_station_ft"]), min(now["end_station_ft"], r["end_station_ft"])
                if hi > lo and now["class_location"] > r["class_location"]:
                    prev = key["class_location_increase"][-1] if key["class_location_increase"] else None
                    if (prev and prev["pipeline_id"] == pid and prev["end_station_ft"] == lo
                            and (prev["recorded"], prev["current"]) == (r["class_location"], now["class_location"])):
                        prev["end_station_ft"] = hi
                        continue
                    key["class_location_increase"].append({"pipeline_id": pid, "begin_station_ft": lo, "end_station_ft": hi,
                                                           "recorded": r["class_location"], "current": now["class_location"]})

        def max_class(a, b):
            return max(r["class_location"] for r in recorded if r["begin_station_ft"] < b and r["end_station_ft"] > a)

        def class_at(sta):
            return max(r["class_location"] for r in recorded if r["begin_station_ft"] <= sta <= r["end_station_ft"])

        # Pipe segments: an original vintage with later replacement sections.
        base_year = _vintage_year(rng)
        sta, n = 0.0, 0
        seg_maops = []
        while sta < length:
            n += 1
            seg_len = min(length - sta, round(rng.uniform(0.5, 3.0) * 5280 / 100) * 100)
            year = base_year if rng.random() < 0.6 else (_vintage_year(rng) or base_year)
            props = _pipe_props(rng, year, nps)
            seg_id = f"{pid}-S{n:02d}"
            end = sta + seg_len
            T["pipe_segment"].append({"segment_id": seg_id, "pipeline_id": pid, "begin_station_ft": sta, "end_station_ft": end,
                                      "nps": nps, "od_in": props["od_in"], "wall_in": props["wall_in"], "grade": props["grade"],
                                      "seam_type": props["seam_type"], "coating": props["coating"], "install_year": year,
                                      "heat_number": f"H-{rng.randint(10000, 99999)}" if props["mtr_on_file"] else None,
                                      "mtr_on_file": props["mtr_on_file"]})
            cls = max(r["class_location"] for r in recorded if r["begin_station_ft"] < end and r["end_station_ft"] > sta)
            dp = calcs.design_pressure(wall_in=props["wall_in"], class_location=cls, seam_type=props["seam_type"],
                                       grade=props["grade"], od_in=props["od_in"])["design_pressure_psig"]
            if props["tested"]:
                tp = round(dp * codes.TEST_FACTOR[cls] * rng.uniform(0.80, 1.0) / 5) * 5
                T["pressure_test"].append({"test_id": f"{seg_id}-PT", "pipeline_id": pid, "begin_station_ft": sta,
                                           "end_station_ft": end, "test_date": f"{(year or 1960) + rng.randint(0, 1)}-0{rng.randint(4, 9)}-1{rng.randint(0, 9)}",
                                           "test_pressure_psig": tp, "duration_hr": 8 if (year or 1960) >= 1970 else rng.choice([4, 8, 24]),
                                           "medium": "water" if (year or 1960) >= 1970 else rng.choice(["water", "air", "gas"])})
                seg_maops.append(min(dp, tp / codes.TEST_FACTOR[cls]))
            else:
                key["missing_pressure_test"].append({"pipeline_id": pid, "segment_id": seg_id, "install_year": year})
                seg_maops.append(dp)
            if not props["mtr_on_file"]:
                key["non_tvc_records"].append({"pipeline_id": pid, "segment_id": seg_id, "install_year": year})
            sta = end
        line_maop = min(seg_maops)

        # Valves per 192.179 spacing (two seeded spacing violations), CP test stations about every mile.
        vsta, vn = 0.0, 0
        while True:
            vn += 1
            rating_cls = next(c for c, p in ANSI_RATING_PSIG.items() if p >= line_maop and c >= 300)
            if vn == 2 and idx % 2 == 0 and line_maop > ANSI_RATING_PSIG[150]:  # seeded on every other line
                rating_cls = max(c for c, p in ANSI_RATING_PSIG.items() if p < line_maop)
                key["underrated_valve"].append({"pipeline_id": pid, "feature_id": f"{pid}-V{vn:02d}", "station_ft": round(vsta),
                                                "rating_psig": ANSI_RATING_PSIG[rating_cls], "line_maop_psig": round(line_maop, 1)})
            la, lo = route.locate(vsta)
            T["point_feature"].append({"feature_id": f"{pid}-V{vn:02d}", "pipeline_id": pid, "station_ft": round(vsta),
                                       "feature_type": "valve", "description": f"{nps}in mainline block valve, ANSI {rating_cls}",
                                       "rating_psig": ANSI_RATING_PSIG[rating_cls], "latitude": la, "longitude": lo})
            if vsta >= length:
                break
            limit_ft = VALVE_SPACING_MI[class_at(vsta)] * 5280
            gap = limit_ft * rng.uniform(0.6, 0.95)
            # Tighten the gap until it respects the strictest class it crosses.
            while (tighter := VALVE_SPACING_MI[max_class(vsta, vsta + gap)] * 5280) < limit_ft:
                limit_ft, gap = tighter, tighter * rng.uniform(0.6, 0.95)
            long_gap = min(length, limit_ft * 1.2)
            long_limit = VALVE_SPACING_MI[max_class(vsta, vsta + long_gap)]
            if vn == 1 and spacing_violations_left and long_gap > long_limit * 5280 * 1.05:
                spacing_violations_left -= 1
                gap = long_gap
                key["valve_spacing_exceeded"].append({"pipeline_id": pid, "from_station_ft": round(vsta),
                                                      "to_station_ft": round(vsta + gap), "limit_mi": long_limit})
            vsta = min(length, vsta + gap)
        for i, s in enumerate(range(2640, int(length), 5280)):
            la, lo = route.locate(s)
            T["point_feature"].append({"feature_id": f"{pid}-TP{i + 1:02d}", "pipeline_id": pid, "station_ft": s,
                                       "feature_type": "cp_test_station", "description": "CP test station", "rating_psig": None,
                                       "latitude": la, "longitude": lo})
        for i in range(rng.randint(1, 4)):
            s = round(rng.uniform(0, length))
            kind = rng.choice(["road_crossing", "water_crossing", "foreign_line_crossing"])
            la, lo = route.locate(s)
            T["point_feature"].append({"feature_id": f"{pid}-X{i + 1:02d}", "pipeline_id": pid, "station_ft": s,
                                       "feature_type": kind, "description": kind.replace("_", " "), "rating_psig": None,
                                       "latitude": la, "longitude": lo})

        # ROW walkdown with seeded findings.
        obs = []
        rock = set()
        for s in range(500, int(length), 500):
            is_rock = rng.random() < 0.05
            if is_rock:
                rock.add(s)
                obs.append((s, "rock", "", "Consolidated rock in trench wall"))
            req = codes.COVER_IN[class_at(s)][1 if is_rock else 0]
            if rng.random() < 0.015:
                doc = req - rng.randint(4, 16)
                key["shallow_cover"].append({"pipeline_id": pid, "station_ft": s, "measured_in": doc, "required_in": req})
                note = rng.choice(["Erosion", "Grading by landowner", "Ditch cleanout", "Farm terracing"])
            else:
                doc, note = req + rng.randint(2, 24), ""
            obs.append((s, "depth_of_cover_in", doc, note))
        for f in T["point_feature"]:
            if f["pipeline_id"] == pid and f["feature_type"] == "cp_test_station":
                if rng.random() < 0.1:
                    mv = rng.randint(-845, -700)
                    key["low_cp"].append({"pipeline_id": pid, "station_ft": f["station_ft"], "mv": mv})
                else:
                    mv = rng.randint(-1250, -880)
                obs.append((f["station_ft"], "cp_potential_mv", mv, f"{f['feature_id']} on reading"))
        for j, (kind, label) in enumerate((("exposed_pipe", "Pipe exposed in drainage"), ("encroachment", "Structure or fence on ROW"))):
            if (idx + j) % 2 == 0:
                s = round(rng.uniform(0, length))
                key[kind].append({"pipeline_id": pid, "station_ft": s})
                obs.append((s, kind, "", label))
        obs.sort(key=lambda o: o[0])
        with open(out / "walkdowns" / f"{pid}_walkdown.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["station_ft", "observation_type", "value", "notes"])
            w.writerows(obs)

    _write_projects(rng, out, T, key)
    # Drawings use their own random stream so the asset tables above stay the same for a given seed.
    key["drawing_discrepancies"] = generate_drawings(out, T, seed)
    for table, rows in T.items():
        _write_csv(out / f"{table}.csv", rows)
    _write_geojson(out / "geojson", T, routes)
    summary = {t: len(r) for t, r in T.items()}
    (out / "answer_key.json").write_text(json.dumps({"seed": seed, "counts": summary, "seeded_issues": key}, indent=2))
    return {"out": str(out), "counts": summary, "seeded_issue_counts": {k: len(v) for k, v in key.items()}}


def _write_projects(rng: random.Random, out: Path, T: dict, key: dict) -> None:
    """Two replacement projects, each with a design BOM and an as-built carrying seeded discrepancies."""
    for i, seg in enumerate(rng.sample(T["pipe_segment"], 2), start=1):
        proj = f"PRJ-{2026}{i:02d}"
        nps, length = int(seg["nps"]), int(seg["end_station_ft"] - seg["begin_station_ft"])
        spec = f"API 5L X60 {seg['wall_in']:.3f}w ERW FBE"
        design = [
            ("P-01", f"{nps}in line pipe", spec, length, "ft"),
            ("F-01", f"{nps}in 90 deg elbow 3R", f"MSS SP-75 WPHY-60 {seg['wall_in']:.3f}w", rng.randint(2, 8), "ea"),
            ("F-02", f"{nps}in 45 deg elbow", f"MSS SP-75 WPHY-60 {seg['wall_in']:.3f}w", rng.randint(1, 4), "ea"),
            ("V-01", f"{nps}in ball valve", "ANSI 600 RF API 6D", 1, "ea"),
            ("C-01", "Field joint coating", "Two-part epoxy", math.ceil(length / 40), "ea"),
            ("A-01", "Anode, magnesium 17 lb", "High potential Mg", rng.randint(4, 12), "ea"),
        ]
        built = [list(r) + [f"H-{rng.randint(10000, 99999)}", "yes"] for r in design]
        issues = []
        built[0][3] = round(length * rng.uniform(1.07, 1.15))
        issues.append({"item_id": "P-01", "type": "quantity_variance"})
        built[2][2] = built[2][2].replace("WPHY-60", "WPHY-42")
        built[2][5], built[2][6] = "", "no"
        issues += [{"item_id": "F-02", "type": "substitution"}, {"item_id": "F-02", "type": "traceability"}]
        if rng.random() < 0.5:
            built[3][2] = "ANSI 300 RF API 6D"
            issues.append({"item_id": "V-01", "type": "substitution"})
        built.pop(5)
        issues.append({"item_id": "A-01", "type": "not_installed"})
        built.append(["C-02", "Rock shield", "Polyethylene mesh", rng.randint(50, 400), "ft", "", ""])
        issues.append({"item_id": "C-02", "type": "unplanned_item"})
        hdr = ["item_id", "description", "spec", "quantity", "unit"]
        with open(out / "projects" / f"{proj}_bom_design.csv", "w", newline="") as fh:
            csv.writer(fh).writerows([hdr] + [list(r) for r in design])
        with open(out / "projects" / f"{proj}_asbuilt.csv", "w", newline="") as fh:
            csv.writer(fh).writerows([hdr + ["heat_number", "mtr_on_file"]] + built)
        key["bom_discrepancy"].append({"project": proj, "pipeline_id": seg["pipeline_id"], "segment_id": seg["segment_id"],
                                       "issues": issues})


def _write_csv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def _write_geojson(folder: Path, T: dict, routes: dict) -> None:
    lines = [{"type": "Feature", "properties": p,
              "geometry": {"type": "LineString", "coordinates": [[round(lo, 6), round(la, 6)] for _, la, lo, _ in routes[p["pipeline_id"]].pts]}}
             for p in T["pipeline"]]
    points = [{"type": "Feature", "properties": f, "geometry": {"type": "Point", "coordinates": [f["longitude"], f["latitude"]]}}
              for f in T["point_feature"]]
    structs = [{"type": "Feature", "properties": s, "geometry": {"type": "Point", "coordinates": [s["longitude"], s["latitude"]]}}
               for s in T["structure"]]
    for name, feats in (("pipelines", lines), ("point_features", points), ("structures", structs)):
        (folder / f"{name}.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a synthetic pipeline dataset")
    ap.add_argument("--out", default="data/synthetic")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--pipelines", type=int, default=6, choices=range(1, len(REGIONS) + 1))
    args = ap.parse_args()
    print(json.dumps(generate(args.out, args.seed, args.pipelines), indent=2))


if __name__ == "__main__":
    main()
