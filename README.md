# SmartGateway

**One front door for all your LLMs and MCP servers.** SmartGateway sits in front of any number of OpenAI-compatible servers
(llama.cpp, vLLM, Ollama, hosted APIs, ...) and gives your tools a single, stable URL, with smart routing, per-client keys, caching,
context compression and live observability. It also combines remote **MCP servers** into one MCP endpoint.

[![Watch the 1-minute SmartGateway overview](docs/video/poster.png)](docs/video/SmartGateway-promo.mp4)

<sub>▶ Click to watch the 1-minute overview (with sound).</sub>

[![Dashboard](docs/screenshots/dashboard.png)](docs/screenshots/dashboard.png)

## Highlights

- **Speaks every dialect.** Each gateway answers the OpenAI API (`chat/completions`, `embeddings`, `audio`, `images`, ...), the
  **Anthropic Messages API** (`/v1/messages`, works with Claude Code) and the **OpenAI Responses API** (works with Codex), detected
  automatically per request. No switches to flip.
- **Smart routing.** Capability matching (vision, audio, tools, reasoning), context-fit, slot-aware FIFO queueing, live slot sync,
  tiered small-to-large routing, fallbacks, sticky sessions, retries and circuit breaker.
- **Council gateways (LLM council).** Ask several models the same question, let them review each other's answers anonymously, and have a
  chairman model write one final answer, all behind a normal chat endpoint any client can use.
- **Your names, your rules.** Expose models under your own public names, plus optional extra aliases (`gpt-4o`, `claude-sonnet`, ...)
  so existing clients work unchanged. A unified endpoint serves several gateways from one URL.
- **MCP Servers and MCP Gateways.** Connect external MCP servers, then publish a curated mix of their tools, resources and prompts as
  one MCP server with its own keys.
- **Multi-tenant and safe.** Per-gateway API keys (hashed, shown once) with RPM/TPM limits, quotas and expiry; admin login;
  upstream keys encrypted at rest.
- **Cheaper and faster.** Response cache (in-memory or Redis, streaming aware) and context compression for long agent sessions.
- **Observability.** Dashboard, live animated flow graph, request log, speed per gateway/model (generation, prompt processing, first
  token, cache hit rate), stress test, MCP call statistics.

| Smart gateways | MCP gateways |
|---|---|
| [![Smart gateways](docs/screenshots/gateways.png)](docs/screenshots/gateways.png) | [![MCP gateways](docs/screenshots/mcp-gateways.png)](docs/screenshots/mcp-gateways.png) |

## Quick start

```bash
git clone https://github.com/ALange/SmartGateway.git && cd SmartGateway
SG_ADMIN_PASSWORD='choose-a-long-password' docker compose up -d --build     # UI + API on http://localhost:8080
```

1. **Endpoints** -> add your upstream server (URL, optional key). Models and capabilities are discovered; enable the ones you want.
2. **Gateways** -> create a gateway, add models under public names. Point clients at `http://HOST:8080/gw/<slug>/v1`.
3. Optional: create API keys on the gateway, add **MCP Servers** and combine them in an **MCP Gateway**.

Try it without real models: `docker compose --profile demo up -d` starts three mock upstreams (`http://mock-a:9000/v1`, `mock-b`, `mock-c`),
and `tools/load.sh http://localhost:8080 30` generates traffic against a gateway with slug `main`.

## Connect your tools

| Client | Setup |
|---|---|
| OpenAI SDKs, curl, Open WebUI, ... | base URL `http://HOST:8080/gw/<slug>/v1`, API key = a gateway key (or anything if the gateway is open) |
| **Claude Code** | `ANTHROPIC_BASE_URL=http://HOST:8080/gw/<slug>` and `ANTHROPIC_AUTH_TOKEN=<key>`; the gateway's **Live** page shows ready-made lines |
| **Codex** | custom provider with `base_url = "http://HOST:8080/gw/<slug>/v1"`, `wire_api = "responses"` (snippet on the **Live** page) |
| **MCP clients** | `claude mcp add --transport http dev http://HOST:8080/mcp/<slug> --header "Authorization: Bearer <key>"` (more on the MCP gateway card) |

