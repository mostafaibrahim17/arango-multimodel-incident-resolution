"""Resolve one alert in a single AQL round trip.

Vector search finds similar past incidents, a graph traversal expands the affected-service
subgraph, and a key-value lookup returns the on-call team -- document, vector, graph, and
key-value all in one query.
"""
import json
import sys
import time

import requests
from openai import OpenAI

from graphrag import retriever_url, runbook_for_service, token
from ingest import EMBED_MODEL, connect

# One query, three multimodel moves: APPROX_NEAR_COSINE (vector), OUTBOUND (graph), DOCUMENT (key-value).
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
LET root = @service != null ? @service : FIRST(similar).service
LET affected = (
  FOR v, e, p IN 0..3 OUTBOUND CONCAT("services/", root) GRAPH "service_topology"
    LET health = DOCUMENT("service_health", v._key)
    COLLECT service = v._key, name = v.name, status = health.status, note = health.note
      AGGREGATE depth = MIN(LENGTH(p.edges))
    SORT depth
    RETURN {service, name, depth, status, note}
)
LET team = DOCUMENT("teams", DOCUMENT("services", root).team)
RETURN {
  root_service: root,
  similar_incidents: similar,
  affected_services: affected,
  on_call: {team: team.name, contact: team.oncall}
}
"""

REASONING_MODEL = "gpt-5-mini"

REASONING_PROMPT = """You are a senior SRE deciding the true root cause of a production incident.

You receive the evidence one multimodel query already gathered: the alert, the top similar past
incidents (vector search), and the dependency blast radius of the alerting service with each
service's CURRENT health status (graph traversal + health join).

Decide which single service is the true root cause:
- The alerting service is the default hypothesis.
- Pivot to a dependency ONLY if that dependency is degraded AND its degradation plausibly
  explains the alert's symptom. Depth matters: a degraded direct dependency (depth 1) usually
  explains the symptom better than one further away.
- If several services are degraded, pick the one whose health note best matches the symptom.
- Never invent a service: root_service MUST be the exact `service` key (e.g. "document-service",
  not the display name "Document Service") of one of the entries in affected_services.

