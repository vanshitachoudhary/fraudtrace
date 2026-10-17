# FraudTrace

**Graph Hacks (WeMakeDevs x FalkorDB), Track 1: Agents That Act on Connected Data**

FraudTrace is an AI agent that investigates flagged transactions by following connections in a FalkorDB graph,
freezes fraud rings, **remembers what it learned in a second graph**, and shows the proof behind every decision.

> Fraud rarely shows up in a single record. Five accounts share one device and the money flows to one leader:
> that is a *relationship* problem, so the agent's context lives in a graph.

## What it does
1. A flagged transaction arrives (for example `ACC-0302 -> ACC-0301`, 8,374).
2. The agent first calls `recall_memory`: has this account been seen in an earlier investigation?
3. Memory miss: it explores the graph (`get_neighbors`, `find_ring`, `money_flow`, `shortest_path`).
4. It decides, then **acts**: `flag_accounts` freezes every ring member, and `save_finding` writes what it learned to the memory graph.
5. Memory hit (a later session, no chat history): it only verifies cheaply and acts, using far fewer steps.
6. Every verdict comes with a **proof path** that the server recomputes with a graph query (the LLM's path is not trusted), plus a decision trace (`Decision -[:BASED_ON]-> Finding`).

Typical result in our runs: first investigation 5 to 7 steps, same ring seen again 1 to 3 steps (varies by run).

## Why the graph is essential (not decoration)
- Ring detection is **FalkorDB WCC** (`algo.WCC`) over shared devices/addresses; the ring leader comes from **FalkorDB PageRank** over transfers.
- Multi-hop reasoning is Cypher traversal: account -> shared device/address -> other accounts.
- Agent memory is a **second FalkorDB graph** (`fraud_memory`): rings, findings and decisions are nodes and edges the agent queries on the next session.
- Without FalkorDB there is no data, no memory and no tools. (`find_ring` and `shortest_path` have a Python fallback only for FalkorDB versions without those procedures; the data and the memory still live in FalkorDB.)

## Architecture
```
Browser UI (SSE)  ->  FastAPI (app.py)  ->  Agent (agent.py, LLM with tool calling)
                                                |  tools discovered over MCP (stdio)
                                          MCP server (mcp_server.py)
                                                |
                                  FalkorDB:  fraud_data  +  fraud_memory
```
- `seed_data.py`: synthetic data, 300 normal accounts + 3 hidden fraud rings (structuring just under 10,000) + 2 false-positive cases.
- `mcp_server.py`: all graph tools (MCP).
- `agent.py`: tool-calling loop, proof-path verification, free-tier model rotation.
- `app.py` + `static/index.html`: API and the "war room" UI (live graph, trace, memory graph tab, decision trace, replay, case file export).

## Graph data model

**Graph `fraud_data`**
| Nodes | Properties |
|---|---|
| `Account` | `id`, `name`, `frozen` |
| `Device` | `id` |
| `Address` | `id` |

| Edges | Meaning |
|---|---|
| `(Account)-[:USED_DEVICE]->(Device)` | account logged in from this device |
| `(Account)-[:LIVES_AT]->(Address)` | account's address |
| `(Account)-[:SENT {id, amount, flagged, ts}]->(Account)` | a transaction |

**Graph `fraud_memory`** (what the agent learned)
| Nodes | Properties |
|---|---|
| `Ring` | `name`, `leader` |
| `Finding` | `id`, `summary`, `evidence`, `ts` |
| `Decision` | `id`, `action`, `reason`, `ts` |
| `Account` | `id` |

Edges: `(Account)-[:MEMBER_OF]->(Ring)`, `(Finding)-[:ABOUT]->(Ring)`, `(Decision)-[:ON]->(Account)`, `(Decision)-[:BASED_ON]->(Finding)`.

## Cypher queries and graph algorithms the product depends on
```cypher
// Community detection (native FalkorDB algorithm): which accounts form one cluster?
CALL algo.WCC({nodeLabels:['Account','Device','Address'], relationshipTypes:['USED_DEVICE','LIVES_AT']})
YIELD node, componentId RETURN node.id, componentId

// Ring leader (native FalkorDB algorithm): PageRank over transfers
CALL algo.pageRank('Account','SENT') YIELD node, score RETURN node.id, score

// Multi-hop step: accounts that share a device or address with this one
MATCH (a:Account {id:$id})-[:USED_DEVICE|LIVES_AT]->(e)<-[:USED_DEVICE|LIVES_AT]-(x:Account)
WHERE x.id <> $id RETURN DISTINCT e.id, x.id LIMIT 40

// Proof path
MATCH (a:Account {id:$a}), (b:Account {id:$b})
MATCH p=shortestPath((a)-[:USED_DEVICE|LIVES_AT|SENT*1..8]-(b))
RETURN [n IN nodes(p) | n.id], [x IN relationships(p) | type(x)]

// Money flow
MATCH (a:Account {id:$id})-[s:SENT]->(b:Account) RETURN b.id, s.amount ORDER BY s.amount DESC LIMIT 15

// ACTION: freeze accounts
MATCH (a:Account) WHERE a.id IN $ids SET a.frozen = true

// MEMORY write: remember a ring
MERGE (r:Ring {name:$r}) SET r.leader=$l
CREATE (f:Finding {id:$f, summary:$s, evidence:$e, ts:$t})-[:ABOUT]->(r)
WITH r UNWIND $m AS m MERGE (a:Account {id:m}) MERGE (a)-[:MEMBER_OF]->(r)

// MEMORY read: is this account in a known ring?
MATCH (a:Account {id:$id})-[:MEMBER_OF]->(r:Ring) RETURN r.name, r.leader

// Decision trace
MATCH (d:Decision)-[:ON]->(a:Account)
OPTIONAL MATCH (d)-[:BASED_ON]->(f:Finding)-[:ABOUT]->(r:Ring)
RETURN d.id, a.id, d.reason, f.id, f.summary, f.evidence, r.name
```

## Setup (local)
Requirements: Docker, Python 3.10+, and a free Gemini API key from https://aistudio.google.com (no card needed).

```bash
# 1. FalkorDB (keep this terminal open). Browser UI: http://localhost:3000
docker run -p 6379:6379 -p 3000:3000 -it --rm falkordb/falkordb:latest

# 2. In a new terminal, inside this folder
pip install -r requirements.txt

# 3. Set your key
#    Windows PowerShell:  $env:GEMINI_API_KEY="your-key"
#    Mac/Linux:           export GEMINI_API_KEY=your-key

# 4. Build the synthetic graph (run again whenever you restart the Docker container)
python seed_data.py

# 5. Start the app, then open http://localhost:8000
python -m uvicorn app:app --port 8000
```
Any OpenAI-compatible LLM works: set `LLM_API_KEY`, `LLM_BASE_URL` and `LLM_MODEL`.
The free Gemini tier allows only about 20 requests per day per model, so `agent.py` rotates through the models in `LLM_MODELS`
(comma list, default `gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-3.1-flash-lite,gemini-3.5-flash-lite`).
Model names change over time; list yours with the `models.list()` call of the OpenAI client.

## Demo
1. Click **Reset memory**, then **Run demo**.
2. Session 1 investigates `TXN-9001` from scratch, freezes the ring and saves a finding.
3. Session 2 is a fresh agent with no chat history on `TXN-9002` (same ring, different member): it gets a memory hit and finishes in far fewer steps.
4. Try `TXN-9007` (a legitimate case: the agent must not freeze anyone), the **Memory graph** tab, **Why this decision?**, and **Ask the graph**.

## Data
All data is synthetic, generated by `seed_data.py`. No private or restricted data is used.

## AI assistance disclosure
This project was built with an AI coding assistant (Claude, by Anthropic). **Claude wrote the code.**
The idea, the direction and the decisions were mine: I chose Track 1 and the fraud-ring investigation idea, insisted that the agent
must remember across sessions (as the hackathon brief asks), asked for the UI and the extra features (decision trace, memory graph view,
replay, case file export, Ask the graph), read the rules and checked the project against them, set up Docker, WSL and FalkorDB on my own
machine, ran and tested every feature, and worked through real problems such as a Docker prerequisite error, an MCP library version
break, retired Gemini model names and free-tier quota limits.

## Limitations
- Synthetic data only; the fraud signals (shared devices, transfers just under 10,000, funds converging on one account) are deliberately simple.
- The LLM can take a slightly different route each run, so step counts vary.
- Free LLM tiers have tight rate and daily limits.