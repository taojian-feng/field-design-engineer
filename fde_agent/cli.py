"""Interactive command line for the FDE agent.

    python -m fde_agent.cli                 # chat with the agent on the sample Line 100 data
    python -m fde_agent.cli --db assets.db  # use (and persist) a SQLite asset database
    python -m fde_agent.cli --data data/synthetic  # load the synthetic 6-line system
    python -m fde_agent.cli -p "What is the MAOP of L100 from station 0 to 26400?"
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .agent import FDEAgent
from .pods_store import PodsStore, seed_sample
from .tools import ToolBox


def _approve(name: str, args: dict) -> bool:
    print(f"\n[approval needed] {name}\n{json.dumps(args, indent=2)}")
    return input("Approve this write? [y/N] ").strip().lower() in ("y", "yes")


def _event(kind: str, data: dict) -> None:
    if kind == "tool_call":
        print(f"  -> {data['name']}({json.dumps(data['input'])[:160]})")


def main() -> None:
    ap = argparse.ArgumentParser(description="Pipeline Field Design Engineer agent")
    ap.add_argument("--db", default=None, help="SQLite asset database (default: in-memory sample)")
    ap.add_argument("--data", default=None, help="Load table CSVs from this folder (e.g. data/synthetic)")
    ap.add_argument("--output", default="output", help="Folder for saved deliverables")
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("-p", "--prompt", help="Run one request and exit")
    args = ap.parse_args()

    fresh = args.db is None or not Path(args.db).exists()
    store = PodsStore(args.db or ":memory:")
    if args.data:
        store.load_csv_dir(args.data)
    elif fresh:
        seed_sample(store)
    agent = FDEAgent(ToolBox(store, args.output, approve=_approve), effort=args.effort, on_event=_event)

    if args.prompt:
        print(agent.ask(args.prompt))
        return
    lines = ", ".join(p["pipeline_id"] for p in store.list_pipelines())
    print(f"FDE agent ready. Pipelines: {lines}. Ctrl-D to exit.")
    while True:
        try:
            q = input("\nengineer> ").strip()
        except EOFError:
            break
        if q:
            print("\n" + agent.ask(q))


if __name__ == "__main__":
    main()
