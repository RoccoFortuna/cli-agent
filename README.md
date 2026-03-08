# Elyos CLI Agent

Streaming CLI chat app with tool calling for weather and research APIs. Single-file Python app (~370 lines) using asyncio, Anthropic Claude SDK, and aiohttp.

Handles 12 documented API quirks gracefully — see [`docs/api-quirks-report.md`](docs/api-quirks-report.md) for full findings.

## Setup

```bash
uv sync
cp .env.example .env  # add your API keys
uv run python main.py
```

## Key Features

- **Streaming**: Token-by-token response display via Anthropic's streaming API
- **Tool calling**: Agentic loop — Claude calls weather/research tools, gets results, continues until done
- **Cancellation**: Ctrl+C cancels cleanly mid-stream or mid-tool-call, preserving conversation history
- **Pending state**: Animated spinner during API calls
- **Quirk handling**: Rate-limit-as-200 detection, schema normalization, empty body retry, stale cache flagging, and more — all in a single `call_api` function

## Project Structure

| File | Description |
|------|-------------|
| `main.py` | The complete chat app |
| `investigate_api_quirks.py` | API investigation script used to discover quirks |
| `docs/api-quirks-report.md` | All 12 quirks documented with test data |
| `docs/architecture.md` | Code walkthrough and design decisions |
| `docs/async-repl-and-ctrl-c.md` | Why asyncio + Ctrl+C is hard, and the three approaches I tried |

## Investigation Script

```bash
uv run python investigate_api_quirks.py --all       # run all API tests
uv run python investigate_api_quirks.py --weather   # weather endpoint only
uv run python investigate_api_quirks.py --research  # research endpoint only
uv run python investigate_api_quirks.py --deep      # deep-dive: schema variance, rate limit recovery
uv run python investigate_api_quirks.py --general   # general/cross-cutting
```
