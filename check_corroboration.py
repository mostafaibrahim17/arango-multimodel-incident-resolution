"""Check that corroboration can AGREE and DISAGREE — on the right kind of alert.

Corroboration compares two independent surfaces:
  - picker 1: the structured pipeline's decided root cause (graph + health),
  - picker 2: a SYMPTOM-ONLY retrieval (`corroboration_citations`) that never names the decided
    service — retrieval's own, independent vote.

Importantly, these CAN disagree. On the two-hop pivots (the hero udb-lag alerts) they SHOULD:
symptom-only search can't jump from an onboarding/auth symptom to user-db, so it surfaces a
different runbook and corroboration is False. That disagreement is the check working.

To show the check also AGREES — and that agreement is not rigged — we use a DIRECT, single-hop
alert (ALR-002, payment-gateway), where the decided root is the alerting service itself and the
symptom-only retrieval independently surfaces that same runbook. Then we hold that retrieval fixed
and change only the CLAIMED root to a service retrieval did NOT surface; corroboration must flip
to False. Same retrieval, different claimed root -> the check compares the two, it does not assert.

Run from the repo root, venv active, Retriever deployed:
    python check_corroboration.py
"""
import copy
import json
import sys

sys.path.insert(0, "src")
from resolver import resolve, answer, corroborated, _citation_service

# A direct, single-hop alert: the cause IS the alerting service, so symptom-only retrieval and the
# graph should land on the same runbook. (The hero, by contrast, honestly fails corroboration —
# that case is covered by the sweep, not here.)
alerts = {a["_key"]: a for a in json.load(open("data/alerts.json"))}
alert = alerts["ALR-002"]   # payment-gateway: card declines / processor timeouts

payload = resolve(alert)                  # picker 1 decides the root (expected: payment-gateway)
cited = answer(payload, alert["text"])    # picker 2: the symptom-only corroboration retrieval

# Read what corroboration actually uses: the symptom-only citations, NOT the answer's citations.
retrieved = list(cited.get("corroboration_citations", {}).values())
retrieved_services = [_citation_service(c) for c in retrieved]
print("decided root (picker 1):", payload["root_service"])
print("symptom-only retrieval surfaced:", retrieved_services)
print("retriever status       :", cited.get("retriever"))

# 1) Real decision on a direct alert: the two surfaces should agree.
real = corroborated(payload, cited)
print("\nreal decision                 ->", real)

# 2) Perturbed: claim a root the symptom-only retrieval did NOT surface; retrieval left untouched.
blob = " ".join(((c.get("file_name") or "") + " " + (c.get("content") or "")).lower()
                for c in retrieved)
candidates = ["user-db", "network-gateway", "merchant-db", "notification-service"]
wrong_root = next((s for s in candidates if s not in blob), candidates[0])

wrong = copy.deepcopy(payload)
wrong["root_service"] = wrong_root
perturbed = corroborated(wrong, cited)
print(f"claimed {wrong_root!r} (not retrieved) ->", perturbed)

assert real["agree"] is True, (
    "expected the direct alert's real root to corroborate; if this fails, the symptom-only "
    "retrieval did not surface the payment-gateway runbook (check the KG/Retriever is built)")
assert perturbed["agree"] is False, "corroboration should disagree when the claimed root was not retrieved"
print("\nOK: corroboration AGREES for the real root (direct alert) and DISAGREES for a root the "
      "symptom-only retrieval did not surface.")
print("    (same retrieval, different claimed root -> the check compares the two, it does not assert.)")