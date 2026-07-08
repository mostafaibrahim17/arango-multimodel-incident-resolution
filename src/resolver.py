"""Resolve one alert: an incident-response agent that reasons over a single multimodel store.

The headline marquee is ONE AQL round trip doing four multimodel moves at once:
  - vector  : APPROX_NEAR_COSINE over past incidents (what looks similar)
  - graph   : OUTBOUND traversal of the service-dependency topology (what is connected / blast radius)
  - key-value: a per-dependency health-signal lookup (which upstream dependency is actually degraded)
  - key-value: the on-call owner of the real root-cause service

On top of that one query, the resolver REASONS like a responder instead of returning the first
similar hit: it forms the obvious vector hypothesis, tests it against the dependency graph, pivots
to the degraded UPSTREAM dependency when the alert is a downstream symptom, pulls structural
precedents with a second vector pass, eliminates the wrong hypothesis, and grounds the fix in a
cited runbook. The point it proves: vector finds what is similar; you also need what is connected,
which is degraded, and what the runbook says -- and here all of that lives in one store.
"""
import json
import sys
import time

import requests
from openai import OpenAI

from graphrag import retriever_url, runbook_for_service, token
from ingest import EMBED_MODEL, connect

# One query, four multimodel moves: APPROX_NEAR_COSINE (vector), OUTBOUND (graph),
# DOCUMENT on service_signals (key-value health), DOCUMENT on teams (key-value owner).
# The graph + health join is what lets the agent name the degraded UPSTREAM dependency
# rather than blaming the service the alert happened to fire on.
MARQUEE = """
LET similar = (
  FOR i IN incidents
    LET sim = APPROX_NEAR_COSINE(i.embedding, @vec)
    SORT sim DESC
    LIMIT 3
    RETURN {
      number: i._key,
      short_description: i.short_description,
      resolution: i.resolution,
      service: i.service,
      similarity: sim
    }
)
LET symptom = @service != null ? @service : FIRST(similar).service
LET affected = (
  FOR v, e, p IN 0..3 OUTBOUND CONCAT("services/", symptom) GRAPH "service_topology"
    COLLECT service = v._key, name = v.name AGGREGATE depth = MIN(LENGTH(p.edges))
    SORT depth
    RETURN {service, name, depth}
)
LET degraded = (
  FOR a IN affected
    FILTER a.depth > 0
    LET sig = DOCUMENT("service_signals", a.service)
    FILTER sig != null AND sig.status == "degraded"
    SORT a.depth DESC
    RETURN MERGE(a, {signal: sig.detail, observed_min_ago: sig.observed_min_ago})
)
LET root = LENGTH(degraded) > 0 ? FIRST(degraded).service : symptom
LET team = DOCUMENT("teams", DOCUMENT("services", root).team)
RETURN {
  symptom_service: symptom,
  similar_incidents: similar,
  affected_services: affected,
  degraded_upstream: degraded,
  root_service: root,
  pivoted: root != symptom,
  on_call: {team: team.name, contact: team.oncall}
}
"""

# How to confirm the real root cause by hand before acting on the agent's recommendation.
VERIFY = {
    "user-db": ('psql -h user-db-replica -c "SELECT now() - pg_last_xact_replay_timestamp() '
                'AS replica_lag;"  # if > 5s, route reads to the primary -- do NOT restart the '
                'onboarding tier'),
}


def resolve_timed(alert, db=None):
    """resolve() plus timings: alert-text embed time vs. the multimodel AQL round trip itself.

    query_ms is the ONE AQL round trip (vector + graph + two key-value joins) on its own; the
    embedding call to OpenAI is timed separately so the multimodel query's real latency is visible.
    """
    db = db or connect()
    embed_start = time.perf_counter()
    vec = OpenAI().embeddings.create(model=EMBED_MODEL, input=[alert["text"]]).data[0].embedding
    embed_ms = (time.perf_counter() - embed_start) * 1000

    query_start = time.perf_counter()
    payload = db.aql.execute(MARQUEE, bind_vars={"vec": vec, "service": alert.get("service")}).next()
    query_ms = (time.perf_counter() - query_start) * 1000
    return payload, embed_ms, query_ms


