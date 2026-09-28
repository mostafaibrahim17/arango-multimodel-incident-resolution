# Provisioning the Project and Deploying the GraphRAG Services

> **Environment:** Arango Contextual Data Platform pilot at `https://your-deployment.arango.ai` (Arango engine Enterprise, gateway on port `8529`). Authenticates with an Arango user account, JWT obtained via `POST /_open/auth`.

Throughout, `$EP` is the external endpoint:

```bash
EP="https://your-deployment.arango.ai"
```

---

## Step 1: Get a JWT [CLI]

The ACP API authenticates with a standard Arango **user** JWT (not a superuser token), generated from the Arango auth endpoint. Use the same `ARANGO_USER` / `ARANGO_PASSWORD` as your `.env`.

```bash
TOKEN=$(curl -s -X POST "$EP/_open/auth" \
  -H "Content-Type: application/json" \
  -d '{"username": "<ARANGO_USER>", "password": "<ARANGO_PASSWORD>"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["jwt"])')

echo "$TOKEN" | cut -c1-20   # sanity check: prints the first chars of a JWT
```

**Success signal:** the response body is `{"jwt":"eyJ..."}`. Export `TOKEN` for every later call.

> If the pilot uses a self-signed cert, add `-k` to every `curl`. The public pilot endpoint normally has a valid cert, so `-k` should not be needed here.

