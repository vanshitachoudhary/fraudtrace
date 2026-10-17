import os
from functools import lru_cache
from falkordb import FalkorDB

DATA = "fraud_data"      # accounts, devices, addresses, transactions
MEM = "fraud_memory"     # what the agent learned across sessions


@lru_cache(maxsize=1)
def db():
    return FalkorDB(host=os.getenv("FALKOR_HOST", "localhost"),
                    port=int(os.getenv("FALKOR_PORT", "6379")))


def q(graph, cypher, params=None):
    return db().select_graph(graph).query(cypher, params or {}).result_set


def kind(i):
    return "device" if i.startswith("DEV") else "address" if i.startswith("ADR") else "account"


def find_path(a, b, rels=("USED_DEVICE", "LIVES_AT", "SENT")):
    """Shortest path between two accounts. Tries FalkorDB shortestPath, falls back to BFS. Returns (node_ids, rel_types) or None."""
    from collections import deque
    try:
        r = q(DATA, "MATCH (a:Account {id:$a}), (b:Account {id:$b}) "
                    "MATCH p=shortestPath((a)-[:" + "|".join(rels) + "*1..8]-(b)) "
                    "RETURN [n IN nodes(p) | n.id], [x IN relationships(p) | type(x)]", {"a": a, "b": b})
        if r:
            return r[0][0], r[0][1]
    except Exception:
        pass
    adj = {}
    for rel in rels:
        for x, y in q(DATA, f"MATCH (x)-[:{rel}]->(y) RETURN x.id, y.id"):
            adj.setdefault(x, []).append((y, rel))
            adj.setdefault(y, []).append((x, rel))
    prev, dq = {a: None}, deque([a])
    while dq:
        cur = dq.popleft()
        if cur == b:
            break
        for nb, rel in adj.get(cur, ()):
            if nb not in prev:
                prev[nb] = (cur, rel)
                dq.append(nb)
    if b not in prev:
        return None
    ids, rs, cur = [b], [], b
    while prev[cur]:
        cur, rel = prev[cur]
        ids.append(cur)
        rs.append(rel)
    return ids[::-1], rs[::-1]