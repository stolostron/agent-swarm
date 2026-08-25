#!/usr/bin/env python3
"""Swarm PR Events Watcher & Session Dispatcher.

Autonomous, firewall-safe GitHub PR watcher daemon for Agent Swarm.
Monitors GitHub repositories with event-driven triggers via outbound ETag polling
and dispatches Swarm sessions via REST API only when actionable PR state changes occur.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

# Add repo root to sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.lib.pr_state import (  # noqa: E402
    DEFAULT_BOT_LOGINS,
    PRAction,
    PRState,
    TrustPolicy,
    TrustStrategy,
    classify_pr_action,
    is_bot_author,
    normalize_ci_checks,
    parse_iso_datetime,
)

log = logging.getLogger("swarm-pr-watcher")


@dataclass
class TriggerConfig:
    id: str
    name: str
    trigger_type: str  # "event" or "cron"
    condition: str  # "new_pr_or_commit", "ci_fail_or_conflict", "review_comments", "any_actionable"
    author_scope: str  # "self", "team", "bots", "all"
    workspace_id: int
    session_id: int
    repos: list[str] = field(default_factory=list)  # ["owner/repo", ...]
    prompt_file: str = ""
    instruction_prompt: str = ""
    enabled: bool = True


@dataclass
class WatcherConfig:
    api_url: str = "http://localhost:8090"
    api_token: str = ""
    api_token_env: str = "SWARM_API_TOKEN"
    poll_interval_seconds: float = 30.0
    sweep_interval_seconds: float = 1800.0  # 30 min periodic sweep
    repo_refresh_interval_seconds: float = 60.0
    db_path: str = "data/swarm_watcher_state.db"
    fix_authors: set[str] = field(default_factory=lambda: {"jpacker"})
    bot_logins: set[str] = field(default_factory=lambda: set(DEFAULT_BOT_LOGINS))
    trust_policy: TrustPolicy = field(default_factory=TrustPolicy)
    tokens: dict[str, str] = field(default_factory=dict)  # org_name -> token
    triggers: list[TriggerConfig] = field(default_factory=list)
    max_fix_attempts: int = 3
    quiet_period_seconds: float = 90.0


class StateStore:
    """SQLite persistent state store for PR action tracking and circuit breakers."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = Path(db_path).parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pr_action_state (
                    repo TEXT NOT NULL,
                    pr_number INTEGER NOT NULL,
                    head_sha TEXT NOT NULL,
                    action TEXT NOT NULL,
                    session_id INTEGER,
                    status TEXT NOT NULL, -- 'dispatched', 'in_progress', 'completed', 'failed', 'blocked'
                    attempts INTEGER NOT NULL DEFAULT 1,
                    last_dispatched_at TIMESTAMP,
                    last_error TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (repo, pr_number, head_sha, action)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS repo_etags (
                    repo TEXT PRIMARY KEY,
                    etag TEXT NOT NULL,
                    last_checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()

    def get_action_state(self, repo: str, pr_number: int, head_sha: str, action: str) -> dict[str, Any] | None:
        with self._get_conn() as conn:
            cur = conn.execute(
                """
                SELECT * FROM pr_action_state
                WHERE repo = ? AND pr_number = ? AND head_sha = ? AND action = ?
                """,
                (repo, pr_number, head_sha, action),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def record_dispatch(
        self,
        repo: str,
        pr_number: int,
        head_sha: str,
        action: str,
        session_id: int | None,
        status: str = "dispatched",
        error: str | None = None,
    ) -> int:
        """Record or update a dispatch attempt. Returns the new attempt count."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            cur = conn.execute(
                """
                SELECT attempts FROM pr_action_state
                WHERE repo = ? AND pr_number = ? AND head_sha = ? AND action = ?
                """,
                (repo, pr_number, head_sha, action),
            )
            row = cur.fetchone()
            if row:
                attempts = row["attempts"] + (1 if status in ("dispatched", "failed") else 0)
                conn.execute(
                    """
                    UPDATE pr_action_state
                    SET session_id = ?, status = ?, attempts = ?, last_dispatched_at = ?, last_error = ?, updated_at = ?
                    WHERE repo = ? AND pr_number = ? AND head_sha = ? AND action = ?
                    """,
                    (session_id, status, attempts, now, error, now, repo, pr_number, head_sha, action),
                )
            else:
                attempts = 1
                conn.execute(
                    """
                    INSERT INTO pr_action_state
                    (repo, pr_number, head_sha, action, session_id, status, attempts, last_dispatched_at, last_error, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (repo, pr_number, head_sha, action, session_id, status, attempts, now, error, now),
                )
            conn.commit()
            return attempts

    def get_etag(self, repo: str) -> str | None:
        with self._get_conn() as conn:
            cur = conn.execute("SELECT etag FROM repo_etags WHERE repo = ?", (repo,))
            row = cur.fetchone()
            return row["etag"] if row else None

    def save_etag(self, repo: str, etag: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO repo_etags (repo, etag, last_checked_at)
                VALUES (?, ?, ?)
                ON CONFLICT(repo) DO UPDATE SET etag = excluded.etag, last_checked_at = excluded.last_checked_at
                """,
                (repo, etag, now),
            )
            conn.commit()

    def remove_etag(self, repo: str) -> None:
        with self._get_conn() as conn:
            conn.execute("DELETE FROM repo_etags WHERE repo = ?", (repo,))
            conn.commit()

    def clean_stale_etags(self, active_repos: set[str]) -> None:
        with self._get_conn() as conn:
            cur = conn.execute("SELECT repo FROM repo_etags")
            for row in cur.fetchall():
                r = row["repo"]
                if r not in active_repos:
                    conn.execute("DELETE FROM repo_etags WHERE repo = ?", (r,))
            conn.commit()


class GitHubClient:
    """GitHub API client with multi-org token resolution and ETag support."""

    def __init__(self, token_map: dict[str, str] | None = None):
        self.token_map = token_map or {}
        self._cache_etags: dict[str, str] = {}

    def resolve_token(self, repo_or_org: str) -> str | None:
        """Resolve GitHub token for a given org or repo (e.g. 'OpenShift-Fleet/agentic-sdlc')."""
        org = repo_or_org.split("/")[0] if "/" in repo_or_org else repo_or_org
        org_normalized = org.replace("-", "_").upper()

        # 1. Configured token map for exact org
        if org in self.token_map:
            return self.token_map[org]

        # 2. Env var GH_TOKEN_<ORG>
        env_var_name = f"GH_TOKEN_{org_normalized}"
        if env_var_name in os.environ:
            return os.environ[env_var_name]

        # 3. Fallbacks
        if "GITHUB_TOKEN" in os.environ:
            return os.environ["GITHUB_TOKEN"]
        if "GH_TOKEN" in os.environ:
            return os.environ["GH_TOKEN"]

        return None

    def _build_request(self, url: str, repo: str, etag: str | None = None) -> urllib.request.Request:
        req = urllib.request.Request(url)
        token = self.resolve_token(repo)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "Swarm-PR-Events-Watcher/1.0")
        if etag:
            req.add_header("If-None-Match", etag)
        return req

    def fetch_events(self, repo: str, etag: str | None = None) -> tuple[int, list[dict[str, Any]], str | None]:
        """Poll repo events. Returns (status_code, events, new_etag)."""
        url = f"https://api.github.com/repos/{repo}/events?per_page=30"
        req = self._build_request(url, repo, etag)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                new_etag = resp.headers.get("ETag")
                data = json.loads(resp.read().decode("utf-8"))
                return resp.status, data, new_etag
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, [], etag
            log.warning("GitHub events API error for %s: HTTP %d %s", repo, e.code, e.reason)
            return e.code, [], None
        except Exception as exc:
            log.warning("Failed to fetch events for %s: %s", repo, exc)
            return 0, [], None

    def fetch_open_prs(self, repo: str) -> list[dict[str, Any]]:
        """Fetch all open PRs for a repo."""
        url = f"https://api.github.com/repos/{repo}/pulls?state=open&per_page=50"
        req = self._build_request(url, repo)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            log.warning("Failed to fetch open PRs for %s: %s", repo, exc)
            return []

    def fetch_pr_details(self, repo: str, pr_number: int) -> dict[str, Any] | None:
        url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
        req = self._build_request(url, repo)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            log.warning("Failed to fetch PR #%d details for %s: %s", pr_number, repo, exc)
            return None

    def fetch_check_runs(self, repo: str, ref: str) -> list[dict[str, Any]]:
        url = f"https://api.github.com/repos/{repo}/commits/{ref}/check-runs"
        req = self._build_request(url, repo)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("check_runs", [])
        except Exception as exc:
            log.warning("Failed to fetch check runs for %s@%s: %s", repo, ref[:8], exc)
            return []

    def fetch_review_comments(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/comments"
        req = self._build_request(url, repo)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            log.warning("Failed to fetch review comments for %s#%d: %s", repo, pr_number, exc)
            return []

    def fetch_team_members(self, org: str, team_slug: str) -> set[str]:
        url = f"https://api.github.com/orgs/{org}/teams/{team_slug}/members?per_page=100"
        req = self._build_request(url, org)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return {item.get("login", "") for item in data if item.get("login")}
        except Exception as exc:
            log.warning("Failed to fetch team members for %s/%s: %s", org, team_slug, exc)
            return set()


class SwarmerDispatcher:
    """Dispatches sessions to Swarmer REST API."""

    def __init__(self, api_url: str, api_token: str):
        self.api_url = api_url.rstrip("/")
        self.api_token = api_token

    def _build_request(self, path: str, method: str = "GET", body: dict | None = None) -> urllib.request.Request:
        url = f"{self.api_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        if self.api_token:
            req.add_header("Authorization", f"Bearer {self.api_token}")
        return req

    async def launch_session(
        self,
        workspace_id: int,
        session_id: int,
        pr_context: dict[str, Any] | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """Launch a Swarm session via POST /api/v1/workspaces/{ws_id}/sessions/{sid}/launch.

        Returns (success: bool, detail_or_error: str, response_data: dict | None).
        """
        path = f"/api/v1/workspaces/{workspace_id}/sessions/{session_id}/launch"
        req = self._build_request(path, method="POST", body=pr_context)

        def _do_post():
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    return True, "Session launched successfully", data
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="ignore")
                return False, f"HTTP {e.code}: {err_body}", None
            except Exception as exc:
                return False, str(exc), None

        return await asyncio.to_thread(_do_post)


def extract_repo_from_url(repo_url: str) -> str:
    """Convert 'https://github.com/owner/repo.git' or 'git@github.com:owner/repo' to 'owner/repo'."""
    url = repo_url.strip()
    if url.endswith(".git"):
        url = url[:-4]
    if "github.com/" in url:
        return url.split("github.com/")[1]
    if "github.com:" in url:
        return url.split("github.com:")[1]
    return url


def resolve_event_scoped_repos(triggers: list[TriggerConfig]) -> set[str]:
    """Resolve the set of watched repositories from enabled EVENT triggers only.

    Repos attached solely to cron triggers are excluded.
    """
    watched = set()
    for tr in triggers:
        if tr.enabled and tr.trigger_type.lower() == "event":
            for r in tr.repos:
                normalized = extract_repo_from_url(r)
                if normalized:
                    watched.add(normalized)
    return watched


class SwarmPRWatcher:
    """Main watcher daemon orchestrating event polling, classification, and session dispatch."""

    def __init__(self, config: WatcherConfig):
        self.config = config
        self.store = StateStore(config.db_path)
        self.gh = GitHubClient(token_map=config.tokens)
        token = config.api_token or os.environ.get(config.api_token_env, "")
        self.dispatcher = SwarmerDispatcher(config.api_url, token)
        self.active_repos: set[str] = set()
        self._cached_team_members: dict[str, set[str]] = {}
        self.refresh_watched_repos()

    def refresh_watched_repos(self) -> set[str]:
        """Recompute the set of watched repositories based on enabled event triggers."""
        new_set = resolve_event_scoped_repos(self.config.triggers)
        if new_set != self.active_repos:
            added = new_set - self.active_repos
            removed = self.active_repos - new_set
            log.info("Watched repos refreshed: %d active repos (+%d, -%d)", len(new_set), len(added), len(removed))
            for r in removed:
                self.store.remove_etag(r)
            self.active_repos = new_set
        return self.active_repos

    def _get_team_members(self, org: str, team_slug: str) -> set[str]:
        key = f"{org}/{team_slug}"
        if key not in self._cached_team_members:
            self._cached_team_members[key] = self.gh.fetch_team_members(org, team_slug)
        return self._cached_team_members[key]

    def build_pr_state(self, repo: str, raw_pr: dict[str, Any]) -> PRState:
        """Enrich raw PR data with check runs and review comment analysis."""
        pr_number = raw_pr["number"]
        head = raw_pr.get("head", {})
        base = raw_pr.get("base", {})
        head_sha = head.get("sha", "")
        head_ref = head.get("ref", "")
        base_ref = base.get("ref", "")
        user = raw_pr.get("user", {})
        author_login = user.get("login", "")
        author_association = raw_pr.get("author_association", "NONE")
        is_draft = raw_pr.get("draft", False)
        title = raw_pr.get("title", "")
        body = raw_pr.get("body", "") or ""
        mergeable_state = raw_pr.get("mergeable_state", "unknown")

        # Fork detection
        is_fork = False
        fork_owner = ""
        head_repo = head.get("repo")
        if head_repo and head_repo.get("fork"):
            is_fork = True
            fork_owner = head_repo.get("owner", {}).get("login", "")

        # Labels
        labels = {lbl.get("name") for lbl in raw_pr.get("labels", []) if lbl.get("name")}

        # Fetch Checks
        check_runs = self.gh.fetch_check_runs(repo, head_sha)
        check_state = normalize_ci_checks(check_runs)

        # Fetch Comments
        comments = self.gh.fetch_review_comments(repo, pr_number)
        unresolved_count = 0
        coderabbit_count = 0
        for c in comments:
            # PR review comments
            c_user = c.get("user", {}).get("login", "")
            if c_user.lower().startswith("coderabbit"):
                coderabbit_count += 1
            unresolved_count += 1

        return PRState(
            repo=repo,
            pr_number=pr_number,
            title=title,
            body=body,
            author_login=author_login,
            author_association=author_association,
            is_draft=is_draft,
            head_sha=head_sha,
            head_ref=head_ref,
            base_ref=base_ref,
            mergeable_state=mergeable_state,
            is_fork=is_fork,
            fork_owner=fork_owner,
            labels=labels,
            unresolved_review_comments=unresolved_count,
            coderabbit_unresolved_comments=coderabbit_count,
            created_at=parse_iso_datetime(raw_pr.get("created_at")),
            updated_at=parse_iso_datetime(raw_pr.get("updated_at")),
            check_state=check_state,
            raw_payload=raw_pr,
        )

    def find_matching_trigger(self, pr: PRState, action: PRAction) -> TriggerConfig | None:
        """Find the matching enabled event trigger for a given repo and action."""
        for tr in self.config.triggers:
            if not tr.enabled or tr.trigger_type.lower() != "event":
                continue

            normalized_repos = {extract_repo_from_url(r) for r in tr.repos}
            if pr.repo not in normalized_repos:
                continue

            # Scope matching
            author_lower = pr.author_login.lower()
            is_self = author_lower in {a.lower() for a in self.config.fix_authors}
            is_bot = is_bot_author(pr.author_login, self.config.bot_logins)

            if tr.author_scope == "self" and not is_self:
                continue
            if tr.author_scope == "team" and (is_self or is_bot):
                continue
            if tr.author_scope == "bots" and not is_bot:
                continue
            # tr.author_scope == "all" matches any author (self, team, bots)

            # Action condition matching
            if action == PRAction.FIX and tr.condition in ("ci_fail_or_conflict", "review_comments", "any_actionable"):
                return tr
            if action == PRAction.REVIEW and tr.condition in ("new_pr_or_commit", "any_actionable"):
                return tr

        return None

    async def evaluate_and_dispatch_pr(self, pr: PRState, dry_run: bool = False) -> tuple[bool, str]:
        """Evaluate a single PR and dispatch if actionable and circuit breaker permits."""
        org_team_members = None
        if self.config.trust_policy.strategy == TrustStrategy.GITHUB_TEAM and self.config.trust_policy.team_slug:
            org = pr.repo.split("/")[0]
            org_team_members = self._get_team_members(org, self.config.trust_policy.team_slug)

        action, reason = classify_pr_action(
            pr,
            fix_authors=self.config.fix_authors,
            bot_logins=self.config.bot_logins,
            trust_policy=self.config.trust_policy,
            org_team_members=org_team_members,
            quiet_period_seconds=self.config.quiet_period_seconds,
        )

        if action in (PRAction.IGNORE, PRAction.AUTO_MERGE_DEFER):
            log.debug("PR %s#%d: %s (%s)", pr.repo, pr.pr_number, action.value, reason)
            return False, reason

        # Circuit Breaker & Dedup Check
        existing = self.store.get_action_state(pr.repo, pr.pr_number, pr.head_sha, action.value)
        if existing:
            status = existing["status"]
            attempts = existing["attempts"]
            if status == "blocked":
                return False, f"Circuit breaker tripped: max attempts ({attempts}) reached for {pr.head_sha[:8]}"
            if status in ("dispatched", "in_progress"):
                return False, f"Action '{action.value}' already in progress for {pr.head_sha[:8]}"

        # Trigger resolution
        trigger = self.find_matching_trigger(pr, action)
        if not trigger:
            return False, f"No matching event trigger found for {action.value} on {pr.repo}"

        log.info(
            "ACTIONABLE PR DETECTED: %s#%d (author=%s, sha=%s, action=%s, reason=%s)",
            pr.repo,
            pr.pr_number,
            pr.author_login,
            pr.head_sha[:8],
            action.value,
            reason,
        )

        if dry_run:
            log.info("[DRY-RUN] Would dispatch session %d in workspace %d", trigger.session_id, trigger.workspace_id)
            return True, f"[DRY-RUN] Dispatched session {trigger.session_id}"

        # Dispatch to Swarmer API
        pr_payload = {
            "repo": pr.repo,
            "pr_number": pr.pr_number,
            "head_sha": pr.head_sha,
            "head_ref": pr.head_ref,
            "base_ref": pr.base_ref,
            "action": action.value,
            "title": pr.title,
        }

        success, detail, data = await self.dispatcher.launch_session(
            workspace_id=trigger.workspace_id,
            session_id=trigger.session_id,
            pr_context=pr_payload,
        )

        if success:
            attempts = self.store.record_dispatch(
                repo=pr.repo,
                pr_number=pr.pr_number,
                head_sha=pr.head_sha,
                action=action.value,
                session_id=trigger.session_id,
                status="dispatched",
            )
            if attempts >= self.config.max_fix_attempts:
                self.store.record_dispatch(
                    repo=pr.repo,
                    pr_number=pr.pr_number,
                    head_sha=pr.head_sha,
                    action=action.value,
                    session_id=trigger.session_id,
                    status="blocked",
                    error="Max fix attempts reached",
                )
            return True, f"Dispatched session {trigger.session_id} (attempt {attempts})"
        else:
            log.error("Failed to dispatch session %d: %s", trigger.session_id, detail)
            # Record failed dispatch without consuming retry budget permanently
            self.store.record_dispatch(
                repo=pr.repo,
                pr_number=pr.pr_number,
                head_sha=pr.head_sha,
                action=action.value,
                session_id=trigger.session_id,
                status="failed",
                error=detail,
            )
            return False, f"Dispatch failed: {detail}"

    async def scan_repo_prs(self, repo: str, dry_run: bool = False) -> int:
        """Scan open PRs in a repository and evaluate them. Returns count of dispatched actions."""
        raw_prs = await asyncio.to_thread(self.gh.fetch_open_prs, repo)
        dispatched = 0
        for raw_pr in raw_prs:
            pr_number = raw_pr["number"]
            # Fetch detailed PR to obtain mergeable_state
            detailed = await asyncio.to_thread(self.gh.fetch_pr_details, repo, pr_number)
            if not detailed:
                detailed = raw_pr
            pr_state = self.build_pr_state(repo, detailed)
            success, reason = await self.evaluate_and_dispatch_pr(pr_state, dry_run=dry_run)
            if success:
                dispatched += 1
        return dispatched

    async def poll_repo_events(self, repo: str, dry_run: bool = False) -> bool:
        """Poll events for a single repo using ETag. Returns True if re-scan occurred."""
        etag = self.store.get_etag(repo)
        status, events, new_etag = await asyncio.to_thread(self.gh.fetch_events, repo, etag)

        if status == 304:
            log.debug("Repo %s: 304 Not Modified (0 rate-limit cost)", repo)
            return False

        if status == 200:
            if new_etag:
                self.store.save_etag(repo, new_etag)
            log.info("Repo %s: 200 OK — received %d event(s). Re-scanning open PRs...", repo, len(events))
            await self.scan_repo_prs(repo, dry_run=dry_run)
            return True

        return False

    async def run_sweep(self, dry_run: bool = False) -> None:
        """Run periodic full sweep across all event-scoped repos."""
        log.info("Starting periodic full sweep across %d event-scoped repos...", len(self.active_repos))
        for repo in list(self.active_repos):
            try:
                await self.scan_repo_prs(repo, dry_run=dry_run)
            except Exception as exc:
                log.warning("Error during sweep of %s: %s", repo, exc)

    async def run_loop(self, once: bool = False, dry_run: bool = False) -> None:
        """Run the main watcher loop."""
        log.info(
            "Swarm PR Watcher started. Polling interval: %0.1fs, Sweep: %0.1fs, Monitored event repos: %d",
            self.config.poll_interval_seconds,
            self.config.sweep_interval_seconds,
            len(self.active_repos),
        )

        last_sweep = time.time()
        last_refresh = time.time()

        while True:
            # Periodic refresh of watched repo set
            if time.time() - last_refresh >= self.config.repo_refresh_interval_seconds:
                self.refresh_watched_repos()
                last_refresh = time.time()

            # Poll active event-scoped repos
            for repo in list(self.active_repos):
                try:
                    await self.poll_repo_events(repo, dry_run=dry_run)
                except Exception as exc:
                    log.warning("Error polling repo %s: %s", repo, exc)

            # Periodic sweep
            if time.time() - last_sweep >= self.config.sweep_interval_seconds:
                await self.run_sweep(dry_run=dry_run)
                last_sweep = time.time()

            if once:
                break

            await asyncio.sleep(self.config.poll_interval_seconds)


def load_config(config_path: str | Path) -> WatcherConfig:
    path = Path(config_path)
    if not path.exists():
        log.warning("Config file %s not found, using default configuration", path)
        return WatcherConfig()

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Parse trust policy
    tp_raw = data.get("trust_policy", {})
    tp_strat = tp_raw.get("strategy", "org_and_collaborators")
    trust_policy = TrustPolicy(
        strategy=TrustStrategy(tp_strat) if tp_strat in TrustStrategy.__members__.values() else TrustStrategy.ORG_AND_COLLABORATORS,
        allowlist=set(tp_raw.get("allowlist", [])),
        team_slug=tp_raw.get("team_slug", ""),
        trusted_label=tp_raw.get("trusted_label", "ok-to-review"),
    )

    # Parse triggers
    triggers = []
    for tr in data.get("triggers", []):
        triggers.append(
            TriggerConfig(
                id=tr.get("id", ""),
                name=tr.get("name", ""),
                trigger_type=tr.get("trigger_type", "event"),
                condition=tr.get("condition", "any_actionable"),
                author_scope=tr.get("author_scope", "all"),
                workspace_id=tr.get("workspace_id", 1),
                session_id=tr.get("session_id", 1),
                repos=tr.get("repos", []),
                prompt_file=tr.get("prompt_file", ""),
                instruction_prompt=tr.get("instruction_prompt", ""),
                enabled=tr.get("enabled", True),
            )
        )

    return WatcherConfig(
        api_url=data.get("api_url", "http://localhost:8090"),
        api_token=data.get("api_token", ""),
        api_token_env=data.get("api_token_env", "SWARM_API_TOKEN"),
        poll_interval_seconds=float(data.get("poll_interval_seconds", 30.0)),
        sweep_interval_seconds=float(data.get("sweep_interval_seconds", 1800.0)),
        repo_refresh_interval_seconds=float(data.get("repo_refresh_interval_seconds", 60.0)),
        db_path=data.get("db_path", "data/swarm_watcher_state.db"),
        fix_authors=set(data.get("fix_authors", ["jpacker"])),
        bot_logins=set(data.get("bot_logins", DEFAULT_BOT_LOGINS)),
        trust_policy=trust_policy,
        tokens=data.get("tokens", {}),
        triggers=triggers,
        max_fix_attempts=int(data.get("max_fix_attempts", 3)),
        quiet_period_seconds=float(data.get("quiet_period_seconds", 90.0)),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Swarm PR Events Watcher & Session Dispatcher")
    parser.add_argument("--config", "-c", default="config/swarm-watcher.json", help="Path to JSON configuration file")
    parser.add_argument("--once", action="store_true", help="Run a single polling cycle and exit")
    parser.add_argument("--sweep", action="store_true", help="Run a full sweep once and exit")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate PRs without dispatching Swarm sessions")
    parser.add_argument("--poll-interval", type=float, help="Override polling interval in seconds")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose debug logging")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = load_config(args.config)
    if args.poll_interval:
        config.poll_interval_seconds = args.poll_interval

    watcher = SwarmPRWatcher(config)

    if args.sweep:
        asyncio.run(watcher.run_sweep(dry_run=args.dry_run))
    else:
        asyncio.run(watcher.run_loop(once=args.once, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
