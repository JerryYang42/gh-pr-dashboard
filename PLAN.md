# Sync performance and GitHub API quota plan

## Goal

Make sync faster as the configured repository list grows while keeping GitHub API
usage visible and controlled. Preserve the current PR classification, event
detection, recently closed PR handling, and offline-first dashboard behavior.

## Plan

### 1. Measure GraphQL usage

- Include `rateLimit { cost remaining resetAt }` in GraphQL responses where
  supported.
- Surface per-request cost and remaining quota in sync output, with enough
  context to identify which repo/query was responsible.
- Capture a baseline for a representative sync: duration, number of requests,
  costs, and remaining quota. Do not assume GitHub Enterprise has the same
  limits as GitHub.com.

### 2. Reduce repeated and unnecessary detail fetching

- Separate discovery from hydration: search for relevant PRs with the fields
  needed to identify and deduplicate them, then fetch full PR details once per
  unique PR ID.
- Evaluate an incremental path that avoids full hydration when a PR's
  `updatedAt` has not changed. Retain the existing full-data path where needed
  to detect state transitions and preserve event history.
- Compare query costs and sync duration against the baseline. Keep the change
  only if it reduces measured API work without losing observable behavior.

### 3. Add bounded concurrency if measurements justify it

- Fetch independent repositories concurrently with a small configurable or
  fixed limit (initially around three in-flight repo fetches).
- Keep SQLite writes serialized and preserve per-repo transaction boundaries.
- Add backoff for rate-limit or secondary-rate-limit responses; do not retry
  indefinitely or increase concurrency in response to failures.
- Verify that concurrency improves wall-clock time without materially
  increasing rate-limit risk. Concurrency is a latency optimization, not a
  quota-reduction strategy.

### 4. Reassess broader search only if needed

- Treat organization-wide or multi-repository search as a separate
  investigation, not part of the initial implementation.
- Compare returned data volume, search result limits, filtering correctness,
  and GraphQL cost before adopting it.

## Validation and acceptance criteria

- Tests cover overlapping role results, unique-PR hydration, unchanged PRs,
  updated PRs, recently closed PRs, lost involvement, and API failures.
- Existing event detection and PR visibility remain behaviorally equivalent.
- Sync reports usable quota telemetry and identifies failed repositories
  explicitly.
- A representative multi-repository sync is compared with the recorded
  baseline for duration, request count, and GraphQL points consumed.
- Concurrency, if implemented, is bounded; SQLite updates remain safe; rate
  limit responses back off without unbounded retries.
