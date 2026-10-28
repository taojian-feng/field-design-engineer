"""Cross-checks between drawings: revision control, design vs vendor, and drawings vs asset records.

Each check returns findings with the same shape, so the agent can sort them, count them and write them up:
{type, severity, doc_number, rev, source, location, detail, action}.
"""

from __future__ import annotations

from .index import DrawingIndex, Family
from .model import DrawingRecord, doc_key, norm_rev, rev_gaps, rev_order

# type: (severity, what the FDE should do about it)
CHECKS = {
    "conflicting_copy": ("high", "Two copies carry the same revision but different content. Confirm which is right "
                                 "with document control and pull the other copy from circulation."),
    "uncontrolled_revision": ("high", "A revision exists outside document control. Find out who issued it, then "
                                      "check it in (or void it) before anyone builds or operates from it."),
    "superseded_reference": ("high", "This record points to an old revision. Update the link to the current "
                                     "revision and check whether work was done to the old one."),
    "vendor_rating_mismatch": ("high", "The vendor drawing and the design disagree on pressure class. Hold "
                                       "installation until engineering resolves it with the vendor."),
    "vendor_not_approved": ("high", "The design relies on a vendor drawing that is not approved. Chase the "
                                    "resubmittal before fabrication or installation."),
    "record_mismatch": ("high", "The current drawing disagrees with the asset record (PODS/GIS). Confirm with "
                                "field data or MTRs which is right, then correct the other through an approved change."),
    "part_number_mismatch": ("medium", "The part number on the design does not match the vendor drawing. Confirm "
                                       "the ordered part with procurement and revise whichever is wrong."),
    "broken_reference": ("medium", "This record cites a drawing that no source has. Find the right document "
                                   "number (often a typo) and fix the link."),
    "title_block_mismatch": ("medium", "The title block disagrees with the register or file name. Correct the "
                                       "metadata so searches return the right revision."),
    "revision_gap": ("medium", "A revision is missing from the controlled history. Ask document control whether "
                               "it was skipped, voided, or never checked in."),
    "stale_copy": ("info", "An old revision is sitting outside document control. Archive or delete it so nobody "
                           "picks it up by mistake."),
}

VENDOR_FIELDS = {"ansi_class": "vendor_rating_mismatch", "rating_psig": "vendor_rating_mismatch",
                 "part_number": "part_number_mismatch"}
SEGMENT_FIELDS = ("begin_station_ft", "end_station_ft", "nps", "wall_in", "grade", "seam_type")
FEATURE_FIELDS = ("station_ft", "rating_psig")
STATION_TOL_FT = 10.0
APPROVED = {"approved", "approved as noted"}


def _finding(kind: str, rec, detail: str, doc_number: str | None = None) -> dict:
    sev, action = CHECKS[kind]
    return {"type": kind, "severity": sev, "doc_number": doc_number or rec.doc_number,
            "rev": getattr(rec, "rev", None), "source": rec.source, "location": rec.location,
            "detail": detail, "action": action}


def verify(index: DrawingIndex, pipeline_id: str | None = None, doc_number: str | None = None,
           begin_station_ft: float | None = None, end_station_ft: float | None = None,
           include_info: bool = True) -> dict:
    if doc_number:
        fam = index.family(doc_number)
        fams = [fam] if fam else []
    elif pipeline_id or begin_station_ft is not None:
        keys = {h["doc_number"] for h in index.search(pipeline_id=pipeline_id, begin_station_ft=begin_station_ft,
                                                      end_station_ft=end_station_ft, limit=10**6)}
        fams = [f for f in index.families.values() if f.doc_number in keys]
    else:
        fams = list(index.families.values())

    findings: list[dict] = []
    for fam in fams:
        findings += check_revisions(fam)
        findings += check_copies(fam)
        findings += check_references(index, fam)
        findings += check_vendor(index, fam)
        if index.store is not None:
            findings += check_records(index.store, fam)
    if not doc_number:
        findings += [_finding("broken_reference", r, f"{r.source} feature {r.feature_id or ''} cites {r.doc_number}"
                              f"{' rev ' + r.rev if r.rev else ''}, which no source has.", r.doc_number)
                     for r in index.dangling
                     if (not pipeline_id or r.pipeline_id == pipeline_id)
                     and (begin_station_ft is None or r.station_ft is None or
                          begin_station_ft <= r.station_ft <= (end_station_ft if end_station_ft is not None else begin_station_ft))]
    if not include_info:
        findings = [f for f in findings if f["severity"] != "info"]
    order = {"high": 0, "medium": 1, "info": 2}
    findings.sort(key=lambda f: (order[f["severity"]], f["doc_number"], f["type"]))
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["type"]] = counts.get(f["type"], 0) + 1
    return {"drawings_checked": len(fams), "finding_counts": counts, "findings": findings}


