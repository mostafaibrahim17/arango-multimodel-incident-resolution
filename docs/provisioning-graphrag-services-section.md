# Provisioning the Project and Deploying the GraphRAG Services

> **Environment:** Arango Contextual Data Platform 4.0 pilot at `https://your-deployment.arango.ai` (Arango engine 3.12.9 Enterprise, gateway on port `8529`). Root auth, JWT obtained via `POST /_open/auth`.
>
> **Known limitation (CDP 4.0, as of 2026-07):** the raw ACP service-install API (`POST /_platform/acp/v1/graphragimporter`) returns `400 "Project  not found"` (blank project name) even with a schema-correct body against a project that exists and reads back fine, so **service deploy must be done in the web UI** on this platform version. Auth, health, project reads, service listing, and every data-plane call work over CLI. Each step below is tagged **[CLI]** or **[UI ONLY]** so the boundary is never ambiguous.

Throughout, `$EP` is the external endpoint:

```bash
EP="https://your-deployment.arango.ai"
```

---

## Step 1 — Get a JWT [CLI]

The ACP API authenticates with a standard Arango **user** JWT (not a superuser token), generated from the Arango auth endpoint.

```bash
TOKEN=$(curl -s -X POST "$EP/_open/auth" \
  -H "Content-Type: application/json" \
  -d '{"username": "root", "password": "<ROOT_PASSWORD>"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["jwt"])')

echo "$TOKEN" | cut -c1-20   # sanity check: prints the first chars of a JWT
```

**Success signal:** the response body is `{"jwt":"eyJ..."}`. Export `TOKEN` for every later call.

> If the pilot uses a self-signed cert, add `-k` to every `curl`. The public pilot endpoint normally has a valid cert, so `-k` should not be needed here.

