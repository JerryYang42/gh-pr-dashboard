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

- [x] Separate discovery from hydration: search for relevant PRs using only
  their ID, number and `updatedAt`, merge overlapping results, then fetch full
  PR details in batches for unique IDs.
- [x] Reuse cached details when `updatedAt` and matched roles are unchanged.
  Hydrate when either changes; still fully refetch open PRs that stop matching
  searches so merges, closures and lost involvement are detected.
- [x] Compare query costs and sync duration against the Stage 1 baseline. Keep
  the optimization only if measured API work falls without behavioral
  regressions.

Live comparison: API cost fell from 392 to 18 points (about 95%); sync duration
fell from 35.20 seconds to 29.33-31.33 seconds (about 14% on average).

### 3. Add bounded concurrency if measurements justify it

- [x] Fetch independent repositories concurrently with a fixed maximum of
  three in-flight repo fetches.
- [x] Keep SQLite reads and writes on the main sync thread and preserve
  per-repo transaction boundaries.
- [x] Retry rate-limit errors at most twice, with 60- and 120-second delays;
  other errors are not retried.
- [ ] Verify that concurrency improves wall-clock time without materially
  increasing rate-limit risk. Concurrency is a latency optimization, not a
  quota-reduction strategy.

Implementation is complete; live concurrency and rate-limit behavior checks
remain pending.

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
