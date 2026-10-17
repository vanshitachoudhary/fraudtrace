"""Builds the synthetic fraud graph: 300 normal accounts + 3 hidden fraud rings."""
import random
import time
from common import db, DATA, MEM

random.seed(7)
NOW = int(time.time())


def acc(i): return f"ACC-{i:04d}"


def main():
    for g in (DATA, MEM):
        try:
            db().select_graph(g).delete()
        except Exception:
            pass
    g = db().select_graph(DATA)
    for lbl in ("Account", "Device", "Address"):
        g.query(f"CREATE INDEX FOR (n:{lbl}) ON (n.id)")

    accounts, devices, addresses, uses, lives, sends = [], [], [], [], [], []
    dev_n = [0]
    adr_n = [0]

    def new_dev():
        dev_n[0] += 1
        d = f"DEV-{dev_n[0]:04d}"
        devices.append({"id": d})
        return d

    def new_adr():
        adr_n[0] += 1
        a = f"ADR-{adr_n[0]:04d}"
        addresses.append({"id": a})
        return a

    # --- normal accounts (a few legit shared devices / family addresses) ---
    prev_d = prev_a = None
    for i in range(1, 301):
        a = acc(i)
        accounts.append({"id": a, "name": f"User {i}"})
        d = prev_d if (prev_d and random.random() < 0.04) else new_dev()
        ad = prev_a if (prev_a and random.random() < 0.10) else new_adr()
        uses.append({"a": a, "e": d})
        lives.append({"a": a, "e": ad})
        prev_d, prev_a = d, ad

    tid = [1000]

    def send(a, b, amt, flagged=False, fid=None):
        if fid is None:
            tid[0] += 1
            fid = f"TXN-{tid[0]}"
        sends.append({"id": fid, "a": a, "b": b, "amt": amt, "fl": flagged,
                      "ts": NOW - random.randint(3600, 86400 * 20)})

    for _ in range(700):
        a, b = random.sample(range(1, 301), 2)
        send(acc(a), acc(b), random.randint(20, 1500))

    # --- three hidden fraud rings: shared devices/addresses + structuring to one leader ---
    nxt = 301
    flagged_id = [9000]
    ring_members = []
    for size in (6, 7, 5):
        members = []
        for _ in range(size):
            a = acc(nxt)
            nxt += 1
            accounts.append({"id": a, "name": f"User {nxt - 1}"})
            members.append(a)
        d1, d2, a1, a2 = new_dev(), new_dev(), new_adr(), new_adr()
        for j, m in enumerate(members):
            if j in (0, 1):
                uses += [{"a": m, "e": d1}, {"a": m, "e": d2}]   # links both devices together
            else:
                uses.append({"a": m, "e": d1 if j % 2 == 0 else d2})
            lives.append({"a": m, "e": a1 if j % 3 else a2})
        leader = members[0]
        mule = acc(random.randint(1, 300))
        total = 0
        for j, m in enumerate(members[1:], start=1):
            for k in range(2):
                amt = random.randint(7000, 9400)    # just under the 10,000 reporting threshold
                total += amt
                if k == 0 and j <= 2:
                    flagged_id[0] += 1
                    send(m, leader, amt, True, f"TXN-{flagged_id[0]}")
                else:
                    send(m, leader, amt)
        send(leader, mule, int(total * 0.9))
        ring_members.append(members)

    # --- two false positives: normal accounts flagged by a dumb rule ---
    for _ in range(2):
        a, b = random.sample(range(1, 301), 2)
        flagged_id[0] += 1
        send(acc(a), acc(b), random.randint(4500, 6000), True, f"TXN-{flagged_id[0]}")

    def batch(cy, rows):
        for i in range(0, len(rows), 400):
            g.query(cy, {"r": rows[i:i + 400]})

    batch("UNWIND $r AS x CREATE (:Account {id:x.id, name:x.name, frozen:false})", accounts)
    batch("UNWIND $r AS x CREATE (:Device {id:x.id})", devices)
    batch("UNWIND $r AS x CREATE (:Address {id:x.id})", addresses)
    batch("UNWIND $r AS x MATCH (a:Account {id:x.a}),(e:Device {id:x.e}) CREATE (a)-[:USED_DEVICE]->(e)", uses)
    batch("UNWIND $r AS x MATCH (a:Account {id:x.a}),(e:Address {id:x.e}) CREATE (a)-[:LIVES_AT]->(e)", lives)
    batch("UNWIND $r AS x MATCH (a:Account {id:x.a}),(b:Account {id:x.b}) "
          "CREATE (a)-[:SENT {id:x.id, amount:x.amt, flagged:x.fl, ts:x.ts}]->(b)", sends)

    print(f"Seeded {len(accounts)} accounts, {len(devices)} devices, {len(addresses)} addresses, {len(sends)} transactions.")
    print("Hidden rings (for your eyes only):", [m[0] + " (leader)" for m in ring_members])
    print("Flagged cases: TXN-9001..TXN-9006  (9001 & 9002 are both Ring 1 -> perfect for the memory demo)")


if __name__ == "__main__":
    main()
