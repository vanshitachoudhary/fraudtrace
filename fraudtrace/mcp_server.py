"""MCP server: every graph tool the agent can use lives here (stdio transport)."""
import json
import time
import uuid
from collections import deque
from mcp.server.fastmcp import FastMCP
from common import q, DATA, MEM, kind, find_path

mcp = FastMCP("fraudtrace")


def N(i, k=None):
    return [i, k or kind(i)]


def out(d, nodes=None, edges=None):
    d["graph"] = {"nodes": nodes or [], "edges": edges or []}   # for the UI animation only
    return json.dumps(d)


@mcp.tool()
def recall_memory(account_id: str) -> str:
    """ALWAYS CALL FIRST. Look in long-term memory: was this account already seen in an earlier
    investigation as part of a known fraud ring? Returns ring, leader, members, earlier finding and decisions."""
    try:
        rows = q(MEM, "MATCH (a:Account {id:$id})-[:MEMBER_OF]->(r:Ring) RETURN r.name, r.leader", {"id": account_id})
    except Exception:
        rows = []
    fr = q(DATA, "MATCH (a:Account {id:$id}) RETURN a.frozen", {"id": account_id})
    frozen = bool(fr and fr[0][0])
    if not rows:
        return out({"memory_hit": False, "account": account_id, "already_frozen": frozen})
    ring, leader = rows[0]
    members = [r[0] for r in q(MEM, "MATCH (a:Account)-[:MEMBER_OF]->(:Ring {name:$r}) RETURN a.id", {"r": ring})]
    f = q(MEM, "MATCH (f:Finding)-[:ABOUT]->(:Ring {name:$r}) RETURN f.id, f.summary, f.evidence ORDER BY f.ts DESC LIMIT 1", {"r": ring})
    dec = q(MEM, "MATCH (d:Decision)-[:ON]->(a:Account) WHERE a.id IN $m RETURN a.id, d.action", {"m": members})
    return out({"memory_hit": True, "ring": ring, "leader": leader, "members": members,
                "earlier_finding": {"id": f[0][0], "summary": f[0][1], "evidence": f[0][2]} if f else None,
                "earlier_decisions": [{"account": a, "action": x} for a, x in dec],
                "this_account_already_frozen": frozen},
               nodes=[N(m, "memory") for m in members])


@mcp.tool()
def get_neighbors(account_id: str) -> str:
    """Accounts that share a device or home address with this account (1 hop). Call it again on the
    new accounts to follow the network several hops deep."""
    rows = q(DATA, "MATCH (a:Account {id:$id})-[r1:USED_DEVICE|LIVES_AT]->(e)<-[r2:USED_DEVICE|LIVES_AT]-(x:Account) "
                   "WHERE x.id <> $id RETURN DISTINCT e.id, x.id LIMIT 40", {"id": account_id})
    nodes, edges = [N(account_id)], []
    for e, x in rows:
        nodes += [N(e), N(x)]
        edges += [[account_id, e, "shares"], [x, e, "shares"]]
    return out({"account": account_id, "linked_accounts": [{"via": e, "account": x} for e, x in rows]}, nodes, edges)


@mcp.tool()
def money_flow(account_id: str) -> str:
    """Incoming and outgoing transfers of an account with amounts, plus totals."""
    o = q(DATA, "MATCH (a:Account {id:$id})-[s:SENT]->(b:Account) RETURN b.id, s.amount ORDER BY s.amount DESC LIMIT 15", {"id": account_id})
    i = q(DATA, "MATCH (b:Account)-[s:SENT]->(a:Account {id:$id}) RETURN b.id, s.amount ORDER BY s.amount DESC LIMIT 15", {"id": account_id})
    nodes = [N(account_id)] + [N(b) for b, _ in o + i]
    edges = [[account_id, b, f"₹{m}"] for b, m in o] + [[b, account_id, f"₹{m}"] for b, m in i]
    return out({"account": account_id, "sent": [{"to": b, "amount": m} for b, m in o],
                "received": [{"from": b, "amount": m} for b, m in i],
                "total_sent": sum(m for _, m in o), "total_received": sum(m for _, m in i)}, nodes, edges)


@mcp.tool()
def shortest_path(account_a: str, account_b: str) -> str:
    """Shortest connection path between two accounts through devices, addresses and transfers."""
    r = find_path(account_a, account_b)
    if not r:
        return out({"path": None})
    ids, rels = r
    return out({"path": ids, "relations": rels}, [N(i) for i in ids],
               [[ids[k], ids[k + 1], rels[k]] for k in range(len(rels))])