Anthropic and Responses requests are translated onto the same routing pipeline, so routing, caching, limits and logs apply to them too.
Upstreams are OpenAI-compatible; limits of the translation (for example `web_search` tools and `previous_response_id`) are listed under
[Compatibility notes](#compatibility-notes).

## Configuration

`SG_ADMIN_PASSWORD` (+ `SG_ADMIN_USER`, default `admin`) is the UI/API login. **The admin API fails closed:** if it is unset, a random password is generated once, kept in the data volume (`admin_password`) and printed in the container log (`docker compose logs`) **on the first start only** (afterwards read it from `/data/admin_password`); you can then choose your own in **Settings → Admin password** (stored as a salted hash; the environment variable, when set, always wins); `SG_ALLOW_OPEN_ADMIN=1` disables login entirely (trusted dev machines only). The container runs as a non-root user · `SG_PORT` · `SG_SECRET_KEY` (otherwise a key file is generated
in the data volume, back it up together with the database) · `SG_CORS_ORIGINS` (browser clients of /gw and /v1) · `SG_ALLOWED_ORIGINS` (extra origins allowed to make state-changing admin API calls; by default only the page's own host) · `SG_SSL_CERT`/`SG_SSL_KEY` (HTTPS, or use
a reverse proxy) · `SG_DEFAULT_SLOTS` · `SG_HEALTH_INTERVAL` · `SG_FALLBACK_GRACE` · `SG_REDIS_URL`. Data lives in the `sg-data` volume
(SQLite); schema upgrades are applied automatically on start. Back up the data volume as a whole: without `secret.key` stored upstream keys
must be re-entered. Gateway API keys cannot be recovered after creation.

## Feature guide

### Endpoints and models
Add any OpenAI-compatible server. Models are discovered via `/models`; capabilities (`chat, reasoning, vision, audio, video, ocr,
embedding, tools, tts, image_gen`), context window and slot count come from listing metadata, llama.cpp `/props`, Ollama `/api/show`, name
heuristics and optional active probes, and everything is editable. Newly discovered models start disabled until you enable them.

### Smart gateways
Combine upstream models behind one URL with your own public model names. Strategies: least busy, weighted round-robin, priority failover,
fastest, random, **tiered** (by prompt complexity), plus fallback members and sticky sessions (`X-SG-Session` / `user`). Per model you can
set a system prompt, default parameters, a max-tokens ceiling, caching and compression. Requests can steer routing with `X-SG-Require`.

**Extra names (aliases):** a public model can have optional additional names. Clients may request any of them; the request is logged,
cached and answered under the name used, and `/models` lists each one. Keys' allowed-model lists accept the canonical name or an alias.

**Text to speech:** `GET /v1/audio/voices` (on `/gw/<slug>/v1` and on `/unified/v1`) lists the voices of the TTS models a caller may use,
or of one model with `?model=` (on the unified endpoint that is a gateway name or alias; without it every exposed TTS model is merged). The gateway forwards the upstream's own `/audio/voices` answer (for example Kokoro-FastAPI's
`{"voices": [...]}`), merging several TTS models. Voices are discovered when an endpoint is added or rescanned and shown as a checklist in the gateway editor, so you can offer a subset
without typing names (**Refresh from upstream** re-reads them); models exposed directly on the unified endpoint (Gateways -> Unified) get the same checklist. Nothing selected means every voice the upstream reports. For servers that
have no such endpoint, type the names in the same field.

### Council gateways
A **council** gateway turns several models into one better answer, behind an ordinary `chat/completions` URL (Gateways → **Council** tab).
It works in three stages:

1. **Councillors answer.** Up to 8 models receive the conversation in parallel and each writes its own answer.
2. **Anonymous peer review.** Each reviewer sees the other answers as "Response A, B, C…" in random order, with no model names, ranks them
   by accuracy and insight, and never reviews its own answer. Reviewers can be the councillors or a separate set of models, and the stage can be switched off.
3. **The chairman decides.** One model reads the answers and the rankings and writes the single final answer for the user, without
   mentioning the council.

Every role is a public model of one of your existing gateways (or a single endpoint model), and each call goes through that gateway's
normal pipeline, so its routing, failover, slots, caching, guardrails, API-key limits and request log all apply. Councils can be nested (up to 3 levels).

