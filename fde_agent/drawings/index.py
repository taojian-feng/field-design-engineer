"""One index over every drawing source: find drawings fast and know which copy is the current revision."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .model import DEAD_STATUSES, DrawingRecord, DrawingRef, doc_key, norm_rev, rev_order
from .sources import DrawingSource, sources_from_folder

@dataclass
class Family:
    """Every copy of every revision of one document number, plus everything that cites it."""
    key: str
    controlled: list[DrawingRecord] = field(default_factory=list)  # system-of-record revisions, oldest first
    copies: list[DrawingRecord] = field(default_factory=list)      # uncontrolled copies (shared drives, ...)
    cited_by: list[DrawingRef] = field(default_factory=list)

    @property
    def doc_number(self) -> str:
        return (self.controlled or self.copies)[0].doc_number

    @property
    def current(self) -> DrawingRecord | None:
        """Latest live revision in the system of record; falls back to uncontrolled copies if there is none."""
        pool = [r for r in self.controlled if r.status.lower() not in DEAD_STATUSES] or self.controlled or self.copies
        return max(pool, key=lambda r: rev_order(r.rev)) if pool else None

    def revision(self, rev: str) -> DrawingRecord | None:
        rev = norm_rev(rev)
        return next((r for r in reversed(self.controlled) if r.rev == rev), None) or \
            next((r for r in self.copies if r.rev == rev), None)

    def all(self) -> list[DrawingRecord]:
        return self.controlled + self.copies


class DrawingIndex:
    def __init__(self, sources: list[DrawingSource], store=None):
        self.sources = sources
        self.store = store  # optional PodsStore, used to check drawings against asset records
        self.families: dict[str, Family] = {}
        self.dangling: list[DrawingRef] = []
        self.refresh()

    @classmethod
    def from_folder(cls, folder: str | Path, store=None) -> "DrawingIndex":
        return cls(sources_from_folder(folder), store)

    def refresh(self) -> None:
        fams: dict[str, Family] = {}
        refs: list[DrawingRef] = []
        for src in self.sources:
            for rec in src.records():
                fam = fams.setdefault(rec.key, Family(rec.key))
                (fam.controlled if rec.controlled else fam.copies).append(rec)
            refs += list(src.refs())
        for fam in fams.values():
            fam.controlled.sort(key=lambda r: rev_order(r.rev))
            # Drawings that cite other drawings (valve detail -> vendor GA) count as references too.
            if (cur := fam.current) is not None:
                refs += [DrawingRef(cur.source, cur.location, r["doc_number"], r.get("rev"), cur.pipeline_id)
                         for r in cur.references]
        self.dangling = []
        for ref in refs:
            if ref.key in fams:
                fams[ref.key].cited_by.append(ref)
            else:
                self.dangling.append(ref)
        self.families = fams

    # ---- lookup ---------------------------------------------------------------------------------------------------
    def family(self, doc_number: str) -> Family | None:
        return self.families.get(doc_key(doc_number))

    def search(self, text: str | None = None, pipeline_id: str | None = None, begin_station_ft: float | None = None,
               end_station_ft: float | None = None, doc_type: str | None = None, feature_id: str | None = None,
               limit: int = 25) -> list[dict]:
        """Find drawing families across all sources. Returns the current revision and where every copy lives."""
        if text and (exact := self.family(text)) is not None:  # a document number, however it was typed
            return [self.describe(exact, brief=True) | {"match_score": 2.0}]
        words = [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in {"the", "for", "of", "at"}]
        hits = []
        for fam in self.families.values():
            cur = fam.current
            if pipeline_id and not any(r.pipeline_id == pipeline_id for r in fam.all()) \
                    and not any(c.pipeline_id == pipeline_id for c in fam.cited_by):
                continue
            if doc_type and doc_type.lower() not in (cur.doc_type or "").lower():
                continue
            if begin_station_ft is not None and not self._in_range(fam, begin_station_ft,
                                                                   end_station_ft if end_station_ft is not None else begin_station_ft):
                continue
            if feature_id and not self._mentions(fam, feature_id):
                continue
            score = 1.0
            if words:
                hay = " ".join([cur.doc_number, cur.title, cur.doc_type, " ".join(cur.attributes),
                                " ".join(str(v) for a in cur.attributes.values() for v in a.values()),
                                " ".join(str(v) for v in cur.meta.values() if isinstance(v, str))]).lower()
                score = sum(1 for w in words if w in hay) / len(words)
                if score < 0.5:
                    continue
            hits.append((score, fam))
        hits.sort(key=lambda h: (-h[0], h[1].doc_number))
        return [self.describe(f, brief=True) | {"match_score": round(s, 2)} for s, f in hits[:limit]]

    def _in_range(self, fam: Family, a: float, b: float) -> bool:
        cur = fam.current
        if cur.begin_station_ft is not None:
            return cur.begin_station_ft <= b and (cur.end_station_ft or cur.begin_station_ft) >= a
        return any(c.station_ft is not None and a <= c.station_ft <= b for c in fam.cited_by)

    def _mentions(self, fam: Family, feature_id: str) -> bool:
        fid = feature_id.upper()
        cur = fam.current
        return (any((c.feature_id or "").upper() == fid for c in fam.cited_by)
                or any(t.upper() == fid or str(a.get("tag", "")).upper() == fid for t, a in cur.attributes.items())
                or str(cur.meta.get("tag", "")).upper() == fid)

    def describe(self, fam: Family, brief: bool = False) -> dict:
        cur = fam.current
        copies = []
        for r in fam.all():
            state = ("current" if r is cur or (r.rev == cur.rev and r.fingerprint == cur.fingerprint)
                     else "superseded" if rev_order(r.rev) < rev_order(cur.rev)
                     else "conflicts_with_current" if r.rev == cur.rev
                     else "newer_than_controlled")
            copies.append({"source": r.source, "rev": r.rev, "status": r.status, "location": r.location, "state": state})
        out = {
            "doc_number": fam.doc_number, "title": cur.title, "doc_type": cur.doc_type,
            "pipeline_id": cur.pipeline_id, "stations_ft": [cur.begin_station_ft, cur.end_station_ft],
            "current_rev": cur.rev, "current_status": cur.status, "current_location": cur.location,
            "controlled": cur.controlled, "copies": copies,
            "cited_by": [{"source": c.source, "feature_id": c.feature_id, "rev": c.rev, "location": c.location}
                         for c in fam.cited_by],
        }
        if not brief:
            out["revision_history"] = [{"rev": r.rev, "status": r.status, "date": r.rev_date,
                                        "description": next((x["description"] for x in r.meta.get("revision_block", [])
                                                             if x["rev"] == r.rev), "")} for r in fam.controlled]
            out["attributes"] = cur.attributes
            out["references"] = cur.references
            out["meta"] = {k: v for k, v in cur.meta.items() if k != "revision_block"}
        return out

    def get(self, doc_number: str, rev: str | None = None) -> dict:
        fam = self.family(doc_number)
        if fam is None:
            close = sorted(self.families.values(), key=lambda f: -_overlap(doc_key(doc_number), f.key))[:5]
            return {"error": f"{doc_number} not found in any source", "did_you_mean": [f.doc_number for f in close]}
        out = self.describe(fam)
        if rev is not None:
            r = fam.revision(rev)
            out["requested_rev"] = ({"rev": r.rev, "status": r.status, "location": r.location, "attributes": r.attributes}
                                    if r else {"error": f"rev {rev} not found"})
        return out


def _overlap(a: str, b: str) -> int:
    return sum(1 for x, y in zip(a, b) if x == y)
