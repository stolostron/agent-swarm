from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from swarmer.config import settings
from swarmer.database import get_db
from swarmer.models.session import Session
from swarmer.models.session_schedule import SessionSchedule
from swarmer.pr_state import (
    DEFAULT_BOT_LOGINS,
    EventTrigger,
    PRState,
    TrustPolicy,
    TrustStrategy,
    evaluate_author_trust,
    evaluate_pr_conditions,
    is_bot_author,
    normalize_ci_checks,
    parse_iso_datetime,
)
from swarmer.pr_watcher_store import (
    get_comment_dispatch,
    get_etag,
    has_event_receipt,
    is_blocked,
    list_due_comment_dispatches,
    list_queued_dispatches,
    prune_etags,
    record_dispatch,
    record_event_receipt,
    resolve_event_triggers,
    save_etag,
    update_comment_dispatch,
    upsert_comment_dispatch,
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


async def _fetch_commit_author(
    client: httpx.AsyncClient, repo: str, sha: str, token: str | None
) -> str:
    """Resolve the GitHub login for a commit author, when GitHub can map it."""
    if not sha:
        return ""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Swarmer-PR-Watcher/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = await client.get(f"https://api.github.com/repos/{repo}/commits/{sha}", headers=headers, timeout=15)
        if resp.is_success:
            return ((resp.json().get("author") or {}).get("login") or "").strip()
    except Exception:
        pass
    return ""


async def _fetch_actor_association(
    client: httpx.AsyncClient, repo: str, login: str, token: str | None
) -> str:
    """Resolve a GitHub user's repository association for event routing."""
    if not login:
        return ""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "Swarmer-PR-Watcher/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = await client.get(
            f"https://api.github.com/repos/{repo}/collaborators/{login}/permission",
            headers=headers,
            timeout=15,
        )
        if resp.is_success:
            permission = (resp.json().get("permission") or "").lower()
            return {"admin": "OWNER", "maintain": "MEMBER", "push": "COLLABORATOR", "triage": "COLLABORATOR"}.get(
                permission, "NONE"
            )
    except Exception:
        pass
    return "NONE"


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


async def _fetch_reviews_and_threads(
    client: httpx.AsyncClient, repo: str, pr_number: int, token: str | None
) -> tuple[int, int]:
    """Fetch unresolved review-thread counts.

    Returns:
        (unresolved_comments_count, coderabbit_unresolved_count)
    """
    if "/" not in repo:
        return 0, 0
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

                    return unresolved_count, cr_count
        except Exception as exc:
            log.debug("pr-watcher: GraphQL review query failed for %s#%d: %s", repo, pr_number, exc)

    # REST fallback
    comments = await _fetch_review_comments(client, repo, pr_number, token)
    unresolved_count = len(comments)
    cr_count = sum(1 for c in comments if (c.get("user", {}).get("login") or "").lower().startswith("coderabbit"))

    return unresolved_count, cr_count


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

    unresolved_count, cr_count = await _fetch_reviews_and_threads(client, repo, pr_number, token)

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
        created_at=parse_iso_datetime(full_pr.get("created_at")),
        updated_at=parse_iso_datetime(full_pr.get("updated_at")),
        check_state=check_state,
        raw_payload=full_pr,
    )
    return pr_state, label_events


def _extract_event_pr_numbers(events: list[dict[str, Any]]) -> set[int]:
    """Extract PR numbers from GitHub repository events payloads."""
    pr_numbers: set[int] = set()
    for event in events:
        payload = event.get("payload") or {}
        event_type = event.get("type", "")
        number: int | None = None

        if event_type in ("PullRequestEvent", "PullRequestReviewEvent", "PullRequestReviewCommentEvent"):
            number = (payload.get("pull_request") or {}).get("number")
        elif event_type == "IssueCommentEvent":
            issue = payload.get("issue") or {}
            if issue.get("pull_request"):
                number = issue.get("number")
        elif event_type == "CheckRunEvent":
            prs = (payload.get("check_run") or {}).get("pull_requests") or []
            for pr_ref in prs:
                n = pr_ref.get("number")
                if isinstance(n, int):
                    pr_numbers.add(n)
        elif event_type == "CheckSuiteEvent":
            prs = (payload.get("check_suite") or {}).get("pull_requests") or []
            for pr_ref in prs:
                n = pr_ref.get("number")
                if isinstance(n, int):
                    pr_numbers.add(n)

        if isinstance(number, int):
            pr_numbers.add(number)

    return pr_numbers


