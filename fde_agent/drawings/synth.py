"""Synthetic drawing sources for the synthetic pipeline system, with planted drawing discrepancies.

Writes, under <out>/drawings/:

    edms/register.csv + edms/files/      document control: alignment sheets, valve details, crossing details
    vendor/submittal_log.csv + files/    vendor GA drawings and their review status
    gis/drawing_links.geojson            GIS features that cite a drawing number and revision
    shared_drive/<line>/...              uncontrolled copies with messy file names, some stale, some wrong

The controlled drawings agree with the asset tables, so every discrepancy comes from a planted issue. Each one is
listed in the answer key under "drawing_discrepancies". Called from fde_agent.synth.generate().
"""

from __future__ import annotations

import csv
import json
import math
import random
import re
from pathlib import Path

from .model import render_drawing

COMPANY = "Synthetic Gas Transmission Co."
SHEET_FT = 10_000
VENDORS = [("RVW", "Ridgeline Valve Works", "RB"), ("BFC", "Bluestem Flow Controls", "TB"),
           ("PVC", "Pecos Valve Co.", "FB")]  # fictional
ANSI_PSIG = {150: 285, 300: 740, 600: 1480, 900: 2220}
WALLS = [0.188, 0.219, 0.250, 0.281, 0.312, 0.344, 0.375, 0.406, 0.438, 0.500, 0.562, 0.625]


