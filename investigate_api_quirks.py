"""
API Investigation Script
========================
Systematically probes the Elyos interview APIs to discover undocumented behaviors.
Findings are documented in docs/api-quirks-report.md with quirk IDs (G1, W1, R1, etc).

Usage: python investigate_api_quirks.py [--weather] [--research] [--general] [--deep] [--all]
"""

import asyncio
import aiohttp
import os
import time
import json
import sys
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://elyos-interview-907656039105.europe-west2.run.app"
API_KEY = os.getenv("ELYOS_API_KEY")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def req(
    session: aiohttp.ClientSession,
    method: str,
    path: str,
    params: dict | None = None,
    headers: dict | None = None,
    label: str = "",
):
    """Fire a request and return a structured result dict. Captures everything we might
    need for analysis: status, headers, body, timing, and parsed JSON if available."""
    url = f"{BASE_URL}{path}"
    hdrs = {"X-API-Key": API_KEY}
    if headers:
        hdrs.update(headers)

    t0 = time.perf_counter()
    try:
        async with session.request(method, url, params=params, headers=hdrs) as resp:
            body = await resp.text()
            elapsed = time.perf_counter() - t0
            result = {
                "label": label,
                "status": resp.status,
                "headers": dict(resp.headers),
                "body": body[:2000],  # truncate huge responses
                "elapsed": round(elapsed, 3),
            }
            # Try to parse JSON
            try:
                result["json"] = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                result["json"] = None
            return result
    except Exception as e:
        return {
            "label": label,
            "error": f"{type(e).__name__}: {e}",
            "elapsed": round(time.perf_counter() - t0, 3),
        }


def print_result(r: dict):
    """Pretty-print a single test result."""
    label = r.get("label", "?")
    if "error" in r:
        print(f"\n  [{label}] ERROR after {r['elapsed']}s — {r['error']}")
        return
    status = r["status"]
    elapsed = r["elapsed"]
    body_preview = r["body"][:300].replace("\n", " ")
    print(f"\n  [{label}] HTTP {status} in {elapsed}s")
    print(f"    Content-Type: {r['headers'].get('Content-Type', 'N/A')}")
    print(f"    Body: {body_preview}")


# ---------------------------------------------------------------------------
# Test suites
# ---------------------------------------------------------------------------