def check_revisions(fam: Family) -> list[dict]:
    out = []
    if fam.controlled and (gaps := rev_gaps([r.rev for r in fam.controlled])):
        cur = fam.current
        out.append(_finding("revision_gap", cur, f"Controlled revisions {[r.rev for r in fam.controlled]} skip "
                                                  f"{gaps}.", fam.doc_number))
    for r in fam.all():
        declared_doc = r.meta.get("filename_doc") if "filename" in r.meta else r.doc_number
        declared_rev = r.meta.get("filename_rev") if "filename" in r.meta else r.rev
        tb_doc, tb_rev = r.title_block.get("doc_number"), r.title_block.get("rev")
        bad = []
        if declared_doc and tb_doc and doc_key(declared_doc) != doc_key(tb_doc):
            bad.append(f"document number {declared_doc} vs {tb_doc} in the title block")
        if declared_rev and tb_rev and norm_rev(declared_rev) != norm_rev(tb_rev):
            bad.append(f"rev {declared_rev} vs rev {tb_rev} in the title block")
        if bad:
            where = "file name" if "filename" in r.meta else f"{r.source} register"
            out.append(_finding("title_block_mismatch", r, f"{where} says " + "; ".join(bad) + ".", fam.doc_number))
    return out


def check_copies(fam: Family) -> list[dict]:
    cur = fam.current
    if cur is None or not fam.controlled:
        return []
    out = []
    for c in fam.copies:
        if rev_order(c.rev) > rev_order(cur.rev):
            out.append(_finding("uncontrolled_revision", c, f"Rev {c.rev} found in {c.source}; the controlled current "
                                                            f"revision is {cur.rev}." + _diff_note(cur, c), fam.doc_number))
        elif c.rev == cur.rev and c.fingerprint != cur.fingerprint:
            out.append(_finding("conflicting_copy", c, f"Same rev {c.rev} as {cur.location}, different content."
                                + _diff_note(cur, c), fam.doc_number))
        elif rev_order(c.rev) < rev_order(cur.rev):
            out.append(_finding("stale_copy", c, f"Rev {c.rev} copy; current is rev {cur.rev}.", fam.doc_number))
    return out


def check_references(index: DrawingIndex, fam: Family) -> list[dict]:
    cur = fam.current
    out = []
    for ref in fam.cited_by:
        if ref.rev and rev_order(ref.rev) < rev_order(cur.rev):
            out.append(_finding("superseded_reference", ref, f"{ref.source} {ref.feature_id or ref.location} cites rev "
                                                              f"{ref.rev}; current is rev {cur.rev} ({cur.status}).",
                                fam.doc_number))
    return out


def check_vendor(index: DrawingIndex, fam: Family) -> list[dict]:
    """Design drawing vs the vendor drawings it cites: approval status, pressure class, part number."""
    cur = fam.current
    if cur is None or cur.doc_type.startswith("vendor"):
        return []
    out = []
    for ref in cur.references:
        vfam = index.family(ref["doc_number"])
        if vfam is None or not vfam.current.doc_type.startswith("vendor"):
            continue
        v = vfam.revision(ref["rev"]) if ref.get("rev") else vfam.current
        if v is None:
            continue
        if v.status.lower() not in APPROVED:
            note = v.meta.get("review_comments") or ""
            out.append(_finding("vendor_not_approved", cur, f"Cites {v.doc_number} rev {v.rev}, which is "
                                                            f"'{v.status}' in the submittal log. {note}".strip(),
                                fam.doc_number))
        mine = _merged(cur.attributes)
        theirs = _merged(v.attributes)
        diffs: dict[str, list[str]] = {}
        for k, kind in VENDOR_FIELDS.items():
            if k in mine and k in theirs and not _same(k, mine[k], theirs[k]):
                diffs.setdefault(kind, []).append(f"{k}: design {mine[k]}, vendor {theirs[k]}")
        out += [_finding(kind, cur, f"Against {v.doc_number} rev {v.rev}: " + "; ".join(d) + ".", fam.doc_number)
                for kind, d in diffs.items()]
    return out


def check_records(store, fam: Family) -> list[dict]:
    """Current controlled drawing vs PODS asset records (segments by id, point features by tag)."""
    cur = fam.current
    if cur is None or not cur.controlled or cur.doc_type.startswith("vendor"):
        return []
    out = []
    for tag, attrs in cur.attributes.items():
        seg = _row(store, "pipe_segment", "segment_id", tag)
        if seg:
            pairs = [(k, attrs[k], seg[k]) for k in SEGMENT_FIELDS if k in attrs]
        elif (feat := _row(store, "point_feature", "feature_id", attrs.get("tag", tag))):
            pairs = [(k, attrs[k], feat[k]) for k in FEATURE_FIELDS if k in attrs and feat[k] is not None]
        else:
            continue
        diffs = [f"{k}: drawing {a}, record {b}" for k, a, b in pairs if not _same(k, a, b)]
        if diffs:
            out.append(_finding("record_mismatch", cur, f"{tag}: " + "; ".join(diffs) + ".", fam.doc_number))
    return out


