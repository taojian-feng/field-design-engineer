"""A small PODS-inspired asset store in SQLite.

Tables loosely follow PODS Lite concepts: a pipeline route with linear stationing,
pipe segments and point features located by station, class location ranges, pressure
test records, and a change log for every as-built write. It is a stand-in for a real
PODS / ArcGIS Utility Network database, and the query surface is kept narrow so a real
backend can replace it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline (
    pipeline_id TEXT PRIMARY KEY, name TEXT, operator TEXT, product TEXT,
    begin_station_ft REAL, end_station_ft REAL
);
CREATE TABLE IF NOT EXISTS pipe_segment (
    segment_id TEXT PRIMARY KEY, pipeline_id TEXT, begin_station_ft REAL, end_station_ft REAL,
    nps REAL, od_in REAL, wall_in REAL, grade TEXT, seam_type TEXT, coating TEXT,
    install_year INTEGER, heat_number TEXT, mtr_on_file INTEGER
);
CREATE TABLE IF NOT EXISTS point_feature (
    feature_id TEXT PRIMARY KEY, pipeline_id TEXT, station_ft REAL, feature_type TEXT,
    description TEXT, rating_psig REAL, latitude REAL, longitude REAL
);
CREATE TABLE IF NOT EXISTS class_location_range (
    pipeline_id TEXT, begin_station_ft REAL, end_station_ft REAL, class_location INTEGER,
    building_count INTEGER
);
CREATE TABLE IF NOT EXISTS pressure_test (
    test_id TEXT PRIMARY KEY, pipeline_id TEXT, begin_station_ft REAL, end_station_ft REAL,
    test_date TEXT, test_pressure_psig REAL, duration_hr REAL, medium TEXT
);
CREATE TABLE IF NOT EXISTS structure (
    structure_id TEXT PRIMARY KEY, pipeline_id TEXT, station_ft REAL, offset_ft REAL,
    structure_type TEXT, stories INTEGER, dwelling_units INTEGER, year_built INTEGER, latitude REAL, longitude REAL
);
CREATE TABLE IF NOT EXISTS change_log (
    change_id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, table_name TEXT, record_id TEXT,
    action TEXT, before_json TEXT, after_json TEXT, reference TEXT
);
"""

WRITABLE = {
    "pipe_segment": "segment_id",
    "point_feature": "feature_id",
    "pressure_test": "test_id",
}


def _rows(cur) -> list[dict]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