- **Resilient:** set the minimum number of answers needed and a timeout per call; a slow or failing councillor is simply left out.
- **Streaming progress:** streaming clients can receive "Stage 1: 2/3 answers in…" lines as reasoning text while the council works.
- **Details on request:** add an `sg_council` field (all answers, reviews and the ranking) to a response, always or per request.
- **Optional run history:** store every question, answer and review so you can open them from the logs. This keeps prompts and answers, which
  the normal request log never does, so it is off by default and has its own retention window.
- **Tunable:** edit the review and chairman prompts, cap tokens per call, and choose the public model name clients use.

Typical use: a "research council" of three different providers with a strong chairman, for questions where one model's blind spots matter.
It costs several model calls per question, so use it where quality matters more than latency.

### MCP Servers and MCP Gateways
**MCP Servers** connects remote MCP servers (Streamable HTTP, with legacy SSE fallback). Headers such as `Authorization` are stored
encrypted, and discovery, health checks and re-scans run automatically. **MCP Gateways** combines endpoints into one server at
`/mcp/<slug>`:

- tools and prompts are namespaced `prefix__name`, resources `mcp://prefix/<original uri>`, so servers never collide;
- per endpoint you tick which items are exposed (new upstream items stay off until you enable them);
- optional MCP API keys with rate limit, expiry and allowed tools; a gateway with no keys is open;
- a built-in **Test** dialog, recent calls, and dashboard statistics (calls, errors, latency, top tools). Only metadata is logged, never
  arguments or results; retention is configurable in Settings → Data.

Not supported: stdio servers, OAuth login flows to upstream servers (use static headers/tokens), sampling, subscriptions and
`listChanged` notifications.

### Unified endpoint
Gateways -> **Unified** tab exposes several gateways through one OpenAI-compatible URL, `/unified/v1`
(`chat/completions`, `completions`, `embeddings`, `models`, plus the Anthropic `messages` and `responses` surfaces).
You choose which gateways are exposed; the `model` a client sends is the **gateway name** (case-insensitive, the slug also works).
A gateway with several public models is reachable as `Name` (its configurable default model) and as `Name/model`.

Authentication: by default each gateway checks its own API key (open gateways need none). Create **unified API keys** in the same
card to protect the whole endpoint: with at least one key, every call needs a key (anonymous access is closed, even to open gateways).
A unified key works for all exposed gateways without their own keys (it carries no per-key limits or quotas); a gateway's own key still
works for that gateway only. Keys are stored hashed and the plaintext is shown once, at creation.
`GET /unified/v1/models` lists only what the caller may use, so protected gateways are not revealed to anonymous callers.
Routing, caching, compression, limits, quotas and logs all happen in the owning gateway, and responses report the gateway name as `model`.
Each exposed gateway can also have optional extra names (aliases), rejected if they clash with another name. It is off by default; the existing `/gw/<slug>/v1` and `/v1` URLs are unchanged.

### OpenCode Zen and OpenCode Go endpoints

Endpoints -> Add endpoint -> choose **OpenCode Zen** (pay-as-you-go, `https://opencode.ai/inference/v1`) or **OpenCode Go**
(subscription, `https://opencode.ai/inference/go/v1`) and paste your API key from the OpenCode console.

- **Catalog, not probes.** Models, capabilities (tools, reasoning, vision) and context windows come straight from OpenCode's `/models`
  metadata. No test requests are sent, so adding it costs nothing and does not touch your Go allowance. Re-scan refreshes the catalog.
- **Key check.** The catalog is public, so the key is verified with one 1-token request to a model listed as free; a rejected key is not saved.
- **Go requirements handled.** Go refuses requests without an `x-opencode-session` header. The gateway sends one id per conversation
  (`X-SG-Session` header, else `user`, else a hash of the system prompt + first user message), plus a `SmartGateway/<version>` user agent.
- **Concurrency.** Hosted models default to 8 concurrent requests (`SG_HOSTED_SLOTS`); adjust per model on the Endpoints page.
- **Limits.** Go allowances (5 h / weekly / monthly) surface as HTTP 429: add a second member as a fallback in the gateway.
  Stress tests on these endpoints spend real credits or allowance, and the dialog warns about it.
- A manually typed `opencode.ai/inference` or `opencode.ai/zen` URL is recognised automatically.

