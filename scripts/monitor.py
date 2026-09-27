"""Print an operational health report from logs/telemetry.db (the dashboard shows the same with charts).

python scripts/monitor.py              # all time
python scripts/monitor.py --hours 24   # last 24 h
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from movie_agent import monitor  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float)
    ap.add_argument("--json", action="store_true", help="machine-readable output (e.g. for a cron alert)")
    args = ap.parse_args()
    turns, calls = monitor.load(since_ts=time.time() - args.hours * 3600 if args.hours else None)
    k = monitor.kpis(turns, calls)
    al = monitor.alerts(k)
    if args.json:
        print(json.dumps({"kpis": k, "alerts": al}, indent=2, default=str))
        sys.exit(1 if al else 0)
    print("== KPIs ==")
    for name, v in k.items():
        slo = monitor.SLOS.get(name)
        print(f"  {name:<24} {v!s:>10}" + (f"   (SLO {slo[0]} {slo[1]})" if slo else ""))
    print("\n== Per tool ==")
    print(monitor.per_tool(calls).to_string() if not calls.empty else "  no tool calls yet")
    print("\n== Alerts ==")
    print("\n".join("  !! " + a for a in al) or "  none")
    ev = monitor.guardrail_events(turns) if not turns.empty else None
    if ev is not None and len(ev):
        print("\n== Recent guardrail triggers ==")
        print(ev.to_string(index=False))
    sys.exit(1 if al else 0)