def _classify_comment_event(event: dict[str, Any]) -> tuple[int, str, datetime, str, str] | None:
    payload = event.get("payload") or {}
    event_type = event.get("type", "")
    action = payload.get("action")
    comment_obj: dict[str, Any] = {}
    if event_type == "IssueCommentEvent" and action == "created":
        issue = payload.get("issue") or {}
        if "pull_request" not in issue or issue.get("pull_request") is None:
            return None
        number = issue.get("number")
        comment_obj = payload.get("comment") or {}
    elif event_type == "PullRequestReviewCommentEvent" and action == "created":
        number = (payload.get("pull_request") or {}).get("number")
        comment_obj = payload.get("comment") or {}
    elif event_type == "PullRequestReviewEvent" and action == "submitted":
        review = payload.get("review") or {}
        if not ((review.get("body") or "").strip()):
            return None
        number = (payload.get("pull_request") or {}).get("number")
        comment_obj = review
    else:
        return None
    if not isinstance(number, int) or not event.get("id"):
        return None
    actor_obj = event.get("actor") or {}
    actor_login = (
        actor_obj.get("login")
        or (comment_obj.get("user") or {}).get("login")
        or (payload.get("sender") or {}).get("login")
        or ""
    )
    author_association = (
        comment_obj.get("author_association")
        or actor_obj.get("author_association")
        or (comment_obj.get("user") or {}).get("author_association")
        or ""
    )
    if not actor_login or not author_association:
        return None

    created_at = parse_iso_datetime(event.get("created_at")) or datetime.now(timezone.utc)
    return number, str(event["id"]), created_at, actor_login, author_association


async def _classify_event_triggers(
    client: httpx.AsyncClient, repo: str, events: list[dict[str, Any]], token: str | None
) -> dict[int, list[EventTrigger]]:
    """Classify event-driven conditions and retain their relevant authors."""
    triggers: dict[int, list[EventTrigger]] = {}
    for event in events:
        payload = event.get("payload") or {}
        event_type = event.get("type", "")
        event_id = str(event.get("id") or "")
        if not event_id:
            continue
        created_at = parse_iso_datetime(event.get("created_at"))
        pr = payload.get("pull_request") or {}
        number = pr.get("number")
        action = payload.get("action")
        actor = (event.get("actor") or {}).get("login", "")
        condition = ""
        relevant_author = actor
        association = ""

        if event_type == "PullRequestEvent" and isinstance(number, int):
            if action in {"opened", "reopened"}:
                condition = "new_pr_or_commit"
                relevant_author = (pr.get("user") or {}).get("login", "")
                association = pr.get("author_association", "")
            elif action == "synchronize":
                condition = "new_pr_or_commit"
                sha = ((pr.get("head") or {}).get("sha") or "")
                relevant_author = await _fetch_commit_author(client, repo, sha, token)
                association = await _fetch_actor_association(client, repo, relevant_author, token)
            elif action in {"ready_for_review", "converted_to_ready_for_review", "review_requested", "labeled", "unlabeled"}:
                condition = "any_actionable"
                association = await _fetch_actor_association(client, repo, actor, token)
        elif event_type == "CheckRunEvent":
            check = payload.get("check_run") or {}
            if action == "completed" and (check.get("conclusion") or "").lower() in {
                "failure", "timed_out", "action_required", "cancelled", "startup_failure"
            }:
                for pr_ref in check.get("pull_requests") or []:
                    pr_number = pr_ref.get("number")
                    if isinstance(pr_number, int):
                        triggers.setdefault(pr_number, []).append(EventTrigger(
                            "ci_fail_or_conflict", event_id, "", pr_number, event_type, created_at,
                        ))
                continue
        elif event_type in {"IssueCommentEvent", "PullRequestReviewCommentEvent", "PullRequestReviewEvent"}:
            if event_type == "PullRequestReviewEvent" and action == "submitted":
                review = payload.get("review") or {}
                if (review.get("state") or "").lower() == "approved" and isinstance(number, int):
                    approved_user = (review.get("user") or {}).get("login", "") or actor
                    approved_association = review.get("author_association", "")
                    if not approved_association:
                        approved_association = await _fetch_actor_association(client, repo, approved_user, token)
                    triggers.setdefault(number, []).append(EventTrigger(
                        "review_approved", event_id, approved_user, number,
                        event_type, created_at, approved_association,
                    ))
            classified = _classify_comment_event(event)
            if classified:
                number, event_id, created_at, actor_login, association = classified
                condition = "pr_comment" if event_type == "IssueCommentEvent" else "review_comments"
                relevant_author = actor_login
        if condition and isinstance(number, int):
            triggers.setdefault(number, []).append(EventTrigger(
                condition, event_id, relevant_author, number, event_type, created_at, association
            ))
            if condition in {"new_pr_or_commit", "any_actionable"}:
                triggers[number].append(EventTrigger(
                    "any_actionable", event_id, actor, number, event_type, created_at, association
                ))
    return triggers