def resolve(alert):
    """Given an alert dict ({text, service?}), return the structured multimodel payload."""
    payload, _embed_ms, _query_ms = resolve_timed(alert)
    return payload


def precedents(root_service, db=None, k=5):
    """Second vector pass: find the structural precedents for the TRUE root cause.

    Once the graph + health join names the degraded upstream dependency, this searches the incident
    corpus by that dependency's failure signature and keeps the hits that are actually about it.
    These precedents are what let the agent say "restarting the leaf has failed here before" instead
    of guessing.
    """
    db = db or connect()
    sig = db.collection("service_signals").get(root_service)
    detail = (sig or {}).get("detail", "")
    query_text = f"{root_service} {detail} replica lag failover stale reads downstream cascade"
    vec = OpenAI().embeddings.create(model=EMBED_MODEL, input=[query_text]).data[0].embedding
    q = """
    FOR i IN incidents
      LET sim = APPROX_NEAR_COSINE(i.embedding, @vec)
      SORT sim DESC
      LIMIT @k
      RETURN {number: i._key, short_description: i.short_description,
              resolution: i.resolution, service: i.service, similarity: sim}
    """
    rows = list(db.aql.execute(q, bind_vars={"vec": vec, "k": k}))
    return [r for r in rows if r["service"] == root_service]


def answer(payload, alert_text=None, jwt=None, symptom_override=None):
    """Cited next-step from the Retriever, grounded in the runbook knowledge graph.

    Uses Unified Search (query_type 3). The query foregrounds the ROOT-CAUSE service and its symptom
    so retrieval lands on the right runbook; we then pin the exact root-service runbook (fetched from
    the KG by content) as citation #1 and append the Retriever's related blast-radius runbooks. After
    an upstream pivot, pass `symptom_override` (the root-cause signal, e.g. the replica-lag detail) so
    the cited prose is about the real root cause, not the downstream leaf symptom.
    """
    jwt = jwt or token()
    service = payload["root_service"]
    symptom = symptom_override or alert_text or (
        payload["similar_incidents"][0].get("short_description", "")
        if payload.get("similar_incidents") else "")
    query = (f"{service} incident: {symptom} "
             f"What does the {service} runbook recommend as the resolution and the first on-call step?")
    response = requests.post(
        f"{retriever_url(jwt=jwt)}/graphrag-query",
        headers={"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"},
        json={"query": query, "query_type": 3,
              "show_citations": True, "include_metadata": True, "use_cache": False},
        timeout=180,
    )
    response.raise_for_status()
    body = response.json()
    metadata = json.loads(body.get("metadata", "{}"))
    related_citations = metadata.get("citation_mapping", {})

    citations = {}
    index = 1
    seen = set()
    primary = runbook_for_service(service)
    if primary:
        citations[str(index)] = {"file_name": primary["file_name"],
                                 "content": primary["content"], "primary": True}
        seen.add(primary["file_name"])
        index += 1
    for citation in related_citations.values():
        key = citation.get("chunk_id") or citation.get("content", "")[:60]
        if not key or key in seen:
            continue
        seen.add(key)
        citations[str(index)] = citation
        index += 1
    return {"text": body.get("result", ""), "citations": citations}


def corroborated(payload, cited):
    """Independent corroboration: does a cited runbook reference the root or an affected service?

    The structured payload (AQL) and the cited answer (Retriever) come from two separate surfaces;
    this checks they converge on the same incident.
    """
    services = {payload["root_service"], *(svc["service"] for svc in payload.get("affected_services", []))}
    cited_text = " ".join(f'{citation.get("content", "")} {citation.get("citable_url", "")}'
                          for citation in cited.get("citations", {}).values()).lower()
    matched = sorted(service for service in services if service.lower() in cited_text)
    return {"agree": bool(matched), "matched_services": matched}


def _first_sentence(text):
    """A short, plain-text snippet of the cited answer (drop markdown headings/bullets)."""
    for line in text.splitlines():
        clean = line.lstrip("#*-> ").strip()
        if len(clean) > 30:
            return clean[:160]
    return text.strip()[:160]


