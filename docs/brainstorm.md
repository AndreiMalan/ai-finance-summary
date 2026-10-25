# Brainstorm — Overall Architecture

Fixed by decisions: FastAPI + HTMX on Vercel (D-002), OpenAI (D-003), Neon Postgres + Blob (D-009, D-014), LangGraph feature mapping (D-011). The open question is **how the long-running LangGraph workflow executes inside short-lived Vercel functions**, and how the code is layered around it.

---

## Approach 1 — Segmented in-request streaming (SSE with time budget)

**Summary.** The browser opens an SSE connection (`GET /reports/{id}/run/stream`). The handler loads the thread from the Postgres checkpointer and calls `graph.stream(..., stream_mode=["updates","custom"])`, forwarding events as SSE. It stops when the graph hits an `interrupt()`, finishes, or a **time budget** (e.g. 240 s of a 300 s limit) is reached — then it breaks after the current super-step (already checkpointed) and sends a `continue` event; the browser reconnects automatically and execution resumes from the checkpoint.

**Steps.**
1. Layered packages: `domain/` (Pydantic schemas), `ingest/`, `analytics/`, `rag/`, `llm/`, `graph/`, `web/`, `storage/`.
2. Graph compiled once per cold start with `PostgresSaver`/`PostgresStore` on a pooled Neon connection.
3. SSE endpoint with budget guard + per-thread advisory lock (`pg_try_advisory_lock`) to prevent two tabs running the same thread.
4. Interrupt payloads rendered as HTMX forms; submitting posts `Command(resume=...)` and reopens the stream.

**Pros.** Real streaming (spec requirement), simplest mental model, one deployment, natural fit for interrupt/resume, checkpoints make budget breaks lossless.
**Cons.** A single node that runs longer than the budget (e.g. a very slow LLM call) can still time out — mitigated by per-call timeouts and retries; SSE through Vercel must flush correctly (verify early).
**Complexity.** Medium.

## Approach 2 — Job queue with polling

**Summary.** `POST /run` writes a job row and returns immediately; work is executed by a separate invocation (self-triggered HTTP call or Vercel Cron every minute) that runs graph steps and writes progress events into a `run_events` table. The UI polls every 1–2 s via HTMX.

**Steps.** Jobs table + status machine; worker endpoint protected by a secret; self-invocation chain to continue past the time limit; polling partials for the timeline.

**Pros.** Request latency independent of graph duration; robust to browser disconnects; events table doubles as an audit log.
**Cons.** Not real streaming (polling); Vercel has no persistent workers so the self-invocation chain is fragile; Cron granularity is 1/min and limited on Hobby; more moving parts (queue, worker auth, retries of the worker itself).
**Complexity.** High.

## Approach 3 — Client-driven step execution

**Summary.** Each HTTP request executes **exactly one graph super-step** (`graph.invoke` with `interrupt_before` on every node, or a step limit), returns the rendered timeline fragment, and HTMX immediately triggers the next request until an interrupt or the end.

**Steps.** Wrap the graph so each call advances one step; HTMX `hx-trigger="load"` chain; progress = re-rendered fragment per step.

**Pros.** Every request is short and predictable; trivially time-safe; easy to debug step by step.
**Cons.** Not genuine LangGraph streaming (custom intra-node progress is lost); `interrupt_before`-everywhere distorts the graph design and makes Send fan-out awkward; many round trips; parallel branches serialize per request.
**Complexity.** Medium.

---

## Recommendation — Approach 1 (segmented in-request streaming)

It is the only option that demonstrates **LangGraph streaming** as the brief requires while keeping a single, simple deployment. Checkpointing per super-step means a budget break loses nothing, so it inherits Approach 3's time-safety without distorting the graph. Approach 2's queue is over-engineered for serverless without persistent workers. We also keep `run_events` (from D-012) as the persisted progress log, so a reconnecting browser can replay the timeline — borrowing Approach 2's best property.

Early verification items (milestone 1): SSE flush behaviour on Vercel Python runtime; PostgresSaver over Neon pooled connection; bundle size.
