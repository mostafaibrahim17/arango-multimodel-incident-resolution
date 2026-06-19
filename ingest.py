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
    topo = json.load(open("topology.json"))

    # Schema: drop + recreate so the script is idempotent.
    if db.has_graph("service_topology"):
        db.delete_graph("service_topology")
    for c in ["incidents", "services", "teams", "alerts", "service_depends_on"]:
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

    # Curated topology + on-call teams (graph + key-value).
    services.insert_many([{"_key": k, **v} for k, v in topo["services"].items()])
    teams.insert_many([{"_key": k, **v} for k, v in topo["teams"].items()])
    db.collection("service_depends_on").insert_many(
        [{"_from": f"services/{a}", "_to": f"services/{b}"} for a, b in topo["depends_on"]]
    )

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
    alert_docs = json.load(open("alerts.json"))
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
        f"ingested {incidents.count()} incidents, {alerts.count()} alerts, {services.count()} services, "
        f"{teams.count()} teams, {len(topo['depends_on'])} dependency edges"
    )


if __name__ == "__main__":
    main()
