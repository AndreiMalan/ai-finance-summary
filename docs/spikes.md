# Risk spikes (M1)

| Risk | How verified | Result | Date |
|---|---|---|---|
| R1 bundle size | `scripts/measure_bundle.py` (all M1–M9 runtime deps, Linux wheels, py3.12) | `TOTAL 373.1 MB (deps 372.8 + app src 0.3; limit 500 MB) -> OK`. Top 5: pandas 73.2, numpy 38.8, sqlalchemy 26.5, numpy.libs 26.2, zstandard 22.9 MB | 2026-10-26 |
| R2 SSE on Vercel | `curl -N https://<deploy>/_diag/sse` shows ticks ~1 s apart | local uvicorn: OK (ticks arrive at 0.03 / 1.03 / 2.05 / 3.06 / 4.08 s, done at 5.09 s). Vercel: **pending deploy** | 2026-10-26 |
| R3 checkpointer on Neon pooler | `scripts/check_checkpointer.py` | local (embedded Postgres): `R3 OK: interrupt persisted and resumed across pools: {'x': 1, 'answer': 'yes'}` (with and without `DATABASE_URL_UNPOOLED`). Neon: **pending** | 2026-10-26 |

## Vercel platform facts

Checked 2026-10-26 against the live docs; these supersede assumptions in `architecture.md`.