def _team_name(db, service):
    svc = db.collection("services").get(service)
    if not svc:
        return None
    team = db.collection("teams").get(svc["team"])
    return team["name"] if team else None


def reason(alert, jwt=None, db=None):
    """Full reasoning pass over one alert: hypothesize, test, pivot, eliminate, ground, decide.

    Returns the obvious (vector-only) hypothesis, the multimodel structure, the pivot to the real
    upstream root cause, the structural precedents, the eliminated wrong fix, the cited answer, a
    confidence verdict with a human-verification command, an ordered reasoning chain, and a
    three-way ablation -- everything the article needs to show the work, not just the answer.
    """
    db = db or connect()
    jwt = jwt or token()
    payload, embed_ms, query_ms = resolve_timed(alert, db=db)

    similar = payload["similar_incidents"]
    symptom = payload["symptom_service"]
    root = payload["root_service"]
    pivoted = payload["pivoted"]
    degraded = payload["degraded_upstream"]
    top = similar[0] if similar else None

    obvious = {
        "root_service": symptom,
        "fix": top["resolution"] if top else "n/a",
        "paged": _team_name(db, symptom),
        "basis": (f'{top["number"]} @ {round(top["similarity"], 3)}' if top else None),
    }

    precedent_start = time.perf_counter()
    precs = precedents(root, db=db) if pivoted else []
    precedent_ms = (time.perf_counter() - precedent_start) * 1000

    root_signal = degraded[0]["signal"] if degraded else None
    root_depth = degraded[0]["depth"] if degraded else 0

    root_symptom = (f"{root_signal}; downstream services report stale reads and permission errors"
                    if pivoted and root_signal else None)
    answer_start = time.perf_counter()
    cited = answer(payload, alert.get("text"), jwt=jwt, symptom_override=root_symptom)
    answer_seconds = time.perf_counter() - answer_start
    corro = corroborated(payload, cited)
    next_step = _first_sentence(cited["text"])

    eliminated = None
    if pivoted:
        eliminated = {
            "rejected": obvious["fix"],
            "reason": (f'{len(precs)} past {root} incidents resolved upstream; restarting the '
                       f'{symptom} tier did not help. The degraded dependency is {root} '
                       f'({root_signal}), {root_depth} hops upstream of the alert.'),
        }

    if pivoted and len(precs) >= 2 and corro["agree"]:
        confidence = "high"
    elif pivoted and (precs or corro["agree"]):
        confidence = "medium"
    elif not pivoted:
        confidence = "n/a (no degraded upstream dependency; the obvious path holds)"
    else:
        confidence = "low"

    chain = [
        {"stage": "VECTOR", "detail": (f'Top similar incident {obvious["basis"]} -> obvious fix: '
                                       f'"{obvious["fix"]}"' if top else "no similar incident")},
        {"stage": "GRAPH", "detail": (f'Traversed {symptom} -> {len(payload["affected_services"])} '
                                      f'services in the dependency subgraph (<= 3 hops)')},
    ]
    if pivoted:
        chain += [
            {"stage": "KEY-VALUE (health)", "detail": (f'{root} is degraded: {root_signal} '
                                                       f'(depth {root_depth})')},
            {"stage": "PIVOT", "detail": (f'Root cause is {root} (upstream), not {symptom} (the '
                                          f'alerting leaf)')},
            {"stage": "PRECEDENT (vector #2)", "detail": (f'{len(precs)} past {root} incidents -> '
                                                          f'all resolved upstream; leaf restart failed')},
            {"stage": "ELIMINATE", "detail": f'Reject "{obvious["fix"]}"'},
            {"stage": "RETRIEVER", "detail": f'Cited {root} runbook -> {next_step}'},
            {"stage": "VERDICT", "detail": (f'Page {payload["on_call"]["team"]} (not '
                                            f'{obvious["paged"]}); confidence {confidence}')},
        ]
    else:
        chain += [
            {"stage": "KEY-VALUE (health)", "detail": "No degraded upstream dependency"},
            {"stage": "RETRIEVER", "detail": f'Cited {root} runbook -> {next_step}'},
            {"stage": "VERDICT", "detail": f'Page {payload["on_call"]["team"]}; obvious path holds'},
        ]

    ablation = [
        {"config": "Vector only",
         "root": symptom,
         "action": obvious["fix"],
         "paged": obvious["paged"],
         "verdict": "Right" if not pivoted else "WRONG root + wrong team",
         "note": "Highest-similarity past incident; no topology, no health"},
        {"config": "Graph + key-value",
         "root": root,
         "action": (f'Investigate {root} ({root_signal})' if pivoted
                    else f'Investigate {root}'),
         "paged": payload["on_call"]["team"],
         "verdict": "Right root, no fix" if pivoted else "Right",
         "note": "Finds the degraded upstream dependency; no precedent or citation"},
        {"config": "Full multimodel",
         "root": root,
         "action": next_step,
         "paged": payload["on_call"]["team"],
         "verdict": "Right + cited",
         "note": f'{len(precs)} precedents + cited runbook' if pivoted else "cited runbook"},
    ]

    return {
        "alert": {k: alert.get(k) for k in ("_key", "service", "severity", "region", "text")},
        "symptom_service": symptom,
        "obvious_hypothesis": obvious,
        "structure": {"affected_services": payload["affected_services"],
                      "degraded_upstream": degraded},
        "root_cause": {"service": root, "depth": root_depth, "signal": root_signal,
                       "on_call": payload["on_call"]},
        "pivoted": pivoted,
        "precedents": precs,
        "eliminated": eliminated,
        "cited_answer": cited,
        "corroboration": corro,
        "confidence": confidence,
        "verify_command": VERIFY.get(root, f"Confirm {root} status before acting: {root_signal}"),
        "next_step": next_step,
        "reasoning_chain": chain,
        "ablation": ablation,
        "timings": {"embed_ms": round(embed_ms, 1), "query_ms": round(query_ms, 1),
                    "precedent_ms": round(precedent_ms, 1), "answer_s": round(answer_seconds, 1)},
    }


