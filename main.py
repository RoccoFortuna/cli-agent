"""
Streaming CLI chat app with tool calling for weather and research APIs.
Handles 12 documented API quirks gracefully (see docs/api-quirks-report.md).
"""

import asyncio
import os
import signal
import sys

import aiohttp
import anthropic
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://elyos-interview-907656039105.europe-west2.run.app"
API_KEY = os.getenv("ELYOS_API_KEY")
MODEL = "claude-sonnet-4-5-20250929"
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

TOOLS: list[anthropic.types.ToolParam] = [
    {
        "name": "get_weather",
        "description": "Get current weather for a city. Fast response (~200ms).",
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "City name, e.g. London, Tokyo"}
            },
            "required": ["location"],
        },
    },
    {
        "name": "research_topic",
        "description": "Research a topic in depth. Takes 3-8 seconds. Use for questions requiring detailed information.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Topic to research, e.g. 'solar energy'"}
            },
            "required": ["topic"],
        },
    },
]

SYSTEM_PROMPT = (
    "You are a helpful assistant with access to real-time weather and research tools. "
    "When presenting tool results, be aware that: weather data is live and may vary between requests; "
    "research data may be cached/stale (if so, the tool result will note this - always caveat your "
    "answer accordingly); and some requests may fail due to timeouts or rate limits. "
    "Present information clearly and note any caveats from the tool results."
)

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


async def call_api(session: aiohttp.ClientSession, endpoint: str, params: dict) -> dict:
    """Single entry point for all API calls. Both endpoints share the same quirks (throttle-as-200,
    timeouts, empty bodies), so handling them in one place avoids duplication and ensures consistency."""
    url = f"{BASE_URL}/{endpoint}"
    headers = {"X-API-Key": API_KEY or ""}  # or "": main() guards None; satisfies type checker

    for attempt in range(MAX_RETRIES):
        try:
            # R3: research endpoint sometimes takes ~15s (documented as 3-8s), need generous timeout
            async with session.get(url, params=params, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=20)) as resp:
                # W2: ~20-25% of requests randomly 504; some locations (Antarctica, Mordor) always 504.
                # No retry on 504: the server already spent ~10s before giving up, so retrying
                # would mean ~30s of waiting for blocklisted locations that will never succeed.
                if resp.status == 504:
                    try:
                        body = await resp.json()
                        detail = body.get("error", "no detail")
                    except (aiohttp.ContentTypeError, ValueError) as e:
                        detail = f"non-JSON response: {e}"
                    return {"error": f"API returned 504 ({detail})"}

                # Standard FastAPI validation error
                if resp.status == 422:
                    try:
                        body = await resp.json()
                        items = body.get("detail", [])
                        detail = items[0].get("msg", "no detail") if items else "no detail"
                    except (aiohttp.ContentTypeError, ValueError, IndexError, AttributeError):
                        detail = "no detail"
                    return {"error": f"API returned 422 ({detail})"}

                if resp.status != 200:
                    return {"error": f"API returned HTTP {resp.status}"}

                try:
                    body = await resp.json()
                except (aiohttp.ContentTypeError, ValueError) as e:
                    return {"error": f"API returned non-JSON response ({e})"}

                if not isinstance(body, dict):
                    return {"error": f"API returned unexpected format ({body})"}

                # G1: the most dangerous quirk — rate limits come back as HTTP 200 (not 429!).
                # Without this check, throttle payloads get silently passed to the LLM as data.
                if body.get("status") == "throttled":
                    # W4: +2s buffer because retry_after undershoots (tested: still throttled 4/5 times
                    # after waiting the exact value, but always recovers with a small buffer)
                    wait = body.get("retry_after_seconds", 5) + 2
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(wait)
                        continue
                    return {"error": f"API rate limited (retry in ~{wait}s)"}

                # R1: API randomly returns empty {} with HTTP 200 (~10-20% of requests)
                if body == {} and attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(1)
                    continue

                return body

        except asyncio.TimeoutError:
            if attempt < MAX_RETRIES - 1:
                continue
            return {"error": "API request timed out (20s)"}
        except aiohttp.ClientError as e:
            return {"error": f"API connection failed ({e})"}

    return {"error": "API max retries exceeded"}


