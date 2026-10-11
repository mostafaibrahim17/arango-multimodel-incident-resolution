"""Create the schema, ingest and embed the incident corpus, and build the service topology.

One multimodel store backs the whole pipeline:
  - a document collection of incidents with vector-indexed embeddings (similarity recall),
  - a named graph of service dependencies (traversal),
  - key-value collections for service health and on-call teams (lookups),
  - a collection of live alerts, also embedded.

Service health is stored per scenario: each incident scenario has its own snapshot of which
services are degraded, so an alert is resolved against the health state of its own scenario.
"""
import json
import os

from arango import ArangoClient
from datasets import load_dataset
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

HOST = os.environ["ARANGO_HOST"]
DB = os.environ.get("ARANGO_DB", "incident_demo")
USER = os.environ["ARANGO_USER"]
PWD = os.environ["ARANGO_PASSWORD"]

EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 1536
DATASET = "6StringNinja/synthetic-servicenow-incidents"


def connect():
    """Connect to the application database, creating it if needed."""
    client = ArangoClient(hosts=HOST)
    sys_db = client.db("_system", username=USER, password=PWD)
    if not sys_db.has_database(DB):
        sys_db.create_database(DB)
    return client.db(DB, username=USER, password=PWD)


def embed(client, texts, batch=100):
    """Embed texts with OpenAI, batched to stay within request limits."""
    out = []
    for i in range(0, len(texts), batch):
        resp = client.embeddings.create(model=EMBED_MODEL, input=texts[i : i + batch])
        out.extend(d.embedding for d in resp.data)
    return out


def main():
    db = connect()
    topo = json.load(open("data/topology.json"))

    # Drop and recreate the collections and graph so the script is idempotent:
    # re-running it always leaves the store in the same state.
    if db.has_graph("service_topology"):
        db.delete_graph("service_topology")
    for c in ["incidents", "services", "teams", "alerts", "service_depends_on", "service_health"]:
        if db.has_collection(c):
            db.delete_collection(c)

    incidents = db.create_collection("incidents")
    services = db.create_collection("services")
    teams = db.create_collection("teams")
    alerts = db.create_collection("alerts")
    graph = db.create_graph("service_topology")
    graph.create_edge_definition(
        edge_collection="service_depends_on",
        from_vertex_collections=["services"],
        to_vertex_collections=["services"],
    )

    # Service topology and on-call teams: the graph vertices/edges and the key-value team records.
    services.insert_many([{"_key": k, **v} for k, v in topo["services"].items()])
    teams.insert_many([{"_key": k, **v} for k, v in topo["teams"].items()])
    db.collection("service_depends_on").insert_many(
        [{"_from": f"services/{a}", "_to": f"services/{b}"} for a, b in topo["depends_on"]]
    )

    # Service health, per scenario. service_signals.json maps each scenario to the services that
    # are degraded in it. For every scenario, every service in the topology gets a health row
    # (default healthy), and the services named in that scenario override with their status and
    # detail. Rows are keyed `<scenario>__<service>` so the resolver can look up the health state
    # for the scenario the alert belongs to. The reasoning layer reads these to decide whether to
    # pivot the root cause to a degraded dependency.
    signals = json.load(open("data/service_signals.json"))
    service_health = db.create_collection("service_health")
    health_rows = []
    for scenario, degraded_map in signals.items():
        for svc in topo["services"]:
            sig = degraded_map.get(svc, {})
            health_rows.append({
                "_key": f"{scenario}__{svc}",
                "scenario": scenario,
                "service": svc,
                "status": sig.get("status", "healthy"),
                "note": sig.get("detail", ""),
            })
    service_health.insert_many(health_rows)

    oai = OpenAI()

    # Incident corpus: the public dataset provides breadth, and the scenario seed incidents provide
    # the domain-specific cases the alerts are meant to recall. Both share one schema and carry a
    # `service`; the dataset rows derive theirs from the category map, the seeds carry theirs
    # directly. They are embedded and indexed together as a single corpus.
    ds = load_dataset(DATASET, split="train")
    cat2svc = topo["category_to_service"]
    seeds = json.load(open("data/seed_incidents.json"))

    ds_records = [
        {
            "_key": r["number"],
            "short_description": r["short_description"],
            "description": r["description"],
            "resolution": r["resolution"],
            "category": r["category"],
            "assignment_group": r["assignment_group"],
            "urgency": r["urgency"],
            "impact": r["impact"],
            "service": cat2svc.get(r["category"], "unknown"),
        }
        for r in ds
    ]
    seed_records = [
        {
            "_key": s["_key"],
            "short_description": s["short_description"],
            "description": s["description"],
            "resolution": s["resolution"],
            "category": s.get("category", ""),
            "assignment_group": s.get("assignment_group", ""),
            "urgency": s.get("urgency", ""),
            "impact": s.get("impact", ""),
            "service": s["service"],
        }
        for s in seeds
    ]
    all_records = ds_records + seed_records

    texts = [f'{r["short_description"]} {r["description"]}' for r in all_records]
    vectors = embed(oai, texts)
    incidents.insert_many(
        [{**rec, "embedding": v} for rec, v in zip(all_records, vectors)]
    )

    # Build the vector index after the documents are loaded, so it trains on the full corpus.
    incidents.add_index(
        {
            "name": "incidents_vec",
            "type": "vector",
            "fields": ["embedding"],
            "params": {"metric": "cosine", "dimension": EMBED_DIM, "nLists": 10},
        }
    )

    # Alerts: stored for reference. They are not vector-searched, the resolver embeds each alert's
    # text at query time, so no alert embedding or vector index is built here. (An earlier version
    # built an `alerts_vec` index that nothing ever queried.)
    alert_docs = json.load(open("data/alerts.json"))
    alerts.insert_many(alert_docs)

    print(
        f"ingested {incidents.count()} incidents ({len(seed_records)} seeded), "
        f"{alerts.count()} alerts, {services.count()} services, "
        f"{teams.count()} teams, {len(topo['depends_on'])} dependency edges, "
        f"{service_health.count()} health rows across {len(signals)} scenarios"
    )


if __name__ == "__main__":
    main()