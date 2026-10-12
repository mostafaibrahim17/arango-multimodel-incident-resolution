"""Reviewer-style test for ALR-010 (the new medium, two-suspect case).

The reviewer's objection to the OLD hero was: the pick was driven by a keyword in the health
note that echoed the alert ("permission"), not by reasoning. This runs the SAME test on ALR-010:
it strips both degraded services' notes down to bare metrics with NO symptom words, then asks the
judge to decide again. If ALR-010 still resolves to auth-service, the pick is driven by structure
(auth-service is the depth-1 dependency on the login path), not by word overlap -> the case is
legitimate. If it flips to user-db or anywhere else, the pick was keyword-driven and ALR-010 is
just the old problem in a new coat -> drop it.

Run from the repo root, venv active, DB + OpenAI reachable:
    python check_alr010_stripnote.py
"""
import json
import sys

sys.path.insert(0, "src")
import resolver

ALR010 = {
    "_key": "ALR-010", "service": "api-gateway", "severity": "P1",
    "scenario": "auth-degraded",
    "text": "Users failing to log in; elevated 5xx on authentication routes with "
            "slow and intermittently rejected sessions.",
}

# Notes stripped to bare metrics: no "login", "auth", "token", "session" words that could overlap
# with the alert text. Pure numbers, so the judge has only the graph structure to go on.
STRIPPED = {
    "auth-service": "p99 elevated to 1.4s; error rate 3%",
    "user-db": "read_replica_lag 6.1s; apply queue backing up",
}

db = resolver.connect()
vec = resolver.OpenAI().embeddings.create(
    model=resolver.EMBED_MODEL, input=[ALR010["text"]]
).data[0].embedding

payload = db.aql.execute(resolver.MARQUEE, bind_vars={
    "vec": vec, "service": ALR010["service"], "scenario": ALR010["scenario"],
}).next()
payload["scenario"] = ALR010["scenario"]

# Overwrite the degraded notes in-memory with the word-free versions before reasoning.
for s in payload["possible_causes"]:
    if s["service"] in STRIPPED:
        s["note"] = STRIPPED[s["service"]]

print("degraded in chain:", [(s["service"], s["depth"]) for s in payload["possible_causes"]
                              if s.get("status") == "degraded"])
print("notes now:", {s["service"]: s["note"] for s in payload["possible_causes"]
                     if s.get("status") == "degraded"})
print()

payload = resolver.reason(payload, alert_text=ALR010["text"], db=db)

print("decided_root :", payload["root_service"])
print("confidence   :", payload["confidence"])
print("method       :", payload["method"])
print("reasoning    :", payload["reasoning"])
print()
if payload["root_service"] == "auth-service":
    print("PASS: still auth-service with the words stripped -> pick is structural, not keyword. "
          "ALR-010 is legitimate.")
else:
    print(f"FAIL: flipped to {payload['root_service']!r} once the words were gone -> the pick was "
          "keyword-driven. Drop ALR-010 or redesign it.")