Docs: [Control Plane (ACP) → Obtaining a Bearer token](https://docs.arango.ai/platform-suite/control-plane-acp/) · [Arango JWT user tokens](https://docs.arango.ai/arangodb/stable/develop/http-api/authentication/#jwt-user-tokens)

---

## Step 2 — Confirm ACP health [CLI]

```bash
curl -s -X GET "$EP/_platform/acp/v1/health" \
  -H "Authorization: Bearer $TOKEN"
```

**Success signal:** `{"status":"OK"}`. (This endpoint requires a valid Bearer token — an empty/expired token fails.)

Docs: [Control Plane (ACP) → Health check](https://docs.arango.ai/platform-suite/control-plane-acp/)

---

## Step 3 — Inspect what is already deployed [CLI]

List all installed services. An **empty body** (`{}`) returns everything; this is how you see what the control plane already has running and grab `serviceId`s.

```bash
curl -s -X POST "$EP/_platform/acp/v1/list_services" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
```

**Success signal:** a JSON array/object of installed services. Before any deploy this may be empty or contain only base services. Capture the output — you will re-run this in Step 6 to read back the UI-deployed services.

Docs: [Control Plane (ACP) → Listing services](https://docs.arango.ai/platform-suite/control-plane-acp/)

---

## Step 4 — Create the GraphRAG project **[UI ONLY]**

> **Why UI:** the ACP project-create API (`POST /_platform/acp/v1/project`) exists, but on this platform version the downstream service-install API rejects the project (`400 "Project  not found"`). The UI creates the project and wires it to the importer in one flow, so create the project in the UI to ensure the deployed services bind to it correctly.

**Click-path:**

1. Open the web interface: `https://your-deployment.arango.ai/ui/`.
2. **Pick the database first** — in the left-hand sidebar, select the database where the project should live. (This matters: the project and all its `<project>_Documents`, `<project>_Chunks`, etc. collections are created inside the selected database.)
3. In the left sidebar, click **Agentic AI Suite**, then click **Run GraphRAG**.
4. In the **GraphRAG projects** view, click **Add new project**.
5. In the **Create GraphRAG project** modal, enter a **Name** (and optionally a description). The name is used as the collection prefix, so keep it to letters/digits/`_`/`-`, ≤ 63 chars.
6. Click **Create project**.

**Success signal:** the new project appears in the **GraphRAG projects** list and opens to a project view with empty **Data Sources** and **Graph** sections.

Docs: [GraphRAG web interface → Create a GraphRAG project](https://docs.arango.ai/agentic-ai-suite/graphrag/web-interface/)

---

## Step 5 — Start the Importer and Retriever services **[UI ONLY]**

> **Why UI:** the raw API cannot do this step on this platform version. `POST /_platform/acp/v1/graphragimporter` returns `400 "Project  not found"` despite a valid project, while the UI's **Start importer service** button succeeds. The same applies to the retriever.

Both services are configured from the **Project Settings** dialog. Open it either way:

- In the **Data Sources** section → click **Add data source** → click **Open project settings**, **or**
- In the **Graph** section → click the **gear icon**.

### 5a — Start importer service (OpenAI provider)

1. In **Project Settings**, the importer configuration dialog is shown.
2. **LLM API Provider** dropdown → select **OpenAI**.
3. **Model** dropdown → pick your chat model (default is **GPT-5.4 Nano**).
4. **OpenAI API Key** → paste your key (or click the key icon to pull a stored secret from Secrets Manager). The same key is used for both chat and embeddings on the OpenAI path.
5. Click **Start importer service**.

**Success signal:** the Importer section shows the service as started/running; it now appears under **Agentic AI Suite → GraphRAG → <project>**, and (Step 6) `list_services` returns an `arangodb-graphrag-importer-<postfix>` entry with status `DEPLOYED`.

### 5b — Start retriever service (OpenAI provider)

1. Back in **Project Settings**, go to the Retriever section.
2. **LLM API Provider** → **OpenAI**.
3. **Model** → pick your model (default **GPT-5.4 Nano**).
4. **OpenAI API Key** → paste (or select a Secrets Manager secret); used for both chat and embeddings.
5. Click **Start retriever service**.

**Success signal:** Retriever section shows started; `list_services` (Step 6) returns an `arangodb-graphrag-retriever-<postfix>` entry, status `DEPLOYED`.

> **Note on OpenRouter:** if you ever switch the provider to OpenRouter, the UI asks for **two** keys — an OpenAI key (used for embeddings) plus an OpenRouter key (used for chat). On the plain OpenAI path one key covers both. Triton only shows up if a Triton Inference Server is deployed in the cluster.

Docs: [GraphRAG web interface → Configure the Importer service / Configure the Retriever service](https://docs.arango.ai/agentic-ai-suite/graphrag/web-interface/) · [Importer quickstart](https://docs.arango.ai/agentic-ai-suite/importer/quickstart/) · [Retriever](https://docs.arango.ai/agentic-ai-suite/retriever/)

---

## Step 6 — Read back serviceIds / postfixes for scripting [CLI]

After the UI deploy, the data plane is fully scriptable. Re-run `list_services` to capture each service's `serviceId`, then derive the postfix.

```bash
curl -s -X POST "$EP/_platform/acp/v1/list_services" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}' | python3 -m json.tool
```

You will see entries shaped like the ACP service-info object, e.g.:

```json
{
  "serviceInfo": {
    "serviceId": "arangodb-graphrag-importer-tm5i7",
    "status": "DEPLOYED",
    "namespace": "arangodb-platform-dev"
  }
}
```

### Deriving `serviceIdPostfix`

The **postfix is the trailing token of the `serviceId`** — the last `-`-delimited segment:

```
arangodb-graphrag-importer-tm5i7   →  tm5i7
arangodb-graphrag-retriever-<xxxxx> →  <xxxxx>
```

```bash
# Extract importer postfix programmatically
IMPORTER_ID=$(curl -s -X POST "$EP/_platform/acp/v1/list_services" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{}' \
  | python3 -c 'import sys,json
d=json.load(sys.stdin)
import re
print([s["serviceInfo"]["serviceId"] for s in (d if isinstance(d,list) else d.get("services",[]))
       if "importer" in s["serviceInfo"]["serviceId"]][0])')

POSTFIX="${IMPORTER_ID##*-}"   # trailing token, e.g. tm5i7
echo "$POSTFIX"
```

> Adjust the JSON walk to the actual `list_services` envelope (array vs. `{"services":[...]}`) once you see the live response — the postfix derivation rule (`${ID##*-}`) is what's load-bearing.

That `POSTFIX` is exactly what the data-plane import/query URLs need, e.g.:

```
POST $EP/graphrag/importer/<POSTFIX>/v1/import
POST $EP/graphrag/importer/<POSTFIX>/v1/import-multiple   # batch
```

You can also confirm a single service's status directly:

```bash
curl -s -X GET "$EP/_platform/acp/v1/service/$IMPORTER_ID" \
  -H "Authorization: Bearer $TOKEN"
```

**Success signal:** `status: "DEPLOYED"`. (`DEPLOYED` = installed; it may take a moment more to become ready to accept import/query traffic.)

Docs: [Control Plane (ACP) → Listing services / Service status / serviceId response shape](https://docs.arango.ai/platform-suite/control-plane-acp/) · [Importer quickstart → call sequence + `:serviceIdPostfix` URLs](https://docs.arango.ai/agentic-ai-suite/importer/quickstart/)

---

## Can any CLI deploy these services today?

There **is** an official Arango command-line tool, but it does **not** wrap the ACP per-service install API, so it cannot replace the UI deploy on this pilot.

### The official tool: `arangodb_operator_platform` (the "Platform CLI")

Downloaded from the [kube-arangodb releases](https://github.com/arangodb/kube-arangodb/releases) (binaries: `arangodb_operator_platform_{linux,darwin,windows}_{amd64,arm64}`). What it actually does is **cluster/operator lifecycle**, all at the Kubernetes/Helm layer:

| Command | Purpose |
| --- | --- |
| `license inventory` | Build `inventory.json` from a running Arango deployment |
| `license generate` | Generate a license key from credentials + inventory/deployment ID |
| `package export` | Download Platform Suite manifests + images into a `.zip` |
| `package import` | Load that package into a container registry |
| `package install` | Install the Platform Suite (web UI, base services) into the K8s namespace |

None of these touch GraphRAG **Importer/Retriever** service instances. There is **no `arangodb_operator_platform service install graphragimporter`-style verb.** The tool stops at "the platform and its UI are running"; per-project AI services are deployed *through* the platform — i.e. via the ACP API or the UI. (There is no `oasisctl`/ArangoGraph-cloud path here either; oasisctl manages ArangoGraph Cloud deployments, not self-managed CDP 4.0 ACP services.)

Docs: [CDP install/upgrade (Platform CLI introduced)](https://docs.arango.ai/contextual-data-platform/install-and-upgrade/) · [Offline setup — full `arangodb_operator_platform` command catalog](https://docs.arango.ai/contextual-data-platform/install-and-upgrade/offline-setup/)

### The intended CLI deploy path (currently blocked)

The ACP REST API is the intended scriptable deploy surface:

```bash
# Intended full-CLI deploy call — currently returns
# 400 "Project  not found" (blank project name) on this platform version.
curl -s -X POST "$EP/_platform/acp/v1/graphragimporter" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "env": {
      "db_name": "<db>",
      "project_name": "<project>",
      "chat_api_provider": "openai",
      "chat_api_url": "https://api.openai.com/v1",
      "embedding_api_provider": "openai",
      "embedding_api_url": "https://api.openai.com/v1",
      "chat_model": "gpt-5.4-nano",
      "embedding_model": "text-embedding-3-small",
      "chat_api_key": "<openai-key>",
      "embedding_api_key": "<openai-key>",
      "embedding_dim": "512"
    }
  }'
```

The body above is schema-correct per the ACP docs, and the named project reads back fine via `GET /_platform/acp/v1/project_by_name/<db>/<project>`. The `400 "Project  not found"` with a **blank** project name indicates a server-side issue where the install handler does not resolve `env.project_name` on this platform version. Once resolved, this path allows the entire deploy (project create → importer install → retriever install) to be scripted end-to-end from CLI, removing the UI step.

---

### Quick reference — endpoints used

| Action | Method + Path | Tag |
| --- | --- | --- |
| Get JWT | `POST /_open/auth` | CLI |
| ACP health | `GET /_platform/acp/v1/health` | CLI |
| List services | `POST /_platform/acp/v1/list_services` (body `{}`) | CLI |
| Get one service status | `GET /_platform/acp/v1/service/{serviceId}` | CLI |
| Create project | UI: Agentic AI Suite → Run GraphRAG → Add new project | UI ONLY |
| Start importer | UI: Project Settings → Start importer service | UI ONLY |
| Start retriever | UI: Project Settings → Start retriever service | UI ONLY |
| Submit import | `POST /graphrag/importer/{postfix}/v1/import` | CLI (after deploy) |
| (Blocked) API deploy importer | `POST /_platform/acp/v1/graphragimporter` → 400 bug | — |
