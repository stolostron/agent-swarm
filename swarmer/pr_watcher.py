from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from typing import Any

import httpx

from swarmer.config import settings
from swarmer.pr_state import (
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
from swarmer.database import get_db
from swarmer.models.session import Session
from swarmer.models.session_schedule import SessionSchedule
from swarmer.pr_watcher_store import (
    get_etag,
    is_blocked,
    prune_etags,
    record_dispatch,
    resolve_event_triggers,
    save_etag,
)

log = logging.getLogger(__name__)

_watcher_task: asyncio.Task | None = None


def start_pr_watcher() -> None:
    global _watcher_task
    stop_pr_watcher()
    if not settings.pr_watcher_enabled:
        log.info("pr-watcher: disabled by settings (PR_WATCHER_ENABLED=false)")
        return
    _watcher_task = asyncio.create_task(_pr_watcher_loop(), name="pr-watcher")
    log.info("pr-watcher: started background loop (interval=%ds)", settings.pr_watcher_poll_interval)


def stop_pr_watcher() -> None:
    global _watcher_task
    if _watcher_task and not _watcher_task.done():
        _watcher_task.cancel()
    _watcher_task = None


async def shutdown() -> None:
    task = _watcher_task
    stop_pr_watcher()
    if task:
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _resolve_github_token_for_workspace_repo(
    workspace_id: int,
    repo: str,
    sched_sessions: list[tuple[SessionSchedule, Session]],
    db,
) -> str | None:
    """Resolve a GitHub token to use for polling a repository in a specific workspace.

    Order of precedence:
      1. Explicit GitHub PAT attached to any session in this workspace watching this repo.
      2. Workspace GitHub App IAT minted for this workspace.
      3. Organization env var GH_TOKEN_<ORG> or GITHUB_TOKEN / GH_TOKEN.
    """
    for _sched, session in sched_sessions:
        if session.github_pat and session.github_pat.pat:
            return session.github_pat.pat

    if workspace_id:
        try:
            from swarmer.github_app import get_workspace_github_app
            from swarmer.github_auth import mint_installation_token

            app = await get_workspace_github_app(workspace_id, db)
            if app:
                token = await mint_installation_token(app)
                if token:
                    return token
        except Exception:
            pass

    org = repo.split("/")[0] if "/" in repo else repo
    org_normalized = org.replace("-", "_").upper()
    env_key = f"GH_TOKEN_{org_normalized}"
    if env_key in os.environ:
        return os.environ[env_key]
    if "GITHUB_TOKEN" in os.environ:
        return os.environ["GITHUB_TOKEN"]
    if "GH_TOKEN" in os.environ:
        return os.environ["GH_TOKEN"]

    return None


async def _fetch_repo_events(
    client: httpx.AsyncClient, repo: str, etag: str | None, token: str | None
) -> tuple[int, list[dict[str, Any]], str | None]:
    """Poll GitHub Events API with ETag. Returns (status_code, events_list, new_etag)."""
    url = f"https://api.github.com/repos/{repo}/events?per_page=30"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Swarmer-PR-Watcher/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if etag:
        headers["If-None-Match"] = etag

    try:
        resp = await client.get(url, headers=headers, timeout=15)
        if resp.status_code == 304:
            return 304, [], etag
        if resp.is_success:
            new_etag = resp.headers.get("ETag")
            return resp.status_code, resp.json(), new_etag
        log.warning("pr-watcher: GitHub events API returned %d for %s: %s", resp.status_code, repo, resp.text[:200])
        return resp.status_code, [], None
    except Exception as exc:
        log.warning("pr-watcher: failed to poll events for %s: %s", repo, exc)
        return 0, [], None


async def _fetch_open_prs(
    client: httpx.AsyncClient, repo: str, token: str | None
) -> list[dict[str, Any]]:
    """Fetch open pull requests for a repository."""
    url = f"https://api.github.com/repos/{repo}/pulls?state=open&per_page=30"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Swarmer-PR-Watcher/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = await client.get(url, headers=headers, timeout=15)
        if resp.is_success:
            return resp.json()
        return []
    except Exception as exc:
        log.warning("pr-watcher: failed to fetch open PRs for %s: %s", repo, exc)
        return []


async def _fetch_pr_details(
    client: httpx.AsyncClient, repo: str, pr_number: int, token: str | None
) -> dict[str, Any]:
    """Fetch detailed single pull request metadata (including mergeable_state)."""
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Swarmer-PR-Watcher/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = await client.get(url, headers=headers, timeout=15)
        if resp.is_success:
            data = resp.json()
            if data.get("mergeable_state") == "unknown":
                await asyncio.sleep(1.0)
                resp2 = await client.get(url, headers=headers, timeout=15)
                if resp2.is_success:
                    data = resp2.json()
            return data
        return {}
    except Exception as exc:
        log.warning("pr-watcher: failed to fetch PR #%d details for %s: %s", pr_number, repo, exc)
        return {}


async def _fetch_check_runs(
    client: httpx.AsyncClient, repo: str, head_sha: str, token: str | None
) -> list[dict[str, Any]]:
    """Fetch check runs for a commit SHA."""
    url = f"https://api.github.com/repos/{repo}/commits/{head_sha}/check-runs"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Swarmer-PR-Watcher/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = await client.get(url, headers=headers, timeout=15)
        if resp.is_success:
            return resp.json().get("check_runs", [])
        return []
    except Exception:
        return []


async def _fetch_review_comments(
    client: httpx.AsyncClient, repo: str, pr_number: int, token: str | None
) -> list[dict[str, Any]]:
    """Fetch review comments on a pull request (REST fallback)."""
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/comments"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Swarmer-PR-Watcher/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = await client.get(url, headers=headers, timeout=15)
        if resp.is_success:
            return resp.json()
        return []
    except Exception:
        return []


async def _fetch_reviews_rest(
    client: httpx.AsyncClient, repo: str, pr_number: int, token: str | None
) -> list[dict[str, Any]]:
    """Fetch PR reviews via REST."""
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Swarmer-PR-Watcher/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = await client.get(url, headers=headers, timeout=15)
        if resp.is_success:
            return resp.json()
        return []
    except Exception:
        return []


async def _fetch_reviews_and_threads(
    client: httpx.AsyncClient, repo: str, pr_number: int, head_sha: str, token: str | None
) -> tuple[int, int, bool]:
    """Fetch review threads and reviews.

    Returns:
        (unresolved_comments_count, coderabbit_unresolved_count, has_agent_review_on_head)
    """
    if "/" not in repo:
        return 0, 0, False
    owner, name = repo.split("/", 1)

    if token:
        gql_query = """
        query($owner: String!, $name: String!, $number: Int!) {
          repository(owner: $owner, name: $name) {
            pullRequest(number: $number) {
              reviewThreads(first: 50) {
                nodes {
                  isResolved
                  isOutdated
                  comments(first: 10) {
                    nodes {
                      author { login }
                      body
                    }
                  }
                }
              }
              reviews(last: 20) {
                nodes {
                  author { login }
                  state
                  commit { oid }
                }
              }
            }
          }
        }
        """
        try:
            gql_resp = await client.post(
                "https://api.github.com/graphql",
                json={"query": gql_query, "variables": {"owner": owner, "name": name, "number": pr_number}},
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "Swarmer-PR-Watcher/1.0",
                },
                timeout=15,
            )
            if gql_resp.is_success:
                pr_data = gql_resp.json().get("data", {}).get("repository", {}).get("pullRequest", {})
                if pr_data:
                    unresolved_count = 0
                    cr_count = 0
                    for thread in pr_data.get("reviewThreads", {}).get("nodes", []):
                        if not thread.get("isResolved") and not thread.get("isOutdated"):
                            unresolved_count += 1
                            comments = thread.get("comments", {}).get("nodes", [])
                            if any((c.get("author", {}).get("login") or "").lower().startswith("coderabbit") for c in comments):
                                cr_count += 1

                    has_agent_review = False
                    for rev in pr_data.get("reviews", {}).get("nodes", []):
                        rev_commit = (rev.get("commit", {}) or {}).get("oid", "")
                        rev_author = ((rev.get("author", {}) or {}).get("login") or "").lower()
                        rev_state = rev.get("state", "")
                        if rev_commit == head_sha and rev_state in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED"):
                            if is_bot_author(rev_author) or rev_author in DEFAULT_BOT_LOGINS:
                                has_agent_review = True
                                break
                    return unresolved_count, cr_count, has_agent_review
        except Exception as exc:
            log.debug("pr-watcher: GraphQL review query failed for %s#%d: %s", repo, pr_number, exc)

    # REST fallback
    comments = await _fetch_review_comments(client, repo, pr_number, token)
    unresolved_count = len(comments)
    cr_count = sum(1 for c in comments if (c.get("user", {}).get("login") or "").lower().startswith("coderabbit"))

    reviews = await _fetch_reviews_rest(client, repo, pr_number, token)
    has_agent_review = any(
        r.get("commit_id") == head_sha and r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED")
        and (is_bot_author(r.get("user", {}).get("login", "")) or r.get("user", {}).get("login", "").lower() in DEFAULT_BOT_LOGINS)
        for r in reviews
    )
    return unresolved_count, cr_count, has_agent_review


