# Incident-Resolution Agent on the Arango Contextual Data Platform

A live alert comes in. This agent returns, in one pass, the most similar past incidents, the
affected-service subgraph, the on-call owner, **and** a cited, runbook-grounded next step. Tickets,
service topology, alerts, and runbooks all live in **one** [Arango Contextual Data
Platform](https://docs.arango.ai/agentic-ai-suite/) deployment. No separate vector store, no graph
database, no stitched-together pipeline.

> **TL;DR.** A P1 alert on `onboarding-api` arrives. One AQL query returns 3 similar past incidents
> (vector), the 8-service blast radius (graph), and the on-call team (key-value), in a single round
> trip. The AutoGraph knowledge graph then grounds a natural-language fix in the exact `onboarding-api`
> runbook plus the related runbooks across the blast radius, with inline citations. **All 8 demo alerts
> ground on the correct runbook, and all 8 corroborate.** It's the support-engineering use case
> Zscaler runs in production at scale (40K+ daily AI requests on the same platform); here it's
> simulated end to end on a public dataset you can run yourself.

![Architecture: one alert in, one query (vector + graph + key-value) plus a grounded, cited answer](assets/architecture.jpg)

## Contents
- [What one alert returns](#what-one-alert-returns)
- [How it works](#how-it-works)
- [The multimodel query](#the-multimodel-query)
- [Results](#results)
- [Setup](#setup)
- [Run](#run)
- [Dataset & topology](#dataset--topology)
- [Deployed services](#deployed-services)
- [Repository contents](#repository-contents)
- [Status](#status)

## What one alert returns
`python src/resolver.py data/alert.sample.json` on a P1 `onboarding-api` alert returns a single JSON payload:

- **`structured`** (one AQL round trip):
  - `similar_incidents`: 3 ranked past incidents with their resolutions (vector, [`APPROX_NEAR_COSINE`](https://docs.arango.ai/arangodb/stable/aql/functions/vector/))
  - `affected_services`: the 8-service subgraph by blast-radius depth (graph, `OUTBOUND` traversal)
  - `on_call`: the owning team and contact (key-value, `DOCUMENT` lookup)
- **`cited_answer`**: a natural-language next step grounded in the runbook knowledge graph, with the
  **exact root-service runbook as the primary citation** plus the related blast-radius runbooks.
- **`corroboration`**: an independent check that the cited runbooks fall inside the affected subgraph,
  so the precise AQL surface and the semantic retrieval surface agree.

Across the 8 demo alerts (`data/alerts.json`), the primary citation is the correct service's runbook
8 times out of 8, and corroboration is 8 out of 8. The full table is under [Results](#results).

## How it works
Two surfaces over one platform, joined by the agent:

1. **Multimodel core** (database `incident_demo`): incidents with embeddings, a curated service
   topology (named graph), on-call teams, and stored alerts. One AQL query does vector, graph, and
   key-value in a single round trip, with no application-side joins.
2. **[AutoGraph](https://docs.arango.ai/agentic-ai-suite/autograph/) knowledge graph** (project
   `incidents-runbook-autograph`): [AutoGraph](https://docs.arango.ai/agentic-ai-suite/autograph/reference/)
   discovers the knowledge domains in the runbooks and builds entities, relationships, communities, and
   chunk embeddings (105 entities, 227 relations, 5 communities from 11 runbooks), queried through the
   project's Retriever.

The agent (`resolver.py`) uses the **precise** root service from the multimodel query to ground the
answer in that service's exact runbook (matched on the runbook's content), and the **semantic**
Retriever pass (Unified Search, `query_type 3`) to add the related runbooks across the incident's blast
radius. Precise scope, grounded context.

The graph traversal returns the real blast radius of the headline alert: the root service in red,
then the services that depend on it, by depth.

![Affected-service subgraph for the onboarding-api alert, colored by blast-radius depth](assets/affected-subgraph.png)

The runbooks import into a real knowledge graph: each runbook a hub, entities clustering around it,
with the entities that appear in more than one runbook bridging them (red).

![AutoGraph knowledge graph: 105 entities extracted from 11 runbooks](assets/knowledge-graph.png)

> Both data figures are regenerated from the live deployment by `python viz.py` (into `assets/`). The
> headline architecture diagram is a static asset; `assets/architecture-schematic.png` is the same
> architecture rendered purely from code if you'd rather have a reproducible version.

## The multimodel query
The marquee query (`resolver.py:MARQUEE`), one store, one language, three moves:

- [`APPROX_NEAR_COSINE(i.embedding, @vec)`](https://docs.arango.ai/arangodb/stable/aql/functions/vector/): nearest past incidents (vector)
- `0..3 OUTBOUND ... GRAPH "service_topology"`: affected-service subgraph, deduped to shortest depth (graph)
- `DOCUMENT("teams", DOCUMENT("services", root).team)`: on-call owner (key-value)

## Results
Every alert in `data/alerts.json`, end to end. Section 7 of the notebook runs `evaluate()` over the
whole set and times both halves of each resolution: the multimodel query (one AQL round trip) and the
cited answer from the Retriever.

![Results: per-alert multimodel-query latency, 8/8 grounded on the correct runbook, 8/8 corroborated](assets/results.png)

For every alert the primary citation lands on the correct service runbook (8/8), the two surfaces
corroborate (8/8), and the multimodel query itself returns in a few milliseconds; the cited answer
adds one Retriever round trip on top. The per-alert table (similar incident, blast radius,
on-call owner, runbook, both timings) renders in the notebook.

## Setup
```bash
pip install -r requirements.txt
cp .env.example .env   # fill in ARANGO_* + OPENAI_API_KEY + GRAPHRAG_PROJECT/GRAPHRAG_DB
```
> On Apple Silicon, run the scripts with `arch -arm64 python3 …` (the Python here is a universal binary).

## Run
```bash
python src/ingest.py                            # 1. multimodel core: 500 incidents + 8 alerts + topology
python src/graphrag_ingest.py                   # 2. build runbook KG with AutoGraph (skip-if-built; --reset to rebuild)
python src/resolver.py data/alert.sample.json   # 3. one alert -> structured payload + cited, grounded answer
# or the whole pipeline at once:
python src/run_all.py
```
The notebook `incident_resolution.ipynb` walks the same flow, importing the same functions.

## Dataset & topology
| Shape | Source | Notes |
|---|---|---|
| Incident tickets | [`6StringNinja/synthetic-servicenow-incidents`](https://huggingface.co/datasets/6StringNinja/synthetic-servicenow-incidents) (HF, MIT, 500 rows) | Loaded at runtime, not vendored. Embeds `short_description + description`; keeps `resolution`. |
| Live alerts | `data/alerts.json` (8 synthetic) | Varied service, severity, region, telemetry. |
| Runbooks | `data/runbooks/` (11 hand-authored, by service-family module) | The cited source-of-truth knowledge graph. |

The data is a **simulation** of an incident estate (breadth across P1 to P3 and across infra to app
services), disclosed as synthetic. The service topology is hand-curated (`data/topology.json`) since
the dataset ships no CMDB; in production you'd derive it from your real service map. For a larger,
real corpus, [`Loukh1/IT-incidents`](https://huggingface.co/datasets/Loukh1/IT-incidents) (MIT, 4,040
rows) is the documented scale-up path.

## Deployed services
The [AutoGraph](https://docs.arango.ai/agentic-ai-suite/autograph/) control service and the project
Retriever are deployed once by creating the AutoGraph project in the platform web UI (on the current
platform version the project/service creation is UI-driven). Everything after that runs over the
documented [AutoGraph REST API](https://docs.arango.ai/agentic-ai-suite/autograph/reference/):
`graphrag_ingest.py` drives `import-multiple → corpus/builds → rag-strategizer`, where AutoGraph
discovers the domains and assigns per-domain retrieval treatment automatically. The final
orchestration (the entity build) is one click in the UI — "Continue to Import" — because the REST
`/orchestrate` endpoint returns zero jobs on the current platform version; everything up to it is
scriptable. The cited answer uses the Retriever's Unified Search (`query_type 3`). Service postfixes
are discovered at runtime (`graphrag.py`), never hardcoded.

## Repository contents
```
src/ingest.py          multimodel core: schema, embed tickets, build topology, store alerts
src/graphrag.py        auth + service discovery + the KG runbook lookup
src/graphrag_ingest.py build the runbook KG with AutoGraph (import -> corpus build -> strategizer -> orchestrate) + verify (skip-if-built / --reset)
src/resolver.py        the marquee AQL query + the cited, grounded answer + corroboration + evaluate()
src/run_all.py         the whole pipeline in one command
src/viz.py             regenerate the figures from live data (subgraph, knowledge graph, results)
incident_resolution.ipynb   narrated, executed walkthrough of the same flow (outputs + figures)
data/topology.json     curated service topology (12 services, 13 dependencies, 5 teams)
data/alerts.json       8 synthetic alerts; data/alert.sample.json is the headline P1
data/runbooks/         hand-authored runbooks (the cited knowledge-graph corpus)
docs/                  provisioning walkthrough for the AutoGraph services
assets/                architecture diagram + the data-driven figures (subgraph, KG, results)
```

## Status
- Multimodel core ✅
- AutoGraph runbook knowledge graph ✅ (11 runbooks → 105 entities, 227 relations, 5 communities; FullGraphRAG — import/corpus/strategizer via the AutoGraph REST API, final orchestrate via the UI)
- Cited, grounded combined resolver ✅ (Unified Search + content grounding; 8/8 demo alerts grounded and corroborated)

The agent here is framework-free Python. LangChain / LangGraph and Arango's built-in **Ada**
assistant are documented extension points.