async def test_weather(session: aiohttp.ClientSession):
    """
    /weather endpoint tests
    -----------------------
    Why these tests:
    - Happy paths: confirm basic functionality and response shape
    - Missing/empty params: how does the API signal input errors?
    - Invalid locations: does it validate? return a default? error?
    - Special chars / unicode: encoding edge cases
    - Case sensitivity: does "london" == "London"?
    - Auth edge cases: missing/wrong key
    - Wrong HTTP method: does it only accept GET?
    - Rapid repeat requests: rate limiting? caching? response variance?
    """
    print("\n" + "=" * 60)
    print("WEATHER ENDPOINT TESTS")
    print("=" * 60)

    tests = [
        # --- Happy paths ---
        ("GET", "/weather", {"location": "London"}, None, "happy: London"),
        ("GET", "/weather", {"location": "Tokyo"}, None, "happy: Tokyo"),
        ("GET", "/weather", {"location": "New York"}, None, "happy: New York"),
        ("GET", "/weather", {"location": "Sydney"}, None, "happy: Sydney"),

        # --- Input edge cases ---
        ("GET", "/weather", None, None, "missing param"),
        ("GET", "/weather", {"location": ""}, None, "empty param"),
        ("GET", "/weather", {"location": "asdfghjkl"}, None, "gibberish location"),
        ("GET", "/weather", {"location": "12345"}, None, "numeric location"),
        ("GET", "/weather", {"location": "São Paulo"}, None, "special chars: São Paulo"),
        ("GET", "/weather", {"location": "a" * 500}, None, "very long location (500 chars)"),
        ("GET", "/weather", {"location": "London   "}, None, "trailing spaces"),

        # --- Case sensitivity ---
        ("GET", "/weather", {"location": "london"}, None, "lowercase: london"),
        ("GET", "/weather", {"location": "LONDON"}, None, "uppercase: LONDON"),

        # --- Auth ---
        # (these need special handling — see below)

        # --- HTTP method ---
        ("POST", "/weather", {"location": "London"}, None, "POST method"),
        ("PUT", "/weather", {"location": "London"}, None, "PUT method"),
    ]

    for method, path, params, headers, label in tests:
        r = await req(session, method, path, params, headers, label)
        print_result(r)

    # Auth tests need a separate session without the default API key
    async with aiohttp.ClientSession() as raw:
        r = await req(raw, "GET", "/weather", {"location": "London"},
                       {"X-API-Key": ""}, "empty API key")
        print_result(r)
        r = await req(raw, "GET", "/weather", {"location": "London"},
                       {"X-API-Key": "wrong-key-123"}, "wrong API key")
        print_result(r)

    # No X-API-Key header at all
    async with aiohttp.ClientSession() as raw:
        url = f"{BASE_URL}/weather"
        t0 = time.perf_counter()
        async with raw.get(url, params={"location": "London"}) as resp:
            body = await resp.text()
            print(f"\n  [no API key header] HTTP {resp.status} in {round(time.perf_counter()-t0,3)}s")
            print(f"    Body: {body[:300]}")

    # Rapid-fire same city: this is how we discovered W1 (schema variance), W3 (rate limiting),
    # and W5 (live data). If all 5 responses are identical, it's cached; if they differ, we
    # need to figure out what's changing (schema? temperature? throttle response?)
    print("\n  --- Rapid repeat test (London x5) ---")
    results = []
    for i in range(5):
        r = await req(session, "GET", "/weather", {"location": "London"}, None, f"rapid #{i+1}")
        results.append(r)
        print_result(r)

    bodies = [r.get("body") for r in results]
    if len(set(bodies)) == 1:
        print("    >> All 5 responses identical")
    else:
        print(f"    >> {len(set(bodies))} unique responses out of 5 — VARIANCE DETECTED")


async def test_research(session: aiohttp.ClientSession):
    """
    /research endpoint tests
    ------------------------
    Why these tests:
    - Happy paths: confirm response shape and actual latency
    - Repeated same topic: does it cache? do responses differ?
    - Missing/empty params: error handling
    - Concurrent requests: can we parallelize? any throttling?
    - Timing verification: is 3-8s consistent?
    """
    print("\n" + "=" * 60)
    print("RESEARCH ENDPOINT TESTS")
    print("=" * 60)

    tests = [
        # --- Happy paths (sequential, note timing) ---
        ("GET", "/research", {"topic": "solar energy"}, None, "happy: solar energy"),
        ("GET", "/research", {"topic": "quantum computing"}, None, "happy: quantum computing"),
        ("GET", "/research", {"topic": "climate change"}, None, "happy: climate change"),

        # --- Input edge cases ---
        ("GET", "/research", None, None, "missing param"),
        ("GET", "/research", {"topic": ""}, None, "empty param"),
        ("GET", "/research", {"topic": "asdfghjkl"}, None, "gibberish topic"),
        ("GET", "/research", {"topic": "a"}, None, "single char topic"),
        ("GET", "/research", {"topic": "12345"}, None, "numeric topic"),
        ("GET", "/research", {"topic": "a" * 500}, None, "very long topic (500 chars)"),

        # --- Case sensitivity ---
        ("GET", "/research", {"topic": "Solar Energy"}, None, "mixed case: Solar Energy"),

        # --- HTTP method ---
        ("POST", "/research", {"topic": "solar energy"}, None, "POST method"),
    ]

    for method, path, params, headers, label in tests:
        r = await req(session, method, path, params, headers, label)
        print_result(r)

    # Same topic repeated: discovered R2 (stale cache) this way — some responses come back
    # with cached:true and a generated_at from ~310 days ago, randomly
    print("\n  --- Repeat same topic x3 (solar energy) ---")
    for i in range(3):
        r = await req(session, "GET", "/research", {"topic": "solar energy"}, None, f"repeat #{i+1}")
        print_result(r)

    # Concurrent requests: if total time ≈ single request time, the server handles parallel well.
    # If total ≈ 3x single, it serializes. This informs whether parallel tool execution is worth it.
    print("\n  --- Concurrent requests (3 different topics) ---")
    t0 = time.perf_counter()
    tasks = [
        req(session, "GET", "/research", {"topic": "AI"}, None, "concurrent: AI"),
        req(session, "GET", "/research", {"topic": "robotics"}, None, "concurrent: robotics"),
        req(session, "GET", "/research", {"topic": "space"}, None, "concurrent: space"),
    ]
    results = await asyncio.gather(*tasks)
    total = round(time.perf_counter() - t0, 3)
    for r in results:
        print_result(r)
    print(f"    >> All 3 concurrent requests completed in {total}s total")


