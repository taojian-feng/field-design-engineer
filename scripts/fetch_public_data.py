"""Download public reference data for the FDE agent into data/public/ (git-ignored).

    python scripts/fetch_public_data.py phmsa              # PHMSA annual report tables via PUDL (parquet)
    python scripts/fetch_public_data.py eia-pipelines --bbox -96.0 29.4 -95.0 30.2
    python scripts/fetch_public_data.py ecfr192            # current text of 49 CFR Part 192 (XML)
    python scripts/fetch_public_data.py all --bbox ...

Only the standard library is used for downloads. Summarizing the PHMSA tables needs pandas and pyarrow.
See docs/public-data-sources.md for what each source contains and its limits.
"""

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "public"

PUDL = "https://s3.us-west-2.amazonaws.com/pudl.catalyst.coop/stable/{table}.parquet"
PUDL_TABLES = [
    "core_phmsagas__yearly_distribution_by_install_decade",
    "core_phmsagas__yearly_distribution_operators",
]
EIA_PIPELINES = ("https://geo.dot.gov/server/rest/services/hosted/Natural_Gas_Pipelines_US_EIA/"
                 "FeatureServer/0/query")
ECFR = "https://www.ecfr.gov/api/versioner/v1/full/{day}/title-49.xml?part=192"


def _get(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "field-design-engineer/0.1"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as fh:
        fh.write(r.read())
    print(f"saved {dest} ({dest.stat().st_size:,} bytes)")
    return dest


def phmsa() -> None:
    for t in PUDL_TABLES:
        _get(PUDL.format(table=t), OUT / "phmsa" / f"{t}.parquet")
    try:
        import pandas as pd
    except ImportError:
        print("pandas not installed; skipping summary")
        return
    d = pd.read_parquet(OUT / "phmsa" / f"{PUDL_TABLES[0]}.parquet")
    latest = d[d.report_date == d.report_date.max()]
    share = latest.groupby("install_decade").mains_miles.sum()
    share = (100 * share / share.sum()).round(1)
    summary = {"report_date": str(d.report_date.max()), "mains_miles_share_by_install_decade_pct": share.to_dict()}
    (OUT / "phmsa" / "vintage_summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


def eia_pipelines(bbox: list[float]) -> None:
    """EIA interstate/intrastate natural gas pipeline routes (approximate, small scale) as GeoJSON."""
    xmin, ymin, xmax, ymax = bbox
    params = {
        "where": "1=1", "outFields": "*", "f": "geojson", "outSR": "4326", "inSR": "4326",
        "geometry": f"{xmin},{ymin},{xmax},{ymax}", "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
    }
    _get(f"{EIA_PIPELINES}?{urllib.parse.urlencode(params)}", OUT / "eia" / "natural_gas_pipelines.geojson")


def ecfr192() -> None:
    _get(ECFR.format(day=date.today().isoformat()), OUT / "ecfr" / "49cfr192.xml")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", choices=["phmsa", "eia-pipelines", "ecfr192", "all"])
    ap.add_argument("--bbox", nargs=4, type=float, metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
                    default=[-96.0, 29.4, -95.0, 30.2], help="lon/lat box for map data (default: Houston area)")
    args = ap.parse_args()
    if args.source in ("phmsa", "all"):
        phmsa()
    if args.source in ("eia-pipelines", "all"):
        eia_pipelines(args.bbox)
    if args.source in ("ecfr192", "all"):
        ecfr192()


if __name__ == "__main__":
    main()
