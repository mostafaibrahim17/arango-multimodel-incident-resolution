# Incident-Resolution Agent on the Arango Contextual Data Platform

A live alert comes in. This agent does what a senior responder does: it forms the obvious
hypothesis, tests it against the service topology, and pivots to the real upstream root cause when
the alert is only a downstream symptom. In one pass it returns the most similar past incidents, the
affected-service subgraph, the degraded upstream dependency, the on-call owner, **and** a cited,
runbook-grounded next step. Tickets, service topology, alerts, health signals, and runbooks all live
in **one** [Arango Contextual Data Platform](https://docs.arango.ai/agentic-ai-suite/) deployment. No
separate vector store, no graph database, no stitched-together pipeline.

> **TL;DR.** A P1 alert fires on `onboarding-api`: merchants can't e-sign, permission errors. The
> nearest past incident (vector, similarity 0.90) says *restart onboarding-api* and page Onboarding
> On-Call. That's wrong. One AQL query (vector + graph + two key-value joins) walks the dependency
> graph, finds the degraded upstream dependency `user-db` (read-replica lag after a failover) two hops
> up, pulls 3 precedents where the leaf restart failed, and re-routes to Identity On-Call with a cited
> `user-db` runbook fix. **Across the 8 demo alerts, 4 turn out to be the same hidden root cause
> masquerading as different services; all 8 ground on the correct runbook and all 8 corroborate.** It's
> the support-engineering use case Zscaler runs in production at scale (40K+ daily AI requests on the
> same platform); here it's simulated end to end on a public dataset you can run yourself.

![Architecture: one alert in, one query (vector + graph + key-value) plus a grounded, cited answer](assets/architecture.jpg)

## Contents
- [What one alert returns](#what-one-alert-returns)
- [The agent reasons, it doesn't just retrieve](#the-agent-reasons-it-doesnt-just-retrieve)
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
`python src/resolver.py data/alert.hero.json` on the P1 `onboarding-api` alert returns one JSON payload:

- **`obvious_hypothesis`**: what vector similarity alone would tell you (top incident `INC-SEED-ESIGN-1`
  @ 0.90 → restart onboarding-api, page Onboarding On-Call). The agent records it, then tests it.
- **`structure`** (one AQL round trip): the affected-service subgraph by blast-radius depth (graph,
  `OUTBOUND`) plus `degraded_upstream` — the dependency flagged degraded by its health signal
  (key-value, `DOCUMENT` on `service_signals`).
- **`root_cause`**: the agent's pivot — `user-db`, 2 hops upstream, with the on-call owner of *that*
  service (Identity On-Call, not Onboarding).
- **`precedents`**: a second vector pass scoped to the true root, returning 3 past `user-db` incidents
  where restarting the leaf failed and the fix was upstream.
- **`eliminated`**: the wrong hypothesis, explicitly rejected, with the reason.
- **`cited_answer`**: a natural-language next step grounded in the runbook knowledge graph, with the
  **exact root-cause runbook as the primary citation** plus related blast-radius runbooks.
- **`confidence` + `verify_command`**: a confidence verdict and a one-line check to run before acting.

Across the 8 demo alerts (`data/alerts.json`), 4 pivot to a hidden upstream root cause, the primary
citation is the correct service's runbook 8/8, and corroboration is 8/8. Full table under [Results](#results).

## The agent reasons, it doesn't just retrieve
Vector search finds what *looks* similar. For `onboarding-api`'s e-sign failure, the most similar past
incident is a real onboarding permission-cache issue that was fixed by a restart — so vector-only
retrieval confidently ships the wrong fix and pages the wrong team. The agent treats that as a
hypothesis to test, not an answer:

![How the agent reasons: vector hypothesis, graph test, health-signal pivot, precedent, eliminate, cited answer, verdict](assets/reasoning-chain.png)

Run the same alert through three retrieval strategies and the capability gap is binary, not marginal:

![Ablation: vector-only is wrong; graph+key-value finds the right root but no fix; full multimodel is right and cited](assets/ablation.png)

That single multimodel query replaces a sequential "Frankenstack" of separate vector, graph, and
key-value systems plus the application code that joins them and handles their partial failures:

![Polyglot Frankenstack (5 sequential calls + glue) versus one AQL round trip](assets/polyglot-vs-aql.png)

## How it works
Two surfaces over one platform, joined by the agent:

1. **Multimodel core** (database `incident_demo`): incidents with embeddings, a curated service
   topology (named graph), on-call teams, per-service health signals, and stored alerts. One AQL
   query does vector, graph, and two key-value joins (health signal + owner) in a single round trip,
   with no application-side joins. That health-signal join is what lets the agent name the degraded
   *upstream* dependency instead of blaming the service the alert fired on.
2. **[AutoGraph](https://docs.arango.ai/agentic-ai-suite/autograph/) knowledge graph** (project
   `incidents-runbook-autograph`): [AutoGraph](https://docs.arango.ai/agentic-ai-suite/autograph/reference/)
   discovers the knowledge domains in the runbooks and builds entities, relationships, communities, and
   chunk embeddings (105 entities, 227 relations, 5 communities from 11 runbooks), queried through the
   project's Retriever.

The agent (`resolver.py`) uses the **precise** root service from the multimodel query to ground the
answer in that service's exact runbook (matched on the runbook's content), and the **semantic**
Retriever pass (Unified Search, `query_type 3`) to add the related runbooks across the incident's blast
radius. Precise scope, grounded context.

The graph traversal returns the real blast radius of the headline alert: the alerting leaf in amber,
the degraded upstream root cause in red, and the cascade path between them.

![Subgraph for the onboarding-api alert: alerting leaf in amber, real root cause user-db in red, two hops upstream](assets/affected-subgraph.png)

The runbooks import into a real knowledge graph: each runbook a hub, entities clustering around it,
with the entities that appear in more than one runbook bridging them (red).

![AutoGraph knowledge graph: 105 entities extracted from 11 runbooks](assets/knowledge-graph.png)

> Both data figures are regenerated from the live deployment by `python viz.py` (into `assets/`). The
> headline architecture diagram is a static asset; `assets/architecture-schematic.png` is the same
> architecture rendered purely from code if you'd rather have a reproducible version.

## The multimodel query
The marquee query (`resolver.py:MARQUEE`), one store, one language, four moves:

- [`APPROX_NEAR_COSINE(i.embedding, @vec)`](https://docs.arango.ai/arangodb/stable/aql/functions/vector/): nearest past incidents (vector)
- `0..3 OUTBOUND ... GRAPH "service_topology"`: affected-service subgraph, deduped to shortest depth (graph)
- `DOCUMENT("service_signals", a.service)` over the subgraph: the degraded upstream dependency (key-value health)
- `DOCUMENT("teams", DOCUMENT("services", root).team)`: on-call owner of the real root cause (key-value)

The `root` the query returns is the deepest degraded upstream dependency when there is one, else the
alerting service — so the on-call owner and the grounded runbook both follow the *root cause*, not the
symptom. A second vector pass (`precedents()`) then retrieves the structural precedents for that root.

## Results
Every alert in `data/alerts.json`, end to end. Section 7 of the notebook runs `evaluate()` over the
whole set and times both halves of each resolution: the multimodel query (one AQL round trip) and the
cited answer from the Retriever.

![Results: per-alert multimodel-query latency, 8/8 grounded on the correct runbook, 8/8 corroborated](assets/results.png)

For every alert the primary citation lands on the correct service runbook (8/8) and the two surfaces
corroborate (8/8). Four of the eight alerts (`onboarding-api`, `auth-service`, `merchant-service`,
`api-gateway`) pivot to the same hidden root cause, `user-db` — a single degraded shared dependency
surfacing as four different-looking incidents — while the other four are genuinely local and the agent
correctly leaves them in place. The multimodel query itself returns in roughly 150–425 ms (median
~194 ms); the cited answer adds one Retriever round trip (~5 s) on top. The per-alert table (symptom
service, pivoted root, blast radius, on-call owner, runbook, both timings) renders in the notebook.

## Setup
```bash
pip install -r requirements.txt
cp .env.example .env   # fill in ARANGO_* + OPENAI_API_KEY + GRAPHRAG_PROJECT/GRAPHRAG_DB
```
> On Apple Silicon, run the scripts with `arch -arm64 python3 …` (the Python here is a universal binary).

## Run
```bash
python src/ingest.py                            # 1. multimodel core: 504 incidents + 8 alerts + topology + health signals
python src/graphrag_ingest.py                   # 2. build runbook KG with AutoGraph (skip-if-built; --reset to rebuild)
python src/resolver.py data/alert.hero.json     # 3. one alert -> full reasoning pass (hypothesis -> pivot -> cited answer)
python src/run_cascade.py                        # 4. hero reasoning + 8-alert sweep + regenerate every figure
# or the whole pipeline at once:
python src/run_all.py
```
The notebook `incident_resolution.ipynb` walks the same flow, importing the same functions.

## Dataset & topology
| Shape | Source | Notes |
|---|---|---|
| Incident tickets | [`6StringNinja/synthetic-servicenow-incidents`](https://huggingface.co/datasets/6StringNinja/synthetic-servicenow-incidents) (HF, MIT, 500 rows) | Loaded at runtime, not vendored. Embeds `short_description + description`; keeps `resolution`. |
| Seed incidents | `data/seed_incidents.json` (4 hand-authored) | Synthetic precedents the public set lacks: one shallow "restart the leaf" decoy + three `user-db` replica-lag cascades where the real fix was upstream. |
| Health signals | `data/service_signals.json` (key-value) | Per-service status; `user-db` degraded (replica lag after failover) so the agent has a real upstream fault to find. |
| Live alerts | `data/alerts.json` (8 synthetic) | Varied service, severity, region, telemetry. `data/alert.hero.json` is the headline cascade. |
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
src/resolver.py        the marquee AQL query + the reasoning agent (reason()) + cited answer + corroboration + evaluate()
src/run_cascade.py     one-shot driver: hero reasoning + 8-alert sweep + render every figure
src/run_all.py         the whole pipeline in one command
src/viz.py             regenerate the figures from live data (reasoning chain, ablation, polyglot, subgraph, KG, results)
incident_resolution.ipynb   narrated, executed walkthrough of the same flow (outputs + figures)
data/topology.json     curated service topology (12 services, 13 dependencies, 5 teams)
data/seed_incidents.json   4 hand-authored synthetic precedents (the decoy + 3 user-db cascades)
data/service_signals.json  per-service health signals (user-db degraded)
data/alerts.json       8 synthetic alerts; data/alert.hero.json is the headline cascade
data/runbooks/         hand-authored runbooks (the cited knowledge-graph corpus)
docs/                  provisioning walkthrough for the AutoGraph services
assets/                architecture diagram + data-driven figures (reasoning chain, ablation, polyglot, subgraph, KG, results)
```

## Status
- Multimodel core ✅ (vector + graph + two key-value joins in one AQL round trip)
- AutoGraph runbook knowledge graph ✅ (11 runbooks → 105 entities, 227 relations, 5 communities; FullGraphRAG — import/corpus/strategizer via the AutoGraph REST API, final orchestrate via the UI)
- Reasoning agent ✅ (vector hypothesis → graph + health-signal test → upstream pivot → precedent → eliminate → cited answer; 4/8 demo alerts pivot to a hidden root cause, 8/8 grounded and corroborated)

The agent here is framework-free Python. LangChain / LangGraph and Arango's built-in **Ada**
assistant are documented extension points.
