# Diagnostic: an unoptimized webhook or API payload

Read on 429s, timeouts, partial or batchy updates, duplicate events, rejected payloads, or values that silently
never arrive.

1. **Size the traffic**: events per minute at peak against the destination's rate limit; show the arithmetic.
2. **Batch to the vendor's documented limits** (for example HubSpot batch upsert 100 inputs; Klaviyo bulk profile
   import 10,000 profiles and 5 MB) instead of one call per record. `lcm sync` builds request bodies inside them.
3. **Send deltas, not full profiles**: only changed fields, keyed by a stable identifier.
4. **Match destination types exactly**; a type mismatch drops the value without an error.
5. **Add an idempotency key** so retries cannot double-write.
6. **Retry with exponential backoff and jitter**, honor `Retry-After`, cap attempts, and route the final failure to
   a dead-letter store someone reads.
7. **Strip fields the destination does not need**, especially personal data.

Deliver the corrected payload, the batching math and the retry policy as code or configuration.
