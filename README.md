# LLM Sales Agent for Avito

An LLM agent that answers buyers on Avito for a country leisure complex (private saunas, dome gazebos, a grill house, an event tent): it quotes exact prices, checks live availability in YCLIENTS and hands the deal to a manager at the right moment.
Runs in production for the client, «ПарМангал» (Troitsk, Moscow region), on FastAPI, PostgreSQL, Redis and DeepSeek.

[![CI](https://github.com/nurzhan2/llm-sales-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/nurzhan2/llm-sales-agent/actions/workflows/ci.yml) ![Python](https://img.shields.io/badge/python-3.12-blue)

Shown with the client's permission. Customer conversations, personal data and photos are removed from the code and from the git history.

## Problem

- Buyers write on Avito at any hour and expect an answer in minutes; managers answered every chat by hand.
- A wrong price or a "yes, it's free" for a slot already booked in YCLIENTS costs real money and a double booking.
- A plain chatbot invents prices, promises bookings it cannot make and talks over the human manager when one joins the chat.

## Features

- **Prices never come from the model.** A deterministic pricing engine reads a YAML catalog; the LLM must call `calculate_price`, and output guards block any amount that did not come from a tool result.
- **Live availability.** `check_availability` and `find_next_available` read YCLIENTS `book_times` and check the whole interval, not just the start time.
- **Twelve tools** behind one tool-calling loop: pricing, availability, discounts, photos, leads, booking hand-off, knowledge-base answers, escalation.
- **Two inbound paths, one pipeline.** Avito webhook and a polling worker feed the same pipeline; duplicates are dropped by a Redis claim and a unique index on `avito_message_id`.
- **One outbound door.** Every message (agent reply, follow-up, operator approval, timeout fallback) passes `OutboundGate`: listing allowlist, kill switch, daily send cap, human takeover, manual hold.
- **Human in the loop.** A Telegram bot for operators: discount approvals with a timeout fallback, take over / return a chat, price edits with audit and revert, a daily self-check report, alerts.
- **Discount policy as code.** Tiered concessions with floors, a per-day limit and a ratchet: a price already quoted in the dialog can only go down.

## Architecture

```mermaid
flowchart LR
  A[Avito webhook] --> P
  B[Avito poller] --> P
  P[Pipeline<br/>dedup, debounce, takeover check] --> L[Agent loop]
  L <--> M[DeepSeek<br/>Anthropic-compatible API]
  L --> T[Tools]
  T --> E[Pricing engine<br/>YAML catalog + DB overrides]
  T --> Y[YCLIENTS API]
  L --> G[Output guards]
  G --> O[OutboundGate] --> AV[Avito Messenger API]
  P & L & O --- DB[(PostgreSQL)]
  P & O --- R[(Redis)]
  TG[Telegram ops bot] --- P
  TG --- E
```

- **Pipeline** (`app/pipeline.py`): accepts a message, drops duplicates and echoes of our own messages, merges bursts of messages within a 10-second window, checks whether a human manager owns the chat.
- **Agent loop** (`app/agent/loop.py`): builds the prompt (catalog, listing context, current date), runs the tool-calling loop, then applies output guards before anything reaches the client.
- **Pricing engine** (`app/pricing/`): pure functions over the knowledge base; returns `ok`, `needs_input`, `invalid` or `blocked` (the last one means "a manager decides").
- **OutboundGate** (`app/channels/outbound_gate.py`): the only object in the process that can send to Avito.
- **Storage**: PostgreSQL holds chats, messages, dialog state, leads, bookings, price overrides and the operator audit log (Alembic migrations); Redis holds dedup claims, the OAuth token, the kill switch, the daily send counter and short holds on quoted slots.

## Tech stack

| Layer | Technology | Why |
|---|---|---|
| API | FastAPI, Uvicorn | async I/O: webhooks, Avito, YCLIENTS and LLM calls in one event loop |
| Database | PostgreSQL 16, SQLAlchemy 2 (async), Alembic | JSONB for LLM metadata, ARRAY for photo ids, versioned schema |
| Cache / locks | Redis | dedup claims, kill switch and counters shared by webhook and poller |
| LLM | DeepSeek via its Anthropic-compatible endpoint, Anthropic SDK | low cost per dialogue; the same code replays dialogues on Claude models for comparison |
| Operator UI | aiogram 3 (Telegram), server-rendered admin pages | operators already live in Telegram |
| Booking | YCLIENTS REST API | the client's booking system of record |
| Observability | structured logs, Prometheus `/metrics`, daily self-check | guard-rail counters and silent-chat detection |
| Deploy | Docker, Railway | migrations run in the container entrypoint before Uvicorn |

## Key decisions & trade-offs

- **The model never produces a price.** Alternative: put the price list in the prompt. In replay runs the model miscomputed totals (for example, pricing two hours right after stating a three-hour minimum), so prices come from `calculate_price` and the last guard compares every amount in the reply with the tool results; a mismatch replaces the reply with a safe question or escalates. Cost: an extra tool round-trip per price question, and some replies get rewritten (53 of 769 LLM replies between August and November 2026).
- **One outbound door instead of checks at each call site.** Four code paths send messages; when checks lived at the call sites, a delayed follow-up once reached a client the agent was not allowed to write to. `OutboundGate` wraps the Avito client and every send method. Cost: a Redis and a DB read per send, and when Redis is down the kill switch and the daily cap fail closed (no sends) instead of open.
- **Read-only YCLIENTS plus soft holds.** The integration token cannot write bookings: a manager enters the booking after the prepayment, hours after the bot quoted the slot, and until then YCLIENTS shows it as free. That gap produced double bookings, so a quoted slot is held in Redis for that chat (short after a quote, longer after the hand-off) and counts as busy for every other chat. Cost: a hold can block a slot for a client who never pays, until it expires.
- **Detecting the human manager from message echoes.** Avito reports the bot's and the manager's messages with the same author id. The gate records each text before sending it, and an echo that matches is ours; anything else is the manager and pauses the bot for that chat. If the client then waits 30 minutes with no reply, the chat returns to the bot and the operator gets an alert. Cost: a text-matching heuristic; photo echoes needed a separate marker.
- **Unknown business rules block instead of guessing.** Catalog fields the client has not confirmed are stored as `value: null` with a question id; the engine returns `blocked` and the bot hands the question to a manager. Operators change prices from Telegram; edits are stored as DB overrides with author, comment and one-step revert. Cost: more handoffs until the catalog is complete.
- **A cheap model, kept honest by code.** DeepSeek runs through its Anthropic-compatible endpoint, so the agent uses the Anthropic SDK and tool format, and Claude models can replay the same dialogues as a baseline and judge. DeepSeek has no strict tool schemas on this endpoint, so every tool validates its own arguments and returns errors to the model instead of crashing the turn. The provider layer supports a fallback after three consecutive errors, but the runtime Anthropic fallback was removed: its production key was a placeholder, and a fallback that only looks configured is worse than none. A daily spend cap pauses the agent.

## LLM quality, safety and cost

- **Output guards** (`app/agent/loop.py`) run after the model and before sending: a price without a tool call, an amount not returned by a tool, a claim about free slots without an availability check, a date that contradicts "today", a booking promised in words, more than one question per message, the bot describing its own instructions. Each firing is logged with the withheld text and counted in Prometheus.
- **Offline replay** (`scripts/replay.py`, `app/quality/`): replays labelled real dialogues through the agent and scores each turn with rule-based asserts and an LLM judge. The labelled dialogues are client data and are not part of this repository.
- **Daily self-check** (`app/ops/selfcheck.py`): listings without a zone, YCLIENTS resources that disappeared, stale price overrides, broken photos, chats where nobody answered the client, and guard-rail counts.
- **Provider failure:** the turn fails closed: the error is logged and nothing half-built is sent to the client. The daily self-check reports chats left without an answer, so a provider outage shows up as a list of chats to answer by hand.

## Quick start

```bash
cp .env.example .env          # DRY_RUN=true by default: replies go to Telegram for approval
docker compose up -d --build  # app on :8000, PostgreSQL, Redis; migrations run on start
curl localhost:8000/health
```

| Variable | Purpose | Example |
|---|---|---|
| `DATABASE_URL` | PostgreSQL connection | `postgresql+asyncpg://parmangal:parmangal@postgres:5432/parmangal` |
| `REDIS_URL` | Redis connection | `redis://redis:6379/0` |
| `LLM_PROVIDER` | model provider (only `deepseek` at runtime) | `deepseek` |
| `DEEPSEEK_API_KEY` | model access | `sk-...` |
| `ANTHROPIC_API_KEY` | optional, offline replay and judge only | `sk-ant-...` |
| `AVITO_CLIENT_ID`, `AVITO_CLIENT_SECRET`, `AVITO_USER_ID` | Avito Messenger API | from the Avito developer cabinet |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OPS_CHAT_ID` | operator bot | `123456:AA...`, `-100...` |
| `DRY_RUN` | `true`: nothing is sent to clients | `true` |

Without Avito and model keys the service starts and `/health` responds, but no messages are processed. The full list of settings with comments is in `.env.example`.

## Testing

```bash
make install
make test      # unit, pipeline and API tests
make test-db   # the same plus PostgreSQL storage and migration tests (Docker)
make lint      # ruff check + format check
```

1,658 tests: 1,567 run without external services, 90 more run against PostgreSQL (repositories, JSONB/ARRAY/ENUM columns, Alembic migrations), and one checks the client photo bundle, which is not shipped here, so it is skipped. External APIs are faked with `respx` and in-memory stores. CI runs ruff and the full suite with a PostgreSQL service on every push ([workflow](.github/workflows/ci.yml)).

## Project structure

```
app/agent/      tool-calling loop, prompts, tools, output guards, LLM providers
app/pricing/    pricing engine and discount policy
app/kb/         YAML knowledge base, loader, operator overrides
app/booking/    YCLIENTS client, availability, short-lived holds
app/channels/   Avito API client, payload parsing, OutboundGate, dedup, kill switch
app/avito/      polling worker and cursors
app/ops/        Telegram operator bot, self-check, follow-up scheduler
app/admin/      admin pages (dialogs, leads, discounts, costs, catalog revert)
app/quality/    replay harness: rule asserts and LLM judge
migrations/     Alembic
scripts/        one-off and maintenance tools (webhook registration, imports, probes)
tests/          pytest suite
docs/           operations runbook, deploy guide, provider comparison (in Russian)
```

## Results

Measured on the production database, 25 August – 17 November 2026 (SQL over `messages`, `operator_actions`, `llm_meta`).

| Metric | Value |
|---|---|
| Customer chats | 467 (1,466 customer messages, 719 agent replies delivered) |
| First reply, agent | median 20.9 s, p90 29.3 s, 95.3% within 5 min (n = 617) |
| First reply, human managers, same period | median 31 min, p90 10.9 h, 33.6% within 5 min (n = 235) |
| Replies stopped by output guards | 53 of 769 LLM replies (6.9%): 33 "free" without an availability check, 9 price without a tool call, 5 wrong amount, 4 wrong date, 2 promises |
| Model cost | 339.59 RUB in total; 0.44 RUB per reply; median 0.76 RUB per chat (n = 290) |
| False "manager took over" events (echo race) | 32 of 230 before the fix, 0 of 126 after (29 Sep – 17 Nov) |
| Listings / zones covered | 65 Avito listings, 9 zones |
| Tests | 1,658 tests, 88% line coverage (`pytest --cov=app` with PostgreSQL); CI green |

The agent's first-reply time includes the 10-second window that merges consecutive customer messages. Manager timings have up to 60 s error from the poller interval.

- **Offline replay of 27 labelled dialogues, 146 turns, DeepSeek, August 2026** (`docs/PROVIDER_COMPARISON.md`): 29 of 29 `calculate_price` calls were valid on the first attempt; median turn latency 4.0 s; cost 8.05 RUB for the run (about 0.30 RUB per dialogue, priced as a cache miss). The most frequent violation was two questions in one message (26 cases), which led to the one-question guard.
- **Not measured yet:** conversion to bookings (bookings are placed by the manager in YCLIENTS without a source tag) and uptime (no external monitoring).

## Limitations & roadmap

- The bot does not write bookings to YCLIENTS: the integration token is read-only, so a manager confirms each booking after the prepayment (`create_booking` hands the details over).
- Night slots (23:00–09:00) and New Year dates are handed to a manager: YCLIENTS exposes sessions only from 09:00 to 23:00, and holiday prices are not in the catalog yet.
- Debounce and the echo registry live in process memory, so the service runs as a single instance; scaling out would move both to Redis.
- Manager detection is a text-matching heuristic over Avito echoes; a manager who repeats the bot's last message word for word is taken for the bot.
- The replay harness depends on labelled client dialogues that are kept private.
- Code comments, prompts and operator docs are in Russian, the language of the business and its customers.

## My role

Client project. I designed and built it end-to-end: architecture, LLM integration and guard rails, pricing and discount engine, Avito and YCLIENTS integrations, Telegram operator tooling, tests, CI, deployment on Railway and production support based on the client's feedback.

## License

Source code shown for review with the client's permission; no license is granted for reuse.
© Nurzhan Kalmurat · [LinkedIn](https://linkedin.com/in/kalmuratnurzhan)
