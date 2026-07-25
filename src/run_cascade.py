"""One-shot driver: reason over the hero alert, sweep all alerts, render every figure.

Computes reason(hero) and evaluate(alerts) once, saves both to runlog (gitignored) so the
figures and the article numbers come from the SAME run, then renders all figures.
"""
import json

import pandas as pd

import viz
from resolver import evaluate, reason

# 1. Hero scenario (one reasoning pass) -> cascade figures + saved JSON.
hero = json.load(open("data/alert.hero.json"))
reasoned = reason(hero)
json.dump(reasoned, open("runlog/hero_reasoned.json", "w"), indent=2, default=str)
viz.cascade_figures(reasoned=reasoned)

# 2. Breadth sweep over all alerts -> results figure + saved rows.
alerts = json.load(open("data/alerts.json"))
rows = evaluate(alerts)
json.dump(rows, open("runlog/sweep_rows.json", "w"), indent=2)
viz.fig_results_from_rows(rows)

# 3. Static / KG figures.
viz.fig_architecture()
viz.fig_kg_sample()

# 4. Summary for the runlog.
print("\n=== HERO ===")
print("alert:", reasoned["alert"]["service"], "->  root cause:", reasoned["root_cause"]["service"],
      "| pivoted:", reasoned["pivoted"], "| confidence:", reasoned["confidence"])
print("obvious (vector-only) pages:", reasoned["obvious_hypothesis"]["paged"],
      "| agent pages:", reasoned["root_cause"]["on_call"]["team"])
print("precedents:", [p["number"] for p in reasoned["precedents"]])
print("timings:", reasoned["timings"])

print("\n=== SWEEP (8 alerts) ===")
df = pd.DataFrame(rows)
print(df[["alert", "service", "symptom_service", "root_service", "pivoted",
          "grounded", "corroborated", "query_ms", "answer_s"]].to_string(index=False))
print(f"\npivoted: {df['pivoted'].sum()}/{len(df)} | grounded: {df['grounded'].sum()}/{len(df)} | "
      f"corroborated: {df['corroborated'].sum()}/{len(df)}")
print(f"median query_ms: {df['query_ms'].median():.0f} | median answer_s: {df['answer_s'].median():.1f}")
