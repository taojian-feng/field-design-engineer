"""Source adapters: one per place drawings live.

Each adapter reads its system's native export and yields DrawingRecords (copies of drawings) and DrawingRefs
(places that cite a drawing). The file-based adapters below read the synthetic data in data/synthetic/drawings/,
and also work on real exports in the same shape:

| Adapter                | Stands in for                                   | Reads                                        |
|------------------------|-------------------------------------------------|----------------------------------------------|
| EdmsRegisterSource     | ProjectWise, Meridian, SharePoint document lib, | a register export (CSV) plus the files       |
|                        | Documentum: the document-control system         |                                              |
| VendorSubmittalSource  | vendor portal / submittal tracker (Aconex,      | the submittal log (CSV) plus vendor drawings |
|                        | Procore, e-mail transmittals)                   |                                              |
| GisLinkSource          | ArcGIS / PODS feature attributes that cite a    | GeoJSON features with drawing_number and     |
|                        | drawing number and revision                     | drawing_rev properties                       |
| FolderSource           | shared drives, project folders, laptop copies   | any folder tree; doc number and revision are |
|                        |                                                 | read from the title block and the filename   |

To connect a live system, write a class with the same three members (name, records(), refs()) that calls its API.
Nothing else in the agent changes.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Callable, Iterable, Protocol

from .model import DOC_TYPES, DrawingRecord, DrawingRef, norm_rev, parse_drawing_text


class DrawingSource(Protocol):
    name: str

    def records(self) -> Iterable[DrawingRecord]: ...

    def refs(self) -> Iterable[DrawingRef]: ...


def _f(v):
    return float(v) if v not in (None, "") else None


def _doc_type(doc_number: str) -> str:
    parts = re.findall(r"[A-Z]+", doc_number.upper())
    return next((DOC_TYPES[p] for p in reversed(parts) if p in DOC_TYPES), "")


def _record_from_text(text: str, **kw) -> DrawingRecord:
    parsed = parse_drawing_text(text)
    tb = parsed["title_block"]
    kw.setdefault("doc_number", tb.get("doc_number", ""))
    kw.setdefault("rev", tb.get("rev", ""))
    kw.setdefault("title", tb.get("title", ""))
    kw.setdefault("status", tb.get("status", ""))
    kw.setdefault("rev_date", tb.get("rev_date", ""))
    kw.setdefault("pipeline_id", tb.get("pipeline_id"))
    if "stations" in tb and "begin_station_ft" not in kw:
        a, _, b = tb["stations"].partition("-")
        kw["begin_station_ft"], kw["end_station_ft"] = _f(a), _f(b or a)
    kw.setdefault("doc_type", _doc_type(kw["doc_number"]))
    rec = DrawingRecord(attributes=parsed["attributes"], references=parsed["references"], title_block=tb, **kw)
    rec.meta["revision_block"] = parsed["revisions"]
    return rec


class EdmsRegisterSource:
    """Document-control register: register.csv (one row per revision) and the file for each revision."""

    def __init__(self, folder: str | Path, name: str = "edms"):
        self.folder, self.name = Path(folder), name

    def records(self):
        with open(self.folder / "register.csv", newline="") as fh:
            for row in csv.DictReader(fh):
                path = self.folder / row["file"]
                text = path.read_text() if path.exists() else ""
                rec = _record_from_text(
                    text, source=self.name, location=str(path), controlled=True,
                    doc_number=row["doc_number"], rev=row["rev"], title=row["title"], doc_type=row["doc_type"],
                    status=row["status"], rev_date=row["rev_date"], pipeline_id=row["pipeline_id"] or None,
                    begin_station_ft=_f(row["begin_station_ft"]), end_station_ft=_f(row["end_station_ft"]))
                if not path.exists():
                    rec.meta["file_missing"] = True
                yield rec

    def refs(self):
        return []


class VendorSubmittalSource:
    """Vendor submittal log: submittal_log.csv (one row per vendor drawing revision) and the vendor drawings."""

    def __init__(self, folder: str | Path, name: str = "vendor"):
        self.folder, self.name = Path(folder), name

    def records(self):
        with open(self.folder / "submittal_log.csv", newline="") as fh:
            for row in csv.DictReader(fh):
                path = self.folder / row["file"]
                rec = _record_from_text(
                    path.read_text() if path.exists() else "", source=self.name, location=str(path), controlled=True,
                    doc_number=row["vendor_doc_number"], rev=row["vendor_rev"], status=row["status"],
                    rev_date=row["reviewed_date"], doc_type="vendor_ga", pipeline_id=row.get("pipeline_id") or None)
                rec.meta.update({k: row[k] for k in ("submittal_id", "vendor", "po_number", "tag", "review_comments")
                                 if k in row})
                yield rec

    def refs(self):
        return []


class GisLinkSource:
    """GIS features that carry a drawing link (drawing_number / drawing_rev attributes), from GeoJSON exports."""

    def __init__(self, folder: str | Path, name: str = "gis"):
        self.folder, self.name = Path(folder), name

    def records(self):
        return []

    def refs(self):
        for path in sorted(self.folder.glob("*.geojson")):
            for f in json.loads(path.read_text())["features"]:
                p = f["properties"]
                if p.get("drawing_number"):
                    yield DrawingRef(self.name, f"{path}#{p.get('feature_id')}", p["drawing_number"],
                                     norm_rev(p["drawing_rev"]) if p.get("drawing_rev") else None,
                                     p.get("pipeline_id"), p.get("feature_id"), _f(p.get("station_ft")))


# Document numbers in filenames are messy: 'SYN-101-AL-001', 'SYN101_AL_1', 'syn-101-vd-03'.
FILENAME_DOC = re.compile(r"([A-Z]{2,4})[-_ ]?(\d{2,4})[-_ ]?([A-Z]{2,3})[-_ ]?(\d{1,3})", re.I)
FILENAME_REV = re.compile(r"(?:^|[^A-Z])(?:REV|R)[\s._-]*([A-Z]|\d{1,2})(?![A-Z0-9])", re.I)


class FolderSource:
    """Walk a folder tree (shared drive, project folder) and read every drawing it can extract text from.

    extractors maps a file suffix to a function that returns the drawing's text layer. Plain text is built in; for
    PDFs add e.g. {".pdf": pdf_text} using pdfplumber, or an OCR / Claude vision call for scans.
    """

    def __init__(self, folder: str | Path, name: str = "shared_drive",
                 extractors: dict[str, Callable[[Path], str]] | None = None):
        self.folder, self.name = Path(folder), name
        self.extractors = {".txt": lambda p: p.read_text(), **(extractors or {})}
        self.skipped: list[str] = []

    def records(self):
        for path in sorted(p for p in self.folder.rglob("*") if p.is_file()):
            extract = self.extractors.get(path.suffix.lower())
            if extract is None:
                self.skipped.append(str(path))
                continue
            fdoc, frev = parse_filename(path.name)
            rec = _record_from_text(extract(path), source=self.name, location=str(path))
            if not rec.doc_number and fdoc:
                rec.doc_number = fdoc
            if not rec.rev and frev:
                rec.rev = frev
            rec.meta.update({"filename": path.name, "filename_doc": fdoc, "filename_rev": frev})
            if rec.doc_number:
                yield rec

    def refs(self):
        return []


def parse_filename(name: str) -> tuple[str | None, str | None]:
    stem = Path(name).stem
    m = FILENAME_DOC.search(stem)
    doc = "-".join(m.groups()).upper() if m else None
    rest = stem[m.end():] if m else stem
    r = FILENAME_REV.search(rest)
    return doc, (norm_rev(r.group(1)) if r else None)


def sources_from_folder(folder: str | Path) -> list:
    """Standard layout: edms/, vendor/, gis/, shared_drive/ (any may be missing)."""
    folder = Path(folder)
    found = []
    for sub, cls in (("edms", EdmsRegisterSource), ("vendor", VendorSubmittalSource), ("gis", GisLinkSource),
                     ("shared_drive", FolderSource)):
        if (folder / sub).is_dir():
            found.append(cls(folder / sub))
    return found