| Fact | Consequence for StatBrief | Source |
|---|---|---|
| FastAPI entrypoint: a top-level `app` in `app.py`, `index.py`, `server.py`, `main.py`, `wsgi.py` or `asgi.py` at the root, or the same names directly inside `src/` or `app/`; or `[tool.vercel] entrypoint = "module:app"` in `pyproject.toml`. | Root `app.py` (adds `src/` to `sys.path`). Our `src/statbrief/web/main.py` is nested, so it is not auto-detected. | [FastAPI on Vercel](https://vercel.com/docs/frameworks/backend/fastapi), [Python runtime – entrypoints](https://vercel.com/docs/functions/runtimes/python#python-entrypoints) |
| Dependencies: `pyproject.toml` (with/without `uv.lock`), `requirements.txt`, or `Pipfile`+`Pipfile.lock`. Docs do not state precedence; the builder is uv-based and reportedly prefers `pyproject.toml` when present ([vercel/vercel#14041](https://github.com/vercel/vercel/issues/14041)). | `[project].dependencies` in `pyproject.toml` now mirrors `requirements.txt` exactly (guarded by `tests/unit/test_packaging.py`); `requirements.txt` kept for pip/local dev. Without this, an empty `[project]` could deploy with nothing installed. | [Python runtime – dependencies](https://vercel.com/docs/functions/runtimes/python#dependencies) |
| Python version from `pyproject.toml`, `.python-version` or `Pipfile.lock`; available 3.12 (default), 3.13, 3.14. | `.python-version` = `3.12`. | [Python runtime – version](https://vercel.com/docs/functions/runtimes/python#python-version) |
| Bundle limit: **500 MB uncompressed** for Python (250 MB for other runtimes); Large Functions beta up to 5 GB on Fluid compute. All reachable files are bundled (no tree-shaking); `.pyc` added "when space allows". | `measure_bundle.py --limit-mb` defaults to 500. R1 = 373 MB → OK, ~127 MB headroom (would be OVER under the old 250 MB assumption). | [Function limits](https://vercel.com/docs/functions/limitations#bundle-size-limits), [Python runtime – bundling](https://vercel.com/docs/functions/runtimes/python#controlling-what-gets-bundled) |
| `functions` in `vercel.json` is keyed by the resolved entrypoint file (e.g. `"app.py"`); supports `maxDuration`, `excludeFiles` (glob relative to project root). | `"functions": {"app.py": {...}}` with `excludeFiles` for tests/docs/scripts/node_modules/etc. | [FastAPI – Vercel Functions](https://vercel.com/docs/frameworks/backend/fastapi#vercel-functions), [vercel.json](https://vercel.com/docs/project-configuration/vercel-json) |
| maxDuration (Fluid compute): Hobby 300 s default **and** maximum; Pro/Ent 300 s default, 800 s max. Streamed responses count toward it. | `maxDuration: 300` (the Hobby ceiling). Long agent runs must stream/checkpoint within 5 min. | [Function limits – max duration](https://vercel.com/docs/functions/limitations#max-duration) |
| `regions`: array of region ids, default `iad1`; Hobby may select any **single** region. | `"regions": ["fra1"]` — functions co-located with the Neon database in Frankfurt to avoid ~90 ms transatlantic round trips per query. | [vercel.json – regions](https://vercel.com/docs/project-configuration/vercel-json#regions) |
| Hobby memory 2 GB / 1 vCPU; request/response body max 4.5 MB. | Uploads > 4.5 MB must go direct to Blob (relevant to `MAX_UPLOAD_MB=10`, M2). | [Function limits](https://vercel.com/docs/functions/limitations) |
| Streaming responses are supported in the Python runtime (`StreamingResponse`). | Basis for R2; still to be confirmed on a real deploy. | [Python runtime – streaming](https://vercel.com/docs/functions/runtimes/python#streaming) |
| `app.mount(..., StaticFiles)` directories are promoted to the CDN at build time (kept in the bundle unless `[tool.vercel.fastapi.static] exclude = true`); disabled automatically when top-level middleware exists. Lifespan shutdown is limited to 500 ms. | `/static` will be served from the CDN. | [FastAPI – static assets](https://vercel.com/docs/frameworks/backend/fastapi#serving-frontend-and-static-assets) |
| Build image: Amazon Linux 2023 (glibc 2.34). | `measure_bundle.py` installs wheels for manylinux_2_34 / 2_28 / 2014 x86_64 (pip does not expand a manylinux tag to older glibc versions). | [Build image](https://vercel.com/docs/builds/build-image) |

## R1 full output

```
$ .venv/Scripts/python scripts/measure_bundle.py
python 3.12 / manylinux_2_34_x86_64 manylinux_2_28_x86_64 manylinux2014_x86_64 / bundle_probe_requirements.txt
TOTAL 373.1 MB (deps 372.8 + app src 0.3; limit 500 MB) -> OK
    73.2 MB  pandas
    38.8 MB  numpy
    26.5 MB  sqlalchemy
    26.2 MB  numpy.libs
    22.9 MB  zstandard
    17.1 MB  langchain_community
    15.0 MB  openai
    14.0 MB  pillow.libs
    13.8 MB  asyncpg
    10.8 MB  psycopg_binary.libs
     8.2 MB  psycopg_binary
     8.1 MB  reportlab
     7.9 MB  aiohttp
     6.9 MB  langsmith
     6.7 MB  PIL
```
Includes `.pyc` files (pip compiles on install), so it is a conservative estimate. `sqlalchemy`, `asyncpg` and `aiohttp` arrive transitively via `langchain-postgres` / `langchain-community`; dropping `langchain-community` (or using only its loaders' own deps) is the first lever if headroom shrinks.

## R3 local output

```
--- pooled only ---
setup      host: 127.0.0.1:43224/postgres  (DATABASE_URL_UNPOOLED not set -> DATABASE_URL)
run/resume host: 127.0.0.1:43224/postgres
interrupted: [Interrupt(value='approve?', id='0b223bc89678dad771603a4c9ef4e9b2', response_schema=None)]
R3 OK: interrupt persisted and resumed across pools: {'x': 1, 'answer': 'yes'}
--- with DATABASE_URL_UNPOOLED (localhost alias) ---
setup      host: localhost:43224/postgres  (DATABASE_URL_UNPOOLED)
run/resume host: 127.0.0.1:43224/postgres
interrupted: [Interrupt(value='approve?', id='887a855c01f85202e0e3f00c9c200b69', response_schema=None)]
R3 OK: interrupt persisted and resumed across pools: {'x': 1, 'answer': 'yes'}
```
Neon re-run: `DATABASE_URL=<pooled> DATABASE_URL_UNPOOLED=<direct> .venv/Scripts/python scripts/check_checkpointer.py`.

## R2 local smoke (uvicorn + `curl -N`)

```
$ curl -N -s -i http://127.0.0.1:8765/_diag/sse   (left column: client-side arrival time)
[ 0.031s] HTTP/1.1 200 OK
[ 0.031s] cache-control: no-cache
[ 0.031s] x-accel-buffering: no
[ 0.031s] content-type: text/event-stream; charset=utf-8
[ 0.031s] Transfer-Encoding: chunked
[ 0.031s] event: tick
[ 0.031s] data: {"i": 0, "t": 0.0}
[ 1.031s] event: tick
[ 1.031s] data: {"i": 1, "t": 1.0}
[ 2.047s] event: tick
[ 2.047s] data: {"i": 2, "t": 2.016}
[ 3.063s] event: tick
[ 3.063s] data: {"i": 3, "t": 3.032}
[ 4.078s] event: tick
[ 4.078s] data: {"i": 4, "t": 4.047}
[ 5.094s] event: done
[ 5.094s] data: {"elapsed": 5.063}
```
On Vercel: `curl -N https://<deploy>/_diag/sse` — pass if ticks arrive ~1 s apart rather than all at ~5 s.