def generate_drawings(out: Path, T: dict, seed: int) -> list[dict]:
    rng = random.Random(f"drawings-{seed}")
    root = out / "drawings"
    for sub in ("edms/files", "vendor/files", "gis", "shared_drive"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    key: list[dict] = []
    docs: dict[str, dict] = {}     # controlled design drawings: doc_number -> {meta, revs: [{rev, status, date, desc, attrs}]}
    vendor: dict[str, dict] = {}   # vendor GA drawings, same shape
    gis: list[dict] = []
    po = 4500100

    def dates(n: int) -> list[str]:
        y, m = rng.randint(2014, 2019), rng.randint(1, 12)
        out_ = []
        for _ in range(n):
            out_.append(f"{y}-{m:02d}-{rng.randint(1, 28):02d}")
            m += rng.randint(2, 14)
            y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
        return out_

    def chain(attrs: dict, extra_max: int, perturb) -> list[dict]:
        """Revision history ending in the given (record-true) attributes. Older revisions may differ."""
        revs = ["A", "0"] + [str(i) for i in range(1, rng.randint(0, extra_max) + 1)]
        final = rng.random() < 0.65
        ds = dates(len(revs))
        out_ = []
        for i, r in enumerate(revs):
            last = i == len(revs) - 1
            desc = ("Issued for review" if r == "A" else "Issued for construction" if r == "0"
                    else "As-built per field survey" if last and final else f"Revised per FCR-{rng.randint(100, 999):04d}")
            status = ("As-Built" if final else "IFC") if last else "Superseded"
            a = json.loads(json.dumps(attrs))
            if not last and rng.random() < 0.5:
                perturb(a)
            out_.append({"rev": r, "status": status, "date": ds[i], "description": desc, "attrs": a})
        return out_

    def bump_wall(a):  # next standard wall up
        tag = rng.choice(list(a))
        a[tag]["wall_in"] = next((w for w in WALLS if w > a[tag]["wall_in"]), round(a[tag]["wall_in"] + 0.062, 3))

    def bump_station(a):
        for v in a.values():
            v["station_ft"] += rng.choice([-40, 25, 60])

    segs = {p["pipeline_id"]: [s for s in T["pipe_segment"] if s["pipeline_id"] == p["pipeline_id"]] for p in T["pipeline"]}
    for p in T["pipeline"]:
        pid, length = p["pipeline_id"], p["end_station_ft"]
        # Alignment sheets, one per 10,000 ft.
        for i in range(math.ceil(length / SHEET_FT)):
            a, b = i * SHEET_FT, min(length, (i + 1) * SHEET_FT)
            attrs = {s["segment_id"]: {k: s[k] for k in ("begin_station_ft", "end_station_ft", "nps", "wall_in", "grade",
                                                         "seam_type", "coating")}
                     for s in segs[pid] if s["begin_station_ft"] < b and s["end_station_ft"] > a}
            doc = f"{pid}-AL-{i + 1:03d}"
            docs[doc] = {"pipeline_id": pid, "doc_type": "alignment_sheet", "a": a, "b": b,
                         "title": f"{p['name']} alignment sheet STA {_sta(a)} to {_sta(b)}",
                         "revs": chain(attrs, 2, bump_wall), "refs": []}
            for s in attrs:
                f = next(x for x in segs[pid] if x["segment_id"] == s)
                gis.append({"feature_id": s, "pipeline_id": pid, "station_ft": f["begin_station_ft"],
                            "feature_type": "pipe_segment", "drawing_number": doc, "geometry": None})
        # Valve details, each citing a vendor GA drawing.
        for f in (x for x in T["point_feature"] if x["pipeline_id"] == pid):
            ftype, fid = f["feature_type"], f["feature_id"]
            if ftype == "valve":
                n = int(re.search(r"V(\d+)", fid).group(1))
                cls = int(re.search(r"ANSI (\d+)", f["description"]).group(1))
                code, vname, style = rng.choice(VENDORS)
                vdoc = f"{code}-{rng.randint(1000, 9999)}-GA-{rng.randint(1, 20):02d}"
                while vdoc in vendor:
                    vdoc = f"{code}-{rng.randint(1000, 9999)}-GA-{rng.randint(1, 20):02d}"
                part = f"{code}-{style}{int(segs[pid][0]['nps'])}{cls // 100}-{rng.randint(10000, 99999)}"
                po += rng.randint(3, 40)
                vattrs = {"valve": {"part_number": part, "model": f"{style}-{cls}", "nps": int(segs[pid][0]["nps"]),
                                    "ansi_class": cls, "rating_psig": ANSI_PSIG[cls], "end_connection": "RF",
                                    "body": "ASTM A216 WCB"}}
                vrevs = [{"rev": "A", "status": "Approved", "comments": ""}]
                if rng.random() < 0.5:
                    vrevs = [{"rev": "A", "status": "Resubmit", "comments": "Add NDE map and hydrotest procedure."},
                             {"rev": "B", "status": rng.choice(["Approved", "Approved as Noted"]), "comments": ""}]
                vds = dates(len(vrevs))
                vendor[vdoc] = {"pipeline_id": pid, "vendor": vname, "po": str(po), "tag": fid,
                                "revs": [dict(r, date=d, attrs=json.loads(json.dumps(vattrs))) for r, d in zip(vrevs, vds)]}
                attrs = {"valve": {"tag": fid, "station_ft": f["station_ft"], "nps": vattrs["valve"]["nps"],
                                   "ansi_class": cls, "rating_psig": f["rating_psig"], "part_number": part,
                                   "end_connection": "RF"}}
                doc = f"{pid}-VD-{n:02d}"
                docs[doc] = {"pipeline_id": pid, "doc_type": "valve_detail", "a": f["station_ft"], "b": f["station_ft"],
                             "title": f"{fid} mainline block valve installation detail",
                             "revs": chain(attrs, 1, bump_station), "refs": [[vdoc, vrevs[-1]["rev"]]]}
            elif "crossing" in ftype:
                n = int(re.search(r"X(\d+)", fid).group(1))
                attrs = {"crossing": {"tag": fid, "station_ft": f["station_ft"], "crossing_type": ftype,
                                      "method": rng.choice(["open cut", "bored, cased", "HDD"])}}
                doc = f"{pid}-XD-{n:02d}"
                docs[doc] = {"pipeline_id": pid, "doc_type": "crossing_detail", "a": f["station_ft"], "b": f["station_ft"],
                             "title": f"{f['description'].title()} at STA {_sta(f['station_ft'])}",
                             "revs": chain(attrs, 1, bump_station), "refs": []}
            else:
                continue
            gis.append({"feature_id": fid, "pipeline_id": pid, "station_ft": f["station_ft"], "feature_type": ftype,
                        "drawing_number": doc, "geometry": {"type": "Point", "coordinates": [f["longitude"], f["latitude"]]}})

    for g in gis:
        g["drawing_rev"] = docs[g["drawing_number"]]["revs"][-1]["rev"]

    # ---- planted discrepancies in the controlled sources (applied before any copies are made) -------------------
    planted: set[str] = set()

    def pick(cands, k=1):
        cands = sorted(d for d in cands if d not in planted)
        got = rng.sample(cands, k)
        planted.update(got)
        return got

    def add(kind, doc, detail):
        key.append({"type": kind, "doc_number": doc, "pipeline_id": docs.get(doc, {}).get("pipeline_id")
                    or next((g["pipeline_id"] for g in gis if g["drawing_number"] == doc), None), "detail": detail})

    multi = [d for d, v in docs.items() if len([r for r in v["revs"] if r["rev"].isdigit()]) >= 3]
    for d in pick(multi):  # a numbered revision never made it into document control
        gone = docs[d]["revs"][-2]["rev"]
        docs[d]["revs"].pop(-2)
        add("revision_gap", d, f"Rev {gone} missing from the EDMS register")
    vds = [d for d, v in docs.items() if v["doc_type"] == "valve_detail"]
    for d in pick(vds, 2):  # part number on the design drawing does not match the vendor drawing
        v = docs[d]["revs"][-1]["attrs"]["valve"]
        v["part_number"] = v["part_number"][:-1] + str((int(v["part_number"][-1]) + 1) % 10)
        add("part_number_mismatch", d, "Design part number differs in the last digit from the vendor GA")
    for d in pick(vds):  # vendor drew a different pressure class than the design calls for
        va = vendor[docs[d]["refs"][0][0]]["revs"][-1]["attrs"]["valve"]
        designed = va["ansi_class"]
        va["ansi_class"] = 300 if designed != 300 else 600
        va["rating_psig"] = ANSI_PSIG[va["ansi_class"]]
        add("vendor_rating_mismatch", d, f"Vendor GA shows ANSI {va['ansi_class']}; design calls for ANSI {designed}")
    for d in pick(vds):  # latest vendor submittal was sent back
        vr = vendor[docs[d]["refs"][0][0]]["revs"][-1]
        vr["status"], vr["comments"] = "Rejected", "Face-to-face dimension does not match station piping; resubmit."
        add("vendor_not_approved", d, "Cited vendor drawing revision is Rejected")
    als = [d for d, v in docs.items() if v["doc_type"] == "alignment_sheet"]
    for d in pick(als):  # as-built sheet disagrees with PODS
        bump_wall(docs[d]["revs"][-1]["attrs"])
        add("record_mismatch", d, "As-built wall thickness differs from the pipe segment record")
    for d in pick(vds):
        bump_station(docs[d]["revs"][-1]["attrs"])
        add("record_mismatch", d, "Valve station on the drawing differs from the asset record")
    tb_edms = pick([d for d in docs if len(docs[d]["revs"]) >= 2])[0]
    add("title_block_mismatch", tb_edms, "EDMS register revision differs from the title block on the attached file")
    multi_gis = [g for g in gis if len(docs[g["drawing_number"]]["revs"]) >= 2 and g["drawing_number"] not in planted]
    for g in rng.sample(sorted(multi_gis, key=lambda g: g["feature_id"]), 2):
        g["drawing_rev"] = docs[g["drawing_number"]]["revs"][-2]["rev"]
        if not any(k["type"] == "superseded_reference" and k["doc_number"] == g["drawing_number"] for k in key):
            add("superseded_reference", g["drawing_number"], f"GIS feature {g['feature_id']} cites superseded rev {g['drawing_rev']}")
        planted.add(g["drawing_number"])
    xg = sorted((g for g in gis if g["feature_type"].endswith("crossing") and g["drawing_number"] not in planted),
                key=lambda g: g["feature_id"])
    if xg:
        g = rng.choice(xg)
        g["drawing_number"] = g["drawing_number"][:-2] + "9" + g["drawing_number"][-1]  # typo: XD-01 -> XD-91
        add("broken_reference", g["drawing_number"], f"GIS feature {g['feature_id']} cites a drawing number that does not exist")

    # ---- write controlled sources -------------------------------------------------------------------------------
    reg = []
    for d, v in sorted(docs.items()):
        for i, r in enumerate(v["revs"]):
            fname = f"files/{d}_R{r['rev']}.txt"
            tb_rev = v["revs"][i - 1]["rev"] if d == tb_edms and i == len(v["revs"]) - 1 else r["rev"]
            text = _drawing(d, v, r, tb_rev)
            (root / "edms" / fname).write_text(text)
            r["text"] = text
            reg.append({"doc_number": d, "rev": r["rev"], "title": v["title"], "doc_type": v["doc_type"],
                        "status": r["status"], "rev_date": r["date"], "pipeline_id": v["pipeline_id"],
                        "begin_station_ft": v["a"], "end_station_ft": v["b"], "file": fname})
    _csv(root / "edms" / "register.csv", reg)
    log = []
    for d, v in sorted(vendor.items()):
        for r in v["revs"]:
            fname = f"files/{d}_{r['rev']}.txt"
            tb = {"title": f"{v['vendor']} ball valve general arrangement", "doc_number": d, "rev": r["rev"],
                  "rev_date": r["date"], "status": r["status"], "vendor": v["vendor"], "po_number": v["po"], "tag": v["tag"]}
            (root / "vendor" / fname).write_text(render_drawing(v["vendor"], tb, r["attrs"], [
                {"rev": x["rev"], "date": x["date"], "description": "Submitted for review"} for x in v["revs"]
                if x["rev"] <= r["rev"]]))
            log.append({"submittal_id": f"SUB-{v['po']}-{r['rev']}", "vendor": v["vendor"], "vendor_doc_number": d,
                        "vendor_rev": r["rev"], "po_number": v["po"], "tag": v["tag"], "pipeline_id": v["pipeline_id"],
                        "status": r["status"], "reviewed_date": r["date"], "review_comments": r["comments"], "file": fname})
    _csv(root / "vendor" / "submittal_log.csv", log)
    (root / "gis" / "drawing_links.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": g["geometry"], "properties": {k: v for k, v in g.items() if k != "geometry"}}
        for g in gis]}, indent=1))

    # ---- shared drive: field copies, stale copies, and planted bad copies ----------------------------------------
    names = {p["pipeline_id"]: p["name"] for p in T["pipeline"]}
    styles = [lambda d, r: f"{d} Rev {r}.txt", lambda d, r: f"{d.replace('-', '')}_R{r}.txt",
              lambda d, r: f"Copy of {d}_Rev{r} FINAL.txt", lambda d, r: f"{d.lower()} rev {r} (field set).txt"]

    def drop(d, sub, fname, text):
        folder = root / "shared_drive" / f"{docs[d]['pipeline_id']} {names[docs[d]['pipeline_id']]}" / sub
        folder.mkdir(parents=True, exist_ok=True)
        (folder / fname).write_text(text)

    copied: set[str] = set()
    for d in sorted(set(docs) - planted):
        revs = docs[d]["revs"]
        if rng.random() < 0.35:
            copied.add(d)
            drop(d, "Field Packages", rng.choice(styles)(d, revs[-1]["rev"]), revs[-1]["text"])
        if len(revs) >= 2 and rng.random() < 0.3:
            old = rng.choice(revs[:-1])
            drop(d, "Old", rng.choice(styles)(d, old["rev"]), old["text"])
            add("stale_copy", d, f"Shared drive copy of superseded rev {old['rev']}")
    free = sorted(set(docs) - planted - copied - {k["doc_number"] for k in key})
    d = rng.choice([x for x in free if docs[x]["doc_type"] == "alignment_sheet"])
    planted.add(d)
    v, last = docs[d], docs[d]["revs"][-1]
    nxt = str(int(last["rev"]) + 1)
    a = json.loads(json.dumps(last["attrs"]))
    bump_wall(a)
    drop(d, "Redlines", f"{d} Rev {nxt} redline.txt",
         _drawing(d, v, new := {**last, "rev": nxt, "status": "As-Built", "attrs": a, "description": "Field redline"}, nxt,
                  v["revs"] + [new]))
    add("uncontrolled_revision", d, f"Rev {nxt} redline on the shared drive was never checked into document control")
    for d in rng.sample([x for x in free if x not in planted], 2):
        planted.add(d)
        last = docs[d]["revs"][-1]
        a = json.loads(json.dumps(last["attrs"]))
        (bump_wall if docs[d]["doc_type"] == "alignment_sheet" else bump_station)(a)
        drop(d, "Field Packages", styles[0](d, last["rev"]), _drawing(d, docs[d], {**last, "attrs": a}, last["rev"], docs[d]["revs"]))
        add("conflicting_copy", d, f"Shared drive rev {last['rev']} content differs from the controlled rev {last['rev']}")
    d = rng.choice([x for x in free if x not in planted and len(docs[x]["revs"]) >= 2])
    drop(d, "Field Packages", styles[0](d, docs[d]["revs"][-2]["rev"]), docs[d]["revs"][-1]["text"])
    add("title_block_mismatch", d, "File name says the previous revision; the title block is the current revision")
    return key


def _drawing(doc: str, v: dict, r: dict, tb_rev: str, hist: list | None = None) -> str:
    if hist is None:  # revision block lists every revision up to this one
        hist = v["revs"][:next(i for i, x in enumerate(v["revs"]) if x is r) + 1]
    tb = {"title": v["title"], "doc_number": doc, "rev": tb_rev, "rev_date": r["date"], "status": r["status"],
          "pipeline_id": v["pipeline_id"], "stations": f"{v['a']:g}-{v['b']:g}",
          "references": [{"doc_number": x, "rev": y} for x, y in v["refs"]]}
    return render_drawing(COMPANY, tb, r["attrs"], [{"rev": x["rev"], "date": x["date"], "description": x["description"]}
                                                     for x in hist])


def _sta(ft: float) -> str:
    return f"{int(ft) // 100}+{int(ft) % 100:02d}"


def _csv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