Docs: [Control Plane (ACP) → Obtaining a Bearer token](https://docs.arango.ai/platform-suite/control-plane-acp/) · [Arango JWT user tokens](https://docs.arango.ai/arangodb/stable/develop/http-api/authentication/#jwt-user-tokens)

---

## Step 2: Confirm ACP health [CLI]

```bash
curl -s -X GET "$EP/_platform/acp/v1/health" \
  -H "Authorization: Bearer $TOKEN"
```

**Success signal:** `{"status":"OK"}`. (This endpoint requires a valid Bearer token, i.e. an empty/expired token fails.)

Docs: [Control Plane (ACP) → Health check](https://docs.arango.ai/platform-suite/control-plane-acp/)

---

## Step 3: Inspect what is already deployed [CLI]

List all installed services. An **empty body** (`{}`) returns everything; this is how you see what the control plane already has running and grab `serviceId`s.

```bash
curl -s -X POST "$EP/_platform/acp/v1/list_services" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
```

**Success signal:** a JSON array/object of installed services. Before any deploy this may be empty or contain only base services. Capture the output; you will re-run this in Step 6 to read back the service the wizard deploys.

Docs: [Control Plane (ACP) → Listing services](https://docs.arango.ai/platform-suite/control-plane-acp/)

---

## Step 4: Build the AutoGraph project in the wizard **[UI ONLY]**

On the current platform version, AutoGraph is provisioned through a three-step wizard: **Documents → Configure → Build**. It deploys the AutoGraph service and builds the Corpus Graph in one flow, then drops you at the project overview where you generate retrieval strategies and build the Knowledge Graph.

Open the web interface (`https://your-deployment.arango.ai/ui/`), **pick the database first** in the left sidebar (the project's collections are created inside the selected database), then open **Agentic AI Suite → AutoGraph Studio** and create a new project.

### 4a: Documents

1. On the **Documents** step, click **Upload files** or **Upload folder** (or drag and drop onto the panel).
2. Each upload becomes a **category** that you name. For this tutorial, upload the 11 runbook files and name the category `runbooks`.

**Success signal:** the file list shows your documents grouped under the category, each marked **Pending**, with a total like "11 documents in this project." Click **Configure LLM** to continue.

### 4b: Configure

Choose the LLM provider used to build the Corpus Graph. These settings are fixed for this build; changing the provider or model later requires a rebuild.

1. **Provider** → **OpenAI**.
2. **Model** → `gpt-5-mini` (this tutorial's chat model).
3. **API key** → paste your key, or pick a stored secret. Leave **Use a separate key for embeddings** unchecked to use the same key for both.
4. **Embedding model** → `text-embedding-3-small`.
5. **Multimodal model** → Provider default.
6. Click **Start build**.

**Success signal:** the panel confirms "Building from 11 documents across 1 category (runbooks)," and **Start build** deploys the AutoGraph service and begins the Corpus Graph build.

### 4c: Build

1. The **Build** step shows **AutoGraph service deployed** with the **AutoGraph service ID** (for example `arangodb-autograph-pp7hk`) once the service is up.
2. Click **Build Corpus Graph** to cluster the uploaded documents into the Corpus Graph.

**Success signal:** the wizard finishes at the project overview. The **Context Graph** card shows the **Corpus Graph** (for example "11 documents · 1 cluster") with **Open in Graph Visualizer**, and the **Knowledge Graph** marked "Not built yet."

Docs: [AutoGraph web interface](https://docs.arango.ai/agentic-ai-suite/autograph/)

---

## Step 5: Generate strategies and build the Knowledge Graph **[UI ONLY]**

From the project overview, the Knowledge Graph is built in three sub-steps: **Configure → Review → Build**.

1. On the **Knowledge Graph** card, click **Generate strategies**.
2. **Configure strategy generation**: the complexity slider sets the starting GraphRAG / VectorRAG mix across the corpus (**Vector only** ↔ **Balanced graph** ↔ **Graph + images**). Higher complexity means richer entity extraction and higher cost; pick the setting you want and click **Generate strategies**.
3. **Review strategies**: each category is grouped into a cluster with a RAG strategy and an ontology picked for it (for the runbooks corpus, one cluster with a GraphRAG strategy and an 11-type ontology). You can override a cluster's strategy or edit the ontology here. Click **Continue to build**.
4. **Build**: the final sub-step opens. Set **Parallel builds** (default `1`; more parallel builds finish faster but put more load on the service), then click **Build Knowledge Graph** to start it. The screen confirms the scope, e.g. "Building 1 GraphRAG cluster and 0 VectorRAG clusters." When it finishes, a **Knowledge Graph built** confirmation appears; click **Go to overview**.

**Success signal:** the **Context Graph** card's **Knowledge Graph** now shows the built graph with an entity and relationship count and an **Open in Graph Visualizer** link. Exact counts vary by corpus, complexity setting, and model version.

> **Retrievers.** Once the Knowledge Graph exists, the project overview's **Start using AutoRAG** section lets you **Deploy a retriever** to query it. **Deploy a retriever** opens a form to choose the chat provider, chat model, and chat API key; the embedding provider and model are **locked to match the corpus build** (only the embedding key is editable), so query embeddings align with the ones used at import. A retriever answers questions against the Knowledge Graph, so there is nothing to query until the graph is built.

Docs: [AutoGraph web interface](https://docs.arango.ai/agentic-ai-suite/autograph/)

---

## Step 6: Read back serviceIds / postfixes for scripting [CLI]

After the wizard deploys the service, the data plane is scriptable. Re-run `list_services` to capture each service's `serviceId`, then derive the postfix. (The tutorial's `src/graphrag.py` does exactly this at runtime, so you normally never hardcode a postfix, so this section is for confirming the deploy and for ad-hoc scripting.)

```bash
curl -s -X POST "$EP/_platform/acp/v1/list_services" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}' | python3 -m json.tool
```

`list_services` returns an object with a `services` array. Each entry carries `serviceId` directly, alongside `serviceMeta` and `serviceType` — there is **no** `serviceInfo` wrapper on this endpoint:

```json
{
  "services": [
    {
      "serviceMeta": { "...": "..." },
      "serviceType": "...",
      "serviceId": "arangodb-autograph-auxwm"
    }
  ]
}
```

### Deriving `serviceIdPostfix`

The **postfix is the trailing token of the `serviceId`** (the last `-`-delimited segment):

```
arangodb-autograph-auxwm            →  auxwm
arangodb-graphrag-retriever-<xxxxx> →  <xxxxx>
```

```bash
# Extract the AutoGraph service postfix programmatically
SERVICE_ID=$(curl -s -X POST "$EP/_platform/acp/v1/list_services" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{}' \
  | python3 -c 'import sys,json
d=json.load(sys.stdin)
print([s["serviceId"] for s in d["services"] if "autograph" in s["serviceId"]][0])')

POSTFIX="${SERVICE_ID##*-}"   # trailing token, e.g. auxwm
echo "$POSTFIX"
```

That `POSTFIX` is exactly what the data-plane control URLs need, e.g.:

```
POST $EP/autograph/<POSTFIX>/v1/import-multiple   # batch import
```

You can also confirm a single service's status directly. Unlike `list_services`, this endpoint **does** wrap its response in a `serviceInfo` object:

```bash
curl -s -X GET "$EP/_platform/acp/v1/service/$SERVICE_ID" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "serviceInfo": {
    "serviceId": "arangodb-autograph-auxwm",
    "description": "Install complete",
    "status": "DEPLOYED",
    "namespace": "arangodb-platform-rnd-<deployment>",
    "dbName": "incident_demo",
    "managingEntity": "ACP",
    "genaiProjectName": "Incidents-runbook-autograph"
  }
}
```

**Success signal:** `status: "DEPLOYED"`. (`DEPLOYED` = installed; it may take a moment more to become ready to accept import/query traffic.)

> Both curl calls need `$EP` and `$TOKEN` set in the current shell. These do **not** carry across terminal tabs/windows — in a fresh shell, re-set `$EP` and re-mint `$TOKEN` (Step 1) first, or the call returns an empty body and `json.tool` errors with `Expecting value: line 1 column 1`.

Docs: [Control Plane (ACP) → Listing services / Service status / serviceId response shape](https://docs.arango.ai/platform-suite/control-plane-acp/)

---

## Can any CLI deploy these services today?

There **is** an official Arango command-line tool, but it does **not** wrap the ACP per-service install API, so it cannot replace the wizard deploy on this pilot.

### The official tool: `arangodb_operator_platform` (the "Platform CLI")

Downloaded from the [kube-arangodb releases](https://github.com/arangodb/kube-arangodb/releases) (binaries: `arangodb_operator_platform_{linux,darwin,windows}_{amd64,arm64}`). What it actually does is **cluster/operator lifecycle**, all at the Kubernetes/Helm layer:

| Command | Purpose |
| --- | --- |
| `license inventory` | Build `inventory.json` from a running Arango deployment |
| `license generate` | Generate a license key from credentials + inventory/deployment ID |
| `package export` | Download Platform Suite manifests + images into a `.zip` |
| `package import` | Load that package into a container registry |
| `package install` | Install the Platform Suite (web UI, base services) into the K8s namespace |

None of these touch the GraphRAG/AutoGraph service instances. The tool stops at "the platform and its UI are running"; per-project AI services are deployed *through* the platform, i.e. via the ACP API or the UI wizard. (There is no `oasisctl`/ArangoGraph-cloud path here either; oasisctl manages ArangoGraph Cloud deployments, not self-managed CDP ACP services.)

Docs: [CDP install/upgrade (Platform CLI introduced)](https://docs.arango.ai/contextual-data-platform/install-and-upgrade/) · [Offline setup: full `arangodb_operator_platform` command catalog](https://docs.arango.ai/contextual-data-platform/install-and-upgrade/offline-setup/)

---

### Quick reference: endpoints used

| Action | Method + Path | Tag |
| --- | --- | --- |
| Get JWT | `POST /_open/auth` | CLI |
| ACP health | `GET /_platform/acp/v1/health` | CLI |
| List services | `POST /_platform/acp/v1/list_services` (body `{}`) | CLI |
| Get one service status | `GET /_platform/acp/v1/service/{serviceId}` | CLI |
| Create project + deploy service | UI: Agentic AI Suite → AutoGraph Studio → Documents → Configure → Build | UI ONLY |
| Build Corpus Graph | UI: Build step → Build Corpus Graph | UI ONLY |
| Generate strategies + build Knowledge Graph | UI: Generate strategies → Review → Continue to build → Build Knowledge Graph | UI ONLY |
| Submit import | `POST /autograph/{postfix}/v1/import-multiple` | CLI (after deploy) |