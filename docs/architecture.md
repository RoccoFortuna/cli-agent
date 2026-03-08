# Architecture Walkthrough

Single-file streaming CLI chat app (~370 lines) in `main.py`. Python + asyncio + Anthropic SDK + aiohttp.

## High-Level Flow

```
User types message
  -> main() appends to conversation history
  -> stream_response() sends history to Claude via streaming API
     -> Claude streams text tokens back (printed in real-time)
     -> If Claude wants to call a tool (stop_reason == "tool_use"):
        -> execute_tool() calls the Elyos API via call_api()
        -> Spinner runs as a background asyncio task during the call
        -> Tool result is appended to history
        -> Loop back to Claude with the result
     -> If Claude is done (stop_reason == "end_turn"):
        -> Append final response to history, return to input prompt
```

## Key Functions

### `call_api(session, endpoint, params)` — lines 58-122
Single HTTP call point for both weather and research endpoints. Every API quirk is handled here:

- **Throttle detection (G1)**: Checks response body for `"status":"throttled"` before using data. The API returns throttles as HTTP 200 (not 429), so checking status codes alone would silently pass garbage to Claude.
- **Retry with buffer (W3/W4)**: On throttle, waits `retry_after_seconds + 2s` buffer because the countdown is unreliable (tested: still throttled 4/5 times after waiting the exact value).
- **504 timeout (W2)**: Returns error immediately (no retry — the API already spent ~10s trying).
- **Empty body retry (R1)**: `{}` responses get retried after 1s.
- **20s client timeout (R3)**: Prevents hanging on the ~15s research outliers.
- Max 3 retries per call. Returns a dict — either the API data or `{"error": "..."}`.

### `normalize_weather(data)` — lines 125-148
Handles both response schemas (W1):
- **Flat**: `{location, temperature_c, condition, humidity}` (~70-80% of responses)
- **Multi-condition**: `{location, conditions: [{...}, ...]}` (~20-30%)

The schema is random per request, not per city. Checks for `conditions` key to decide which format to parse. Unknown schemas pass raw data through to the LLM.

### `format_research(data)` — lines 151-170
Formats research results and flags staleness:
- If `cached: true`, appends a note with `generated_at` and age in days so Claude can caveat its response.
- If no `summary` field (empty body that survived retry), returns a "no data" message.

### `execute_tool(session, name, input_data)` — lines 173-188
Simple dispatcher: routes `get_weather` and `research_topic` to `call_api()` + the appropriate normalizer.

### `spinner(message, done)` — lines 191-203
Animated braille spinner that runs as a background `asyncio.create_task`. Uses an `asyncio.Event` to distinguish completion from cancellation — shows a checkmark when the tool call completes, clears the line silently on user cancel.

### `stream_response(client, session, messages)` — lines 206-283
The core agentic loop:

1. Open a streaming connection to Claude (`client.messages.stream()`)
2. Print text deltas as they arrive (`async for text in stream.text_stream`)
3. Get the final message to check `stop_reason`
4. If `end_turn`: append to history, done
5. If `tool_use`: extract tool blocks, execute each with spinner, append results to history, loop back to step 1

On cancellation mid-stream, partial text is saved to conversation history. On cancellation mid-tool-call, remaining tools get "Cancelled by user." results so the API sees complete tool_use/tool_result pairs.

Tools are executed **sequentially** (not in parallel). This is a deliberate trade-off: simpler cancellation and the APIs have aggressive rate limits anyway.

### `main()` — lines 292-362
The REPL loop with cancellation support:

- **Single event loop**: One `asyncio.run()`, one `aiohttp.ClientSession`, one `AsyncAnthropic` client — all persistent for the session.
- **SIGINT handler**: `loop.add_signal_handler(signal.SIGINT, on_sigint)` intercepts Ctrl+C. During streaming, it calls `current_task.cancel()` which propagates `CancelledError` through the await chain. At the input prompt, first Ctrl+C prints a hint, second calls `os._exit(0)`.
- **Input**: `run_in_executor(None, input)` keeps the event loop alive while waiting for user input.

## Cancellation Strategy

```
Ctrl+C during streaming:
  SIGINT handler -> current_task.cancel() -> CancelledError propagates
  -> async with (stream) cleans up HTTP connection
  -> partial text saved to history
  -> Back to input prompt

Ctrl+C during tool execution:
  Same path, but CancelledError also cancels the spinner task via finally block
  Remaining tools get "Cancelled by user." results

Ctrl+C at input prompt:
  First press: prints hint
  Second press: os._exit(0) (input thread blocks clean shutdown)

Ctrl+D (EOF) at input prompt:
  EOFError caught, exits cleanly
```

## What Claude Knows (System Prompt)

The system prompt tells Claude that:
- Weather data is live and may vary between requests
- Research data may be cached/stale (tool result will note this)
- Some requests may fail due to timeouts or rate limits

This way Claude caveats its responses when it gets stale or error data from tools, rather than presenting it as fact.

## Trade-Offs

1. **Sequential vs parallel tool execution**: Chose sequential for simpler cancellation. Parallel would be faster if Claude requests both tools at once, but adds complexity (cancelling one while the other runs, rate limit interactions).

2. **No retry on 504**: The API spends ~10s before returning a 504. Retrying means the user waits ~30s for a blocklisted location that will never succeed. The user can just ask again.

3. **Single-file architecture**: Everything in `main.py`. Clean at ~370 lines.

4. **`os._exit(0)` for double Ctrl+C**: The `input()` thread can't be interrupted cooperatively, so we force-kill on the second press. A library like prompt_toolkit would fix this but adds a dependency.

5. **Model choice**: `claude-sonnet-4-5` — fast and cheap for tool use. Opus would be smarter but slower and more expensive for a chat loop.