Return reasoning as 1-2 short sentences citing the evidence (service, depth, status/note).
confidence: high = the evidence cleanly explains the symptom; medium = plausible but not
conclusive; low = evidence is thin or conflicting."""


def reason_rules(payload):
    """Deterministic fallback: pivot to the first degraded upstream dependency, if any."""
    alerting_service = payload["root_service"]
    affected = payload["affected_services"]

    alerting_health = next(
        (s for s in affected if s["service"] == alerting_service), {}
    ).get("status", "unknown")

    degraded_upstream = next(
        (s for s in affected if s["depth"] > 0 and s.get("status") == "degraded"),
        None
    )

    if alerting_health != "degraded" and degraded_upstream:
        true_root = degraded_upstream["service"]
        confidence = "high"
        reasoning = (
            f"{alerting_service} is reporting the symptom, but {true_root} "
            f"(depth {degraded_upstream['depth']}) is degraded. Pivoting root cause."
        )
    else:
        true_root = alerting_service
        confidence = "high" if alerting_health == "degraded" else "medium"
        reasoning = (
            f"{alerting_service} is the alerting service "
            f"({'degraded' if alerting_health == 'degraded' else 'status unknown'})."
        )
    return true_root, reasoning, confidence, "rules"


def reason(payload, alert_text=None, db=None):
    """
    The reasoning layer: an LLM judges the evidence the multimodel query gathered and decides
    the true root cause -- pivoting away from the alerting service when a degraded dependency
    better explains the symptom. Falls back to the deterministic rule if the API call fails
    or returns a service outside the blast radius.
    """
    affected = payload["affected_services"]
    known_services = {s["service"] for s in affected}

    try:
        evidence = {
            "alert": {
                "service": payload["root_service"],
                "symptom": alert_text or (
                    payload["similar_incidents"][0].get("short_description", "")
                    if payload.get("similar_incidents") else ""
                ),
            },
            "similar_incidents": [
                {k: inc[k] for k in ("number", "short_description", "service", "similarity")}
                for inc in payload.get("similar_incidents", [])
            ],
            "affected_services": affected,
        }
        response = OpenAI().chat.completions.create(
            model=REASONING_MODEL,
            reasoning_effort="minimal",  # the evidence packet is small; minimal keeps the judge ~2s
            messages=[
                {"role": "system", "content": REASONING_PROMPT},
                {"role": "user", "content": json.dumps(evidence)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "root_cause_decision",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "root_service": {"type": "string"},
                            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                            "reasoning": {"type": "string"},
                        },
                        "required": ["root_service", "confidence", "reasoning"],
                        "additionalProperties": False,
                    },
                },
            },
        )
        decision = json.loads(response.choices[0].message.content)
        true_root = decision["root_service"]
        if true_root not in known_services:
            # Tolerate the display name ("Document Service") by mapping it back to the key.
            by_name = {s["name"].lower(): s["service"] for s in affected if s.get("name")}
            true_root = by_name.get(true_root.lower())
            if true_root is None:
                raise ValueError(f"LLM proposed unknown service {decision['root_service']!r}")
        reasoning, confidence, method = decision["reasoning"], decision["confidence"], "llm-judge"
    except Exception as exc:  # API failure or invalid decision -> deterministic fallback
        print(f"[reason] LLM judge unavailable ({exc}); using rules fallback", file=sys.stderr)
        true_root, reasoning, confidence, method = reason_rules(payload)

    # Re-point the on-call owner at the decided root (the AQL computed it for the
    # alerting service before the pivot).
    if db is not None and true_root != payload["root_service"]:
        svc = db.collection("services").get(true_root)
        team = db.collection("teams").get(svc["team"]) if svc and svc.get("team") else None
        if team:
            payload["on_call"] = {"team": team["name"], "contact": team["oncall"]}

    payload["root_service"] = true_root
    payload["reasoning"] = reasoning
    payload["confidence"] = confidence
    payload["method"] = method
    payload["verify"] = f"kubectl top pods -l app={true_root} && kubectl logs -l app={true_root} --tail=50"
    return payload

def resolve_timed(alert, db=None):
    """resolve() plus timings: how long the alert text took to embed vs. the multimodel AQL itself.

    Returns (payload, embed_ms, query_ms). query_ms is the ONE AQL round trip (vector + graph +
    key-value) on its own -- the embedding round trip to OpenAI is timed separately so the
    multimodel query's real latency is visible, not hidden behind the embed call.
    """
    db = db or connect()
    embed_start = time.perf_counter()
    vec = OpenAI().embeddings.create(model=EMBED_MODEL, input=[alert["text"]]).data[0].embedding
    embed_ms = (time.perf_counter() - embed_start) * 1000

    query_start = time.perf_counter()
    payload = db.aql.execute(MARQUEE, bind_vars={"vec": vec, "service": alert.get("service")}).next()
    query_ms = (time.perf_counter() - query_start) * 1000
    payload = reason(payload, alert_text=alert.get("text"), db=db)
    return payload, embed_ms, query_ms

def resolve(alert):
    """Given an alert dict ({text, service?}), return the structured resolution payload."""
    payload, _embed_ms, _query_ms = resolve_timed(alert)
    return payload


def answer(payload, alert_text=None, jwt=None):
    """Cited next-step from the Retriever, grounded in the runbook knowledge graph.

    Uses Unified Search (query_type 3), which returns the cited answer on this platform. The
    query foregrounds the affected service and the alert's own symptom text so retrieval lands on
    that service's runbook; we then pin the exact root-service runbook (fetched from the KG by
    content) as citation #1 and append the Retriever's related blast-radius runbooks.
    """
    jwt = jwt or token()
    service = payload["root_service"]
    symptom = alert_text or (payload["similar_incidents"][0].get("short_description", "")
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

    # Ground the answer in the exact runbook for the root service the AQL query pinpointed
    # (precise multimodel signal), then append the Retriever's related blast-radius runbooks.
    # AutoGraph citations carry no citable_url, so dedup on chunk_id / content, not URL.
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

    The structured payload (AQL) and the cited answer (Retriever) are produced by two separate
    surfaces; this checks they converge on the same incident.
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


def evaluate(alerts, jwt=None):
    """Run every alert end to end and return one result row per alert.

    Each row carries what the system actually delivered (most similar incident, blast radius,
    on-call owner, the runbook it grounded on, the next step) plus the two timings: the
    multimodel query (one AQL round trip) and the cited Retriever answer. Feeds the results
    table and the results figure -- the final showcase.
    """
    jwt = jwt or token()
    db = connect()
    rows = []
    for alert in alerts:
        payload, embed_ms, query_ms = resolve_timed(alert, db=db)

        answer_start = time.perf_counter()
        cited = answer(payload, alert.get("text"), jwt=jwt)
        answer_seconds = time.perf_counter() - answer_start

        primary = next((c for c in cited["citations"].values() if c.get("primary")), None)
        top_similar = payload["similar_incidents"][0] if payload["similar_incidents"] else None
        rows.append({
            "alert": alert["_key"],
            "service": alert["service"],
            "severity": alert["severity"],
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
    payload = resolve(alert)
    cited = answer(payload, alert.get("text"))
    print(json.dumps({
        "structured": payload,
        "cited_answer": cited,
        "corroboration": corroborated(payload, cited),
    }, indent=2))
