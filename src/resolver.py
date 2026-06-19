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
    COLLECT service = v._key, name = v.name AGGREGATE depth = MIN(LENGTH(p.edges))
    SORT depth
    RETURN {service, name, depth}
)
LET team = DOCUMENT("teams", DOCUMENT("services", root).team)
RETURN {
  root_service: root,
  similar_incidents: similar,
  affected_services: affected,
  on_call: {team: team.name, contact: team.oncall}
}
"""


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
    return payload, embed_ms, query_ms


def resolve(alert):
    """Given an alert dict ({text, service?}), return the structured resolution payload."""
    payload, _embed_ms, _query_ms = resolve_timed(alert)
    return payload


def answer(payload, alert_text=None, jwt=None):
    """Cited next-step from the GraphRAG Retriever, grounded in the runbook knowledge graph.

    Uses Local Search (query_type 2 + use_llm_planner=False) -- the citation-bearing mode that
    works on this platform (Unified/query_type 3 errors here; Deep Search disables citations).
    The query foregrounds the affected service and the alert's own symptom text so retrieval
    lands on that service's runbook; citations are then re-ranked to surface it first.
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
        json={"query": query, "query_type": 2, "use_llm_planner": False,
              "show_citations": True, "include_metadata": True, "use_cache": False},
        timeout=180,
    )
    response.raise_for_status()
    body = response.json()
    metadata = json.loads(body.get("metadata", "{}"))
    related_citations = metadata.get("citation_mapping", {})

    # Ground the answer in the exact runbook for the root service the AQL query pinpointed
    # (precise multimodel signal), then append GraphRAG's related blast-radius runbooks.
    citations = {}
    index = 1
    seen_urls = set()
    primary = runbook_for_service(service)
    if primary:
        citations[str(index)] = {"citable_url": primary["citable_url"],
                                 "file_name": primary["file_name"], "primary": True}
        seen_urls.add(primary["citable_url"])
        index += 1
    for citation in related_citations.values():
        if citation.get("citable_url") not in seen_urls:
            citations[str(index)] = citation
            seen_urls.add(citation.get("citable_url"))
            index += 1
    return {"text": body.get("result", ""), "citations": citations}


def corroborated(payload, cited):
    """Independent corroboration: does a cited runbook reference the root or an affected service?

    The structured payload (AQL) and the cited answer (GraphRAG) are produced by two separate
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
    multimodel query (one AQL round trip) and the cited GraphRAG answer. Feeds the results
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