def normalize_weather(data: dict) -> str:
    """W1: API randomly returns two schemas — flat {temperature_c, condition, humidity} or
    multi-condition {conditions: [{...}, ...]}. Tested 20 cities x 5 rounds: schema is random
    per request, not per city. Both must be handled on every call."""
    if "error" in data:
        return f"Error: {data['error']}"
    location = data.get("location", "Unknown")
    conditions = data.get("conditions")
    if isinstance(conditions, list):
        parts = [f"Weather in {location} (multiple conditions reported):"]
        for c in conditions:
            if isinstance(c, dict):
                parts.append(
                    f"  - {c.get('condition', '?')}: {c.get('temperature_c', '?')}°C, "
                    f"humidity {c.get('humidity', '?')}%"
                )
            else:
                parts.append(f"  - (unexpected format: {c})")
        return "\n".join(parts)
    if "condition" in data:
        return (
            f"Weather in {location}: {data.get('condition', '?')}, "
            f"{data.get('temperature_c', '?')}°C, humidity {data.get('humidity', '?')}%"
        )
    # Unknown schema - pass raw data so the LLM can interpret it
    return f"Weather data for {location} (unexpected format): {data}"


def format_research(data: dict) -> str:
    """R2: ~10-15% of responses are randomly stale (~310 days old). We surface the staleness
    info to the LLM so it can caveat its answer rather than presenting old data as current."""
    if "error" in data:
        return f"Error: {data['error']}"
    if not data.get("summary"):
        if data == {} or data is None:
            return "Research returned no data for this topic."
        return f"Research returned unexpected format: {data}"
    result = data["summary"]
    if data.get("cached"):
        try:
            age_days = int(data.get("cache_age_seconds", 0)) // 86400
        except (TypeError, ValueError):
            age_days = "unknown"
        generated = data.get("generated_at", "unknown date")
        result += (
            f"\n\n[Note: This data is cached from {generated} "
            f"(~{age_days} days old) and may not reflect recent developments.]"
        )
    return result


async def execute_tool(session: aiohttp.ClientSession, name: str, input_data: dict) -> str:
    """Dispatch tool call to the appropriate API endpoint + normalizer."""
    try:
        if name == "get_weather":
            location = input_data.get("location", "")
            if not location:
                return "Error: No location provided."
            return normalize_weather(await call_api(session, "weather", {"location": location}))
        elif name == "research_topic":
            topic = input_data.get("topic", "")
            if not topic:
                return "Error: No topic provided."
            return format_research(await call_api(session, "research", {"topic": topic}))
        return f"Error: Unknown tool '{name}'."
    except Exception as e:
        return f"Error executing {name}: {e}"


async def spinner(message: str, done: asyncio.Event) -> None:
    """Animated spinner. Uses an asyncio.Event to distinguish normal completion from user
    cancellation — checkmark on success, silent clear on cancel (no misleading 'done' message)."""
    i = 0
    try:
        while True:
            print(f"\r{SPINNER_FRAMES[i % len(SPINNER_FRAMES)]} {message}", end="", flush=True)
            await asyncio.sleep(0.1)
            i += 1
    except asyncio.CancelledError:
        if done.is_set():  # tool call completed successfully before spinner was cancelled
            print(f"\r✓ {message} - done", flush=True)
        else:  # user cancelled — clear the spinner line silently
            print(f"\r{' ' * (len(message) + 4)}\r", end="", flush=True)


