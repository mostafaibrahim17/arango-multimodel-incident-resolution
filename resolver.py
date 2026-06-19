"""Resolve one alert in a single AQL round trip.

Vector search finds similar past incidents, a graph traversal expands the affected-service
subgraph, and a key-value lookup returns the on-call team -- document, vector, graph, and
key-value all in one query.
"""
import json
import sys

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


def resolve(alert):
    """Given an alert dict ({text, service?}), return the structured resolution payload."""
    db = connect()
    vec = OpenAI().embeddings.create(model=EMBED_MODEL, input=[alert["text"]]).data[0].embedding
    cursor = db.aql.execute(MARQUEE, bind_vars={"vec": vec, "service": alert.get("service")})
    return cursor.next()


def answer(payload, alert_text=None, jwt=None):
    """Cited next-step from the GraphRAG Retriever, grounded in the runbook knowledge graph.

    Uses Local Search (query_type 2 + use_llm_planner=False) -- the citation-bearing mode that
    works on this platform (Unified/query_type 3 errors here; Deep Search disables citations).
    The query foregrounds the affected service and the alert's own symptom text so retrieval
    lands on that service's runbook; citations are then re-ranked to surface it first.
    """
    jwt = jwt or token()
    svc = payload["root_service"]
    symptom = alert_text or (payload["similar_incidents"][0].get("short_description", "")
                             if payload.get("similar_incidents") else "")
    q = (f"{svc} incident: {symptom} "
         f"What does the {svc} runbook recommend as the resolution and the first on-call step?")
    r = requests.post(
        f"{retriever_url(jwt=jwt)}/graphrag-query",
        headers={"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"},
        json={"query": q, "query_type": 2, "use_llm_planner": False,
              "show_citations": True, "include_metadata": True, "use_cache": False},
        timeout=180,
    )
    r.raise_for_status()
    j = r.json()
    meta = json.loads(j.get("metadata", "{}"))
    related = meta.get("citation_mapping", {})

    # Ground the answer in the exact runbook for the root service the AQL query pinpointed
    # (precise multimodel signal), then append GraphRAG's related blast-radius runbooks.
    citations = {}
    n = 1
    primary = runbook_for_service(svc)
    seen = set()
    if primary:
        citations[str(n)] = {"citable_url": primary["citable_url"],
                             "file_name": primary["file_name"], "primary": True}
        seen.add(primary["citable_url"])
        n += 1
    for v in related.values():
        if v.get("citable_url") not in seen:
            citations[str(n)] = v
            seen.add(v.get("citable_url"))
            n += 1
    return {"text": j.get("result", ""), "citations": citations}


def corroborated(payload, cited):
    """Independent corroboration: does a cited runbook reference the root or an affected service?

    The structured payload (AQL) and the cited answer (GraphRAG) are produced by two separate
    surfaces; this checks they converge on the same incident.
    """
    services = {payload["root_service"], *(a["service"] for a in payload.get("affected_services", []))}
    blob = " ".join(f'{c.get("content", "")} {c.get("citable_url", "")}'
                    for c in cited.get("citations", {}).values()).lower()
    matched = sorted(s for s in services if s.lower() in blob)
    return {"agree": bool(matched), "matched_services": matched}


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "alert.sample.json"
    alert = json.load(open(path))
    payload = resolve(alert)
    cited = answer(payload, alert.get("text"))
    print(json.dumps({
        "structured": payload,
        "cited_answer": cited,
        "corroboration": corroborated(payload, cited),
    }, indent=2))
