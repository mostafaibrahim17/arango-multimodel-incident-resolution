"""Close the fallback gap: with the LLM judge forced OFF, confirm the deterministic
reason_rules() still pivots the hero alert to user-db (not document-service).

Run from the repo root with the venv active:
    python check_rules_fallback.py

The DB and OpenAI embeddings must be reachable (the marquee query still runs); only the
reasoning judge is simulated as down, so reason() falls back to reason_rules().
"""
import json
import sys

sys.path.insert(0, "src")
import resolver


class _DeadOpenAI:
    """Stand-in for a judge outage: any chat-completions call raises."""
    def __init__(self, *a, **k):
        self.chat = self
        self.completions = self

    def create(self, *a, **k):
        raise RuntimeError("simulated LLM judge outage")


# Only break the reasoning judge. reason() constructs OpenAI() inside its try, so forcing that
# global to the dead client sends it down the except -> reason_rules() path. We embed first with
# the real client (below), so the marquee still runs on real data.
_real_openai = resolver.OpenAI

alert = json.load(open("data/alert.sample.json"))

# Patch ONLY the judge. We embed first with the real client, then swap in the dead one so the
# reasoning call fails over to the rules fallback.
db = resolver.connect()
vec = _real_openai().embeddings.create(
    model=resolver.EMBED_MODEL, input=[alert["text"]]
).data[0].embedding

payload = db.aql.execute(resolver.MARQUEE, bind_vars={
    "vec": vec,
    "service": alert.get("service"),
    "scenario": alert.get("scenario") or resolver.DEFAULT_SCENARIO,
}).next()

# Now force the judge down and run just the reasoning layer.
resolver.OpenAI = _DeadOpenAI
payload = resolver.reason(payload, alert_text=alert.get("text"), db=db)
resolver.OpenAI = _real_openai  # restore

print("method        :", payload["method"])          # expect: rules
print("decided_root  :", payload["root_service"])     # expect: user-db
print("confidence    :", payload["confidence"])
print("reasoning     :", payload["reasoning"])

assert payload["method"] == "rules", "judge should have been bypassed"
assert payload["root_service"] == "user-db", \
    f"rules fallback picked {payload['root_service']!r}, expected user-db"
print("\nOK: judge down, rules fallback still pivots to user-db. Gap closed.")