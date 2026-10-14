"""Manual check: simulate a Retriever/auth outage and confirm the resolver degrades to
runbook-only instead of crashing.

Run from the repo root with the venv active:
    python check_retriever_fallback.py

The DB and OpenAI must be reachable (the root-cause decision still runs); only the Retriever
is simulated as down, by making auth fail.
"""
import json
import sys

sys.path.insert(0, "src")
import resolver


def _boom(*args, **kwargs):
    raise RuntimeError("simulated Retriever/auth outage")


resolver.token = _boom  # the Retriever path mints a JWT first, so this stands in for an outage

alert = json.load(open("data/alert.sample.json"))
payload = resolver.resolve(alert)   # root-cause decision: vector + graph + health, no Retriever
cited = resolver.answer(payload)    # should fall back to the pinned runbook, not raise

print("decided_root    :", payload["root_service"])
print("retriever       :", cited["retriever"])                          # expect: unavailable
print("retriever cites :", list(cited["citations"]))                    # expect: [] (retriever down)
print("primary_runbook :", (cited.get("primary_runbook") or {}).get("file_name"))
print("text head       :", cited["text"][:140])

assert cited["retriever"] == "unavailable", "expected degraded mode"
assert cited.get("primary_runbook"), "pinned runbook should survive a Retriever outage"
print("\nOK: Retriever outage degraded to runbook-only, decision intact, no crash.")