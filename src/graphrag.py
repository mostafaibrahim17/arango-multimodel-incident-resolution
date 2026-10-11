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
GRAPHRAG_DB = os.environ.get("GRAPHRAG_DB", "incident_demo")
GRAPHRAG_PROJECT = os.environ.get("GRAPHRAG_PROJECT", "incident-runbook-autograph")


def kg_db():
    """python-arango handle for the database holding the GraphRAG knowledge graph."""
    return ArangoClient(hosts=HOST).db(GRAPHRAG_DB, username=USER, password=PWD)


def runbook_for_service(svc):
    """The KG runbook for this service, grounded on the runbook BODY (not metadata).

    AutoGraph stores each runbook in `{project}_sources` with the body in `content` and a
    `**Service:** <svc>` header line. We match on that line -- NOT on file_name or citable_url:
    AutoGraph leaves citable_url empty, and the file_name on the derived `_Documents` rows can be
    misaligned with the content. `_sources` keeps filename and content aligned, so it yields both
    the right body and a clean runbook name. This pins the answer to the exact runbook for the root
    service the multimodel query identified, instead of relying on semantic retrieval alone.
    """
    q = (f"FOR d IN `{GRAPHRAG_PROJECT}_sources` "
         "FILTER CONTAINS(LOWER(d.content), CONCAT('**service:** ', @svc)) "
         "LIMIT 1 RETURN {file_name: d.filename, content: d.content}")
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


def _postfix(service_type, jwt=None, project=None):
    """Postfix = the tail of the serviceId after its type prefix.

    serviceId conventions on this platform:
      arangodb-graphrag-importer-<pf>  /  arangodb-graphrag-retriever-<pf>  /  arangodb-autograph-<pf>
    The importer keeps a '-0' replica suffix that IS part of the postfix
    (arangodb-graphrag-importer-sa1wc-0 -> 'sa1wc-0'). When several services of a type exist
    (one per project), pass `project` to disambiguate by genaiProjectName.
    """
    prefixes = ([f"arangodb-graphrag-{service_type}-"] if service_type in ("importer", "retriever")
                else [f"arangodb-{service_type}-", f"arangodb-graphrag-{service_type}-"])
    for s in list_services(jwt):
        if project and s.get("genaiProjectName") != project:
            continue
        sid = s.get("serviceId", "")
        for prefix in prefixes:
            if sid.startswith(prefix):
                return sid[len(prefix):]
    raise RuntimeError(f"no {service_type} service found for project {project!r} in list_services "
                       "(create the AutoGraph project / deploy the service via the UI first)")


def importer_postfix(jwt=None):
    return _postfix("importer", jwt)


def retriever_postfix(jwt=None):
    # one retriever per project -> disambiguate to the AutoGraph project's retriever
    return _postfix("retriever", jwt, project=GRAPHRAG_PROJECT)


def autograph_postfix(jwt=None):
    return _postfix("autograph", jwt, project=GRAPHRAG_PROJECT)


def importer_url(postfix=None, jwt=None):
    return f"{HOST}/graphrag/importer/{postfix or importer_postfix(jwt)}/v1"


def retriever_url(postfix=None, jwt=None):
    return f"{HOST}/graphrag/retriever/{postfix or retriever_postfix(jwt)}/v1"


def autograph_url(postfix=None, jwt=None):
    # AutoGraph control plane: import-multiple, corpus/builds, rag-strategizer, orchestrate
    return f"{HOST}/autograph/{postfix or autograph_postfix(jwt)}/v1"


if __name__ == "__main__":
    jwt = token()
    print("auth: ok")
    for s in list_services(jwt):
        print(" ", s.get("serviceId"), "->", s.get("status"), "| project", s.get("genaiProjectName"))
