"""Shared helpers for the GraphRAG layer: auth + service discovery.

The GraphRAG Importer/Retriever/AutoGraph run as platform services. We never hardcode
their serviceId postfixes (a reinstall changes them) -- we discover them from the ACP
control plane at runtime and confirm against the health endpoint.
"""
import os

import requests
from arango import ArangoClient
from dotenv import load_dotenv

load_dotenv()

HOST = os.environ["ARANGO_HOST"].rstrip("/")  # gateway base, e.g. https://your-deployment.arango.ai
USER = os.environ["ARANGO_USER"]
PWD = os.environ["ARANGO_PASSWORD"]
GRAPHRAG_DB = os.environ.get("GRAPHRAG_DB", "_system")
GRAPHRAG_PROJECT = os.environ.get("GRAPHRAG_PROJECT", "test-incident-demo")


def kg_db():
    """python-arango handle for the database holding the GraphRAG knowledge graph."""
    return ArangoClient(hosts=HOST).db(GRAPHRAG_DB, username=USER, password=PWD)


def runbook_for_service(svc):
    """The KG runbook Document whose citable_url targets this service.

    Lets the agent ground the answer in the exact runbook for the root service the multimodel
    query precisely identified, rather than relying on fuzzy semantic retrieval alone.
    """
    q = (f"FOR d IN `{GRAPHRAG_PROJECT}_Documents` FILTER CONTAINS(LOWER(d.citable_url), @svc) "
         "LIMIT 1 RETURN {citable_url: d.citable_url, file_name: d.file_name, content: d.content}")
    rows = list(kg_db().aql.execute(q, bind_vars={"svc": svc.lower()}))
    return rows[0] if rows else None


def token():
    """Mint a user JWT via the documented auth endpoint (POST /_open/auth)."""
    r = requests.post(f"{HOST}/_open/auth", json={"username": USER, "password": PWD}, timeout=20)
    r.raise_for_status()
    return r.json()["jwt"]


def list_services(jwt=None):
    """All installed ACP services (POST /_platform/acp/v1/list_services)."""
    jwt = jwt or token()
    r = requests.post(
        f"{HOST}/_platform/acp/v1/list_services",
        headers={"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"},
        json={},
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("services", [])


def _postfix(service_type, jwt=None):
    """Postfix = everything after 'arangodb-graphrag-{type}-' in the serviceId.

    NB: the Importer carries a '-0' replica suffix that IS part of the postfix
    (e.g. arangodb-graphrag-importer-sa1wc-0 -> 'sa1wc-0').
    """
    prefix = f"arangodb-graphrag-{service_type}-"
    for s in list_services(jwt):
        sid = s.get("serviceId", "")
        if sid.startswith(prefix):
            return sid[len(prefix):]
    raise RuntimeError(f"no {service_type} service found in list_services (deploy it via the UI first)")


def importer_postfix(jwt=None):
    return _postfix("importer", jwt)


def retriever_postfix(jwt=None):
    return _postfix("retriever", jwt)


def autograph_postfix(jwt=None):
    return _postfix("autograph", jwt)


def importer_url(postfix=None, jwt=None):
    return f"{HOST}/graphrag/importer/{postfix or importer_postfix(jwt)}/v1"


def retriever_url(postfix=None, jwt=None):
    return f"{HOST}/graphrag/retriever/{postfix or retriever_postfix(jwt)}/v1"


def autograph_url(jwt=None):
    return f"{HOST}/autograph/v1"


if __name__ == "__main__":
    jwt = token()
    print("auth: ok")
    for s in list_services(jwt):
        print(" ", s.get("serviceId"), "->", s.get("status"), "| project", s.get("genaiProjectName"))