async def _fetch_label_events(
    client: httpx.AsyncClient, repo: str, pr_number: int, token: str | None
) -> list[dict[str, Any]]:
    """Fetch issue events for label auditing with resolved actor associations."""
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/events?per_page=50"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Swarmer-PR-Watcher/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = await client.get(url, headers=headers, timeout=15)
        if not resp.is_success:
            return []
        events = [e for e in resp.json() if e.get("event") == "labeled"]
        for ev in events:
            actor = ev.get("actor", {}) or {}
            actor_login = actor.get("login")
            if actor_login and not ev.get("author_association"):
                perm_url = f"https://api.github.com/repos/{repo}/collaborators/{actor_login}/permission"
                try:
                    p_resp = await client.get(perm_url, headers=headers, timeout=10)
                    if p_resp.is_success:
                        perm = p_resp.json().get("permission", "").lower()
                        if perm in ("admin", "write", "maintain"):
                            ev["author_association"] = "COLLABORATOR"
                        elif perm in ("triage", "read"):
                            ev["author_association"] = "CONTRIBUTOR"
                except Exception:
                    pass
        return events
    except Exception:
        return []


async def _build_pr_state(
    client: httpx.AsyncClient, repo: str, raw_pr: dict[str, Any], token: str | None
) -> tuple[PRState, list[dict[str, Any]]]:
    pr_number = raw_pr["number"]

    # Fetch detailed PR for accurate mergeable_state
    detail = await _fetch_pr_details(client, repo, pr_number, token)
    full_pr = {**raw_pr, **detail} if detail else raw_pr

    head = full_pr.get("head", {})
    base = full_pr.get("base", {})
    head_sha = head.get("sha", "")
    head_ref = head.get("ref", "")
    base_ref = base.get("ref", "")
    user = full_pr.get("user", {})
    author_login = user.get("login", "")
    author_association = full_pr.get("author_association", "NONE")
    is_draft = full_pr.get("draft", False)
    title = full_pr.get("title", "")
    body = full_pr.get("body", "") or ""
    mergeable_state = full_pr.get("mergeable_state") or "unknown"

    # Fork detection & maintainer push capability
    is_fork = False
    fork_owner = ""
    head_repo = head.get("repo")
    if head_repo and head_repo.get("fork"):
        is_fork = True
        fork_owner = head_repo.get("owner", {}).get("login", "")

    labels = {lbl.get("name") for lbl in full_pr.get("labels", []) if lbl.get("name")}

    check_runs = await _fetch_check_runs(client, repo, head_sha, token)
    check_state = normalize_ci_checks(check_runs)

    unresolved_count, cr_count, has_agent_review = await _fetch_reviews_and_threads(
        client, repo, pr_number, head_sha, token
    )

    label_events: list[dict[str, Any]] = []
    if "ok-to-review" in labels:
        label_events = await _fetch_label_events(client, repo, pr_number, token)

    pr_state = PRState(
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
        coderabbit_unresolved_comments=cr_count,
        has_agent_review_on_head=has_agent_review,
        created_at=parse_iso_datetime(full_pr.get("created_at")),
        updated_at=parse_iso_datetime(full_pr.get("updated_at")),
        check_state=check_state,
        raw_payload=full_pr,
    )
    return pr_state, label_events


