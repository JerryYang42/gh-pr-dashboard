# gh-pr-dashboard

A local-first terminal dashboard and progress tracker for the pull requests you're involved in on **GitHub Enterprise**: ones you own, ones where your review is requested, ones where a re-review is requested, and ones you're otherwise following.

It works from an explicit list of local clones that you maintain by hand. Nothing is scanned or guessed.

- **Local-first:** all data is cached in a local SQLite file. The TUI reads only from that cache, so it opens instantly and works offline. Syncing is explicit (or on a timer).
- **Progress tracking:** every sync is diffed against the previous one, so you see *what changed* (new commits, CI flipped, a review landed, merged), not just the current state.
- **No token management:** it uses the [GitHub CLI](https://cli.github.com) (`gh`) and whatever login you already have.
- **One file:** the whole tool is `prdash.py`.

## Requirements

- Python 3.11 or newer
- [`gh`](https://cli.github.com) authenticated against your GHE host
- [`textual`](https://textual.textualize.io) 0.80 or newer (only for the TUI; sync and list use the standard library only)
- `git`

## Setup

```sh
# 1. Authenticate gh against your GitHub Enterprise host (once)
gh auth login --hostname ghe.yourcompany.com

# 2. Run with uv (installs textual automatically via inline script metadata)...
uv run prdash.py init

# ...or install textual yourself and run with plain Python
pip install textual
python prdash.py init

# 3. Edit the generated config and list your repos
$EDITOR ~/.config/prdash/repos.toml

# 4. Verify that repos resolve and auth works
uv run prdash.py check

# 5. Launch
uv run prdash.py
```

A shell alias makes this convenient:

```sh
alias prdash='uv run ~/path/to/gh-pr-dashboard/prdash.py'
```

## Configuration

`~/.config/prdash/repos.toml` (override with `-c PATH` or `$PRDASH_CONFIG`):

```toml
[settings]
closed_days = 7          # keep showing merged/closed PRs for this many days
auto_sync_minutes = 15   # sync on start if the cache is older, and every N minutes while open (0 = manual only)
stale_days = 14          # flag open PRs with no activity for this long
# db = "~/.local/share/prdash/prdash.db"

[[repo]]
path = "~/src/payments-service"

[[repo]]
path = "~/src/web-app"
# remote = "upstream"        # which git remote points at the GHE repo (default: origin)
# host = "ghe.example.com"   # override the host derived from the remote
# slug = "my-org/web-app"    # override owner/name derived from the remote
```

The GHE host and `owner/name` are read from each repo's git remote (SSH and HTTPS URL forms are both supported). Use `host` and `slug` only if the remote can't be parsed or points somewhere unexpected.

Repos can be on different GHE hosts; each host just needs a `gh auth login`.

Removing a repo from the list hides its PRs from the dashboard. The cached data stays in the database until it ages out.

## Usage

| Command | What it does |
| --- | --- |
| `prdash` | Open the TUI (default) |
| `prdash init` | Write an example config (never overwrites an existing one) |
| `prdash check` | Show how each repo resolves, and verify `gh` auth per host |
| `prdash sync` | Refresh the cache without opening the UI (suitable for cron) |
| `prdash list` | Print the cached dashboard as plain text |

Global option: `-c, --config PATH`.

### TUI keys

| Key | Action |
| --- | --- |
| `r` | Sync now |
| `0` `1` `2` `3` `4` | Filter: all / review / mine / following / closed |
| `a` | Toggle "needs action only" (unread, failing, blocked, or waiting on you) |
| `/` | Search repo, title, author or PR number (`Esc` clears) |
| `o` | Open the PR in your browser (also marks it seen) |
| `m` / `M` | Mark the PR seen / mark everything seen |
| `e` | Activity log across all PRs |
| `c` | Run `gh pr checkout` for the PR inside its local clone |
| `q` | Quit (`Ctrl+Q` is disabled) |

The lower pane shows details for the highlighted PR: status, your role, CI, threads, size, requested reviewers, review states, local-clone info and its recent events.

## How PRs are classified

Each PR lands in exactly one bucket, checked in this order:

| Bucket | Rule |
| --- | --- |
| **CLOSED** | Merged or closed within `closed_days` |
| **MINE** | You are the author |
| **RE-REVIEW** | A review is requested of you *and* you've reviewed it before |
| **REVIEW** | A review is requested of you, or of a team you belong to |
| **FOLLOW** | Anything else you're involved in: assigned, previously reviewed, mentioned or commented |

The status column then summarises what needs attention. For your own PRs: merge conflicts, CI failing, changes requested, unresolved threads, ready to merge, or who you're waiting on. For PRs you review: re-review requested, or new commits since your last review.

## What gets tracked

Each sync compares the new state of every PR against the stored one and records events such as:

- review requested, re-review requested, team review requested
- new commits pushed (flagged when they landed after your review)
- CI passed or failed
- a reviewer approved, requested changes or commented
- new unresolved threads and new comments
- marked ready for review, converted to draft
- merge conflicts appeared
- merged or closed

PRs with events you haven't looked at are marked with a `●`. The very first sync of a repo records no events and marks nothing unread, so you don't start with a wall of noise. Events older than 90 days, and closed PRs older than 90 days, are pruned.

## Data and files

| What | Where |
| --- | --- |
| Config | `~/.config/prdash/repos.toml` (`$XDG_CONFIG_HOME`, `$PRDASH_CONFIG`, or `-c`) |
| Cache and history | `~/.local/share/prdash/prdash.db` (`$XDG_DATA_HOME`, or `db =` in settings) |

The database is plain SQLite. Delete it any time to start fresh; it is rebuilt on the next sync.

Other environment variables: `PRDASH_GH` points to a different `gh` binary.

## How syncing works

For each repo, one GraphQL request (through `gh api graphql --hostname <host>`) runs a set of searches scoped to that repo only: PRs you authored, are assigned to, have a review requested on, have reviewed, or are otherwise involved in, open plus recently closed. Results are merged by PR, stored, and diffed. Open PRs that stopped matching any search are re-fetched by id so that merges and closures are still recorded; if one is still open but no longer involves you, it is hidden.

`prdash sync` reports GraphQL request count, cost, remaining quota, reset time
and duration per request, grouped by host. The values come from GitHub's
`rateLimit` GraphQL field; when a host does not return rate-limit data, the
report shows `n/a`. Capture a baseline by running `prdash sync` with a
representative repo list and noting the per-host totals, request details and
sync duration. Quota limits can differ between GitHub.com and GitHub Enterprise.

The tool is read-only toward GitHub and your repos. It never pushes, comments, fetches or modifies a clone. The only write action is `c`, which runs `gh pr checkout` when you press it.

## Troubleshooting

- **`check` reports a FAILED host:** run `gh auth login --hostname <host>`. If your org enforces SAML SSO, authorise the token for the org.
- **"cannot read host/owner/name from remote":** set `host` and `slug` on that repo entry, or `remote` if the GHE remote isn't called `origin`.
- **"not a git repository":** the `path` is wrong or not a clone. Repos listed with both `host` and `slug` skip this check.
- **A PR you expect is missing:** its repo may not be in your list, or it was closed more than `closed_days` ago.
- **Warning about more than 50 results:** each search returns at most 50 PRs per role per repo.
- **Sync is slow or hits GraphQL limits:** run `prdash sync` to inspect per-host request costs and remaining quota. Raise `auto_sync_minutes` to reduce request frequency; splitting repos across runs can spread load but does not reduce total API points.

## Limitations

- GitHub Enterprise via `gh` only; no GitLab or Bitbucket.
- Relies on GitHub search, which is eventually consistent. A brand new change can take a short while to show up.
- Team review requests are detected through GitHub's own `review-requested` search; the tool doesn't resolve team membership itself.
- Tested end to end against a fake `gh` and a stubbed UI layer. If you hit a Textual-version or GHE-specific problem, please report it.
