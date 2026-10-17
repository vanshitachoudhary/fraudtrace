import asyncio
import json
import os
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from common import q, db, DATA, MEM
from agent import investigate, ask

app = FastAPI()
HERE = os.path.dirname(os.path.abspath(__file__))
_tasks = set()


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


@app.get("/api/cases")
def cases():
    rows = q(DATA, "MATCH (a:Account)-[s:SENT]->(b:Account) WHERE s.flagged = true RETURN s.id, a.id, b.id, s.amount ORDER BY s.id")
    return [{"id": i, "from": a, "to": b, "amount": m} for i, a, b, m in rows]


@app.get("/api/stats")
def stats():
    frozen = q(DATA, "MATCH (a:Account) WHERE a.frozen = true RETURN count(a)")[0][0]
    try:
        rings = q(MEM, "MATCH (r:Ring) RETURN count(r)")[0][0]
        findings = q(MEM, "MATCH (f:Finding) RETURN count(f)")[0][0]
    except Exception:
        rings = findings = 0
    return {"frozen": frozen, "rings": rings, "findings": findings}


@app.post("/api/reset")
def reset():
    try:
        db().select_graph(MEM).delete()
    except Exception:
        pass
    q(DATA, "MATCH (a:Account) SET a.frozen = false")
    return {"ok": True}


@app.get("/api/investigate")
async def run(txn: str):
    queue = asyncio.Queue()
    t = asyncio.create_task(investigate(txn, queue))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)

    async def gen():
        while True:
            ev = await queue.get()
            yield f"data: {json.dumps(ev)}\n\n"
            if ev["type"] in ("final", "error"):
                break
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/memory")
def memory_graph():
    try:
        rows = q(MEM, "MATCH (a)-[r]->(b) RETURN labels(a)[0], coalesce(a.id, a.name), labels(b)[0], coalesce(b.id, b.name), type(r)")
    except Exception:
        rows = []
    nodes = {}
    for la, a, lb, b, _ in rows:
        nodes[a], nodes[b] = la, lb
    return {"nodes": [[i, l] for i, l in nodes.items()], "edges": [[a, b, t] for _, a, _, b, t in rows]}


@app.get("/api/decisions")
def decisions():
    try:
        rows = q(MEM, "MATCH (d:Decision)-[:ON]->(a:Account) OPTIONAL MATCH (d)-[:BASED_ON]->(f:Finding)-[:ABOUT]->(r:Ring) "
                      "RETURN d.id, a.id, d.reason, f.id, f.summary, f.evidence, r.name ORDER BY d.ts DESC LIMIT 30")
    except Exception:
        rows = []
    return [dict(id=i, account=a, reason=rs, finding=f, summary=s, evidence=e, ring=r) for i, a, rs, f, s, e, r in rows]


@app.get("/api/ask")
async def ask_graph(question: str):
    return await ask(question)