async def req_with_retry(session, method, path, params, headers, label, max_retries=3):
    """Retry-aware wrapper for deep-dive tests. Without this, hitting a throttle mid-batch
    would pollute results — we'd be measuring rate limit behavior instead of the quirk we're after."""
    for attempt in range(max_retries):
        r = await req(session, method, path, params, headers, label)
        if r.get("json") and r["json"].get("status") == "throttled":
            wait = r["json"].get("retry_after_seconds", 5) + 1
            print(f"    >> Throttled, waiting {wait}s before retry (attempt {attempt+1}/{max_retries})...")
            await asyncio.sleep(wait)
            continue
        return r
    return r  # return last throttled result if all retries fail


async def test_deep(session: aiohttp.ClientSession):
    """
    Deep-dive tests
    ----------------
    Why:
    - W2 showed Tokyo has a different schema. How many other variants exist?
    - Test famous cities, small towns, non-English names, ambiguous names
    - Test research response variance: do other topics also return {} or cached?
    - Test rate limit recovery: does retry_after actually work?
    """
    print("\n" + "=" * 60)
    print("DEEP-DIVE: WEATHER SCHEMA VARIANCE")
    print("=" * 60)
    print("  Testing diverse cities with rate-limit-aware retries...\n")

    # Diverse city selection to test multiple axes: does schema depend on region? city size?
    # character encoding? name ambiguity? This is how we confirmed W1 is truly random per
    # request (not per city/region) and discovered the W2 blocklist (Antarctica always 504s
    # but North Pole and Atlantis work fine — it's not "fictional" or "extreme" locations).
    cities = [
        # Major world cities
        "London", "Tokyo", "Paris", "Berlin", "Moscow",
        "Beijing", "Mumbai", "Cairo", "Lagos", "Dubai",
        # US cities
        "San Francisco", "Chicago", "Miami",
        # Small / obscure towns
        "Slough", "Wolverhampton", "Reykjavik", "Timbuktu",
        "Ushuaia", "Tromsø", "Yakutsk",
        # Non-English / tricky names
        "São Paulo", "München", "Zürich", "Kraków",
        # Ambiguous names
        "Springfield", "Portland", "Richmond",
        # Extreme locations
        "Antarctica", "North Pole",
    ]

    # Group responses by their JSON keys to identify distinct schemas.
    # This revealed exactly two: flat (4 keys) and multi-condition (3 keys with "conditions" array).
    schemas_seen = {}
    for city in cities:
        r = await req_with_retry(session, "GET", "/weather", {"location": city}, None, f"deep: {city}")
        print_result(r)

        j = r.get("json", {})
        if j:
            keys = sorted(j.keys())
            schema_key = str(keys)
            if schema_key not in schemas_seen:
                schemas_seen[schema_key] = []
            schemas_seen[schema_key].append(city)

    print("\n  --- Schema summary ---")
    for schema, cities_list in schemas_seen.items():
        print(f"    {schema}: {', '.join(cities_list)}")

    # --- Deep research variance ---
    print("\n" + "=" * 60)
    print("DEEP-DIVE: RESEARCH RESPONSE VARIANCE")
    print("=" * 60)
    print("  Testing diverse topics for empty bodies, caching, timing...\n")

    topics = [
        "solar energy", "quantum computing", "climate change",
        "machine learning", "blockchain", "nuclear fusion",
        "CRISPR gene editing", "dark matter", "ocean acidification",
        "artificial general intelligence",
    ]

    # Flag the two research quirks (R1: empty body, R2: stale cache) as they appear.
    # Running multiple topics confirms these are random per request, not topic-specific.
    for topic in topics:
        r = await req_with_retry(session, "GET", "/research", {"topic": topic}, None, f"deep: {topic}")
        print_result(r)
        j = r.get("json", {})
        flags = []
        if j == {}:
            flags.append("EMPTY BODY")
        if j.get("cached"):
            flags.append(f"CACHED (age: {j.get('cache_age_seconds', '?')}s)")
        if flags:
            print(f"    >> FLAGS: {', '.join(flags)}")

    # --- Rate limit recovery test ---
    print("\n" + "=" * 60)
    print("DEEP-DIVE: RATE LIMIT RECOVERY")
    print("=" * 60)

    # Deliberately trigger the rate limit, then test if retry_after_seconds is accurate.
    # This is how we discovered W4: waiting the exact value still leaves you throttled ~80%
    # of the time. A separate script (test_retry_after.py, not committed) confirmed this
    # across 5 cycles and found that a +2s buffer always works.
    print("  Hitting weather rate limit deliberately...")
    for i in range(10):
        r = await req(session, "GET", "/weather", {"location": "London"}, None, f"ratelimit #{i+1}")
        j = r.get("json", {})
        if j.get("status") == "throttled":
            retry_after = j.get("retry_after_seconds", 5)
            print(f"    >> Throttled at request #{i+1}, retry_after={retry_after}s")
            print(f"    >> Waiting exactly {retry_after}s then retrying...")
            await asyncio.sleep(retry_after)
            r2 = await req(session, "GET", "/weather", {"location": "London"}, None, "post-recovery")
            print_result(r2)
            if r2.get("json", {}).get("status") == "throttled":
                print("    >> STILL THROTTLED after waiting retry_after!")
            else:
                print("    >> Recovery successful after retry_after wait")
            break
        print_result(r)


