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
    """Call the Elyos API. Handles: throttle-as-200 (G1), 504 (W2), retry+buffer (W3/W4),
    empty body (R1), slow response timeout (R3)."""
    url = f"{BASE_URL}/{endpoint}"
    headers = {"X-API-Key": API_KEY or ""}  # or "": main() guards None; satisfies type checker

    for attempt in range(MAX_RETRIES):
        try:
            # R3: research endpoint sometimes takes ~15s (documented as 3-8s), need generous timeout
            async with session.get(url, params=params, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=20)) as resp:
                # W2: valid locations randomly 504 (~20-25%), some always 504 (e.g. Antarctica, Mordor)
                if resp.status == 504:
                    # No retry: API already spent ~10s before returning 504
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

                # G1: rate limits come back as HTTP 200 (not 429!), only detectable in the body
                if body.get("status") == "throttled":
                    wait = body.get("retry_after_seconds", 5) + 2  # W4: retry_after lies, still throttled after waiting exact value
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
    """W1: API returns two different JSON schemas for the same city, randomly per request."""
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
    """R2: ~10-15% of responses are randomly stale (~310 days old), must flag for the LLM."""
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
    """Animated spinner. Shows checkmark on completion, clears on cancellation."""
    i = 0
    try:
        while True:
            print(f"\r{SPINNER_FRAMES[i % len(SPINNER_FRAMES)]} {message}", end="", flush=True)
            await asyncio.sleep(0.1)
            i += 1
    except asyncio.CancelledError:
        if done.is_set():
            print(f"\r✓ {message} - done", flush=True)
        else:
            print(f"\r{' ' * (len(message) + 4)}\r", end="", flush=True)


async def stream_response(
    client: anthropic.AsyncAnthropic,
    session: aiohttp.ClientSession,
    messages: list,
) -> None:
    """Stream Claude's response, executing tools in a loop until end_turn."""
    while True:
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
            if partial_text.strip():
                messages.append({"role": "assistant", "content": partial_text})
            raise
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

        # Always append - includes both text and tool_use blocks
        messages.append({"role": "assistant", "content": response.content})

        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        if not tool_blocks:  # plain text response, no tools requested
            print()
            return

        # Every tool_use must have a matching tool_result or the API rejects the next request.
        # If the user cancels mid-batch, remaining tools get "Cancelled by user." results.
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



async def async_input(prompt: str) -> str:
    """Run input() in a thread so the event loop can still receive SIGINT."""
    return await asyncio.get_running_loop().run_in_executor(None, lambda: input(prompt))


async def main():
    if not API_KEY:
        print("Error: ELYOS_API_KEY not set in .env")
        sys.exit(1)
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY not set in .env")
        sys.exit(1)

    client = anthropic.AsyncAnthropic()
    messages: list[dict] = []
    current_task: asyncio.Task | None = None
    sigint_count = 0

    loop = asyncio.get_running_loop()

    def on_sigint():
        # task.cancel() instead of KeyboardInterrupt: asyncio can't deliver
        # KeyboardInterrupt to a running coroutine (see docs/async-repl-and-ctrl-c.md)
        nonlocal current_task, sigint_count
        if current_task and not current_task.done():
            current_task.cancel()
            sigint_count = 0
        else:
            sigint_count += 1
            if sigint_count >= 2:
                print("\nGoodbye!")
                os._exit(0)  # input() thread blocks clean shutdown
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