def compare(index: DrawingIndex, doc_a: str, rev_a: str | None = None, doc_b: str | None = None,
            rev_b: str | None = None, source_b: str | None = None) -> dict:
    """Attribute-level diff between two drawings, two revisions, or a copy and the controlled revision."""
    fa = index.family(doc_a)
    fb = index.family(doc_b or doc_a)
    if fa is None or fb is None:
        return {"error": f"{doc_a if fa is None else doc_b} not found"}
    a = fa.revision(rev_a) if rev_a else fa.current
    if rev_b is None and source_b is None:
        b = fb.current
    else:
        pool = [r for r in fb.all() if (not rev_b or r.rev == norm_rev(rev_b)) and (not source_b or r.source == source_b)]
        b = pool[0] if pool else None
    if a is None or b is None:
        return {"error": "revision not found", "available": [(r.source, r.rev) for r in fb.all()]}
    rows, one_sided = [], []
    for tag in sorted(set(a.attributes) | set(b.attributes)):
        x, y = a.attributes.get(tag, {}), b.attributes.get(tag, {})
        for k in sorted(set(x) | set(y)):
            if k in x and k in y and not _same(k, x[k], y[k]):
                rows.append({"item": tag, "field": k, "a": x[k], "b": y[k]})
            elif (k in x) != (k in y):
                one_sided.append({"item": tag, "field": k, "only_in": "a" if k in x else "b", "value": x.get(k, y.get(k))})
    side = lambda r: {"doc_number": r.doc_number, "rev": r.rev, "status": r.status, "source": r.source,
                      "location": r.location}
    return {"a": side(a), "b": side(b), "identical": not rows and not one_sided, "differences": rows,
            "fields_on_one_side_only": one_sided}


def _merged(attributes: dict) -> dict:
    out: dict = {}
    for vals in attributes.values():
        out.update(vals)
    return out


def _row(store, table: str, key: str, value) -> dict | None:
    cur = store.conn.execute(f"SELECT * FROM {table} WHERE {key}=?", (str(value),))
    row = cur.fetchone()
    return dict(zip([d[0] for d in cur.description], row)) if row else None


def _same(k: str, a, b) -> bool:
    try:
        tol = STATION_TOL_FT if "station" in k else 1e-6
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return str(a).strip().upper() == str(b).strip().upper()


def _diff_note(a: DrawingRecord, b: DrawingRecord) -> str:
    diffs = []
    for tag in sorted(set(a.attributes) | set(b.attributes)):
        x, y = a.attributes.get(tag, {}), b.attributes.get(tag, {})
        diffs += [f"{tag} {k}: {x.get(k)} -> {y.get(k)}" for k in sorted(set(x) | set(y)) if not _same(k, x.get(k), y.get(k))]
    return (" Differences: " + "; ".join(diffs[:5]) + ".") if diffs else ""


def field_package(index: DrawingIndex, pipeline_id: str, begin_station_ft: float, end_station_ft: float) -> dict:
    """The drawing set a crew needs for a station range: the current revision of every drawing, the vendor drawings
    they rely on, the open issues that block issuing the package, and the stray copies to ignore."""
    hits = index.search(pipeline_id=pipeline_id, begin_station_ft=begin_station_ft, end_station_ft=end_station_ft,
                        limit=10**6)
    issues = verify(index, pipeline_id, begin_station_ft=begin_station_ft, end_station_ft=end_station_ft)["findings"]
    drawings, vendor_docs, ignore = [], {}, []
    for h in hits:
        fam = index.family(h["doc_number"])
        cur = fam.current
        drawings.append({"doc_number": h["doc_number"], "rev": cur.rev, "status": cur.status, "title": cur.title,
                         "open_location": cur.location,
                         "issues": [f["type"] for f in issues if f["doc_number"] == h["doc_number"] and f["severity"] != "info"]})
        for ref in cur.references:
            vfam = index.family(ref["doc_number"])
            v = (vfam.revision(ref["rev"]) if ref.get("rev") else vfam.current) if vfam else None
            vendor_docs[ref["doc_number"]] = ({"rev": v.rev, "submittal_status": v.status, "open_location": v.location}
                                              if v else {"error": "not found in any source"})
        ignore += [c["location"] for c in h["copies"] if c["source"] != cur.source and c["state"] != "current"]
    blocking = [f for f in issues if f["severity"] == "high"]
    return {"pipeline_id": pipeline_id, "stations_ft": [begin_station_ft, end_station_ft],
            "ready_to_issue": not blocking, "drawings": drawings, "vendor_drawings": vendor_docs,
            "blocking_issues": blocking, "other_issues": [f for f in issues if f["severity"] == "medium"],
            "do_not_use": ignore}
