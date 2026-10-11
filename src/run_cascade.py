"""One-shot driver: resolve the hero alert, sweep all alerts, render the figures.

Uses the project's standard entry points, resolve() for the hero alert and evaluate() for the
sweep, so the numbers here match the rest of the project. Both results are saved to runlog/
(gitignored) so the figures and the reported numbers come from a single run, then the figures
defined in viz.py are rendered. Figure calls are guarded: a figure that is missing or fails prints
a warning and is skipped, so the numbers always print.
"""
import json
import os

import pandas as pd

import viz
from resolver import evaluate, resolve


def _safe(label, fn, *args, **kwargs):
    """Run a figure function without letting a figure error abort the run."""
    try:
        fn(*args, **kwargs)
    except Exception as exc:
        print(f"[run_cascade] skipped {label}: {type(exc).__name__}: {exc}")


os.makedirs("runlog", exist_ok=True)

# 1. Hero scenario. resolve() runs the marquee first, so the payload is fully populated
#    (possible_causes, impacted_services, root_service, reasoning, confidence, on_call).
hero = json.load(open("data/alert.hero.json"))
reasoned = resolve(hero)
json.dump(reasoned, open("runlog/hero_reasoned.json", "w"), indent=2, default=str)

# 2. Breadth sweep over all alerts, with the saved rows and the results figure.
alerts = json.load(open("data/alerts.json"))
rows = evaluate(alerts)
json.dump(rows, open("runlog/sweep_rows.json", "w"), indent=2)

# 3. Figures (guarded).
_safe("fig_results_from_rows", viz.fig_results_from_rows, rows)
_safe("fig_affected_subgraph", viz.fig_affected_subgraph)
_safe("fig_architecture", viz.fig_architecture)
_safe("fig_kg_sample", viz.fig_kg_sample)

# 4. Summary for the runlog.
print("\n=== HERO ===")
print("alert:", hero["service"], " ->  root cause:", reasoned["root_service"],
      "| confidence:", reasoned["confidence"],
      "(judge:", reasoned.get("judge_confidence"), ") | method:", reasoned["method"])
print("confidence basis:", reasoned.get("confidence_basis"))
print("on-call:", reasoned["on_call"]["team"])
print("possible causes:", [c["service"] for c in reasoned["possible_causes"]])
print("impacted (blast radius):", [s["service"] for s in reasoned["impacted_services"]])
print("reasoning:", reasoned["reasoning"])

print(f"\n=== SWEEP ({len(alerts)} alerts) ===")
df = pd.DataFrame(rows)
desired = ["alert", "service", "expected_root", "decided_root", "correct",
           "confidence", "judge_confidence", "runbook_found", "corroborated",
           "query_ms", "answer_s"]
cols = [c for c in desired if c in df.columns]  # show whatever the resolver provided
print(df[cols].to_string(index=False))

n = len(df)
print(f"\ncorrect root cause: {int(df['correct'].sum())}/{n}")
_found_col = "runbook_found" if "runbook_found" in df.columns else ("grounded" if "grounded" in df.columns else None)
_found = f"{int(df[_found_col].sum())}/{n}" if _found_col else "n/a"
print(f"runbook_found: {_found}   corroborated: {int(df['corroborated'].sum())}/{n}")
print(f"median query_ms: {df['query_ms'].median():.0f}   "
      f"median answer_s: {df['answer_s'].median():.1f}")