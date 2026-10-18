# FraudTrace: giving an AI agent a graph to follow fraud rings, and a graph to remember them

*Built for Graph Hacks (WeMakeDevs x FalkorDB), Track 1: Agents That Act on Connected Data.*
*Code: https://github.com/vanshitachoudhary/fraudtrace | Replay demo: https://vanshitachoudhary.github.io/fraudtrace/*

## The problem

Fraud rarely shows up in a single transaction. One account sending 8,000 looks normal. But five accounts that share one device,
all sending just under 10,000 to the same leader, is a ring. The clue is in the *connections*, and an agent that reads one record at a time cannot see it.
Agents also forget: whatever they worked out yesterday is gone today.

## The idea

Give the agent a graph as its context, and a second graph as its memory. I used FalkorDB for both.

**Data graph (`fraud_data`):** `Account`, `Device`, `Address`, connected by `USED_DEVICE`, `LIVES_AT` and `SENT`.
The data is synthetic: 300 normal accounts and three hidden fraud rings.

**Memory graph (`fraud_memory`):** `Ring`, `Finding` and `Decision`. After an investigation, the agent writes what it found and why it froze each account.

## How the agent works

A flagged transaction arrives. The agent follows the same loop every time:

1. `recall_memory`: have I seen this account in a ring before?
2. If not, explore: `get_neighbors`, `find_ring`, `money_flow`, `shortest_path`.
3. Act: `flag_accounts` freezes the ring, `save_finding` writes it to the memory graph.

All tools live in an MCP server, and the agent picks which tool to call at each step. `find_ring` uses FalkorDB's native `algo.WCC`
(to find the cluster) and `algo.pageRank` (to find the leader). The proof path in the verdict is **recomputed by the server with a graph query**,
so I never have to trust the model's word for it.

## Does the memory help?

In my recorded run, the first investigation took 7 steps. Then I restarted the agent (no chat history) and gave it another transaction from the
same ring. Its first step found the ring in memory, and it finished in 3 steps, about 57% fewer. The only thing connecting the two sessions is the graph.
Step counts vary from run to run because the LLM does not always take the same route.

## What went wrong (and what I learned)

- **Setup:** Docker Desktop failed to install at first because Windows needed the Virtual Machine Platform feature, so I had to install WSL as administrator.
- **A library update broke my code:** the new `mcp` 2.x renamed `FastMCP`, so I pinned `mcp<2`.
- **LLM quota:** I could not pay for an API, so I used Gemini's free tier. Its models were renamed or retired, and the free quota is tiny (the limit I hit was 20 requests per day for one model).
  The agent now rotates between several models. When it switched models in the middle of an investigation, Gemini rejected the request (missing "thought signature"),
  so I flatten the earlier tool calls into plain text before switching.
- **Honest limit:** a live public deployment would not stay up on free tiers (a free FalkorDB Cloud instance sleeps and expires, and the LLM quota is tiny),
  so I added a replay demo that plays back real recorded runs. It is a recording, not a live backend, and the page says so.

## How I built it

I built this with an AI coding assistant (Claude, by Anthropic). **Claude wrote the code.** The idea, the direction and the decisions were mine:
I chose Track 1 and the fraud-ring idea, insisted that the agent must remember across sessions, asked for the interface and extra features
(decision trace, memory graph view, replay, case file export), read the hackathon rules and checked the project against them,
set up Docker and FalkorDB on my own machine, and tested every feature.

## Takeaways

- Put the relationships in a graph and a tricky question (is this a ring?) becomes a short query.
- Giving an agent its memory as a graph makes that memory inspectable: I can open it and see why a decision was made.
- Do not trust the model for the proof. Recompute it.
- Free tiers change under you: pin library versions, expect model names to change, and keep a recorded run as a fallback demo.

## What's next

Real transaction data would need privacy care, so I kept everything synthetic. Next I would add more fraud signals,
let the agent revisit and revise old findings, and give each tenant its own graph.

Thanks for reading. The full setup steps are in the repo README.