async def stream_response(
    client: anthropic.AsyncAnthropic,
    session: aiohttp.ClientSession,
    messages: list,
) -> None:
    """Core agentic loop: stream Claude's response, execute any requested tools, feed results
    back, and repeat until Claude says end_turn. The while loop is what makes this agentic —
    Claude can chain multiple tool calls across turns before giving a final answer."""
    while True:
        # Track partial text so we can save it to history if the user cancels mid-stream
        partial_text = ""
        try:
            async with client.messages.stream(
                model=MODEL, max_tokens=4096, system=SYSTEM_PROMPT,
                tools=TOOLS, messages=messages,
            ) as stream:
                async for text in stream.text_stream:
                    partial_text += text
                    print(text, end="", flush=True)
                response = await stream.get_final_message()
        except asyncio.CancelledError:
            # Preserve whatever Claude already said so conversation history stays coherent
            if partial_text.strip():
                messages.append({"role": "assistant", "content": partial_text})
            raise
        # Anthropic SDK errors: pop the user message so they can retry without a dangling message
        except anthropic.AuthenticationError:
            print("\nError: Invalid ANTHROPIC_API_KEY. Check your .env file.")
            messages.pop()
            return
        except anthropic.RateLimitError:
            print("\nError: Anthropic rate limit hit. Please wait a moment and try again.")
            messages.pop()
            return
        except anthropic.APIStatusError as e:
            print(f"\nError: Anthropic API error (HTTP {e.status_code}). Please try again.")
            messages.pop()
            return
        except anthropic.APIConnectionError:
            print("\nError: Could not connect to Anthropic API. Check your internet connection.")
            messages.pop()
            return

        # Append the full response (text + tool_use blocks) before checking for tools.
        # This keeps history valid regardless of what happens during tool execution.
        messages.append({"role": "assistant", "content": response.content})

        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        if not tool_blocks:  # plain text response, no tools requested
            print()
            return

        # Anthropic API requires every tool_use to have a matching tool_result — missing pairs
        # cause a 400 on the next request. So even on cancellation, we fill in all results.
        # Tools run sequentially (not parallel) — simpler cancellation, and the APIs rate-limit
        # aggressively anyway so parallel wouldn't help much.
        tool_results = []
        cancelled = False
        for tool in tool_blocks:
            if cancelled:
                tool_results.append({"type": "tool_result", "tool_use_id": tool.id,
                                     "content": "Cancelled by user."})
                continue

            label = (f"Getting weather for {tool.input.get('location', '?')}..."
                     if tool.name == "get_weather"
                     else f"Researching {tool.input.get('topic', '?')}...")
            done = asyncio.Event()
            spin_task = asyncio.create_task(spinner(label, done))
            try:
                result = await execute_tool(session, tool.name, tool.input)
                done.set()
            except asyncio.CancelledError:
                cancelled = True
                tool_results.append({"type": "tool_result", "tool_use_id": tool.id,
                                     "content": "Cancelled by user."})
                continue
            finally:
                spin_task.cancel()
                await spin_task

            tool_results.append({"type": "tool_result", "tool_use_id": tool.id, "content": result})

        messages.append({"role": "user", "content": tool_results})
        if cancelled:
            raise asyncio.CancelledError()
        # Loop continues: Claude will see the tool results and either respond or request more tools



async def async_input(prompt: str) -> str:
    """Run blocking input() in a thread pool. This keeps the event loop free to process
    our SIGINT handler — if input() ran on the main thread, signals would be blocked."""
    return await asyncio.get_running_loop().run_in_executor(None, lambda: input(prompt))


async def main():
    if not API_KEY:
        print("Error: ELYOS_API_KEY not set in .env")
        sys.exit(1)
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY not set in .env")
        sys.exit(1)

    # Persistent client and session for the whole app — no per-turn recreation,
    # connection pooling works across turns for both aiohttp and httpx
    client = anthropic.AsyncAnthropic()
    messages: list[dict] = []
    current_task: asyncio.Task | None = None
    sigint_count = 0

    loop = asyncio.get_running_loop()

    def on_sigint():
        # We use task.cancel() instead of letting KeyboardInterrupt propagate because
        # asyncio can't reliably deliver KeyboardInterrupt to a running coroutine.
        # CancelledError is the cooperative cancellation mechanism asyncio understands
        # natively — it propagates through the await chain and triggers context manager
        # cleanup (closing HTTP connections, etc). See docs/async-repl-and-ctrl-c.md
        nonlocal current_task, sigint_count
        if current_task and not current_task.done():
            current_task.cancel()
            sigint_count = 0
        else:
            sigint_count += 1
            if sigint_count >= 2:
                print("\nGoodbye!")
                # os._exit because the input() thread can't be interrupted cooperatively —
                # it blocks in C code. A library like prompt_toolkit would fix this.
                os._exit(0)
            else:
                print("\n(Press Ctrl+C again to exit)")
                print("\nYou: ", end="", flush=True)

    loop.add_signal_handler(signal.SIGINT, on_sigint)

    print("""
  Hi Elyos team! Thanks for reviewing this project.
  This is a streaming CLI chat app with tool calling.
  Claude has access to two tools: weather and research.

  Try: 'What's the weather in London?'
       'Research quantum computing'

  Type 'quit' to exit, Ctrl+C to cancel a response.
-------------------------------------------------------""")

    try:
        async with aiohttp.ClientSession() as session:
            while True:
                sigint_count = 0
                try:
                    user_input = await async_input("\nYou: ")
                except EOFError:  # ctrl+D on empty line to quit
                    print("\nGoodbye!")
                    return

                sigint_count = 0
                if user_input.strip().lower() in ("quit", "exit", "q"):
                    print("Goodbye!")
                    return
                if not user_input.strip():
                    continue

                messages.append({"role": "user", "content": user_input})
                print("\nAssistant: ", end="", flush=True)

                # Wrap in a task so on_sigint can call .cancel() on it
                current_task = asyncio.create_task(stream_response(client, session, messages))
                try:
                    await current_task
                except asyncio.CancelledError:
                    print("\n[Cancelled]")
    finally:
        await client.close()  # clean up httpx transport on quit/exit/Ctrl+D


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nGoodbye!")