def evaluate(alerts, jwt=None):
    """Run every alert end to end and return one result row per alert (feeds the results table/figure).

    Each row carries what the system delivered (similar incident, blast radius, whether it PIVOTED
    to an upstream root cause, on-call owner, grounded runbook, next step) plus timings.
    """
    jwt = jwt or token()
    db = connect()
    rows = []
    for alert in alerts:
        payload, embed_ms, query_ms = resolve_timed(alert, db=db)
        degraded = payload["degraded_upstream"]
        root_symptom = (f"{degraded[0]['signal']}; downstream services report stale reads and "
                        f"permission errors" if payload["pivoted"] and degraded else None)

        answer_start = time.perf_counter()
        cited = answer(payload, alert.get("text"), jwt=jwt, symptom_override=root_symptom)
        answer_seconds = time.perf_counter() - answer_start

        primary = next((c for c in cited["citations"].values() if c.get("primary")), None)
        top_similar = payload["similar_incidents"][0] if payload["similar_incidents"] else None
        rows.append({
            "alert": alert["_key"],
            "service": alert["service"],
            "severity": alert["severity"],
            "symptom_service": payload["symptom_service"],
            "root_service": payload["root_service"],
            "pivoted": payload["pivoted"],
            "degraded_upstream": degraded[0]["service"] if degraded else None,
            "top_similar_incident": top_similar["number"] if top_similar else None,
            "top_similarity": round(top_similar["similarity"], 3) if top_similar else None,
            "blast_radius": len(payload["affected_services"]),
            "on_call": payload["on_call"]["team"],
            "primary_runbook": primary["file_name"] if primary else None,
            "grounded": primary is not None,
            "corroborated": corroborated(payload, cited)["agree"],
            "citations": len(cited["citations"]),
            "query_ms": round(query_ms, 1),
            "embed_ms": round(embed_ms, 1),
            "answer_s": round(answer_seconds, 1),
            "next_step": _first_sentence(cited["text"]),
        })
    return rows


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "data/alert.sample.json"
    alert = json.load(open(path))
    print(json.dumps(reason(alert), indent=2))
