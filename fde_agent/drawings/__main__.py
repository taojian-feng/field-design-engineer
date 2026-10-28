"""Drawing check without the LLM: index a sources folder and print a markdown report.

    python -m fde_agent.drawings data/synthetic/drawings --data data/synthetic
    python -m fde_agent.drawings data/synthetic/drawings --pipeline SYN-101 --search "HDD crossing"
"""

from __future__ import annotations

import argparse
import time

from ..pods_store import PodsStore
from .index import DrawingIndex
from .verify import verify


def main() -> None:
    ap = argparse.ArgumentParser(description="Index drawing sources and report discrepancies")
    ap.add_argument("folder", help="Folder with edms/, vendor/, gis/, shared_drive/")
    ap.add_argument("--data", help="Asset table CSVs, to check drawings against records")
    ap.add_argument("--pipeline")
    ap.add_argument("--search")
    ap.add_argument("--no-info", action="store_true", help="Hide stale-copy housekeeping findings")
    args = ap.parse_args()

    t0 = time.perf_counter()
    store = PodsStore().load_csv_dir(args.data) if args.data else None
    idx = DrawingIndex.from_folder(args.folder, store)
    n_files = sum(len(f.all()) for f in idx.families.values())
    print(f"# Drawing check\n\nIndexed {n_files} files as {len(idx.families)} drawings from "
          f"{', '.join(s.name for s in idx.sources)} in {time.perf_counter() - t0:.2f} s.\n")
    if args.search:
        print(f"## Search: {args.search}\n\n| Drawing | Current rev | Status | Copies | Open |\n|---|---|---|---|---|")
        for h in idx.search(text=args.search, pipeline_id=args.pipeline):
            print(f"| {h['doc_number']} | {h['current_rev']} | {h['current_status']} | {len(h['copies'])} | "
                  f"`{h['current_location']}` |")
        print()
    r = verify(idx, pipeline_id=args.pipeline, include_info=not args.no_info)
    print(f"## Findings ({len(r['findings'])} across {r['drawings_checked']} drawings)\n")
    print("| Severity | Type | Drawing | Rev | Source | Detail |\n|---|---|---|---|---|---|")
    for f in r["findings"]:
        print(f"| {f['severity']} | {f['type']} | {f['doc_number']} | {f['rev'] or ''} | {f['source']} | "
              f"{f['detail'].replace('|', '/')} |")


if __name__ == "__main__":
    main()
