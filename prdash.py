#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["textual>=0.80"]
# ///
"""prdash - local-first terminal dashboard and progress tracker for your PRs.

* Works only on an explicit list of local clones (repos.toml). Nothing is scanned.
* Talks to GitHub Enterprise through the `gh` CLI, so it reuses your `gh auth` login.
* Everything is cached in a local SQLite file. The TUI reads only from that cache,
  so it opens instantly and works offline; `r` (or `prdash sync`) refreshes it.
* Every sync diffs the new PR state against the previous one and records events
  (review requested, new commits, CI flipped, new review, merged, ...). PRs with
  unseen events are marked with a dot until you open or mark them seen.

Commands:
    prdash            open the TUI (default)
    prdash init       write an example config
    prdash check      show resolved repos/hosts and verify gh auth
    prdash sync       refresh the cache (no UI)
    prdash list       print the cached dashboard as plain text

Setup: `gh auth login --hostname <your-ghe-host>` once, then `prdash init`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import tomllib
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, TextIO

GH = os.environ.get("PRDASH_GH", "gh")
SYNC_WORKERS = 256
RATE_LIMIT_RETRY_DELAYS = (60, 120)

EXAMPLE_CONFIG = """\
# prdash config: an explicit list of local clones. Nothing is scanned or guessed.
# The GitHub Enterprise host and owner/name are read from each repo's git remote.

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
"""


# --------------------------------------------------------------------------- config

class ConfigError(Exception):
    pass


@dataclass
class RepoSpec:
    path: Path
    host: str
    slug: str  # owner/name

    @property
    def key(self) -> str:
        return f"{self.host}/{self.slug}"


@dataclass
class Config:
    repos: list[RepoSpec]
    errors: list[str]
    db: Path
    closed_days: int = 7
    auto_sync_minutes: int = 15
    stale_days: int = 14


def config_path(arg: str | None = None) -> Path:
    if arg:
        return Path(arg).expanduser()
    if os.environ.get("PRDASH_CONFIG"):
        return Path(os.environ["PRDASH_CONFIG"]).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser()
    return base / "prdash" / "repos.toml"


def default_db() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME") or "~/.local/share").expanduser()
    return base / "prdash" / "prdash.db"


_REMOTE_RE = re.compile(
    r"^(?:[a-z+]+://)?(?:[^@/]+@)?(?P<host>[^:/]+)(?::\d+)?[:/](?P<slug>[^/]+/[^/]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)


def parse_remote(url: str) -> tuple[str, str] | None:
    m = _REMOTE_RE.match(url.strip())
    return (m.group("host"), m.group("slug")) if m else None


def git(path: Path | str, *args: str, timeout: int = 10) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 1, ""
    return r.returncode, r.stdout.strip()


def load_config(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(f"No config at {path}. Run `prdash init` to create one.")
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e

    s = data.get("settings", {})
    cfg = Config(
        repos=[],
        errors=[],
        db=Path(s["db"]).expanduser() if s.get("db") else default_db(),
        closed_days=int(s.get("closed_days", 7)),
        auto_sync_minutes=int(s.get("auto_sync_minutes", 15)),
        stale_days=int(s.get("stale_days", 14)),
    )
    entries = [e if isinstance(e, dict) else {"path": e} for e in [*data.get("repo", []), *data.get("repos", [])]]
    seen: set[str] = set()
    for e in entries:
        raw_path = e.get("path")
        if not raw_path:
            cfg.errors.append("a repo entry has no `path`")
            continue
        p = Path(raw_path).expanduser()
        host, slug = e.get("host"), e.get("slug")
        if not (host and slug):
            if git(p, "rev-parse", "--git-dir")[0] != 0:
                cfg.errors.append(f"{raw_path}: not a git repository")
                continue
            rc, url = git(p, "remote", "get-url", e.get("remote", "origin"))
            parsed = parse_remote(url) if rc == 0 else None
            if not parsed:
                cfg.errors.append(f"{raw_path}: cannot read host/owner/name from remote "
                                  f"'{e.get('remote', 'origin')}' (set host= and slug= explicitly)")
                continue
            host, slug = host or parsed[0], slug or parsed[1]
        spec = RepoSpec(p, host, slug)
        if spec.key in seen:
            cfg.errors.append(f"{raw_path}: duplicate of {spec.key}, ignored")
            continue
        seen.add(spec.key)
        cfg.repos.append(spec)
    return cfg


# --------------------------------------------------------------------------- storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS pr (
    id TEXT PRIMARY KEY,          -- GraphQL node id
    repo_key TEXT NOT NULL,       -- host/owner/name
    host TEXT NOT NULL,
    number INTEGER NOT NULL,
    raw TEXT NOT NULL,            -- last fetched PR payload (+ _roles)
    state TEXT NOT NULL,
    hidden INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL,
    last_event REAL NOT NULL DEFAULT 0,
    seen REAL NOT NULL DEFAULT 0, -- unread = last_event > seen
    synced REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS pr_repo ON pr(repo_key);
CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_id TEXT NOT NULL,
    ts REAL NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS event_pr ON event(pr_id, ts);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def meta_get(con: sqlite3.Connection, k: str) -> str | None:
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else None


def meta_set(con: sqlite3.Connection, k: str, v: str) -> None:
    con.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


# --------------------------------------------------------------------------- GitHub (via gh)

class GhError(Exception):
    pass


@dataclass
class GraphQLUsage:
    host: str
    context: str
    cost: int | None = None
    remaining: int | None = None
    reset_at: str | None = None
    duration_seconds: float = 0


def gh_graphql(host: str, query: str, variables: dict | None = None, *,
               context: str = "GraphQL request",
               usage: list[GraphQLUsage] | None = None) -> tuple[dict, list[str]]:
    request_usage = GraphQLUsage(host, context)
    if usage is not None:
        usage.append(request_usage)
    body = json.dumps({"query": query, "variables": variables or {}})
    env = {**os.environ, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"}
    started = time.monotonic()
    try:
        for attempt in range(len(RATE_LIMIT_RETRY_DELAYS) + 1):
            try:
                p = subprocess.run(
                    [GH, "api", "graphql", "--hostname", host, "--method", "POST", "--input", "-"],
                    input=body, capture_output=True, text=True, timeout=180, env=env,
                )
            except FileNotFoundError:
                raise GhError("`gh` CLI not found on PATH (https://cli.github.com)") from None
            except subprocess.TimeoutExpired:
                raise GhError(f"timed out talking to {host}") from None
            out = None
            if p.stdout.strip():
                try:
                    out = json.loads(p.stdout)
                except json.JSONDecodeError:
                    pass
            errors = [e.get("message", "") for e in (out or {}).get("errors") or []]
            msg = "; ".join(errors) or (p.stderr or p.stdout).strip() or f"gh exited with {p.returncode}"
            rate_limited = _is_rate_limit_error(msg)
            if rate_limited and attempt < len(RATE_LIMIT_RETRY_DELAYS):
                time.sleep(RATE_LIMIT_RETRY_DELAYS[attempt])
                continue
            if not out or out.get("data") is None or rate_limited:
                raise GhError(msg[:400])
            break
    finally:
        request_usage.duration_seconds = time.monotonic() - started
    limit = out["data"].get("rateLimit") or {}
    request_usage.cost = limit.get("cost")
    request_usage.remaining = limit.get("remaining")
    request_usage.reset_at = limit.get("resetAt")
    return out["data"], errors


def _is_rate_limit_error(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in (
        "rate limit", "rate-limit", "abuse detection", "abuse limit",
    ))


def get_me(host: str, usage: list[GraphQLUsage] | None = None) -> str:
    try:
        data, _ = gh_graphql(
            host, "query { viewer { login } rateLimit { cost remaining resetAt } }",
            context="viewer lookup", usage=usage,
        )
    except GhError as e:
        raise GhError(f"{e}  (try: gh auth login --hostname {host})") from None
    return data["viewer"]["login"]


PR_FRAGMENT = """
fragment PR on PullRequest {
  id number title url isDraft state createdAt updatedAt closedAt mergedAt
  author { login }
  baseRefName headRefName headRefOid
  mergeable reviewDecision
  additions deletions changedFiles
  comments { totalCount }
  commitCount: commits { totalCount }
  headCommit: commits(last: 1) { nodes { commit { oid statusCheckRollup { state } } } }
  assignees(first: 10) { nodes { login } }
  reviewRequests(first: 20) { nodes { requestedReviewer {
    __typename
    ... on User { login }
    ... on Team { slug organization { login } }
  } } }
  latestReviews(first: 20) { nodes { author { login } state submittedAt commit { oid } } }
  reviewThreads(first: 100) { totalCount nodes { isResolved } }
}
"""

# (role, search qualifier, also search recently closed?)
ROLE_QUERIES = [
    ("author", "author:{me}", True),
    ("assignee", "assignee:{me}", True),
    ("review_requested", "review-requested:{me}", False),  # includes teams you belong to
    ("reviewed", "reviewed-by:{me}", True),
    ("involved", "involves:{me}", True),                    # mentions / commenters
]


def fetch_repo(spec: RepoSpec, me: str, since: str,
               usage: list[GraphQLUsage] | None = None) -> tuple[dict[str, dict], list[str]]:
    aliases, parts = [], []
    for role, qual, with_closed in ROLE_QUERIES:
        for st, extra in (("open", "is:open"), ("closed", f"is:closed closed:>={since}")):
            if st == "closed" and not with_closed:
                continue
            q = f"repo:{spec.slug} is:pr {extra} {qual.format(me=me)}"
            alias = f"{role}_{st}"
            aliases.append(alias)
            parts.append(
                f"{alias}: search(query: {json.dumps(q)}, type: ISSUE, first: 50) "
                "{ pageInfo { hasNextPage } nodes { ... on PullRequest { id number updatedAt } } }"
            )
    query = "query {\nrateLimit { cost remaining resetAt }\n" + "\n".join(parts) + "\n}"
    data, warns = gh_graphql(spec.host, query, context=f"{spec.key} discovery", usage=usage)
    found: dict[str, dict] = {}
    roles: dict[str, set[str]] = {}
    for alias in aliases:
        block = data.get(alias) or {}
        if (block.get("pageInfo") or {}).get("hasNextPage"):
            warns.append(f"{spec.key}: more than 50 results for '{alias}', showing the 50 most relevant")
        for node in block.get("nodes") or []:
            if not node or "id" not in node:
                continue
            found[node["id"]] = node
            roles.setdefault(node["id"], set()).add(alias.rsplit("_", 1)[0])
    for pid, node in found.items():
        node["_roles"] = sorted(roles[pid])
    return found, warns


def fetch_nodes(host: str, ids: list[str], *,
                context: str = "lost PR refetch",
                usage: list[GraphQLUsage] | None = None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 40):
        chunk = ids[i:i + 40]
        query = ("query($ids: [ID!]!) { rateLimit { cost remaining resetAt } "
                 "nodes(ids: $ids) { ... on PullRequest { ...PR } } }" + PR_FRAGMENT)
        data, _ = gh_graphql(host, query, {"ids": chunk},
                             context=f"{context} ({len(chunk)} PRs)", usage=usage)
        for n in data.get("nodes") or []:
            if n and "id" in n:
                out[n["id"]] = n
    return out


# --------------------------------------------------------------------------- derived state

def parse_ts(s: str | None) -> float | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def ago(ts: float | str | None) -> str:
    if isinstance(ts, str):
        ts = parse_ts(ts)
    if not ts:
        return ""
    secs = max(0.0, time.time() - ts)
    for unit, n in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n:
            return f"{int(secs // n)}{unit}"
    return "now"


def ago_phrase(ts: float | str | None) -> str:
    a = ago(ts)
    return "just now" if a == "now" else f"{a} ago"


def _login(x: dict | None) -> str:
    return (x or {}).get("login") or "ghost"


@dataclass
class D:
    """Everything the dashboard needs, derived from one fetched PR payload."""
    id: str
    number: int
    title: str
    url: str
    me: str
    author: str
    state: str                 # OPEN / MERGED / CLOSED
    draft: bool
    base: str
    head_ref: str
    head: str                  # head commit oid
    ci: str | None             # SUCCESS / FAILURE / ERROR / PENDING / EXPECTED / None
    decision: str | None       # APPROVED / CHANGES_REQUESTED / REVIEW_REQUIRED / None
    conflict: bool
    unresolved: int
    comments: int
    commits: int
    adds: int
    dels: int
    files: int
    created: str | None
    updated: str | None
    closed: str | None
    roles: list[str]
    requested_users: list[str]
    requested_teams: list[str]
    reviews: dict[str, dict]   # login -> {state, oid, at}
    assignees: list[str]

    @property
    def requested_me(self) -> bool:
        return self.me in self.requested_users

    @property
    def requested_team(self) -> bool:
        return "review_requested" in self.roles and not self.requested_me and bool(self.requested_teams)

    @property
    def my_review(self) -> dict | None:
        return self.reviews.get(self.me)

    @property
    def stale_review(self) -> bool:
        """New commits were pushed after my last review."""
        r = self.my_review
        return bool(r and r.get("oid") and self.head and r["oid"] != self.head)

    @property
    def updated_ts(self) -> float:
        return parse_ts(self.updated) or 0.0

    @property
    def bucket(self) -> str:
        if self.state != "OPEN":
            return "closed"
        if self.author == self.me:
            return "mine"
        if self.requested_me and self.my_review:
            return "rereview"
        if self.requested_me or self.requested_team:
            return "review"
        return "following"


BUCKET_ORDER = {"rereview": 0, "review": 1, "mine": 2, "following": 3, "closed": 4}
BUCKET_LABEL = {"rereview": "RE-REVIEW", "review": "REVIEW", "mine": "MINE", "following": "FOLLOW", "closed": "CLOSED"}


def derive(raw: dict, me: str) -> D:
    users, teams = [], []
    for n in (raw.get("reviewRequests") or {}).get("nodes") or []:
        rr = (n or {}).get("requestedReviewer") or {}
        if rr.get("__typename") == "User":
            users.append(rr.get("login") or "ghost")
        elif rr.get("__typename") == "Team":
            teams.append(f"{(rr.get('organization') or {}).get('login', '?')}/{rr.get('slug')}")
    reviews = {}
    for n in (raw.get("latestReviews") or {}).get("nodes") or []:
        reviews[_login(n.get("author"))] = {
            "state": n.get("state"), "oid": (n.get("commit") or {}).get("oid"), "at": n.get("submittedAt"),
        }
    hc = (raw.get("headCommit") or {}).get("nodes") or []
    ci = ((hc[0].get("commit") or {}).get("statusCheckRollup") or {}).get("state") if hc else None
    threads = (raw.get("reviewThreads") or {}).get("nodes") or []
    return D(
        id=raw["id"], number=raw["number"], title=raw.get("title") or "", url=raw.get("url") or "", me=me,
        author=_login(raw.get("author")), state=raw.get("state") or "OPEN", draft=bool(raw.get("isDraft")),
        base=raw.get("baseRefName") or "", head_ref=raw.get("headRefName") or "", head=raw.get("headRefOid") or "",
        ci=ci, decision=raw.get("reviewDecision"), conflict=raw.get("mergeable") == "CONFLICTING",
        unresolved=sum(1 for t in threads if not t.get("isResolved")),
        comments=(raw.get("comments") or {}).get("totalCount", 0),
        commits=(raw.get("commitCount") or {}).get("totalCount", 0),
        adds=raw.get("additions") or 0, dels=raw.get("deletions") or 0, files=raw.get("changedFiles") or 0,
        created=raw.get("createdAt"), updated=raw.get("updatedAt"), closed=raw.get("closedAt") or raw.get("mergedAt"),
        roles=list(raw.get("_roles") or []), requested_users=users, requested_teams=teams, reviews=reviews,
        assignees=[_login(a) for a in (raw.get("assignees") or {}).get("nodes") or []],
    )


def status(d: D, stale_days: int = 14) -> tuple[str, str]:
    """(text, level) where level is one of bad / warn / good / info / dim."""
    b = d.bucket
    if b == "closed":
        return ("merged" if d.state == "MERGED" else "closed", "dim")
    if b == "mine":
        if d.draft:
            return "draft", "dim"
        if d.conflict:
            return "merge conflicts", "bad"
        if d.ci in ("FAILURE", "ERROR"):
            return "CI failing", "bad"
        if d.decision == "CHANGES_REQUESTED":
            return "changes requested", "bad"
        if d.unresolved:
            return f"{d.unresolved} unresolved thread{'s' if d.unresolved != 1 else ''}", "warn"
        if d.decision == "APPROVED":
            return ("ready to merge", "good") if d.ci in (None, "SUCCESS") else ("approved, CI running", "good")
        waiting = d.requested_users + d.requested_teams
        if waiting:
            more = f" +{len(waiting) - 3}" if len(waiting) > 3 else ""
            return f"waiting on {', '.join(waiting[:3])}{more}", "info"
        return "no reviewers requested", "warn"
    if b in ("rereview", "review"):
        if d.draft:
            return "draft", "dim"
        text = "re-review requested" if b == "rereview" else ("review requested via team" if d.requested_team else "review requested")
        if d.stale_review:
            text += " - new commits"
        return text, "warn"
    # following
    if d.my_review:
        if d.stale_review:
            return "new commits since your review", "warn"
        return f"you: {str(d.my_review['state']).lower().replace('_', ' ')}", "info"
    stale = d.updated_ts and (time.time() - d.updated_ts) > stale_days * 86400
    if stale:
        return f"stale {ago(d.updated)}", "dim"
    if "assignee" in d.roles or d.me in d.assignees:
        return "assigned to you", "info"
    return "mentioned / commented", "dim"


def describe_roles(d: D) -> list[str]:
    out = []
    if d.author == d.me:
        out.append("author")
    if d.me in d.assignees or "assignee" in d.roles:
        out.append("assignee")
    if d.requested_me:
        out.append("re-review requested" if d.my_review else "review requested")
    elif d.requested_team:
        out.append("review requested via team")
    if d.my_review:
        out.append(f"reviewed ({str(d.my_review['state']).lower().replace('_', ' ')})")
    if not out:
        out.append("mentioned / commented")
    return out


# --------------------------------------------------------------------------- change tracking

_REVIEW_VERB = {"APPROVED": "approved", "CHANGES_REQUESTED": "requested changes", "COMMENTED": "left review comments"}


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def diff_events(old: D, new: D) -> list[str]:
    """Human-readable changes between two syncs of the same PR."""
    ev: list[str] = []
    me = new.me
    mine = new.author == me
    relevant = mine or bool(new.my_review) or new.requested_me

    if old.state == "OPEN" and new.state == "MERGED":
        ev.append("Merged")
    elif old.state == "OPEN" and new.state == "CLOSED":
        ev.append("Closed without merging")
    if old.draft and not new.draft:
        ev.append("Marked ready for review")
    elif not old.draft and new.draft:
        ev.append("Converted to draft")

    if not mine:
        if new.requested_me and not old.requested_me:
            ev.append("Re-review requested of you" if new.my_review else "Review requested of you")
        for t in new.requested_teams:
            if t not in old.requested_teams and new.requested_team:
                ev.append(f"Review requested of your team {t}")
        if new.head != old.head and new.state == "OPEN":
            n = max(new.commits - old.commits, 1)
            ev.append(f"{plural(n, 'new commit')} pushed" + (" (since your review)" if new.stale_review else ""))

    if relevant:
        for who, r in new.reviews.items():
            if who == me or r.get("state") not in _REVIEW_VERB:
                continue
            o = old.reviews.get(who)
            if not o or o.get("at") != r.get("at"):
                ev.append(f"{who} {_REVIEW_VERB[r['state']]}")
        if new.ci != old.ci and new.ci in ("SUCCESS", "FAILURE", "ERROR"):
            ev.append("CI passed" if new.ci == "SUCCESS" else "CI failed")
        if new.unresolved > old.unresolved:
            ev.append(f"+{plural(new.unresolved - old.unresolved, 'unresolved thread')}")
        if new.comments > old.comments:
            ev.append(f"+{plural(new.comments - old.comments, 'comment')}")
    if mine and new.conflict and not old.conflict:
        ev.append("Merge conflicts appeared")
    return ev


# --------------------------------------------------------------------------- sync

@dataclass
class SyncResult:
    repos_ok: int = 0
    events: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    graphql_usage: list[GraphQLUsage] = field(default_factory=list)
    duration_seconds: float = 0


@dataclass
class RepoFetchResult:
    found: dict[str, dict] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    lost: list[str] = field(default_factory=list)
    refetched: dict[str, dict] = field(default_factory=dict)
    usage: list[GraphQLUsage] = field(default_factory=list)
    error: str | None = None


def _fetch_repo_state(spec: RepoSpec, me: str, since: str,
                      existing: dict[str, dict]) -> RepoFetchResult:
    result = RepoFetchResult()
    try:
        discovered, result.warnings = fetch_repo(spec, me, since, result.usage)
        hydrate_ids = []
        found = {}
        for pid, candidate in discovered.items():
            old_row = existing.get(pid)
            if old_row is None:
                hydrate_ids.append(pid)
                continue
            old_raw = json.loads(old_row["raw"])
            if (candidate.get("updatedAt")
                    and candidate.get("updatedAt") == old_raw.get("updatedAt")
                    and candidate["_roles"] == old_raw.get("_roles", [])):
                found[pid] = old_raw
            else:
                hydrate_ids.append(pid)
        hydrated = fetch_nodes(
            spec.host, hydrate_ids, context=f"{spec.key} PR hydration", usage=result.usage,
        ) if hydrate_ids else {}
        missing = [pid for pid in hydrate_ids if pid not in hydrated]
        if missing:
            raise GhError(f"{spec.key}: GraphQL returned no PR data for {len(missing)} discovered PR(s)")
        for pid in hydrate_ids:
            hydrated[pid]["_roles"] = discovered[pid]["_roles"]
        found.update(hydrated)
        result.found = found
        result.lost = [
            pid for pid, row in existing.items()
            if pid not in found and not row["hidden"] and row["state"] == "OPEN"
        ]
        result.refetched = fetch_nodes(
            spec.host, result.lost, context=f"{spec.key} lost PR refetch", usage=result.usage,
        ) if result.lost else {}
    except GhError as e:
        result.error = str(e)
    return result


def _upsert(con: sqlite3.Connection, spec: RepoSpec, me: str, raw: dict, old_row: sqlite3.Row | None,
            now: float, first_sync: bool, hidden: bool = False) -> int:
    d_new = derive(raw, me)
    if old_row is None:
        if first_sync or d_new.author == me or hidden:
            events: list[str] = []
        else:
            events = [f"New: {describe_roles(d_new)[0]}"]
        con.execute(
            "INSERT INTO pr(id,repo_key,host,number,raw,state,hidden,first_seen,last_event,seen,synced) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (d_new.id, spec.key, spec.host, d_new.number, json.dumps(raw), d_new.state, int(hidden), now,
             now if events else 0, 0 if events else now, now),
        )
    else:
        d_old = derive(json.loads(old_row["raw"]), me)
        events = [] if (old_row["hidden"] or hidden) else diff_events(d_old, d_new)
        con.execute(
            "UPDATE pr SET raw=?, state=?, hidden=?, last_event=?, synced=? WHERE id=?",
            (json.dumps(raw), d_new.state, int(hidden), now if events else old_row["last_event"], now, d_new.id),
        )
    for text in events:
        con.execute("INSERT INTO event(pr_id, ts, text) VALUES(?,?,?)", (d_new.id, now, text))
    return len(events)


def sync_all(cfg: Config, progress: Callable[[str], None] = lambda m: None) -> SyncResult:
    res = SyncResult()
    started = time.monotonic()
    con = connect(cfg.db)
    since = (date.today() - timedelta(days=cfg.closed_days)).isoformat()
    mes: dict[str, str] = {}
    try:
        host_errors: dict[str, str] = {}
        for host in sorted({spec.host for spec in cfg.repos}):
            try:
                mes[host] = get_me(host, res.graphql_usage)
            except GhError as e:
                host_errors[host] = str(e)

        existing_by_repo = {
            spec.key: {
                row["id"]: dict(row)
                for row in con.execute("SELECT * FROM pr WHERE repo_key=?", (spec.key,))
            }
            for spec in cfg.repos
        }
        jobs = [
            (spec, mes[spec.host], existing_by_repo[spec.key])
            for spec in cfg.repos
            if spec.host in mes
        ]
        for spec in cfg.repos:
            if spec.host in host_errors:
                res.failures.append((spec.key, host_errors[spec.host]))

        with ThreadPoolExecutor(max_workers=min(SYNC_WORKERS, max(1, len(jobs)))) as pool:
            futures = {
                pool.submit(_fetch_repo_state, spec, me, since, existing): (spec, me, existing)
                for spec, me, existing in jobs
            }
            completed = 0
            for future in as_completed(futures):
                spec, me, existing = futures[future]
                result = future.result()
                res.graphql_usage.extend(result.usage)
                res.warnings.extend(result.warnings)
                completed += 1
                progress(f"Syncing {spec.key} ({completed}/{len(jobs)})...")
                if result.error:
                    res.failures.append((spec.key, result.error))
                    continue

                now = time.time()
                first = meta_get(con, f"repo_synced:{spec.key}") is None
                with con:
                    meta_set(con, f"me:{spec.host}", me)
                    for pid, raw in result.found.items():
                        res.events += _upsert(con, spec, me, raw, existing.get(pid), now, first)
                    for pid in result.lost:
                        raw = result.refetched.get(pid)
                        if raw is None:  # deleted, transferred or no longer visible
                            con.execute("UPDATE pr SET hidden=1, synced=? WHERE id=?", (now, pid))
                            continue
                        raw["_roles"] = json.loads(existing[pid]["raw"]).get("_roles", [])
                        if raw.get("state") == "OPEN":
                            # still open but your involvement is gone: stop showing it
                            raw["_roles"] = []
                            res.events += _upsert(con, spec, me, raw, existing[pid], now, False, hidden=True)
                        else:
                            res.events += _upsert(con, spec, me, raw, existing[pid], now, False)
                    meta_set(con, f"repo_synced:{spec.key}", str(now))
                res.repos_ok += 1

        with con:
            meta_set(con, "last_sync", str(time.time()))
            cutoff = time.time() - 90 * 86400
            con.execute("DELETE FROM event WHERE pr_id IN (SELECT id FROM pr WHERE state!='OPEN' AND synced<?)", (cutoff,))
            con.execute("DELETE FROM pr WHERE state!='OPEN' AND synced<?", (cutoff,))
    finally:
        con.close()
        res.duration_seconds = time.monotonic() - started
    return res


def print_sync_telemetry(usage: list[GraphQLUsage], duration_seconds: float,
                         stream: TextIO = sys.stderr) -> None:
    by_host: dict[str, list[GraphQLUsage]] = {}
    for request in usage:
        by_host.setdefault(request.host, []).append(request)
    for host, requests in by_host.items():
        reported = [request for request in requests if request.cost is not None]
        cost = sum(request.cost or 0 for request in reported)
        remaining = next((request.remaining for request in reversed(requests)
                          if request.remaining is not None), None)
        reset_at = next((request.reset_at for request in reversed(requests)
                         if request.reset_at), None)
        quota = f"{cost} points ({len(reported)}/{len(requests)} reported)"
        if not reported:
            quota = f"n/a (0/{len(requests)} reported)"
        suffix = f"; remaining {remaining}" if remaining is not None else "; remaining n/a"
        if reset_at:
            suffix += f"; resets {reset_at}"
        print(f"GraphQL {host}: {len(requests)} requests; cost {quota}{suffix}", file=stream)
        for request in requests:
            cost_text = str(request.cost) if request.cost is not None else "n/a"
            remaining_text = str(request.remaining) if request.remaining is not None else "n/a"
            reset_text = f"; reset {request.reset_at}" if request.reset_at else ""
            print(
                f"  {request.context}: cost {cost_text}; remaining {remaining_text}"
                f"{reset_text}; {request.duration_seconds:.2f}s",
                file=stream,
            )
    print(f"Sync duration: {duration_seconds:.2f}s", file=stream)


# --------------------------------------------------------------------------- views

@dataclass
class Item:
    d: D
    spec: RepoSpec
    unread: bool
    last_event: float

    @property
    def id(self) -> str:
        return self.d.id


def load_items(cfg: Config, con: sqlite3.Connection) -> list[Item]:
    specs = {s.key: s for s in cfg.repos}
    mes = {r["k"][3:]: r["v"] for r in con.execute("SELECT k, v FROM meta WHERE k LIKE 'me:%'")}
    cutoff = time.time() - cfg.closed_days * 86400
    items = []
    for row in con.execute("SELECT * FROM pr WHERE hidden=0"):
        spec = specs.get(row["repo_key"])
        if not spec:
            continue  # repo no longer in your list
        d = derive(json.loads(row["raw"]), mes.get(row["host"], ""))
        if d.state != "OPEN" and (parse_ts(d.closed) or 0) < cutoff:
            continue
        items.append(Item(d, spec, row["last_event"] > row["seen"], row["last_event"]))
    items.sort(key=lambda it: (BUCKET_ORDER[it.d.bucket], -it.d.updated_ts))
    return items


def mark_seen(con: sqlite3.Connection, ids: list[str] | None) -> None:
    now = time.time()
    with con:
        if ids is None:
            con.execute("UPDATE pr SET seen=? WHERE last_event>seen", (now,))
        else:
            con.executemany("UPDATE pr SET seen=? WHERE id=?", [(now, i) for i in ids])


def recent_events(con: sqlite3.Connection, pr_id: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
    sql = ("SELECT e.ts, e.text, p.repo_key, p.number, p.raw FROM event e JOIN pr p ON p.id=e.pr_id "
           + ("WHERE e.pr_id=? " if pr_id else "") + "ORDER BY e.ts DESC, e.id DESC LIMIT ?")
    return list(con.execute(sql, (pr_id, limit) if pr_id else (limit,)))


def local_info(path: Path, head_ref: str, head_oid: str) -> dict:
    """What the local clone knows about this PR's branch (read-only git calls)."""
    info: dict = {}
    rc, out = git(path, "rev-parse", "--abbrev-ref", "HEAD")
    info["current"] = out if rc == 0 else None
    rc, out = git(path, "status", "--porcelain", timeout=5)
    info["dirty"] = len(out.splitlines()) if rc == 0 else None
    rc, out = git(path, "rev-parse", "--verify", "--quiet", f"refs/heads/{head_ref}")
    info["branch_sha"] = out if rc == 0 else None
    info["have_head"] = bool(head_oid) and git(path, "cat-file", "-e", f"{head_oid}^{{commit}}")[0] == 0
    return info