def _match_trigger_for_pr(
    pr: PRState,
    action: PRAction,
    sched_sessions: list[tuple[SessionSchedule, Session]],
) -> tuple[SessionSchedule, Session] | None:
    """Find a matching (SessionSchedule, Session) for an actionable PR."""
    for sched, session in sched_sessions:
        if not sched.enabled or sched.trigger_type != "event":
            continue

        # Check author scope
        author_lower = pr.author_login.lower()
        fix_logins = sched.fix_author_logins
        is_self = author_lower in fix_logins if fix_logins else False
        is_bot = is_bot_author(pr.author_login, set(DEFAULT_BOT_LOGINS))

        if sched.author_scope == "self" and not is_self:
            continue
        if sched.author_scope == "team" and (is_self or is_bot):
            continue
        if sched.author_scope == "bots" and not is_bot:
            continue
        # sched.author_scope == "all" matches any author

        # Check action condition
        if action == PRAction.FIX and sched.event_condition in ("ci_fail_or_conflict", "review_comments", "any_actionable"):
            return sched, session
        if action == PRAction.REVIEW and sched.event_condition in ("new_pr_or_commit", "any_actionable"):
            return sched, session

    return None


async def _evaluate_and_dispatch_prs(
    client: httpx.AsyncClient,
    repo: str,
    sched_sessions: list[tuple[SessionSchedule, Session]],
    token: str | None,
    db,
) -> None:
    """Scan open PRs in repo, evaluate against trigger conditions, and dispatch sessions."""
    open_prs = await _fetch_open_prs(client, repo, token)
    if not open_prs:
        return

    for raw_pr in open_prs:
        pr_state, label_events = await _build_pr_state(client, repo, raw_pr, token)

        # Collect configured fix_authors across all triggers for this repo
        all_fix_authors: set[str] = set()
        for sched, _sess in sched_sessions:
            all_fix_authors.update(sched.fix_author_logins)

        action, reason = classify_pr_action(
            pr_state,
            fix_authors=all_fix_authors,
            bot_logins=set(DEFAULT_BOT_LOGINS),
            trust_policy=TrustPolicy(strategy=TrustStrategy.ORG_AND_COLLABORATORS),
            label_events=label_events,
            quiet_period_seconds=float(settings.pr_watcher_debounce_seconds),
        )

        if action in (PRAction.IGNORE, PRAction.AUTO_MERGE_DEFER):
            log.debug("pr-watcher: PR %s#%d -> %s (%s)", repo, pr_state.pr_number, action.value, reason)
            continue

        # Check circuit breaker and deduplication
        if await is_blocked(db, repo, pr_state.pr_number, pr_state.head_sha, action.value):
            log.debug(
                "pr-watcher: PR %s#%d [%s] already in-flight, completed, or blocked on head SHA %s",
                repo, pr_state.pr_number, action.value, pr_state.head_sha[:8],
            )
            continue

        match = _match_trigger_for_pr(pr_state, action, sched_sessions)
        if not match:
            log.debug("pr-watcher: PR %s#%d [%s] matched no active trigger condition", repo, pr_state.pr_number, action.value)
            continue

        sched, session = match

        # Per-session serialization: if session is already active, skip this cycle (will re-evaluate next loop)
        if session.is_active:
            log.info(
                "pr-watcher: session %d (%s) is already %s — deferring PR %s#%d dispatch",
                session.id, session.name, session.phase, repo, pr_state.pr_number,
            )
            continue

        ws = session.workspace
        if not ws:
            continue

        # Prepare event context
        event_ctx = {
            "trigger_type": "event",
            "schedule_id": sched.id,
            "schedule_label": sched.label or sched.trigger_label,
            "repo": repo,
            "pr_number": pr_state.pr_number,
            "head_sha": pr_state.head_sha,
            "head_ref": pr_state.head_ref,
            "base_ref": pr_state.base_ref,
            "title": pr_state.title,
            "author": pr_state.author_login,
            "action": action.value,
            "cause": reason,
        }

        # Attempt dispatch
        try:
            from swarmer.routers.sessions import _do_launch

            session.mode = "prompt"
            session.active_schedule_id = sched.id
            session.event_context = json.dumps(event_ctx)
            await db.commit()

            log.info(
                "pr-watcher: dispatching session %d (%s) for PR %s#%d [%s: %s]",
                session.id, session.name, repo, pr_state.pr_number, action.value, reason,
            )
            await _do_launch(session, ws, db)

            attempts = await record_dispatch(
                db,
                repo=repo,
                pr_number=pr_state.pr_number,
                head_sha=pr_state.head_sha,
                action=action.value,
                session_id=session.id,
                status="dispatched",
            )
            log.info("pr-watcher: session %d launched (phase=%s, attempt=%d)", session.id, session.phase, attempts)

        except Exception as exc:
            log.exception("pr-watcher: failed to launch session %d for PR %s#%d: %s", session.id, repo, pr_state.pr_number, exc)
            attempts = await record_dispatch(
                db,
                repo=repo,
                pr_number=pr_state.pr_number,
                head_sha=pr_state.head_sha,
                action=action.value,
                session_id=session.id,
                status="failed",
                error=str(exc),
            )
            if attempts >= settings.pr_watcher_max_fix_attempts:
                await record_dispatch(
                    db,
                    repo=repo,
                    pr_number=pr_state.pr_number,
                    head_sha=pr_state.head_sha,
                    action=action.value,
                    session_id=session.id,
                    status="blocked",
                    error=f"Max dispatch attempts ({attempts}) reached",
                )