async def _fresh_comment_events(db, repo: str, events: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    fresh: dict[int, dict[str, Any]] = {}
    for event in events:
        classified = _classify_comment_event(event)
        if not classified:
            continue
        number, event_id, event_at, actor_login, author_association = classified
        if not await has_event_receipt(db, repo, event_id):
            observed_at = datetime.now(timezone.utc)
            previous = fresh.get(number)
            if previous is None or event_at >= previous["created_at"]:
                fresh[number] = {
                    "id": event_id,
                    "type": event.get("type", ""),
                    "at": observed_at,
                    "created_at": event_at,
                    "actor_login": actor_login,
                    "author_association": author_association,
                }
    return fresh


def _schedule_matches_author_scope(
    pr: PRState,
    sched: SessionSchedule,
    label_events: list[dict[str, Any]] | None = None,
    actor_login: str | None = None,
    actor_association: str = "",
) -> bool:
    """Apply author routing after the event condition has matched."""
    author = actor_login if actor_login is not None else pr.author_login
    author_lower = author.lower()
    fix_logins = sched.fix_author_logins
    is_self = author_lower in fix_logins if fix_logins else False
    is_bot = is_bot_author(author, set(DEFAULT_BOT_LOGINS))

    if sched.author_scope == "self" and not is_self:
        return False
    if sched.author_scope == "team":
        if is_self or is_bot:
            return False
        scoped_pr = replace(
            pr,
            author_login=author,
            author_association=actor_association or (pr.author_association if actor_login is None else "NONE"),
        )
        trust = evaluate_author_trust(
            scoped_pr,
            policy=TrustPolicy(strategy=TrustStrategy.ORG_AND_COLLABORATORS),
            label_events=label_events if actor_login is None else None,
        )
        if not trust.is_trusted:
            return False
    if sched.author_scope == "bots" and not is_bot:
        return False
    return True


def _match_triggers_for_pr(
    pr: PRState,
    matched_conditions: set[str],
    sched_sessions: list[tuple[SessionSchedule, Session]],
    label_events: list[dict[str, Any]] | None = None,
    blocked_conditions: set[str] | None = None,
    event_triggers: list[EventTrigger] | None = None,
) -> list[tuple[SessionSchedule, Session, EventTrigger | None]]:
    """Find all schedules whose conditions and author scopes match a PR."""
    if pr.is_draft:
        return []

    matches: list[tuple[SessionSchedule, Session, EventTrigger | None]] = []
    for sched, session in sched_sessions:
        if not sched.enabled or sched.trigger_type != "event":
            continue
        if sched.event_condition not in matched_conditions:
            continue
        if blocked_conditions and sched.event_condition in blocked_conditions:
            continue
        relevant = [e for e in (event_triggers or []) if e.condition == sched.event_condition]
        if event_triggers is not None and sched.event_condition in {
            "new_pr_or_commit", "review_comments", "review_approved", "pr_comment", "any_actionable", "ci_fail_or_conflict"
        } and not relevant:
            continue
        if not relevant:
            relevant = [None]
        matching_event = next((event for event in relevant if _schedule_matches_author_scope(
                pr,
                sched,
                label_events,
                actor_login=event.actor_login if event else None,
                actor_association=event.actor_association if event else "",
            )), None)
        if matching_event is not None or any(event is None for event in relevant):
            matches.append((sched, session, matching_event))
    return matches


def _match_trigger_for_pr(
    pr: PRState,
    matched_conditions: set[str],
    sched_sessions: list[tuple[SessionSchedule, Session]],
    label_events: list[dict[str, Any]] | None = None,
    blocked_conditions: set[str] | None = None,
) -> tuple[SessionSchedule, Session] | None:
    """Return the first matching schedule for legacy callers."""
    matches = _match_triggers_for_pr(
        pr,
        matched_conditions,
        sched_sessions,
        label_events,
        blocked_conditions,
    )
    return matches[0][:2] if matches else None


def _build_event_context(
    *,
    sched: SessionSchedule,
    repo: str,
    pr_state: PRState,
    condition: str,
    event: EventTrigger | None = None,
) -> dict[str, Any]:
    return {
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
        "event_id": event.event_id if event else "",
        "event_actor": event.actor_login if event else "",
        "event_type": event.event_type if event else "",
        "event_condition": condition,
        "fork_no_push": bool(
            pr_state.is_fork and not pr_state.raw_payload.get("maintainer_can_modify", False)
        ),
        "cause": f"Matched event condition: {condition}",
    }


async def _dispatch_session_run(
    db,
    *,
    session: Session,
    sched: SessionSchedule,
    event_ctx_json: str,
    action_key: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    queue_if_active: bool,
    event_id: str = "",
) -> tuple[str, str]:
    """Dispatch a schedule run for a session, optionally queueing if the session is busy.

    Returns:
        (outcome, error) where outcome is "launched", "deferred", or "failed".
    """
    if session.is_active:
        if queue_if_active:
            attempts = await record_dispatch(
                db,
                repo=repo,
                pr_number=pr_number,
                head_sha=head_sha,
                action=action_key,
                event_id=event_id,
                session_id=session.id,
                status="queued",
                event_context=event_ctx_json,
            )
            log.info(
                "pr-watcher: session %d (%s) busy (%s) — queued PR %s#%d [%s], attempt=%d",
                session.id,
                session.name,
                session.phase,
                repo,
                pr_number,
                action_key,
                attempts,
            )
        return "deferred", ""

    ws = session.workspace
    if not ws:
        return "failed", f"Session {session.id} has no workspace"

    try:
        from swarmer.routers.sessions import _do_launch

        session.mode = "prompt"
        session.active_schedule_id = sched.id
        session.event_context = event_ctx_json
        await db.commit()

        await _do_launch(session, ws, db)

        attempts = await record_dispatch(
            db,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            action=action_key,
            event_id=event_id,
            session_id=session.id,
            status="dispatched",
            event_context=event_ctx_json,
        )
        log.info("pr-watcher: session %d launched (phase=%s, attempt=%d)", session.id, session.phase, attempts)
        return "launched", ""
    except Exception as exc:
        log.exception("pr-watcher: failed to launch session %d for PR %s#%d: %s", session.id, repo, pr_number, exc)
        attempts = await record_dispatch(
            db,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            action=action_key,
            event_id=event_id,
            session_id=session.id,
            status="failed",
            error=str(exc),
            event_context=event_ctx_json,
        )
        if attempts >= settings.pr_watcher_max_fix_attempts:
            await record_dispatch(
                db,
                repo=repo,
                pr_number=pr_number,
                head_sha=head_sha,
                action=action_key,
                event_id=event_id,
                session_id=session.id,
                status="blocked",
                error=f"Max dispatch attempts ({attempts}) reached",
                event_context=event_ctx_json,
            )
        return "failed", str(exc)


async def _load_session_with_context(db, session_id: int) -> Session | None:
    result = await db.execute(
        select(Session)
        .options(
            selectinload(Session.workspace),
            selectinload(Session.github_pat),
            selectinload(Session.repos),
            selectinload(Session.schedules),
        )
        .where(Session.id == session_id)
    )
    return result.scalar_one_or_none()


async def _drain_queued_dispatches(db, client: httpx.AsyncClient | None = None) -> None:
    """Replay queued watcher dispatches for sessions that are now idle."""
    queued_rows = await list_queued_dispatches(db)

    launched_sessions: set[int] = set()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for row in await list_due_comment_dispatches(db, now):
        session = await _load_session_with_context(db, row.session_id)
        sched = next((s for s in (session.schedules if session else []) if s.id == row.schedule_id), None)
        if not session or not sched or not sched.enabled or sched.trigger_type != "event" or sched.event_condition != "pr_comment":
            await update_comment_dispatch(db, row, status="completed")
            continue
        if session.is_active or row.session_id in launched_sessions:
            continue
        if client:
            token = await _resolve_github_token_for_workspace_repo(
                session.workspace_id, row.repo, [(sched, session)], db
            )
            detail = await _fetch_pr_details(client, row.repo, row.pr_number, token)
            if not detail:
                continue
            if detail.get("state") != "open" or detail.get("draft", False):
                await update_comment_dispatch(db, row, status="completed")
                continue
            curr_head_sha = (detail.get("head") or {}).get("sha") or row.head_sha
            if curr_head_sha and curr_head_sha != row.head_sha:
                row.head_sha = curr_head_sha
                if row.event_context:
                    try:
                        ctx = json.loads(row.event_context)
                        ctx["head_sha"] = curr_head_sha
                        row.event_context = json.dumps(ctx)
                    except Exception:
                        pass
                await db.commit()
        outcome, err = await _dispatch_session_run(
            db, session=session, sched=sched, event_ctx_json=row.event_context,
            action_key="pr_comment", repo=row.repo, pr_number=row.pr_number,
            head_sha=row.head_sha, queue_if_active=False,
            event_id=row.last_comment_event_id,
        )
        if outcome == "launched":
            await update_comment_dispatch(db, row, status="dispatched")
            launched_sessions.add(row.session_id)
        elif outcome == "failed":
            await update_comment_dispatch(db, row, status="failed", error=err)
    for row in queued_rows:
        if not row.session_id or row.session_id in launched_sessions:
            continue

        session = await _load_session_with_context(db, row.session_id)
        if not session or session.is_active:
            continue

        ctx_json = row.event_context or ""
        try:
            ctx = json.loads(ctx_json) if ctx_json else {}
        except Exception:
            ctx = {}
        schedule_id = int(ctx.get("schedule_id") or 0)
        if not schedule_id:
            log.warning(
                "pr-watcher: queued dispatch row %d missing schedule_id in event_context; marking failed",
                row.id,
            )
            await record_dispatch(
                db,
                repo=row.repo,
                pr_number=row.pr_number,
                head_sha=row.head_sha,
                action=row.action,
                session_id=row.session_id,
                status="failed",
                error="queued dispatch missing schedule_id",
                event_context=ctx_json,
            )
            continue

        sched = next((s for s in (session.schedules or []) if s.id == schedule_id), None)
        if not sched or not sched.enabled or sched.trigger_type != "event":
            log.info(
                "pr-watcher: dropping queued dispatch for session %d schedule %d (missing/disabled/non-event)",
                session.id,
                schedule_id,
            )
            await record_dispatch(
                db,
                repo=row.repo,
                pr_number=row.pr_number,
                head_sha=row.head_sha,
                action=row.action,
                session_id=row.session_id,
                status="completed",
                event_context=ctx_json,
            )
            continue

        outcome, _ = await _dispatch_session_run(
            db,
            session=session,
            sched=sched,
            event_ctx_json=ctx_json,
            action_key=row.action,
            repo=row.repo,
            pr_number=row.pr_number,
            head_sha=row.head_sha,
            queue_if_active=False,
            event_id=ctx.get("event_id", ""),
        )
        if outcome == "launched":
            launched_sessions.add(session.id)


async def _evaluate_and_dispatch_prs(
    client: httpx.AsyncClient,
    repo: str,
    sched_sessions: list[tuple[SessionSchedule, Session]],
    token: str | None,
    db,
    target_pr_numbers: set[int] | None = None,
    comment_events: dict[int, dict[str, Any]] | None = None,
    event_triggers: dict[int, list[EventTrigger]] | None = None,
) -> None:
    """Scan open PRs in repo, evaluate against trigger conditions, and dispatch sessions."""
    open_prs = await _fetch_open_prs(client, repo, token)
    if not open_prs:
        return

    for raw_pr in open_prs:
        if target_pr_numbers is not None and raw_pr.get("number") not in target_pr_numbers:
            continue

        pr_state, label_events = await _build_pr_state(client, repo, raw_pr, token)

        # A commit event during a comment quiet period changes the head to
        # evaluate, but must not restart the quiet-period timer.
        if comment_events is not None and pr_state.pr_number not in comment_events:
            for sched, _session in sched_sessions:
                if sched.enabled and sched.event_condition == "pr_comment":
                    queued = await get_comment_dispatch(db, repo, pr_state.pr_number, sched.id)
                    if queued and queued.status == "queued":
                        queued.head_sha = pr_state.head_sha
                        queued.event_context = json.dumps(_build_event_context(
                            sched=sched, repo=repo, pr_state=pr_state, condition="pr_comment",
                        ))
                        await db.commit()

        event_conditions = {
            sched.event_condition
            for sched, _sess in sched_sessions
            if sched.enabled and sched.trigger_type == "event"
        }
        if comment_events is None:
            event_conditions.discard("pr_comment")
        matched_conditions = evaluate_pr_conditions(
            pr_state,
            event_conditions=event_conditions,
            quiet_period_seconds=float(settings.pr_watcher_debounce_seconds),
        )
        pr_event_triggers = list((event_triggers or {}).get(pr_state.pr_number, []))
        pr_event_triggers = [
            replace(trigger, actor_login=pr_state.author_login, actor_association=pr_state.author_association)
            if trigger.condition == "ci_fail_or_conflict" and not trigger.actor_login
            else trigger
            for trigger in pr_event_triggers
        ]
        if event_triggers is not None and pr_state.mergeable_state == "dirty":
            pr_event_triggers.extend(
                EventTrigger(
                    "ci_fail_or_conflict", trigger.event_id, pr_state.author_login,
                    pr_state.pr_number, trigger.event_type, trigger.created_at,
                    pr_state.author_association,
                )
                for trigger in pr_event_triggers
                if trigger.event_type == "PullRequestEvent" and trigger.condition == "new_pr_or_commit"
            )
        if event_triggers is not None:
            # Actionable state is event-driven. Never infer its actor from a
            # periodically refreshed PR snapshot.
            matched_conditions.difference_update({"any_actionable", "new_pr_or_commit", "review_comments", "pr_comment"})
            matched_conditions.update(trigger.condition for trigger in pr_event_triggers)
        if comment_events and pr_state.pr_number in comment_events and "pr_comment" in event_conditions:
            matched_conditions.add("pr_comment")
        if not matched_conditions:
            continue

        comment = (comment_events or {}).get(pr_state.pr_number)
        matching_event_triggers = pr_event_triggers if event_triggers is not None else None
        if matching_event_triggers is None and comment:
            matching_event_triggers = [EventTrigger(
                "pr_comment",
                comment.get("id", ""),
                comment.get("actor_login", ""),
                pr_state.pr_number,
                comment.get("type", "IssueCommentEvent"),
                comment.get("created_at"),
                comment.get("author_association", ""),
            )]
        matches = _match_triggers_for_pr(
            pr_state,
            matched_conditions,
            sched_sessions,
            label_events,
            event_triggers=matching_event_triggers,
        )
        if not matches:
            log.debug(
                "pr-watcher: PR %s#%d matched conditions %s but no matching schedules",
                repo,
                pr_state.pr_number,
                sorted(matched_conditions),
            )
            continue

        pr_comment_matches = [
            (s, sess, event) for s, sess, event in matches if s.event_condition == "pr_comment"
        ]
        other_matches = [
            (s, sess, event) for s, sess, event in matches if s.event_condition != "pr_comment"
        ]

        if pr_comment_matches:
            if comment:
                comment_rows: list[tuple[SessionSchedule, Session, Any, str]] = []
                for sched, session, event in pr_comment_matches:
                    event_ctx = _build_event_context(
                        sched=sched, repo=repo, pr_state=pr_state, condition="pr_comment", event=event,
                    )
                    ctx_str = json.dumps(event_ctx)
                    row = await upsert_comment_dispatch(
                        db, repo=repo, pr_number=pr_state.pr_number, session_id=session.id,
                        schedule_id=sched.id, head_sha=pr_state.head_sha,
                        event_context=ctx_str, event_id=comment["id"], event_at=comment["at"],
                        delay_minutes=sched.delay_minutes,
                        event_type=comment.get("type", ""),
                        event_created_at=comment.get("created_at"),
                        commit=False,
                    )
                    comment_rows.append((sched, session, row, ctx_str))

                # Atomically commit all matching comment dispatches and the receipt
                try:
                    await db.commit()
                except IntegrityError:
                    await db.rollback()
                    raise
                for _, _, r, _ in comment_rows:
                    await db.refresh(r)

                for sched, session, row, event_ctx_json in comment_rows:
                    if sched.delay_minutes:
                        continue
                    outcome, err = await _dispatch_session_run(
                        db, session=session, sched=sched, event_ctx_json=event_ctx_json,
                        action_key="pr_comment", repo=repo, pr_number=pr_state.pr_number,
                        head_sha=pr_state.head_sha, queue_if_active=False,
                        event_id=comment["id"],
                    )
                    if outcome == "launched":
                        await update_comment_dispatch(db, row, status="dispatched")
                    elif outcome == "failed":
                        await update_comment_dispatch(db, row, status="failed", error=err)
            else:
                for sched, session, event in pr_comment_matches:
                    queued = await get_comment_dispatch(db, repo, pr_state.pr_number, sched.id)
                    if queued and queued.status == "queued":
                        previous_ctx = json.loads(queued.event_context or "{}")
                        previous_event = EventTrigger(
                            "pr_comment",
                            previous_ctx.get("event_id", queued.last_comment_event_id),
                            previous_ctx.get("event_actor", ""),
                            pr_state.pr_number,
                            previous_ctx.get("event_type", ""),
                            previous_ctx.get("event_created_at"),
                        ) if previous_ctx.get("event_id") or queued.last_comment_event_id else None
                        event_ctx = _build_event_context(
                            sched=sched, repo=repo, pr_state=pr_state, condition="pr_comment",
                            event=event or previous_event,
                        )
                        queued.head_sha = pr_state.head_sha
                        queued.event_context = json.dumps(event_ctx)
                        await db.commit()

        for sched, session, event in other_matches:
            condition = sched.event_condition
            event_ctx = _build_event_context(
                sched=sched,
                repo=repo,
                pr_state=pr_state,
                condition=condition,
                event=event,
            )
            event_ctx_json = json.dumps(event_ctx)

            if await is_blocked(
                db,
                repo,
                pr_state.pr_number,
                pr_state.head_sha,
                condition,
                session_id=session.id,
                event_id=event_ctx.get("event_id", ""),
            ):
                log.debug(
                    "pr-watcher: PR %s#%d [%s] session=%d schedule=%d already in-flight, completed, or blocked on head SHA %s",
                    repo,
                    pr_state.pr_number,
                    condition,
                    session.id,
                    sched.id,
                    pr_state.head_sha[:8],
                )
                continue

            await _dispatch_session_run(
                db,
                session=session,
                sched=sched,
                event_ctx_json=event_ctx_json,
                action_key=condition,
                repo=repo,
                pr_number=pr_state.pr_number,
                head_sha=pr_state.head_sha,
                queue_if_active=True,
                event_id=event_ctx.get("event_id", ""),
            )

    if comment_events:
        for pr_num, comment_info in comment_events.items():
            ev_id = comment_info.get("id")
            if ev_id and not await has_event_receipt(db, repo, ev_id):
                await record_event_receipt(
                    db, repo=repo, event_id=ev_id, event_type=comment_info.get("type", ""),
                    pr_number=pr_num, event_created_at=comment_info.get("created_at"),
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

                    await _drain_queued_dispatches(db, client)

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
                                        comment_events = await _fresh_comment_events(db, repo, events)
                                        event_triggers = await _classify_event_triggers(client, repo, events, token)
                                        if new_etag:
                                            await save_etag(db, repo, new_etag)
                                        target_prs = _extract_event_pr_numbers(events)
                                        log.info(
                                            "pr-watcher: ws=%d %s 200 OK (%d events, prs=%d) — evaluating PRs",
                                            ws_id, repo, len(events), len(target_prs),
                                        )
                                        await _evaluate_and_dispatch_prs(
                                            client,
                                            repo,
                                            sched_sessions,
                                            token,
                                            db,
                                            target_pr_numbers=target_prs or None,
                                            comment_events=comment_events,
                                            event_triggers=event_triggers,
                                        )
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