async def test_general(session: aiohttp.ClientSession):
    """
    General / cross-cutting tests
    -----------------------------
    Why these tests:
    - Root/unknown endpoints: does the server leak info?
    - Content negotiation: does Accept header change response format?
    - CORS: relevant if browser-based usage is expected
    - Response headers: look for caching, rate-limit, custom headers
    """
    print("\n" + "=" * 60)
    print("GENERAL / CROSS-CUTTING TESTS")
    print("=" * 60)

    # Probe the server surface: what endpoints exist, what methods are allowed,
    # does content negotiation work? This found G2 (GET-only) and G3 (ignores Accept header).
    tests = [
        ("GET", "/", None, None, "root endpoint"),
        ("GET", "/health", None, None, "health check"),
        ("GET", "/healthz", None, None, "healthz"),  # common in GCP deployments
        ("GET", "/unknown", None, None, "unknown endpoint"),
        ("OPTIONS", "/weather", {"location": "London"}, None, "OPTIONS /weather"),
        ("HEAD", "/weather", {"location": "London"}, None, "HEAD /weather"),
        ("GET", "/weather", {"location": "London"}, {"Accept": "text/plain"}, "Accept: text/plain"),
        ("GET", "/weather", {"location": "London"}, {"Accept": "application/xml"}, "Accept: xml"),
    ]

    for method, path, params, headers, label in tests:
        r = await req(session, method, path, params, headers, label)
        print_result(r)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    if not API_KEY:
        print("ERROR: ELYOS_API_KEY not found. Check your .env file.")
        sys.exit(1)

    suites = sys.argv[1:] if len(sys.argv) > 1 else ["--all"]

    async with aiohttp.ClientSession() as session:
        if "--weather" in suites or "--all" in suites:
            await test_weather(session)
        if "--research" in suites or "--all" in suites:
            await test_research(session)
        if "--deep" in suites or "--all" in suites:
            await test_deep(session)
        if "--general" in suites or "--all" in suites:
            await test_general(session)

    print("\n" + "=" * 60)
    print("INVESTIGATION COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
