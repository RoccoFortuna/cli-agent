# API Quirks Report

## Base URL
`https://elyos-interview-907656039105.europe-west2.run.app`

## Endpoints
- `GET /weather?location=<string>` - typically fast (~150-400ms), but randomly 504s after ~10s
- `GET /research?topic=<string>` - slow (4-8s typical, up to 15s)
- Both require `X-API-Key` header

---

## Cross-Cutting Quirks (G)

### G1 - Rate limit disguised as HTTP 200

Both endpoints return throttle responses as **HTTP 200** (not 429). Body: `{"status":"throttled","message":"Rate limit exceeded. Please wait.","retry_after_seconds":N,"data":null}`.

This is the single most impactful quirk. Naive code checking only HTTP status codes will silently pass throttle payloads to the LLM as if they were real data.

- **Mitigation**: ALWAYS inspect response body for `"status":"throttled"` as the first check before using any data. Cannot rely on HTTP status code alone.

### G2 - GET-only endpoints

POST, PUT, OPTIONS, HEAD all return 405 Method Not Allowed. Not documented.

- **Mitigation**: Use GET only. Not a problem for our use case.

### G3 - Content-Type always JSON regardless of Accept header

Sending `Accept: text/plain` or `Accept: application/xml` still returns `application/json`.

- **Mitigation**: Always parse as JSON. Not a problem.

---

## Weather Endpoint Quirks (W)

### W1 - Inconsistent response schema (random per request)

Two distinct response shapes exist, assigned **randomly per request** regardless of location:

- **Flat** (~70-80% of responses): `{"location", "temperature_c", "condition", "humidity"}`
- **Multi-condition** (~20-30%): `{"location", "conditions": [{"temperature_c", "condition", "humidity"}, ...], "note": "Multiple conditions reported"}`

Tested across 20 cities over 5 rounds: every city can return either schema. London was flat 3/5 times and multi 2/5. Chicago was flat 2/4 and multi 2/4. No city is locked to one schema.

- **Mitigation**: Check for `conditions` key on every response. If array, iterate all entries. If flat, use directly. Must handle both schemas for every city on every request.

### W2 - Random 504 timeouts (~20-25% of requests) + deterministic blocklist

Valid locations randomly return `504` with `{"error":"Weather API timeout"}` after ~10s. There are two distinct failure modes:

**1. Random failures (~20-25% per request):** Most cities fail intermittently. Tested 20 cities x 5 rounds: Chicago 20%, Paris 40%, New York 40%, Sydney 40%, Ushuaia 60%. Cities like London, Tokyo, Moscow showed 0% in this batch but would likely fail in larger samples. The probability appears to be ~20-25% per request, independent of location.

**2. Deterministic blocklist (100% failure):** Some locations always 504. Confirmed with 15 consecutive requests each:
- "Antarctica": 15/15 timeouts (100%)
- "Mordor": 15/15 timeouts (100%)
- "North Pole": 0/15 timeouts (works fine)
- "Atlantis": 0/15 timeouts (works fine)
- "Null Island": 0/15 timeouts (works fine)

The blocklist is not simply "fictional locations" (Atlantis works) or "extreme locations" (North Pole works). It appears to be a hardcoded list of specific strings.

- **Mitigation**: Retry automatically on 504. Configurable retry count (default 3). Retrying helps for the ~20-25% random failures but will never succeed for blocklisted locations. Distinguish 504 (upstream timeout, worth retrying) from 404 (location not found) and 422 (missing param). Tell the user weather is temporarily unavailable if all retries fail.

### W3 - Undocumented rate limiting

Rate limit kicks in after ~4-6 requests in quick succession. `retry_after_seconds` starts at ~29s and counts down. Not documented.

- **Mitigation**: Implement retry loop respecting `retry_after_seconds` with buffer. See W4.

### W4 - `retry_after_seconds` is unreliable

Tested across 5 rate limit cycles: waiting exactly `retry_after_seconds` resulted in still being throttled **4/5 times (80%)**. The initial `retry_after_seconds` (19-29s) consistently undershoots by ~1-2s. After re-throttle, the API returns `retry_after_seconds: 1`, and waiting 1s + 1s buffer recovered successfully 4/4 times (100%).

- **Mitigation**: Add a buffer (+2s) when waiting. Implement a retry loop (not single retry) that re-checks for throttle on each attempt.

### W5 - Live data (temperature changes between requests)

Same city returns different temperature values across requests (e.g. London: 7.1C, 8.1C, 9.3C). Data is live, not static. This is expected behavior.

- **Mitigation**: Don't cache weather responses. Always show fresh data.

---

## Research Endpoint Quirks (R)

### R1 - Random empty JSON body `{}`

Some requests return HTTP 200 with body `{}` (zero fields). This is **non-deterministic and not topic-specific**. In testing:
- "climate change" returned `{}` 3/3 times in one session, but returned cached data in an earlier session
- "solar energy" and "dark matter" also returned `{}` occasionally (1/3 times)
- Any topic can return empty on any request

