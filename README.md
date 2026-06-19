# Incident-Resolution Agent on the Arango Contextual Data Platform

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/mostafaibrahim17/arango-multimodel-incident-resolution/blob/main/incident_resolution.ipynb)

An incident-resolution agent that, given a live alert, returns in one pass: the most similar past
incidents, the affected-service subgraph, the on-call owner, **and** a cited, runbook-grounded
next step. Tickets, service topology, alerts, and runbooks all live in **one** Arango Contextual
Data Platform deployment — no separate vector store, graph database, or stitched-together pipeline.

> **TL;DR.** A P1 alert on `onboarding-api` comes in. One AQL query returns 3 similar past
> incidents (vector), the 8-service blast radius (graph), and the on-call team (key-value), in a
> single round trip. The GraphRAG layer then grounds a natural-language fix in the exact
> `onboarding-api` runbook and the related runbooks across the blast radius — with clickable
> citations. **8/8 demo alerts ground in the correct runbook.** This is the support-engineering
> use case proven out in production at scale (Zscaler: 40K+ daily AI requests on the same
> platform); here it is simulated end to end on a public dataset you can run yourself.

![Architecture: one alert in → one query (vector + graph + key-value) + a grounded, cited answer](assets/architecture.jpg)

## Contents
- [What one alert returns](#what-one-alert-returns) · [How it works](#how-it-works) · [The multimodel query](#the-multimodel-query) · [Setup](#setup) · [Run](#run) · [Dataset & topology](#dataset--topology) · [Deployed services](#deployed-services) · [Repository contents](#repository-contents) · [Status](#status)

## What one alert returns
`python resolver.py alert.sample.json` on a P1 `onboarding-api` alert returns a single JSON payload:

- **`structured`** (one AQL round trip):
  - `similar_incidents` — 3 ranked past incidents with their resolutions (vector / `APPROX_NEAR_COSINE`)
  - `affected_services` — the 8-service subgraph by blast-radius depth (graph / `OUTBOUND` traversal)
  - `on_call` — the owning team + contact (key-value / `DOCUMENT` lookup)
- **`cited_answer`** — a natural-language next step grounded in the runbook knowledge graph, with
  the **exact root-service runbook as the primary citation** plus the related blast-radius runbooks.
- **`corroboration`** — an independent check that the cited runbooks fall inside the affected subgraph
  (the precise AQL surface and the GraphRAG surface agree).

Across the 8 demo alerts (`alerts.json`), the primary citation is the correct service's runbook
**8/8**, and corroboration is **8/8** — captured in `runlog/`.

## How it works
Two surfaces over one platform, joined by the agent:

1. **Multimodel core** (database `incident_demo`): incidents (with embeddings), a curated service
   topology (named graph), on-call teams, and stored alerts. One AQL query does vector + graph +
   key-value in a single round trip — no application-side joins.
2. **GraphRAG knowledge graph** (project `test-incident-demo`): the runbooks, imported into entities,
   relationships, communities, and chunk embeddings, queried through the GraphRAG Retriever.

The agent (`resolver.py`) uses the **precise** root service from the multimodel query to ground the
answer in that service's exact runbook, and the **semantic** GraphRAG retrieval to add the related
runbooks across the incident's blast radius. Precise scope + grounded, cited context.

The graph traversal returns the real blast radius of the headline alert — the root service in red,
then the services that depend on it by depth:

![Affected-service subgraph for the onboarding-api alert, colored by blast-radius depth](assets/affected-subgraph.png)

And the runbooks import into a real knowledge graph — each runbook a hub, entities clustering around
it, with the entities that appear in more than one runbook bridging them (red):

![GraphRAG knowledge graph: 80 entities extracted from 11 runbooks](assets/knowledge-graph.png)

> Both data figures are regenerated from the live deployment by `python viz.py` (→ `assets/`); the
> headline architecture diagram is a static asset. `assets/architecture-schematic.png` is the same
> architecture rendered purely from code if you prefer a reproducible version.

## The multimodel query
The marquee query (`resolver.py:MARQUEE`) — one store, one language, three moves:

- `APPROX_NEAR_COSINE(i.embedding, @vec)` — nearest past incidents (vector)
- `0..3 OUTBOUND ... GRAPH "service_topology"` — affected-service subgraph, deduped to shortest depth (graph)
- `DOCUMENT("teams", DOCUMENT("services", root).team)` — on-call owner (key-value)

## Setup
```bash
pip install -r requirements.txt
cp .env.example .env   # fill in ARANGO_* + OPENAI_API_KEY + GRAPHRAG_PROJECT/GRAPHRAG_DB
```
> On Apple Silicon, run the scripts with `arch -arm64 python3 …` (the Python here is a universal binary).

## Run
```bash
python ingest.py                       # 1. multimodel core: 500 incidents + 8 alerts + topology
python graphrag_ingest.py              # 2. import runbooks -> knowledge graph (skip-if-built; --reset to rebuild)
python resolver.py alert.sample.json   # 3. one alert -> structured payload + cited, grounded answer
# or the whole pipeline at once:
python run_all.py
```
The notebook `incident_resolution.ipynb` walks the same flow, importing the same functions.

## Dataset & topology
| Shape | Source | Notes |
|---|---|---|
| Incident tickets | [`6StringNinja/synthetic-servicenow-incidents`](https://huggingface.co/datasets/6StringNinja/synthetic-servicenow-incidents) (HF, MIT, 500 rows) | Loaded at runtime, not vendored. Embeds `short_description + description`; keeps `resolution`. |
| Live alerts | `alerts.json` (8 synthetic) | Varied service / severity / region / telemetry. |
| Runbooks | `runbooks/` (11 hand-authored, by service-family module) | The cited source-of-truth knowledge graph. |

The data is a **simulation** of an incident estate (breadth across P1–P3 and across infra → app
services), disclosed as synthetic. The service topology is hand-curated (`topology.json`) since the
dataset ships no CMDB; in production you derive it from your real service map. For a larger, real
corpus, [`Loukh1/IT-incidents`](https://huggingface.co/datasets/Loukh1/IT-incidents) (MIT, 4,040 rows)
is the documented scale-up path.

## Deployed services
The GraphRAG **Importer** and **Retriever** are deployed once through the platform web UI (on the
current platform version the deploy is UI-driven), then everything else — import, knowledge-graph
build, queries — is scriptable via the data-plane API. **AutoGraph** (which automatically discovers
domains and assigns per-domain retrieval treatment) is shown as a web-UI walkthrough. Service IDs
are discovered at runtime (`graphrag.py`), never hardcoded.

## Repository contents
```
ingest.py              multimodel core: schema, embed tickets, build topology, store alerts
graphrag.py            auth + service discovery + the KG runbook lookup
graphrag_ingest.py     import runbooks into the knowledge graph + verify (skip-if-built / --reset)
resolver.py            the marquee AQL query + the cited, grounded answer + corroboration
run_all.py             the whole pipeline in one command
viz.py                 regenerate the affected-subgraph + knowledge-graph figures from live data
incident_resolution.ipynb   narrated walkthrough of the same flow (executed, with outputs + figures)
incident_resolution.colab.ipynb   the same notebook, outputs cleared — the "Open in Colab" runnable copy
topology.json          curated service topology (12 services, 13 dependencies, 5 teams)
alerts.json            8 synthetic alerts; alert.sample.json is the headline P1
runbooks/              hand-authored runbooks (the cited knowledge-graph corpus)
assets/                architecture diagram + the two data-driven figures
```

## Status
Multimodel core ✅ · runbook knowledge graph ✅ (11 runbooks → 80 entities, 120 relations) ·
cited, grounded combined resolver ✅ (8/8 demo alerts). The agent here is framework-free Python;
LangChain / LangGraph and Arango's built-in **Ada** assistant are documented extension points.
