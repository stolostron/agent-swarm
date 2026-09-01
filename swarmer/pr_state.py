"""PR State classifier, CI status normalizer, and author trust resolver.

Shared library for the in-process Swarm PR Events Watcher (swarmer/pr_watcher.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import re
from typing import Any


class AuthorAssociation(str, Enum):
    OWNER = "OWNER"
    MEMBER = "MEMBER"
    COLLABORATOR = "COLLABORATOR"
    CONTRIBUTOR = "CONTRIBUTOR"
    FIRST_TIME_CONTRIBUTOR = "FIRST_TIME_CONTRIBUTOR"
    NONE = "NONE"


TRUSTED_ASSOCIATIONS = {
    AuthorAssociation.OWNER,
    AuthorAssociation.MEMBER,
    AuthorAssociation.COLLABORATOR,
    AuthorAssociation.CONTRIBUTOR,
    "OWNER",
    "MEMBER",
    "COLLABORATOR",
    "CONTRIBUTOR",
}

DEFAULT_BOT_LOGINS = {
    "dependabot[bot]",
    "dependabot",
    "renovate[bot]",
    "renovate",
    "coderabbitai[bot]",
    "coderabbitai",
    "openshift-bot",
    "red-hat-konflux[bot]",
    "app/github-actions",
    "github-actions[bot]",
}


class TrustStrategy(str, Enum):
    ORG_AND_COLLABORATORS = "org_and_collaborators"
    EXPLICIT_ALLOWLIST = "allowlist"
    GITHUB_TEAM = "github_team"


@dataclass
class TrustPolicy:
    strategy: TrustStrategy = TrustStrategy.ORG_AND_COLLABORATORS
    allowlist: set[str] = field(default_factory=set)
    team_slug: str = ""
    trusted_label: str = "ok-to-review"
    require_label_applier_trusted: bool = True


@dataclass
class AuthorTrustResult:
    is_trusted: bool
    reason: str
    matched_layer: str  # "association", "allowlist", "github_team", "trusted_label", "untrusted"


@dataclass
class CheckState:
    total: int = 0
    passing: int = 0
    failing: int = 0
    in_progress: int = 0
    queued: int = 0
    latest_completed_at: datetime | None = None
    failed_check_names: list[str] = field(default_factory=list)

    @property
    def is_fully_completed(self) -> bool:
        return self.total > 0 and (self.in_progress + self.queued) == 0

    @property
    def has_failures(self) -> bool:
        return self.failing > 0


@dataclass
class PRState:
    repo: str  # e.g. "owner/repo"
    pr_number: int
    title: str
    body: str
    author_login: str
    author_association: str  # e.g. "MEMBER", "COLLABORATOR", "NONE"
    is_draft: bool
    head_sha: str
    head_ref: str
    base_ref: str
    mergeable_state: str  # "clean", "dirty", "unstable", "blocked", "unknown"
    is_fork: bool
    fork_owner: str = ""
    labels: set[str] = field(default_factory=set)
    unresolved_review_comments: int = 0
    coderabbit_unresolved_comments: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    check_state: CheckState = field(default_factory=CheckState)
    raw_payload: dict[str, Any] = field(default_factory=dict)


def parse_iso_datetime(dt_str: str | None) -> datetime | None:
    if not dt_str:
        return None
    try:
        if dt_str.endswith("Z"):
            dt_str = dt_str[:-1] + "+00:00"
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None


def normalize_ci_checks(check_runs_or_statuses: list[dict[str, Any]]) -> CheckState:
    """Normalize GitHub check_runs and/or commit status contexts into a CheckState."""
    total = len(check_runs_or_statuses)
    passing = 0
    failing = 0
    in_progress = 0
    queued = 0
    failed_names: list[str] = []
    latest_completed: datetime | None = None

    for item in check_runs_or_statuses:
        name = item.get("name") or item.get("context") or "unnamed_check"
        completed_at_str = item.get("completed_at") or item.get("updated_at")
        completed_at = parse_iso_datetime(completed_at_str)
        if completed_at:
            if latest_completed is None or completed_at > latest_completed:
                latest_completed = completed_at

        # CheckRuns API
        if "status" in item and "conclusion" in item:
            status = (item.get("status") or "").lower()
            conclusion = (item.get("conclusion") or "").lower()

            if status in ("queued", "waiting", "pending"):
                queued += 1
            elif status in ("in_progress", "running"):
                in_progress += 1
            elif status == "completed":
                if conclusion in ("success", "neutral", "skipped"):
                    passing += 1
                elif conclusion in ("failure", "timed_out", "action_required", "cancelled", "startup_failure"):
                    failing += 1
                    failed_names.append(name)
                else:
                    # Treat unknown non-success as failure
                    failing += 1
                    failed_names.append(name)
            else:
                in_progress += 1

        # Legacy Commit Statuses API (state: success | failure | error | pending)
        elif "state" in item:
            state = (item.get("state") or "").lower()
            if state == "pending":
                in_progress += 1
            elif state == "success":
                passing += 1
            elif state in ("failure", "error"):
                failing += 1
                failed_names.append(name)
            else:
                in_progress += 1
        else:
            passing += 1

    return CheckState(
        total=total,
        passing=passing,
        failing=failing,
        in_progress=in_progress,
        queued=queued,
        latest_completed_at=latest_completed,
        failed_check_names=failed_names,
    )


def evaluate_ci_completion_barrier(
    check_state: CheckState,
    quiet_period_seconds: float = 90.0,
    current_time: datetime | None = None,
) -> tuple[bool, str]:
    """Check if CI is fully finished and satisfied the quiet-period debounce barrier.

    Returns:
        (is_ready, reason_description)
    """
    if check_state.total == 0:
        return True, "No checks configured"

    if check_state.in_progress > 0 or check_state.queued > 0:
        return False, f"Checks in progress ({check_state.in_progress} in progress, {check_state.queued} queued)"

    if not check_state.is_fully_completed:
        return False, "Checks not fully completed"

    if quiet_period_seconds > 0 and check_state.latest_completed_at is not None:
        now = current_time or datetime.now(timezone.utc)
        if check_state.latest_completed_at.tzinfo is None:
            comp_time = check_state.latest_completed_at.replace(tzinfo=timezone.utc)
        else:
            comp_time = check_state.latest_completed_at

        elapsed = (now - comp_time).total_seconds()
        if elapsed < quiet_period_seconds:
            remaining = int(quiet_period_seconds - elapsed)
            return False, f"Debounce quiet period active ({remaining}s remaining)"

    return True, "Checks complete and debounce satisfied"


def evaluate_author_trust(
    pr: PRState,
    policy: TrustPolicy,
    org_team_members: set[str] | None = None,
    label_events: list[dict[str, Any]] | None = None,
) -> AuthorTrustResult:
    """Evaluate whether a PR's author is trusted based on the 3-layer trust model.

    Layer 1: Native GitHub author_association (OWNER, MEMBER, COLLABORATOR, CONTRIBUTOR).
    Layer 2: Explicit workspace allowlist or GitHub Team membership.
    Layer 3: Trusted label opt-in (e.g. 'ok-to-review') with RBAC applier check.
    """
    author = pr.author_login.lower()

    # Layer 2: Explicit Allowlist (takes precedence if configured)
    if policy.strategy == TrustStrategy.EXPLICIT_ALLOWLIST:
        lowered_allowlist = {u.lower() for u in policy.allowlist}
        if author in lowered_allowlist:
            return AuthorTrustResult(
                is_trusted=True,
                reason=f"Author '{pr.author_login}' is in explicit team allowlist",
                matched_layer="allowlist",
            )

    # Layer 2: GitHub Team Membership
    if policy.strategy == TrustStrategy.GITHUB_TEAM and org_team_members:
        lowered_team = {u.lower() for u in org_team_members}
        if author in lowered_team:
            return AuthorTrustResult(
                is_trusted=True,
                reason=f"Author '{pr.author_login}' is a member of GitHub team '{policy.team_slug}'",
                matched_layer="github_team",
            )

    # Layer 1: Native GitHub Author Association
    if pr.author_association in TRUSTED_ASSOCIATIONS:
        return AuthorTrustResult(
            is_trusted=True,
            reason=f"Author '{pr.author_login}' has trusted association '{pr.author_association}'",
            matched_layer="association",
        )

    # Layer 3: Untrusted/External PR Gatekeeper via 'ok-to-review' label
    if policy.trusted_label and policy.trusted_label in pr.labels:
        if not policy.require_label_applier_trusted:
            return AuthorTrustResult(
                is_trusted=True,
                reason=f"PR has trusted label '{policy.trusted_label}'",
                matched_layer="trusted_label",
            )

        # Defense-in-depth: Verify that the user who added the label is trusted
        if label_events is not None:
            applier_trusted = False
            for ev in label_events:
                if ev.get("label", {}).get("name") == policy.trusted_label or ev.get("name") == policy.trusted_label:
                    actor = ev.get("actor", {}) or ev.get("sender", {})
                    actor_login = (actor.get("login") or "").lower()
                    actor_assoc = ev.get("author_association") or actor.get("author_association", "")
                    if actor_assoc in TRUSTED_ASSOCIATIONS or (policy.allowlist and actor_login in {u.lower() for u in policy.allowlist}):
                        applier_trusted = True
                        break
            if applier_trusted:
                return AuthorTrustResult(
                    is_trusted=True,
                    reason=f"PR has trusted label '{policy.trusted_label}' applied by verified collaborator",
                    matched_layer="trusted_label",
                )
            return AuthorTrustResult(
                is_trusted=False,
                reason=f"PR has label '{policy.trusted_label}' but applier is not a verified collaborator",
                matched_layer="untrusted",
            )

        return AuthorTrustResult(
            is_trusted=False,
            reason=f"PR has label '{policy.trusted_label}' but applier verification required and no timeline audit events available",
            matched_layer="untrusted",
        )

    return AuthorTrustResult(
        is_trusted=False,
        reason=f"Author '{pr.author_login}' has untrusted association '{pr.author_association}' and no '{policy.trusted_label}' label",
        matched_layer="untrusted",
    )


def is_bot_author(author_login: str, bot_logins: set[str] | None = None) -> bool:
    all_bots = {b.lower() for b in (bot_logins or DEFAULT_BOT_LOGINS)}
    author_lower = author_login.lower()
    if author_lower in all_bots:
        return True
    if author_lower.endswith("[bot]") or author_lower.startswith("app/"):
        return True
    if re.search(r"[-_.]bot$", author_lower):
        return True
    return False


def evaluate_pr_conditions(
    pr: PRState,
    event_conditions: set[str],
    quiet_period_seconds: float = 90.0,
    current_time: datetime | None = None,
) -> set[str]:
    """Return the configured event conditions currently satisfied by a PR.

    Event conditions describe *when* to run. The selected schedule Prompt
    determines what the agent does, and author scopes are evaluated separately
    by the watcher after this function returns.
    """
    matched: set[str] = set()
    if pr.is_draft:
        return matched

    if "ci_fail_or_conflict" in event_conditions or "any_actionable" in event_conditions:
        if pr.mergeable_state == "dirty":
            matched.add("ci_fail_or_conflict")
        elif pr.check_state.has_failures:
            ready, _reason = evaluate_ci_completion_barrier(
                pr.check_state,
                quiet_period_seconds=quiet_period_seconds,
                current_time=current_time,
            )
            if ready:
                matched.add("ci_fail_or_conflict")

    if "review_comments" in event_conditions or "any_actionable" in event_conditions:
        if pr.unresolved_review_comments > 0 or pr.coderabbit_unresolved_comments > 0:
            matched.add("review_comments")

    if "new_pr_or_commit" in event_conditions or "any_actionable" in event_conditions:
        matched.add("new_pr_or_commit")

    if "any_actionable" in event_conditions and matched:
        matched.add("any_actionable")

    return matched
