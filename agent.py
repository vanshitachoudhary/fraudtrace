"""The agent: an LLM + tools discovered over MCP. Memory lives ONLY in the graph (fresh context every session).
Uses any OpenAI-compatible endpoint. Default: Google Gemini free tier (AI Studio key in GEMINI_API_KEY)."""
import asyncio
import json
import os
import sys
import traceback
from openai import AsyncOpenAI, RateLimitError, InternalServerError, APIConnectionError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from common import q, DATA, find_path

API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("LLM_API_KEY") or "missing-key"
BASE_URL = os.getenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
# Free tier = ~20 requests/day PER MODEL, so we rotate through several models automatically.
MODELS = [m.strip() for m in os.getenv("LLM_MODELS", "gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-3.1-flash-lite,gemini-3.5-flash-lite").split(",") if m.strip()]
_first = os.getenv("LLM_MODEL")
if _first:
    if _first in MODELS:
        MODELS.remove(_first)
    MODELS.insert(0, _first)
_dead = set()   # models whose daily quota is used up (until the server restarts)
client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)

SYSTEM = """You are FraudTrace, a fraud-investigation agent. Your only context is a graph database, reached through tools.
Procedure:
1. Call recall_memory on the sender account FIRST.
2. Memory hit: verify cheaply (max 2 checks, e.g. money_flow or shortest_path to the remembered leader), then flag_accounts for the sender if not already frozen. Do not redo the whole investigation.
3. No hit: investigate. Use get_neighbors (call it again on new accounts to go several hops), find_ring, money_flow, shortest_path.
4. Fraud-ring signals: many accounts sharing devices/addresses, transfers just under 10,000, money converging on one leader. A small harmless cluster (e.g. two accounts at one home address, no convergence) is NOT fraud.
5. Fraud: flag_accounts for ALL ring members, then save_finding (pass its finding_id to flag_accounts if you save first; order is your choice). Legit: flag nothing, save nothing.
6. Write one short sentence of reasoning before each tool call. When several independent checks are needed, request them together in ONE turn (parallel tool calls) to save requests. Be efficient: use at most 6 tool calls in total and stop as soon as you can decide.
Final reply: ONLY a JSON object:
{"verdict":"fraud_ring"|"legit","risk":0-100,"ring_members":[ids],"leader":id|null,"path":[ids from the flagged account to the leader via shared device/address],"reasons":[short strings],"used_memory":true|false}"""

READ_ONLY = {"recall_memory", "get_neighbors", "money_flow", "shortest_path", "find_ring", "list_memory"}
ASK_SYSTEM = ("You answer questions about a fraud-investigation graph using read-only tools. Never freeze or save anything. "
              "Be concise, cite account ids, and say so if memory is empty (nothing investigated yet).")