@mcp.tool()
def find_ring(account_id: str) -> str:
    """Community detection (weakly connected components over shared devices/addresses) plus PageRank on transfers:
    returns the cluster, internal money flow and the likely ring leader."""
    pairs = q(DATA, "MATCH (a:Account)-[:USED_DEVICE|LIVES_AT]->(e) RETURN a.id, e.id")
    adj = {}
    for a, e in pairs:
        adj.setdefault(a, set()).add(e)
        adj.setdefault(e, set()).add(a)
    engine, seen = "python BFS", None
    try:   # native FalkorDB algorithm
        rows = q(DATA, "CALL algo.WCC({nodeLabels:['Account','Device','Address'], relationshipTypes:['USED_DEVICE','LIVES_AT']}) "
                       "YIELD node, componentId RETURN node.id, componentId")
        comp = dict(rows)
        if account_id in comp:
            seen = {i for i, c in comp.items() if c == comp[account_id]}
            engine = "FalkorDB WCC"
    except Exception:
        pass
    if seen is None:
        seen, dq = {account_id}, deque([account_id])
        while dq:
            for nb in adj.get(dq.popleft(), ()):
                if nb not in seen:
                    seen.add(nb)
                    dq.append(nb)
    members = sorted(x for x in seen if kind(x) == "account")
    shared = [x for x in seen if kind(x) != "account" and len(adj.get(x, ())) > 1]
    flows = q(DATA, "MATCH (a:Account)-[s:SENT]->(b:Account) WHERE a.id IN $m AND b.id IN $m RETURN a.id, b.id, s.amount", {"m": members})
    inflow = {}
    for _, b, amt in flows:
        inflow[b] = inflow.get(b, 0) + amt
    leader = max(inflow, key=inflow.get) if inflow else None
    if inflow:
        try:   # native PageRank, restricted to this cluster
            pr = dict(q(DATA, "CALL algo.pageRank('Account','SENT') YIELD node, score RETURN node.id, score"))
            sc = {m: pr[m] for m in members if m in pr}
            if sc:
                leader = max(sc, key=sc.get)
                engine += " + FalkorDB PageRank"
        except Exception:
            engine += " + inflow ranking"
    near = sum(1 for _, _, m in flows if 7000 <= m < 10000)
    edges = [[a, e, "shares"] for a in members for e in adj.get(a, ()) if e in shared]
    return out({"engine": engine, "cluster_size": len(members), "members": members, "shared_entities": shared,
                "internal_transfers": len(flows), "transfers_just_under_10000": near,
                "internal_total": sum(m for *_, m in flows), "likely_leader": leader,
                "leader_inflow": inflow.get(leader, 0)},
               [N(m) for m in members] + [N(x) for x in shared], edges)


@mcp.tool()
def flag_accounts(account_ids: list[str], reason: str, finding_id: str = "") -> str:
    """ACTION: freeze these accounts in the data graph and record each decision in memory, linked to its supporting finding."""
    q(DATA, "MATCH (a:Account) WHERE a.id IN $ids SET a.frozen = true", {"ids": account_ids})
    for a in account_ids:
        did = "D-" + uuid.uuid4().hex[:6]
        q(MEM, "MERGE (a:Account {id:$a}) CREATE (d:Decision {id:$d, action:'freeze', reason:$r, ts:$t})-[:ON]->(a)",
          {"a": a, "d": did, "r": reason, "t": int(time.time())})
        if finding_id:
            q(MEM, "MATCH (d:Decision {id:$d}), (f:Finding {id:$f}) MERGE (d)-[:BASED_ON]->(f)", {"d": did, "f": finding_id})
        else:   # known ring: link to the latest earlier finding about it
            q(MEM, "MATCH (d:Decision {id:$d}), (:Account {id:$a})-[:MEMBER_OF]->(:Ring)<-[:ABOUT]-(f:Finding) "
                   "WITH d, f ORDER BY f.ts DESC LIMIT 1 MERGE (d)-[:BASED_ON]->(f)", {"d": did, "a": a})
    return out({"frozen": account_ids}, [N(a, "frozen") for a in account_ids])


@mcp.tool()
def save_finding(ring_name: str, members: list[str], leader: str, summary: str, evidence_path: str) -> str:
    """MEMORY WRITE: store what you learned (ring, members, leader, summary, evidence path) so future sessions recall it. Returns finding_id."""
    fid = "F-" + uuid.uuid4().hex[:6]
    q(MEM, "MERGE (r:Ring {name:$r}) SET r.leader=$l "
           "CREATE (f:Finding {id:$f, summary:$s, evidence:$e, ts:$t})-[:ABOUT]->(r) "
           "WITH r UNWIND $m AS m MERGE (a:Account {id:m}) MERGE (a)-[:MEMBER_OF]->(r)",
      {"r": ring_name, "l": leader, "f": fid, "s": summary, "e": evidence_path, "t": int(time.time()), "m": members})
    q(MEM, "MATCH (d:Decision)-[:ON]->(a:Account) WHERE a.id IN $m OPTIONAL MATCH (d)-[x:BASED_ON]->(:Finding) "
           "WITH d, x WHERE x IS NULL MATCH (f:Finding {id:$f}) MERGE (d)-[:BASED_ON]->(f)", {"m": members, "f": fid})
    return out({"finding_id": fid, "saved_members": len(members)}, [N(m, "memory") for m in members])


@mcp.tool()
def list_memory() -> str:
    """Overview of long-term memory: known rings (leader, member count), findings and freeze decisions. Use for questions about past investigations."""
    try:
        rings = q(MEM, "MATCH (r:Ring) OPTIONAL MATCH (a:Account)-[:MEMBER_OF]->(r) RETURN r.name, r.leader, count(a)")
        finds = q(MEM, "MATCH (f:Finding)-[:ABOUT]->(r:Ring) RETURN f.id, r.name, f.summary LIMIT 20")
        decs = q(MEM, "MATCH (d:Decision)-[:ON]->(a:Account) OPTIONAL MATCH (d)-[:BASED_ON]->(f:Finding) RETURN d.id, a.id, d.reason, f.id LIMIT 40")
    except Exception:
        rings, finds, decs = [], [], []
    return out({"rings": [{"name": n, "leader": l, "members": c} for n, l, c in rings],
                "findings": [{"id": i, "ring": r, "summary": t} for i, r, t in finds],
                "decisions": [{"id": i, "account": a, "reason": r, "based_on": f} for i, a, r, f in decs]})


if __name__ == "__main__":
    mcp.run()