Estimated probability: ~10-20% per request.

- **Mitigation**: Retry automatically on empty body. Configurable retry count (default 3). If still empty after retries, inform user that research returned no data.

### R2 - Stale cached responses (random, ~310 days old)

Some responses return `{"cached":true, "cache_age_seconds":26784000, "generated_at":"2024-03-15T09:00:00Z"}` with summary text noting data is from early 2024. This is **random, not tied to specific topics**. In testing:
- "blockchain" returned cached 1/3 times, fresh 2/3 times
- Topics that were "always cached" in earlier runs returned fresh in later runs
- The cached payload is always identical when it appears (same date, same age)

Estimated probability: ~10-15% per request.

- **Mitigation**: Check for `cached` field. If present, surface staleness warning to user with the `cache_age_seconds` and `generated_at` values so the LLM can caveat its response.

### R3 - Response time far exceeds documented 3-8s

One request ("quantum computing") took 15.06s, correlated with an empty `{}` response. Normal requests range 4-8s. The 15s outlier may be a failure mode rather than normal latency.

- **Mitigation**: Set HTTP timeout to >=20s. Pending state indicator and cancellation support are critical so the user doesn't wait blindly.

### R4 - Rate limit windows differ from weather endpoint

Weather `retry_after_seconds`: ~29s. Research `retry_after_seconds`: 1-4s. Very different rate limit behavior per endpoint.

- **Mitigation**: Handle rate limit retries per-call (no need for global tracking). The retry buffer approach in call_api handles both endpoints.

### R5 - No input validation

Empty string, gibberish ("asdfghjkl"), single character ("a"), numbers ("12345") all return HTTP 200 with a generic templated summary.

- **Mitigation**: Mostly an LLM responsibility (send meaningful topics). Not worth adding client-side validation.

---

## Testing Log

### Run 1 - Weather (batch)
- London: 200, 0.285s - flat schema
- Tokyo: 200, 0.159s - multi-condition schema
- New York: 200, 0.157s - flat schema
- Sydney: 200, 0.157s - flat schema
- Missing param: 422
- Empty param: 404 `Location "" not found`
- After 4 successful requests, all subsequent returned throttle with ~28-29s retry
- POST/PUT: 405
- Auth (empty/wrong/missing key): 401

### Run 2 - Research (batch)
- "solar energy": 200, 7.77s - normal response with sources
- "quantum computing": 200, 15.06s - returned empty `{}`
- "climate change": 200, 7.65s - cached response from March 2024 (310 days stale)
- Empty topic "": 200, 6.5s - returns generic summary (no validation)
- Hit rate limit after ~8 requests, retry_after was only 1-2s

### Run 3 - General
- `/`: 200, 3.6s - HTML poem page
- `/health`: 200 - `{"status":"healthy"}`
- `/healthz`: 404 (Google Cloud default)
- `/unknown`: 404 `{"detail":"Not Found"}`
- Accept header ignored - always JSON
- London temp changed from 7.1C to 8.1C between runs (live data)

### Run 4 - Weather deep-dive (20 cities x 5 rounds, with throttle-retry)

504 timeout rates (non-throttled requests only):
| City | OK | 504 | Rate |
|------|-----|-----|------|
| London | 5 | 0 | 0% |
| Chicago | 4 | 1 | 20% |
| Tokyo | 5 | 0 | 0% |
| Zurich | 5 | 0 | 0% |
| Paris | 3 | 2 | 40% |
| Springfield | 5 | 0 | 0% |
| Berlin | 5 | 0 | 0% |
| Timbuktu | 4 | 1 | 20% |
| Mumbai | 4 | 1 | 20% |
| Antarctica | 0 | 5 | 100% |
| New York | 3 | 2 | 40% |
| Sydney | 3 | 2 | 40% |
| Ushuaia | 2 | 3 | 60% |
| North Pole | 5 | 0 | 0% |
| Cairo | 5 | 0 | 0% |
| Lagos | 4 | 1 | 20% |
| Dubai | 4 | 1 | 20% |
| Moscow | 5 | 0 | 0% |
| San Francisco | 4 | 1 | 20% |
| Miami | 5 | 0 | 0% |

Schema variance (of successful responses):
| City | Flat | Multi | Total |
|------|------|-------|-------|
| London | 3 | 2 | 5 |
| Chicago | 2 | 2 | 4 |
| Tokyo | 4 | 1 | 5 |
| Cairo | 3 | 2 | 5 |
| Dubai | 2 | 2 | 4 |
| Berlin | 4 | 1 | 5 |
| Moscow | 4 | 1 | 5 |
| Miami | 4 | 1 | 5 |
| Mumbai | 3 | 1 | 4 |
| Paris | 2 | 1 | 3 |

### Run 5 - Research deep-dive (8 topics x 3 rounds)
- Empty `{}` responses: climate change (3/3), solar energy (1/3), dark matter (1/3)
- Cached response: blockchain (1/3) - all others fresh
- Previous "always cached" topics now return fresh data
- Confirms: both empty and cached are random, not topic-specific
