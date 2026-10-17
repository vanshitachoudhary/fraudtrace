"""Builds docs/index.html: the same UI, but it replays the recorded runs in docs/recordings (no database, no LLM, no key).
Usage: python make_static_demo.py   (after recording runs with RECORD=1, see README)"""
import glob
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
REC = os.path.join(HERE, "docs", "recordings")
recorded = sorted(os.path.basename(f)[:-5] for f in glob.glob(os.path.join(REC, "TXN-*.json")))
assert recorded, "No recordings found in docs/recordings. Run the app with RECORD=1 first."

cases_path = os.path.join(REC, "cases.json")
cases = [c for c in json.load(open(cases_path, encoding="utf-8")) if c["id"] in recorded]
json.dump(cases, open(cases_path, "w", encoding="utf-8"))

SHIM = """<script>
/* Replay shim: serves the recorded runs instead of the live backend. */
const _fetch = window.fetch.bind(window);
let current = null;
const asJson = o => Promise.resolve(new Response(JSON.stringify(o), {headers: {"Content-Type": "application/json"}}));
window.fetch = (url, opts) => {
  const u = String(url);
  if (u.startsWith("/api/cases")) return _fetch("recordings/cases.json");
  if (u.startsWith("/api/stats")) return asJson(current ? current.stats : {rings: 0, frozen: 0, findings: 0});
  if (u.startsWith("/api/memory")) return asJson(current ? current.memory : {nodes: [], edges: []});
  if (u.startsWith("/api/decisions")) return asJson(current ? current.decisions : []);
  if (u.startsWith("/api/reset")) { current = null; return asJson({ok: true}); }
  if (u.startsWith("/api/ask")) return asJson({answer: "Ask the graph needs the live backend (FalkorDB + an LLM). This page only replays recorded runs. Run the project locally to try it.", tools: []});
  return _fetch(url, opts);
};
class EventSource {
  constructor(url) {
    const txn = new URL(url, location.href).searchParams.get("txn");
    this.closed = false;
    _fetch("recordings/" + txn + ".json").then(r => { if (!r.ok) throw new Error("missing"); return r.json(); }).then(async rec => {
      for (const ev of rec.events) {
        await new Promise(r => setTimeout(r, ev.type === "thought" ? 900 : 650));
        if (this.closed) return;
        if (ev.type === "final") current = rec;
        this.onmessage && this.onmessage({data: JSON.stringify(ev)});
      }
    }).catch(() => this.onmessage && this.onmessage({data: JSON.stringify({type: "error", text: "This case was not recorded for the replay demo."})}));
  }
  close() { this.closed = true; }
}
</script>"""
BANNER = ('<div style="background:#2a2150;color:#d9e0ec;padding:8px 20px;font-size:13px;border-bottom:1px solid #a78bfa">'
          'Replay demo: this page replays real recorded runs of FraudTrace (no database or API key needed). '
          'The full app, with FalkorDB and the live agent, runs locally: see the README in the repo.</div>')

src = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
marker = "<script>\nconst $ ="
assert src.count(marker) == 1 and src.count("</header>") == 1
html = src.replace(marker, SHIM + "\n" + marker, 1).replace("</header>", "</header>\n" + BANNER, 1)
open(os.path.join(HERE, "docs", "index.html"), "w", encoding="utf-8").write(html)
print("Built docs/index.html with recordings:", ", ".join(recorded))