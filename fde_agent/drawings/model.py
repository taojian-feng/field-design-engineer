"""Drawing records, document-number and revision normalization, and title block parsing.

Every source adapter turns whatever it reads (an EDMS register row, a vendor submittal, a file on a shared drive)
into a DrawingRecord, and every place that cites a drawing (a GIS feature, another drawing) into a DrawingRef. The
index and the checks only ever see these two types, so a new source only has to produce them.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field

# Status words that mean a revision is no longer the one to build or operate from.
DEAD_STATUSES = {"superseded", "void", "cancelled", "obsolete"}

DOC_TYPES = {
    "AL": "alignment_sheet", "VD": "valve_detail", "XD": "crossing_detail", "PID": "p&id",
    "GA": "vendor_ga", "DS": "vendor_datasheet",
}


def doc_key(doc_number: str) -> str:
    """Match key for a document number: 'SYN-101-AL-001', 'syn101_al_1' and 'SYN 101-AL-01' all agree."""
    parts = re.findall(r"[A-Z]+|\d+", doc_number.upper())
    return "".join((p.lstrip("0") or "0") if p.isdigit() else p for p in parts)


def norm_rev(rev) -> str:
    """'Rev B', 'R1', 'rev.0', ' b ' -> 'B', '1', '0', 'B'."""
    s = str(rev).strip().upper()
    s = re.sub(r"^(REVISION|REV)[\s._-]*", "", s)
    s = re.sub(r"^R(?=\d)", "", s)
    return (s.lstrip("0") or "0") if s.isdigit() else s


def rev_order(rev) -> tuple:
    """Lettered review revisions (A, B, ...) come before numbered issued revisions (0, 1, ...)."""
    r = norm_rev(rev)
    return (1, int(r), "") if r.isdigit() else (0, 0, r)


def rev_gaps(revs: list[str]) -> list[str]:
    """Revisions missing from an otherwise consecutive sequence, e.g. ['0', '1', '3'] -> ['2']."""
    nums = sorted(int(r) for r in map(norm_rev, revs) if r.isdigit())
    lets = sorted(ord(r) for r in map(norm_rev, revs) if len(r) == 1 and r.isalpha())
    missing = [str(n) for a, b in zip(nums, nums[1:]) for n in range(a + 1, b)]
    return [chr(c) for a, b in zip(lets, lets[1:]) for c in range(a + 1, b)] + missing


@dataclass
class DrawingRecord:
    """One copy of one revision of a document, as found in one source."""
    source: str                  # adapter name, e.g. "edms", "vendor", "shared_drive"
    location: str                # path or URI where an engineer can open it
    doc_number: str
    rev: str
    title: str = ""
    doc_type: str = ""
    status: str = ""             # IFR, IFC, As-Built, Superseded, Approved, Rejected, ...
    rev_date: str = ""
    pipeline_id: str | None = None
    begin_station_ft: float | None = None
    end_station_ft: float | None = None
    attributes: dict = field(default_factory=dict)   # tag -> {field: value}, from the drawing's data block
    references: list = field(default_factory=list)   # [{"doc_number":..., "rev":...}] cited by this drawing
    title_block: dict = field(default_factory=dict)  # what the drawing itself says (doc number, rev, ...)
    controlled: bool = False     # True when the source is the document-control system of record
    meta: dict = field(default_factory=dict)         # source-specific extras (submittal status, filename, ...)

    def __post_init__(self):
        self.rev = norm_rev(self.rev)

    @property
    def key(self) -> str:
        return doc_key(self.doc_number)

    @property
    def fingerprint(self) -> str:
        """Hash of the engineering content, so two copies of the same revision can be compared."""
        return hashlib.sha256(json.dumps(self.attributes, sort_keys=True).encode()).hexdigest()[:12]

    def summary(self) -> dict:
        d = asdict(self)
        for k in ("attributes", "references", "title_block", "meta"):
            d.pop(k)
        return d


@dataclass
class DrawingRef:
    """A place that cites a drawing: a GIS feature, a field package, another drawing."""
    source: str
    location: str
    doc_number: str
    rev: str | None = None
    pipeline_id: str | None = None
    feature_id: str | None = None
    station_ft: float | None = None

    @property
    def key(self) -> str:
        return doc_key(self.doc_number)


# ---- title block parsing ----------------------------------------------------------------------------------------

_TB_FIELDS = {"DRAWING NO": "doc_number", "DOC NO": "doc_number", "REV": "rev", "TITLE": "title", "DATE": "rev_date",
              "STATUS": "status", "PIPELINE": "pipeline_id", "STATIONS": "stations", "REFERENCES": "references",
              "VENDOR": "vendor", "PO": "po_number", "TAG": "tag"}


def parse_drawing_text(text: str) -> dict:
    """Parse the text layer of a drawing (title block, DATA block, revision block).

    The synthetic drawings are plain text laid out like the text you get from a PDF title block or a CAD attribute
    export. For scanned drawings, put an OCR or vision extractor in front of this that returns the same keys.
    """
    out: dict = {"title_block": {}, "attributes": {}, "revisions": [], "references": []}
    block = "title"
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("--"):
            block = "data" if "DATA" in line.upper() else "revisions" if "REVISION" in line.upper() else block
            continue
        if block == "title" and ":" in line:
            k, v = (s.strip() for s in line.split(":", 1))
            name = _TB_FIELDS.get(k.upper())
            if name == "references":
                out["references"] = [_split_ref(r) for r in v.split(",") if r.strip()]
            elif name:
                out["title_block"][name] = v
        elif block == "data" and ":" in line:
            tag, kv = (s.strip() for s in line.split(":", 1))
            out["attributes"][tag] = {k.strip(): _num(v.strip()) for k, v in
                                      (p.split("=", 1) for p in kv.split(";") if "=" in p)}
        elif block == "revisions" and "|" in line:
            rev, date, desc = ([s.strip() for s in line.split("|")] + ["", ""])[:3]
            out["revisions"].append({"rev": norm_rev(rev), "date": date, "description": desc})
    if "rev" in out["title_block"]:
        out["title_block"]["rev"] = norm_rev(out["title_block"]["rev"])
    return out


def _split_ref(s: str) -> dict:
    m = re.match(r"\s*(\S+)(?:\s+(?:REV|R)\s*(\S+))?", s.strip(), re.I)
    return {"doc_number": m.group(1), "rev": norm_rev(m.group(2)) if m.group(2) else None}


def _num(v: str):
    try:
        f = float(v)
        return int(f) if f.is_integer() and "." not in v else f
    except ValueError:
        return v


def render_drawing(company: str, tb: dict, attributes: dict, revisions: list[dict]) -> str:
    """Inverse of parse_drawing_text; used by the synthetic generator."""
    lines = [company.upper()]
    for label, k in (("TITLE", "title"), ("DRAWING NO", "doc_number"), ("REV", "rev"), ("DATE", "rev_date"),
                     ("STATUS", "status"), ("PIPELINE", "pipeline_id"), ("STATIONS", "stations"), ("VENDOR", "vendor"),
                     ("PO", "po_number"), ("TAG", "tag")):
        if tb.get(k) not in (None, ""):
            lines.append(f"{label}: {tb[k]}")
    if tb.get("references"):
        lines.append("REFERENCES: " + ", ".join(
            f"{r['doc_number']} REV {r['rev']}" if r.get("rev") else r["doc_number"] for r in tb["references"]))
    lines.append("-- DATA --")
    lines += [f"{tag}: " + "; ".join(f"{k}={v}" for k, v in vals.items()) for tag, vals in attributes.items()]
    lines.append("-- REVISION BLOCK --")
    lines += [f"{r['rev']} | {r['date']} | {r['description']}" for r in revisions]
    return "\n".join(lines) + "\n"
