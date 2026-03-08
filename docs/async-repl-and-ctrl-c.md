# Async REPL + Ctrl+C: What We Tried and Why

## The Problem

Building an async CLI chat app that needs to:
1. Stream responses via `asyncio` + `aiohttp` + Anthropic SDK
2. Accept user input between turns
3. Support Ctrl+C to cancel an in-progress streaming/tool response **without exiting the app**

These goals conflict because `asyncio.run()` and `KeyboardInterrupt` don't play well together.

---

## Attempt 1: Fully Async REPL with `asyncio.to_thread(input)`

```python
async def main():
    async with aiohttp.ClientSession() as session:
        while True:
            user_input = await asyncio.to_thread(input, "\nYou: ")
            # ... stream response ...
```

**Issue:** When Ctrl+C is pressed during `asyncio.to_thread(input(...))`, the `KeyboardInterrupt` goes to the event loop, not the coroutine. The `try/except KeyboardInterrupt` inside `main()` never catches it — instead `asyncio.run()` receives it and tears down the entire loop. The app exits immediately.

The same problem occurs with `loop.run_in_executor(None, input)` — both delegate to a thread pool, and SIGINT is delivered to the main thread's event loop machinery, not to the awaiting coroutine.

---

## Attempt 2: Synchronous Outer Loop, `asyncio.run()` Per Turn

```python
def main():
    while True:
        user_input = input("\nYou: ")  # sync — Ctrl+C works naturally here
        try:
            asyncio.run(handle_turn(messages, user_input))
        except KeyboardInterrupt:
            print("\n[Cancelled]")
```

**What worked:** Ctrl+C during input and during streaming both behaved correctly. `input()` in a sync context handles SIGINT naturally, and `asyncio.run()` propagates `KeyboardInterrupt` to the caller.

**Issues:**
- Creates a **new event loop per turn** — wasteful
- Creates a **new `AsyncAnthropic` client per turn** — the httpx transport's `__del__` tries to close on the (now-dead) event loop, producing `RuntimeError: Event loop is closed` tracebacks on the next turn
- Creates a **new `aiohttp.ClientSession` per turn** — no connection reuse

The `Event loop is closed` error was fixed by adding `await client.close()` in a `finally` block, but the architecture was fundamentally wrong.

---

## Attempt 3 (Final): Single Event Loop + SIGINT Handler

```python
async def main():
    client = anthropic.AsyncAnthropic()
    current_task: asyncio.Task | None = None

    loop = asyncio.get_running_loop()
    def on_sigint():
        nonlocal current_task
        if current_task and not current_task.done():
            current_task.cancel()
    loop.add_signal_handler(signal.SIGINT, on_sigint)

    async with aiohttp.ClientSession() as session:
        while True:
            user_input = await loop.run_in_executor(None, lambda: input("\nYou: "))
            current_task = asyncio.create_task(stream_response(...))
            try:
                await current_task
            except asyncio.CancelledError:
                print("\n[Cancelled]")
```

**How it works:**
- Single persistent event loop, client, and session for the entire app lifetime
- `signal.SIGINT` handler calls `current_task.cancel()` instead of raising `KeyboardInterrupt`
- The `await current_task` catches `CancelledError` cleanly, cleans up conversation history, and returns to the prompt
- When no task is active (waiting at input prompt), SIGINT falls through to the executor thread and raises `KeyboardInterrupt` normally, exiting the app

**Why this is correct:**
- `asyncio.Task.cancel()` is the idiomatic way to interrupt async work — it propagates `CancelledError` through the entire await chain, including `async with` context managers (which clean up HTTP connections properly)
- No event loop churn, no client recreation, no transport cleanup races
- Connection pooling works across turns (both aiohttp and httpx)

---

## Key Takeaway

`KeyboardInterrupt` and `asyncio` are fundamentally incompatible for "cancel current work but keep running" patterns. The solution is to **not use `KeyboardInterrupt` at all** during async work — instead, install a SIGINT handler that uses `task.cancel()` to cooperatively cancel via `CancelledError`, which asyncio understands natively.