### Deploying on another host

`deploy/` holds a standalone compose file (no source needed on the target):

```bash
./deploy/build-image.sh linux/amd64     # or linux/arm64 - match the target host's CPU
# copy dist-deploy/ to the target, then there:
gunzip -c smartgateway-image.tar.gz | docker load
cp .env.example .env                    # set SG_ADMIN_PASSWORD, SG_PORT, ...
docker compose up -d                    # add --profile redis for the Redis cache
```

### Context compression
Long agent conversations resend every earlier tool output on every turn. **Context compression** shrinks those *older* tool results
(`role: "tool"` messages, including Anthropic `tool_result` blocks) before they reach the model; user, assistant and system text is never
touched, and the newest results (everything after the last assistant message) are left intact. Content is classified (JSON, logs, search
output, diffs, code, text) and routed to a compressor: JSON arrays are sampled, long strings trimmed, logs keep their error lines and
collapse repeats, grep output keeps paths and line numbers, everything else is truncated head+tail. Results are cached by content hash,
so later turns send a byte-identical (prompt-cache friendly) prefix, and a result is only used if it is shorter than the original.
Compressing before routing also lets long conversations fit smaller context windows.

* **Modes:** `off` (default) · `shadow` (measure what it would save, forward the original) · `on`. Set the global default in
  **Settings → Compression**; each public model can override it in the gateway editor. A client can opt out per request with
  `X-SG-Compress: off`. Compressed requests carry `X-SG-Compressed: <tokens saved>`.
* **Compression page:** savings by content type and compressor (unique savings vs. tokens not sent on every turn), a **cache-alignment**
  view (volatile content such as timestamps/UUIDs in system prompts lowers a 0–100 score and breaks prefix/KV-cache reuse), **context
  composition** (what fills the context, top repeated blocks, growth per session) and a **Try it** tool to preview compression on pasted text.
  Only sizes, labels and hashes are recorded, never request content.
* Compression is lossy (markers replace dropped parts): evaluate with `shadow` first.