# --------------------------------------------------------------------------- plain-text list

def cmd_list(cfg: Config) -> int:
    con = connect(cfg.db)
    items = load_items(cfg, con)
    last = meta_get(con, "last_sync")
    con.close()
    print(f"prdash - {len(items)} PRs, last sync {ago_phrase(float(last)) if last else 'never'}")
    cur = None
    for it in items:
        d = it.d
        if d.bucket != cur:
            cur = d.bucket
            print(f"\n== {BUCKET_LABEL[cur]} ==")
        text, _ = status(d, cfg.stale_days)
        print(f"{'*' if it.unread else ' '} {it.spec.slug.split('/')[-1]:<18} #{d.number:<6} {d.title[:52]:<52} {d.author:<14} {text}")
    return 0


# --------------------------------------------------------------------------- TUI

def run_tui(cfg: Config) -> int:
    try:
        from rich.text import Text
        from textual import work
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Container, VerticalScroll
        from textual.screen import ModalScreen
        from textual.widgets import DataTable, Footer, Header, Input, Static
    except ImportError:
        print("The TUI needs Textual:  pip install textual   (or: uv run prdash.py)", file=sys.stderr)
        return 1

    LEVEL = {"bad": "bold red", "warn": "yellow", "good": "green", "info": "cyan", "dim": "dim"}
    BUCKET_STYLE = {"rereview": "bold magenta", "review": "bold yellow", "mine": "bold cyan", "following": "", "closed": "dim"}
    CI_CELL = {"SUCCESS": ("✓", "green"), "FAILURE": ("✗", "bold red"), "ERROR": ("✗", "bold red"),
               "PENDING": ("●", "yellow"), "EXPECTED": ("●", "yellow")}
    FILTERS = {"all": None, "review": ("rereview", "review"), "mine": ("mine",), "following": ("following",), "closed": ("closed",)}

    class Activity(ModalScreen):
        BINDINGS = [Binding("escape", "close", "Close"), Binding("q", "close", "Close")]
        DEFAULT_CSS = """
        Activity { align: center middle; }
        #box { width: 95%; height: 85%; border: thick $primary; background: $surface; }
        """

        def __init__(self, rows: list[sqlite3.Row]) -> None:
            super().__init__()
            self.rows = rows

        def compose(self) -> ComposeResult:
            with Container(id="box"):
                yield DataTable(id="ev", cursor_type="row", zebra_stripes=True)

        def on_mount(self) -> None:
            t = self.query_one("#ev", DataTable)
            t.add_columns("When", "Repo", "PR", "Title", "What happened")
            for r in self.rows:
                raw = json.loads(r["raw"])
                t.add_row(ago(r["ts"]), r["repo_key"].split("/", 1)[1], f"#{r['number']}",
                          Text(raw.get("title", "")[:50], no_wrap=True), Text(r["text"]))
            if not self.rows:
                t.add_row("", "", "", "", "No events recorded yet")
            t.focus()

        def action_close(self) -> None:
            self.dismiss()

    class PRDash(App):
        TITLE = "prdash"
        CSS = """
        #status { height: 1; padding: 0 1; background: $boost; }
        #filter { display: none; }
        #table { height: 3fr; }
        #detail-scroll { height: 2fr; border-top: tall $primary; padding: 0 1; }
        """
        BINDINGS = [
            Binding("r", "sync", "Sync"),
            Binding("o", "open", "Open in browser"),
            Binding("m", "seen", "Mark seen"),
            Binding("M", "seen_all", "Mark all seen"),
            Binding("c", "checkout", "gh pr checkout"),
            Binding("e", "events", "Activity"),
            Binding("a", "attention", "Needs action"),
            Binding("/", "filter", "Search"),
            Binding("escape", "clear_filter", "Clear", show=False),
            Binding("0", "bucket('all')", "All", show=False),
            Binding("1", "bucket('review')", "Review", show=False),
            Binding("2", "bucket('mine')", "Mine", show=False),
            Binding("3", "bucket('following')", "Following", show=False),
            Binding("4", "bucket('closed')", "Closed", show=False),
            Binding("q", "quit", "Quit"),
            Binding("ctrl+q", "ignore_ctrl_q", show=False),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.items: list[Item] = []
            self.visible_items: dict[str, Item] = {}
            self.bucket_filter = "all"
            self.attention_only = False
            self.syncing = False
            self.sync_msg = ""
            self.local: dict[str, dict] = {}
            self.last_sync = 0.0

        def action_ignore_ctrl_q(self) -> None:
            pass

        # ---- layout
        def compose(self) -> ComposeResult:
            yield Header()
            yield Static(id="status")
            yield Input(placeholder="search repo / title / author, Enter to keep, Esc to clear", id="filter")
            yield DataTable(id="table")
            yield VerticalScroll(Static(id="detail"), id="detail-scroll")
            yield Footer()

        def on_mount(self) -> None:
            t = self.query_one("#table", DataTable)
            t.cursor_type = "row"
            t.zebra_stripes = True
            t.add_column("", width=1)
            t.add_column("Bucket", width=9)
            t.add_column("Repo", width=16)
            t.add_column("#", width=6)
            t.add_column("Title", width=46)
            t.add_column("Author", width=14)
            t.add_column("CI", width=2)
            t.add_column("Review", width=10)
            t.add_column("Status", width=34)
            t.add_column("Upd", width=4)
            t.focus()
            for e in cfg.errors:
                self.notify(e, severity="warning", timeout=10)
            self.reload()
            if cfg.auto_sync_minutes:
                con = connect(cfg.db)
                last = float(meta_get(con, "last_sync") or 0)
                con.close()
                if time.time() - last > cfg.auto_sync_minutes * 60:
                    self.action_sync()
                self.set_interval(cfg.auto_sync_minutes * 60, self.action_sync)

        # ---- data
        def reload(self) -> None:
            con = connect(cfg.db)
            self.items = load_items(cfg, con)
            self.last_sync = float(meta_get(con, "last_sync") or 0)
            con.close()
            self.refresh_table()

        def current_id(self) -> str | None:
            t = self.query_one("#table", DataTable)
            if t.row_count == 0:
                return None
            try:
                return t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
            except Exception:
                return None

        def passes(self, it: Item) -> bool:
            allowed = FILTERS[self.bucket_filter]
            if allowed and it.d.bucket not in allowed:
                return False
            if self.attention_only:
                lvl = status(it.d, cfg.stale_days)[1]
                if not (it.unread or lvl in ("bad", "warn")):
                    return False
            q = self.query_one("#filter", Input).value.strip().lower()
            if q:
                hay = f"{it.spec.slug} {it.d.title} {it.d.author} #{it.d.number}".lower()
                return all(w in hay for w in q.split())
            return True

        def cells(self, it: Item) -> list:
            d = it.d
            text, level = status(d, cfg.stale_days)
            ci_txt, ci_style = CI_CELL.get(d.ci or "", ("–", "dim"))
            review = {"APPROVED": ("✓ approved", "green"), "CHANGES_REQUESTED": ("✗ changes", "bold red"),
                      "REVIEW_REQUIRED": ("… needed", "dim")}.get(d.decision or "", ("", ""))
            if d.draft:
                review = ("draft", "dim")
            return [
                Text("●", style="bold magenta") if it.unread else Text(""),
                Text(BUCKET_LABEL[d.bucket], style=BUCKET_STYLE[d.bucket]),
                Text(it.spec.slug.split("/")[-1], no_wrap=True),
                Text(f"#{d.number}"),
                Text(d.title, no_wrap=True, overflow="ellipsis"),
                Text(d.author, no_wrap=True),
                Text(ci_txt, style=ci_style),
                Text(review[0], style=review[1]),
                Text(text, style=LEVEL[level], no_wrap=True, overflow="ellipsis"),
                Text(ago(d.updated), style="dim"),
            ]

        def refresh_table(self) -> None:
            t = self.query_one("#table", DataTable)
            prev = self.current_id()
            t.clear()
            self.visible_items = {}
            for it in self.items:
                if self.passes(it):
                    self.visible_items[it.id] = it
                    t.add_row(*self.cells(it), key=it.id)
            if prev in self.visible_items:
                try:
                    t.move_cursor(row=t.get_row_index(prev))
                except Exception:
                    pass
            if not self.visible_items:
                self.query_one("#detail", Static).update(
                    Text("Nothing to show. Press r to sync, or 0 to clear the bucket filter." if self.items
                         else "Cache is empty. Press r to sync.", style="dim"))
            self.update_status()

        def update_status(self) -> None:
            unread = sum(1 for i in self.items if i.unread)
            sync = f"last sync {ago_phrase(self.last_sync)}" if self.last_sync else "never synced"
            flt = self.bucket_filter + (" + needs-action" if self.attention_only else "")
            msg = self.sync_msg or f"{len(self.visible_items)}/{len(self.items)} shown · {unread} changed · {sync} · filter: {flt} · keys 0-4 buckets"
            self.query_one("#status", Static).update(Text(msg))

        # ---- detail pane
        def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
            key = event.row_key.value if event.row_key else None
            if key and key in self.visible_items:
                self.render_detail(key)
                self.load_local(self.visible_items[key])

        @work(thread=True, exclusive=True, group="local")
        def load_local(self, it: Item) -> None:
            info = local_info(it.spec.path, it.d.head_ref, it.d.head)
            self.call_from_thread(self.apply_local, it.id, info)

        def apply_local(self, pr_id: str, info: dict) -> None:
            self.local[pr_id] = info
            if self.current_id() == pr_id:
                self.render_detail(pr_id)

        def render_detail(self, pr_id: str) -> None:
            it = self.visible_items.get(pr_id)
            if not it:
                return
            d = it.d
            text, level = status(d, cfg.stale_days)
            t = Text()
            t.append(f"{d.title}\n", style="bold")
            t.append(f"{it.spec.key} #{d.number} · by {d.author} · {d.base} ← {d.head_ref}\n", style="dim")
            t.append(f"{d.url}\n\n", style="underline")
            t.append("Status    ", style="bold"); t.append(f"{text}\n", style=LEVEL[level])
            t.append("You       ", style="bold"); t.append(", ".join(describe_roles(d)) + "\n")
            t.append("CI        ", style="bold"); t.append(f"{(d.ci or 'none').lower()}\n")
            t.append("Threads   ", style="bold"); t.append(f"{d.unresolved} unresolved · {d.comments} comments\n")
            t.append("Size      ", style="bold"); t.append(f"+{d.adds} −{d.dels} in {d.files} files · {d.commits} commits\n")
            t.append("Opened    ", style="bold"); t.append(f"{ago(d.created)} ago · updated {ago(d.updated)} ago\n")
            if d.requested_users or d.requested_teams:
                t.append("Requested ", style="bold"); t.append(", ".join(d.requested_users + d.requested_teams) + "\n")
            if d.reviews:
                t.append("Reviews   ", style="bold")
                t.append("  ".join(f"{w}: {str(r['state']).lower().replace('_', ' ')}" for w, r in d.reviews.items()) + "\n")
            loc = self.local.get(pr_id)
            t.append("Local     ", style="bold")
            if loc is None:
                t.append("…\n", style="dim")
            else:
                bits = [f"on {loc['current']}" if loc["current"] else "not a checkout"]
                if loc["dirty"]:
                    bits.append(f"{loc['dirty']} uncommitted change{'s' if loc['dirty'] != 1 else ''}")
                if loc["branch_sha"]:
                    bits.append("branch present, " + ("up to date" if loc["branch_sha"] == d.head else "differs from PR head"))
                else:
                    bits.append("branch not checked out")
                bits.append("head commit fetched" if loc["have_head"] else "head commit not fetched")
                t.append(" · ".join(bits) + "\n")
            con = connect(cfg.db)
            evs = recent_events(con, pr_id, 8)
            con.close()
            t.append("\nActivity\n", style="bold")
            if not evs:
                t.append("  nothing recorded since tracking started\n", style="dim")
            for e in evs:
                t.append(f"  {ago(e['ts']):>4}  ", style="dim"); t.append(f"{e['text']}\n")
            self.query_one("#detail", Static).update(t)

        # ---- actions
        def action_sync(self) -> None:
            if self.syncing:
                return
            self.syncing = True
            self.run_sync()

        @work(thread=True, exclusive=True, group="sync")
        def run_sync(self) -> None:
            def progress(m: str) -> None:
                self.call_from_thread(self.set_msg, m)
            try:
                res = sync_all(cfg, progress)
                self.call_from_thread(self.after_sync, res, None)
            except Exception as e:  # keep the UI alive whatever happens
                self.call_from_thread(self.after_sync, None, str(e))

        def set_msg(self, m: str) -> None:
            self.sync_msg = m
            self.update_status()

        def after_sync(self, res: SyncResult | None, err: str | None) -> None:
            self.syncing = False
            self.sync_msg = ""
            self.reload()
            if err:
                self.notify(f"Sync failed: {err}", severity="error", timeout=10)
            elif res and res.failures:
                f = res.failures[0]
                self.notify(f"{len(res.failures)} repo(s) failed. {f[0]}: {f[1]}", severity="error", timeout=12)
            elif res:
                self.notify(f"Synced {res.repos_ok} repos · {res.events} new events" +
                            (f" · {len(res.warnings)} warnings" if res.warnings else ""))

        def action_open(self) -> None:
            pid = self.current_id()
            if pid:
                webbrowser.open(self.visible_items[pid].d.url)
                self.action_seen()

        def action_seen(self) -> None:
            pid = self.current_id()
            if pid:
                con = connect(cfg.db); mark_seen(con, [pid]); con.close()
                self.reload()

        def action_seen_all(self) -> None:
            con = connect(cfg.db); mark_seen(con, None); con.close()
            self.reload()

        def action_checkout(self) -> None:
            pid = self.current_id()
            if not pid:
                return
            it = self.visible_items[pid]
            with self.suspend():
                print(f"\n$ gh pr checkout {it.d.number}   (in {it.spec.path})\n")
                subprocess.run([GH, "pr", "checkout", str(it.d.number)], cwd=it.spec.path,
                               env={**os.environ, "GH_HOST": it.spec.host})
                input("\nPress Enter to return to prdash...")
            self.local.pop(pid, None)
            self.render_detail(pid)
            self.load_local(it)

        def action_events(self) -> None:
            con = connect(cfg.db)
            rows = recent_events(con)
            con.close()
            self.push_screen(Activity(rows))

        def action_bucket(self, name: str) -> None:
            self.bucket_filter = name
            self.refresh_table()

        def action_attention(self) -> None:
            self.attention_only = not self.attention_only
            self.refresh_table()

        def action_filter(self) -> None:
            inp = self.query_one("#filter", Input)
            inp.display = True
            inp.focus()

        def action_clear_filter(self) -> None:
            inp = self.query_one("#filter", Input)
            inp.value = ""
            inp.display = False
            self.query_one("#table", DataTable).focus()
            self.refresh_table()

        def on_input_changed(self, event: Input.Changed) -> None:
            self.refresh_table()

        def on_input_submitted(self, event: Input.Submitted) -> None:
            if not event.value.strip():
                self.query_one("#filter", Input).display = False
            self.query_one("#table", DataTable).focus()

    PRDash().run()
    return 0


# --------------------------------------------------------------------------- CLI

def cmd_check(cfg: Config) -> int:
    rc = 0
    for e in cfg.errors:
        print(f"  ! {e}")
        rc = 1
    for s in cfg.repos:
        print(f"  {s.path}  ->  {s.key}")
    for host in sorted({s.host for s in cfg.repos}):
        try:
            print(f"  {host}: authenticated as {get_me(host)}")
        except GhError as e:
            print(f"  {host}: FAILED - {e}")
            rc = 1
    print(f"  cache: {cfg.db}")
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="prdash", description="Local-first PR dashboard for GitHub Enterprise.")
    ap.add_argument("-c", "--config", help="path to repos.toml (default: ~/.config/prdash/repos.toml or $PRDASH_CONFIG)")
    sub = ap.add_subparsers(dest="cmd")
    for name, h in (("tui", "open the dashboard (default)"), ("sync", "refresh the local cache"),
                    ("list", "print the cached dashboard"), ("check", "verify repos and gh auth"),
                    ("init", "write an example config")):
        sub.add_parser(name, help=h)
    args = ap.parse_args(argv)
    cmd = args.cmd or "tui"
    path = config_path(args.config)

    if cmd == "init":
        if path.exists():
            print(f"{path} already exists; not overwriting.")
            return 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(EXAMPLE_CONFIG)
        print(f"Wrote {path}. Edit the [[repo]] entries, then run `prdash check`.")
        return 0

    try:
        cfg = load_config(path)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2

    if cmd == "check":
        return cmd_check(cfg)
    if cmd == "sync":
        for e in cfg.errors:
            print(f"! {e}", file=sys.stderr)
        res = sync_all(cfg, lambda m: print(m, file=sys.stderr))
        print_sync_telemetry(res.graphql_usage, res.duration_seconds)
        for repo, err in res.failures:
            print(f"! {repo}: {err}", file=sys.stderr)
        for w in res.warnings:
            print(f"! {w}", file=sys.stderr)
        print(f"Synced {res.repos_ok}/{len(cfg.repos)} repos, {res.events} new events.")
        return 1 if res.failures else 0
    if cmd == "list":
        return cmd_list(cfg)
    return run_tui(cfg)


if __name__ == "__main__":
    sys.exit(main())