async def _pr_watcher_loop() -> None:
    """Main async background polling loop."""
    log.info("pr-watcher: background poller initialized")
    last_sweep = 0.0

    async with httpx.AsyncClient() as client:
        while True:
            try:
                async for db in get_db():
                    triggers_by_workspace = await resolve_event_triggers(db)
                    all_active_repos = {
                        repo
                        for repo_map in triggers_by_workspace.values()
                        for repo in repo_map.keys()
                    }

                    if all_active_repos:
                        await prune_etags(db, all_active_repos)

                    now_ts = datetime.now(timezone.utc).timestamp()
                    is_sweep_due = (now_ts - last_sweep) >= settings.pr_watcher_sweep_interval

                    for ws_id, repo_map in triggers_by_workspace.items():
                        for repo, sched_sessions in repo_map.items():
                            try:
                                token = await _resolve_github_token_for_workspace_repo(
                                    ws_id, repo, sched_sessions, db
                                )
                                cached_etag = await get_etag(db, repo)

                                if is_sweep_due:
                                    # Periodic sweep across active event repos
                                    await _evaluate_and_dispatch_prs(client, repo, sched_sessions, token, db)
                                else:
                                    status, events, new_etag = await _fetch_repo_events(
                                        client, repo, cached_etag, token
                                    )
                                    if status == 200:
                                        if new_etag:
                                            await save_etag(db, repo, new_etag)
                                        log.info(
                                            "pr-watcher: ws=%d %s 200 OK (%d events) — evaluating PRs",
                                            ws_id, repo, len(events),
                                        )
                                        await _evaluate_and_dispatch_prs(client, repo, sched_sessions, token, db)
                            except Exception as repo_err:
                                log.warning(
                                    "pr-watcher: error processing ws=%d repo %s: %s",
                                    ws_id, repo, repo_err,
                                )

                    if is_sweep_due:
                        last_sweep = now_ts

                    break  # break out of async generator
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("pr-watcher: unhandled error in watcher loop cycle")

            await asyncio.sleep(max(5, settings.pr_watcher_poll_interval))