Credits: the compression algorithms are ported from [TinyClaude](https://github.com/ALange/TinyClaude), which itself builds on
[Headroom](https://github.com/headroomlabs-ai/headroom). Small deviations: logs keep their original order and timestamped lines are not
auto-pinned, volatile-token detection strips only surrounding punctuation, results that are not shorter are discarded.

### Stress test
Each gateway card has a **Stress test** button that measures how much a gateway can deliver. It ramps up the number of simultaneous
streaming requests in stages (profiles: Quick `1,2,4,8`, Standard `…16`, Thorough `…64`, or custom levels) and reports per stage the total
and per-request tokens/s, prompt-processing speed, time to first token, queue wait, latency and the spread over upstream servers. The
summary gives the **peak throughput** (lowest concurrency within 2% of the best), the **single-stream speed**, the **comfortable
concurrency** (each request keeps ≥ 70% of its single-stream speed and does not queue) and where **saturation** starts, next to the
configured slot count. The test runs on the server through the real routing pipeline with unique prompts; the response cache and
compression are bypassed and the synthetic requests are not written to the request log or the dashboard. It stops early when requests start
failing or throughput falls past its peak, can be stopped at any time, only one test runs at a time, and past runs are kept for comparison
(last 20 per gateway). It sends real load to your upstream servers and needs an explicit confirmation.

### Response cache and optional Redis
Caching is configured **per public model** in the gateway editor (on/off, TTL, whether non-zero-temperature requests are cached too,
and whether **streaming requests** are cached). Cached: `chat/completions` and `embeddings`; send `X-SG-Cache: 0` to bypass or
`X-SG-Cache: 1` to force caching of one request. Responses carry `X-SG-Cache: hit|miss|skip|bypass`.

**Streaming** (opt-in per model, "Also cache streaming requests"): a stream is cached as one aggregated response once it has
completed cleanly, and replayed as a fresh SSE stream on later hits (`X-SG-Replay: instant|realistic`; *realistic* paces the replay over
the original generation time, capped at 10 s). Because streaming and non-streaming requests share the same cache key, an answer stored
by a stream also serves non-streaming clients and vice versa; the Anthropic `/v1/messages` and Responses surfaces benefit as well.
Interrupted streams (client disconnect, upstream error), `n > 1`, `logprobs` requests and legacy `completions` streams are never cached.
Replayed chunks are re-split on word boundaries, so chunk boundaries differ from the original stream; the content, tool calls, reasoning,
finish reason and usage are identical. The Playground's **Test cache** button demonstrates all of this.

By default entries live in process memory (lost on restart). To keep them across restarts and share them between instances, open
**Settings → General** and enter a Redis URL (`redis://[:password@]host:6379/0`, `rediss://` for TLS), press *Test connection*, enable and save —
no restart needed. A bundled Redis is available with `docker compose --profile redis up -d` (URL `redis://redis:6379/0`); `SG_REDIS_URL`
can preconfigure it. The URL (with its password) is stored encrypted. If Redis is unreachable requests keep working: the cache falls back
to memory (switchable in Settings) and reconnects automatically (`SG_CACHE_RETRY_AFTER`, default 15 s). Cached responses are shared by all
API keys of a gateway.


## Compatibility notes

- Anthropic: thinking blocks, streaming events, usage, stop reasons, errors and `/models` follow the Anthropic shape for Anthropic-style
  requests. Upstreams stay OpenAI-compatible, so `cache_control` hints are ignored.
- Codex / Responses: function and custom tools, reasoning items and streaming events are translated; `web_search`, `local_shell` and
  `previous_response_id` are not supported.
- The gateway **Test** dialog and Playground send OpenAI-format requests.

## Development
`cd backend && pip install -r requirements-dev.txt && pytest` (Python 3.11–3.12) · `cd frontend && npm i && npm run dev` ·
`python tools/e2e_stress.py` covers the stress test · `python tools/e2e_stream_cache.py` covers streaming cache · `python tools/e2e_compression.py` covers compression/alignment/insights · `python tools/e2e_redis.py` (needs Docker; starts temporary Redis containers) covers the cache/Redis scenarios · `python tools/e2e_v2.py` runs an end-to-end suite against throw-away mock upstreams and a temporary gateway (never touches your data).

## Exposing it to the internet

The admin UI and API always require a password (see Configuration), but the gateway URLs themselves are only as closed as you make them. Checklist:

- **Put a TLS reverse proxy in front** (Caddy, nginx, Traefik) and set `SG_FORWARDED_ALLOW_IPS` to the proxy's address, so login lockouts, the audit log and rate limits see the real client IP. Without it every visitor looks like the proxy. `Strict-Transport-Security` is sent automatically over HTTPS.
- **Give every public gateway, unified endpoint and MCP gateway an API key.** A gateway without keys is open to anyone who can reach the URL and spends your upstream capacity. Set `SG_IP_RPM` (requests per minute per client IP, 0 = off) as a second line of defence.
- **Set your own long `SG_ADMIN_PASSWORD`** (or change the generated one in Settings → Security). Failed logins are rate-limited per IP.
- Consider not publishing the admin UI at all: route only `/gw/`, `/v1/`, `/unified/` and `/mcp/` through the public proxy and keep `/` and `/api/` on a private network or VPN.
- Request bodies are capped (`SG_MAX_BODY_MB`, default 100 for gateway calls; `SG_MAX_ADMIN_BODY_MB`, default 20), and all request bodies in flight together are bounded too (`SG_MAX_INFLIGHT_BODY_MB`, default 512; beyond it requests get 503 + Retry-After). Public error messages are generic; full upstream errors and routing headers are only shown to the signed-in admin (`SG_VERBOSE_ERRORS=1` shows them to everyone, for private networks).
- Upstream response headers such as cookies, CORS and server banners are never passed on to clients. The container runs as a non-root user and its dependencies are pinned to patched releases (`backend/requirements.txt`); rebuild regularly.
- Content filter regexes are written by the admin and run on client text: avoid patterns with nested quantifiers.

## Security notes for contributors

- Never commit `.env`, `secret.key`, `*.db`, backups or exported images (all are in `.gitignore`). Use `.env.example` as the template.
- Set `SG_ADMIN_PASSWORD` in any deployment reachable from a network; without it the UI and management API are open.
- Back up `secret.key` (or keep `SG_SECRET_KEY` in your secret manager) together with the database.