class PodsStore:
    def __init__(self, path: str | Path = ":memory:"):
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript(SCHEMA)

    # ---- reads -------------------------------------------------------------
    def list_pipelines(self) -> list[dict]:
        return _rows(self.conn.execute("SELECT * FROM pipeline"))

    def segments_at(self, pipeline_id: str, begin_ft: float, end_ft: float | None = None) -> dict:
        end_ft = begin_ft if end_ft is None else end_ft
        # A point query is inclusive; a range query skips records that only touch its ends.
        q = ("WHERE pipeline_id=? AND begin_station_ft<=? AND end_station_ft>=?" if end_ft == begin_ft
             else "WHERE pipeline_id=? AND begin_station_ft<? AND end_station_ft>?")
        args = (pipeline_id, end_ft, begin_ft)
        return {
            "pipe_segments": _rows(self.conn.execute(f"SELECT * FROM pipe_segment {q} ORDER BY begin_station_ft", args)),
            "class_locations": _rows(self.conn.execute(f"SELECT * FROM class_location_range {q}", args)),
            "pressure_tests": _rows(self.conn.execute(f"SELECT * FROM pressure_test {q}", args)),
            "point_features": _rows(self.conn.execute(
                "SELECT * FROM point_feature WHERE pipeline_id=? AND station_ft BETWEEN ? AND ? ORDER BY station_ft",
                (pipeline_id, begin_ft, end_ft))),
        }

    def structures(self, pipeline_id: str, max_offset_ft: float = 660) -> list[dict]:
        """Structures within max_offset_ft of centerline (660 ft = 220 yards, the 192.5 class location corridor)."""
        return _rows(self.conn.execute(
            "SELECT * FROM structure WHERE pipeline_id=? AND ABS(offset_ft)<=? ORDER BY station_ft",
            (pipeline_id, max_offset_ft)))

    def load_csv_dir(self, folder: str | Path) -> "PodsStore":
        """Load table_name.csv files (as written by the synthetic generator) into the store."""
        import csv
        folder = Path(folder)
        tables = ["pipeline", "pipe_segment", "point_feature", "class_location_range", "pressure_test", "structure"]
        for t in tables:
            f = folder / f"{t}.csv"
            if not f.exists():
                continue
            with open(f, newline="") as fh:
                rows = list(csv.DictReader(fh))
            if not rows:
                continue
            cols = list(rows[0])
            vals = [tuple(None if r[c] == "" else r[c] for c in cols) for r in rows]
            self.conn.executemany(
                f"INSERT OR REPLACE INTO {t} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", vals)
        self.conn.commit()
        return self

    def change_log(self, limit: int = 20) -> list[dict]:
        return _rows(self.conn.execute("SELECT * FROM change_log ORDER BY change_id DESC LIMIT ?", (limit,)))

    # ---- writes (gated by the agent's approval hook) ----------------------
    def upsert(self, table: str, record: dict, reference: str) -> dict:
        if table not in WRITABLE:
            raise ValueError(f"Table {table!r} is not writable; allowed: {sorted(WRITABLE)}")
        key = WRITABLE[table]
        if key not in record:
            raise ValueError(f"Record must include {key}")
        cur = self.conn.execute(f"SELECT * FROM {table} WHERE {key}=?", (record[key],))
        before = _rows(cur)
        before = before[0] if before else None
        merged = {**(before or {}), **record}
        cols = ", ".join(merged)
        marks = ", ".join("?" for _ in merged)
        self.conn.execute(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({marks})", tuple(merged.values()))
        self.conn.execute(
            "INSERT INTO change_log (ts, table_name, record_id, action, before_json, after_json, reference) "
            "VALUES (?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), table, record[key],
             "update" if before else "insert", json.dumps(before), json.dumps(merged), reference),
        )
        self.conn.commit()
        return {"table": table, "record_id": record[key], "action": "update" if before else "insert", "after": merged}


def seed_sample(store: PodsStore) -> PodsStore:
    """Fictional 'Line 100' 12-inch gas transmission lateral used for demos and tests."""
    c = store.conn
    c.execute("INSERT OR REPLACE INTO pipeline VALUES ('L100','Line 100 Lateral','Example Midstream','Natural gas',0,26400)")
    c.executemany("INSERT OR REPLACE INTO pipe_segment VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        ("L100-S01", "L100", 0, 8000, 12, 12.75, 0.250, "X52", "erw", "FBE", 1998, "H-44821", 1),
        ("L100-S02", "L100", 8000, 15500, 12, 12.75, 0.375, "X52", "erw", "FBE", 1998, "H-44907", 1),
        ("L100-S03", "L100", 15500, 21000, 12, 12.75, 0.250, "X42", "unknown", "Coal tar", 1965, None, 0),
        ("L100-S04", "L100", 21000, 26400, 12, 12.75, 0.250, "X52", "erw", "FBE", 2012, "H-71230", 1),
    ])
    c.executemany("INSERT OR REPLACE INTO class_location_range VALUES (?,?,?,?,?)", [
        ("L100", 0, 8000, 1, 6), ("L100", 8000, 15500, 3, 58), ("L100", 15500, 26400, 2, 19),
    ])
    c.executemany("INSERT OR REPLACE INTO pressure_test VALUES (?,?,?,?,?,?,?,?)", [
        ("PT-1998-01", "L100", 0, 15500, "1998-06-14", 1400, 8, "water"),
        ("PT-2012-01", "L100", 21000, 26400, "2012-09-02", 1250, 8, "water"),
    ])
    c.executemany("INSERT OR REPLACE INTO point_feature VALUES (?,?,?,?,?,?,?,?)", [
        ("V-101", "L100", 0, "valve", "Launcher block valve, ANSI 600", 1480, 29.7601, -95.3701),
        ("V-102", "L100", 15500, "valve", "Mainline valve, ANSI 300", 740, 29.7712, -95.3420),
        ("TP-07", "L100", 12000, "cp_test_station", "CP test station", None, 29.7680, -95.3510),
        ("X-03", "L100", 18250, "road_crossing", "FM 1960 cased crossing", None, 29.7755, -95.3330),
    ])
    c.commit()
    return store
