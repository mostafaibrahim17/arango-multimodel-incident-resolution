"""Create the schema, ingest + embed synthetic-servicenow tickets, and build the service topology.

One multimodel store: a document collection of incidents (with vector-indexed embeddings),
a named graph of service dependencies, and a key-value style teams collection.
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

    # Schema: drop + recreate so the script is idempotent.
    if db.has_graph("service_topology"):
        db.delete_graph("service_topology")
    for c in ["incidents", "services", "teams", "alerts", "service_depends_on", "service_signals"]:
        if db.has_collection(c):
            db.delete_collection(c)

    incidents = db.create_collection("incidents")
    services = db.create_collection("services")
    teams = db.create_collection("teams")
    alerts = db.create_collection("alerts")
    signals = db.create_collection("service_signals")
    graph = db.create_graph("service_topology")
    graph.create_edge_definition(
        edge_collection="service_depends_on",
        from_vertex_collections=["services"],
        to_vertex_collections=["services"],
    )

    # Curated topology + on-call teams (graph + key-value).
    services.insert_many([{"_key": k, **v} for k, v in topo["services"].items()])
    teams.insert_many([{"_key": k, **v} for k, v in topo["teams"].items()])
    db.collection("service_depends_on").insert_many(
        [{"_from": f"services/{a}", "_to": f"services/{b}"} for a, b in topo["depends_on"]]
    )

    # Service health signals (key-value): live per-service status the resolver joins to the
    # dependency subgraph to find the degraded UPSTREAM dependency, not just the alerting leaf.
    sig = json.load(open("data/service_signals.json"))
    signals.insert_many([{"_key": k, **v} for k, v in sig.items()])

    # Tickets: load the dataset, embed short_description + description, store with resolution.
    oai = OpenAI()
    ds = load_dataset(DATASET, split="train")
    cat2svc = topo["category_to_service"]
    texts = [f'{r["short_description"]} {r["description"]}' for r in ds]
    vectors = embed(oai, texts)
    incidents.insert_many(
        [
            {
                "_key": r["number"],
                "short_description": r["short_description"],
                "description": r["description"],
                "resolution": r["resolution"],
                "category": r["category"],
                "assignment_group": r["assignment_group"],
                "urgency": r["urgency"],
                "impact": r["impact"],
                "service": cat2svc.get(r["category"], "onboarding-api"),
                "embedding": v,
            }
            for r, v in zip(ds, vectors)
        ]
    )

    # Seed incidents: a small set of hand-authored synthetic precedents (disclosed as synthetic)
    # the public dataset lacks -- a shallow "restart the leaf" decoy plus user-db replica-lag
    # cascades where the real fix was upstream. These give the resolver real structural precedent
    # to retrieve. Embedded the same way and indexed alongside the dataset rows.
    seed = json.load(open("data/seed_incidents.json"))
    seed_vecs = embed(oai, [f'{s["short_description"]} {s["description"]}' for s in seed])
    incidents.insert_many([{**s, "embedding": v} for s, v in zip(seed, seed_vecs)])

    # Vector index is created after the data loads so it trains on the full set.
    incidents.add_index(
        {
            "name": "incidents_vec",
            "type": "vector",
            "fields": ["embedding"],
            "params": {"metric": "cosine", "dimension": EMBED_DIM, "nLists": 10},
        }
    )

    # Alerts: the third data shape -- live alerts, stored + embedded so they are queryable too.
    alert_docs = json.load(open("data/alerts.json"))
    alert_vecs = embed(oai, [a["text"] for a in alert_docs])
    alerts.insert_many([{**a, "embedding": v} for a, v in zip(alert_docs, alert_vecs)])
    alerts.add_index(
        {
            "name": "alerts_vec",
            "type": "vector",
            "fields": ["embedding"],
            "params": {"metric": "cosine", "dimension": EMBED_DIM, "nLists": 2},
        }
    )

    print(
        f"ingested {incidents.count()} incidents ({len(seed)} hand-authored seeds), "
        f"{alerts.count()} alerts, {services.count()} services, {teams.count()} teams, "
        f"{len(topo['depends_on'])} dependency edges, {signals.count()} service signals"
    )


if __name__ == "__main__":
    main()