def root_error(e):
    """anyio wraps errors in ExceptionGroup; dig out the real one so the UI shows it."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return e


def clean(schema):
    """Gemini is strict about JSON schemas: drop keys it rejects."""
    if isinstance(schema, dict):
        return {k: clean(v) for k, v in schema.items() if k not in ("title", "additionalProperties", "default", "$schema")}
    if isinstance(schema, list):
        return [clean(x) for x in schema]
    return schema


def to_tools(mcp_tools, only=None):
    return [{"type": "function", "function": {"name": t.name, "description": t.description or "", "parameters": clean(t.inputSchema)}}
            for t in mcp_tools if only is None or t.name in only]


def flatten_history(msgs):
    """Switching models mid-investigation: turn earlier tool calls into plain text so no thought_signature is required."""
    head = [dict(m) for m in msgs[:2]]
    calls, lines = {}, []
    for m in msgs[2:]:
        if m["role"] == "assistant":
            if m.get("content"):
                lines.append("You noted: " + m["content"])
            for c in m.get("tool_calls") or []:
                calls[c["id"]] = (c["function"]["name"], c["function"]["arguments"])
        elif m["role"] == "tool":
            n, args = calls.get(m["tool_call_id"], ("tool", "{}"))
            lines.append(f"Tool {n}({args}) returned: {m['content'][:1500]}")
    if lines:
        head[1]["content"] += "\n\nProgress so far in this same investigation (continue from here, do not repeat these calls):\n" + "\n".join(lines)
    return head


def strip_signatures(msgs):
    """Thought signatures belong to one model; drop them when we switch."""
    for m in msgs:
        for c in m.get("tool_calls", []) or []:
            c.pop("extra_content", None)


async def chat(msgs, tools, queue=None):
    """Retry on rate limits / overload; rotate to another free model when one is exhausted or busy."""
    async def note(text):
        if queue:
            await queue.put({"type": "thought", "text": text})
    busy = {}
    for attempt in range(14):
        live = [m for m in MODELS if m not in _dead]
        if not live:
            raise RuntimeError("Every free model is out of daily quota. It resets once a day (Pacific midnight). "
                               "Check usage at aistudio.google.com, or try a different provider.")
        model = live[0]
        try:
            return await client.chat.completions.create(model=model, messages=msgs, tools=tools, tool_choice="auto", max_tokens=1500)
        except (RateLimitError, InternalServerError, APIConnectionError) as e:
            detail = str(e)
            if "perday" in detail.lower().replace(" ", "").replace("_", ""):
                _dead.add(model)
                msgs[:] = flatten_history(msgs)
                await note(f"{model}: daily free quota used up, switching to the next model.")
                continue
            busy[model] = busy.get(model, 0) + 1
            if busy[model] >= 2 and len(live) > 1:
                MODELS.remove(model)
                MODELS.append(model)
                msgs[:] = flatten_history(msgs)
                await note(f"{model} is busy, trying another model.")
                await asyncio.sleep(2)
                continue
            wait = 8 * busy[model]
            await note(f"{model}: rate limit or busy, waiting {wait}s and retrying. Google says: {detail[:120]}")
            await asyncio.sleep(wait)
    raise RuntimeError("The free Gemini models keep refusing. Wait a few minutes and try again.")


def assistant_msg(m):
    d = {"role": "assistant", "content": m.content or ""}
    if m.tool_calls:
        calls = []
        for tc in m.tool_calls:
            c = {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
            ec = (tc.model_extra or {}).get("extra_content")
            if ec:
                c["extra_content"] = ec
            calls.append(c)
        d["tool_calls"] = calls
    return d


def verify_path(result, src):
    """Don't trust the LLM's proof path: recompute it from the graph (devices/addresses first, then any link)."""
    leader = result.get("leader")
    if result.get("verdict") != "fraud_ring" or not leader:
        return result
    if leader == src:
        result["path"], result["path_verified"] = [src], True
        return result
    for rels in (("USED_DEVICE", "LIVES_AT"), ("USED_DEVICE", "LIVES_AT", "SENT")):
        try:
            r = find_path(src, leader, rels)
        except Exception:
            r = None
        if r:
            result["path"], result["path_verified"] = r[0], True
            return result
    result["path_verified"] = False
    return result


def tool_json(res):
    """A tool error comes back as plain text: turn it into JSON so the agent can see it and carry on."""
    raw = res.content[0].text if res.content else ""
    try:
        return json.loads(raw)
    except Exception:
        return {"error": raw[:500] or "tool failed"}


def parse_final(text):
    try:
        return json.loads(text[text.index("{"): text.rindex("}") + 1])
    except Exception:
        return {"verdict": "unknown", "risk": 0, "ring_members": [], "leader": None, "path": [], "reasons": [(text or "")[:300]], "used_memory": False}


async def investigate(txn_id, queue):
    try:
        row = q(DATA, "MATCH (a:Account)-[s:SENT {id:$t}]->(b:Account) RETURN a.id, b.id, s.amount", {"t": txn_id})
        if not row:
            await queue.put({"type": "error", "text": f"{txn_id} not found. Did you run seed_data.py?"})
            return
        src, dst, amt = row[0]
        await queue.put({"type": "start", "txn": txn_id, "from": src, "to": dst, "amount": amt})
        params = StdioServerParameters(command=sys.executable, args=["mcp_server.py"])
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = to_tools((await s.list_tools()).tools)
                msgs = [{"role": "system", "content": SYSTEM},
                        {"role": "user", "content": f"Flagged transaction {txn_id}: {src} sent {amt} to {dst}. Investigate and act."}]
                steps = 0
                for _ in range(14):
                    resp = await chat(msgs, tools, queue)
                    m = resp.choices[0].message
                    msgs.append(assistant_msg(m))
                    if not m.tool_calls:
                        await queue.put({"type": "final", "steps": steps, "result": verify_path(parse_final(m.content or ""), src),
                                         "txn": txn_id, "from": src, "to": dst, "amount": amt})
                        return
                    if (m.content or "").strip():
                        await queue.put({"type": "thought", "text": m.content.strip()})
                    for tc in m.tool_calls:
                        steps += 1
                        args = json.loads(tc.function.arguments or "{}")
                        res = await s.call_tool(tc.function.name, args)
                        data = tool_json(res)
                        graph = data.pop("graph", {"nodes": [], "edges": []})
                        await queue.put({"type": "step", "n": steps, "tool": tc.function.name, "args": args,
                                         "result": data, "graph": graph, "memory_hit": data.get("memory_hit")})
                        msgs.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(data)})
                await queue.put({"type": "error", "text": "Agent hit the step limit."})
    except BaseException as e:
        traceback.print_exc()
        e = root_error(e)
        await queue.put({"type": "error", "text": f"{type(e).__name__}: {str(e)[:600]}"})


async def ask(question):
    try:
        params = StdioServerParameters(command=sys.executable, args=["mcp_server.py"])
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = to_tools((await s.list_tools()).tools, READ_ONLY)
                msgs, used = [{"role": "system", "content": ASK_SYSTEM}, {"role": "user", "content": question}], []
                for _ in range(8):
                    resp = await chat(msgs, tools)
                    m = resp.choices[0].message
                    msgs.append(assistant_msg(m))
                    if not m.tool_calls:
                        return {"answer": m.content or "", "tools": used}
                    for tc in m.tool_calls:
                        used.append(tc.function.name)
                        d = tool_json(await s.call_tool(tc.function.name, json.loads(tc.function.arguments or "{}")))
                        d.pop("graph", None)
                        msgs.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(d)})
        return {"answer": "Too many steps, try a simpler question.", "tools": used}
    except BaseException as e:
        traceback.print_exc()
        e = root_error(e)
        return {"answer": f"{type(e).__name__}: {str(e)[:600]}", "tools": []}