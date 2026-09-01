"""Tests for the in-process Swarm PR Events Watcher (swarmer/pr_watcher.py,
swarmer/pr_state.py, swarmer/pr_watcher_store.py)."""

from datetime import datetime, timedelta, timezone
import json
import os
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from swarmer.pr_state import (  # noqa: E402
    CheckState,
    PRState,
    TrustPolicy,
    TrustStrategy,
    evaluate_author_trust,
    evaluate_ci_completion_barrier,
    evaluate_pr_conditions,
    is_bot_author,
    normalize_ci_checks,
)
from swarmer.pr_watcher_store import extract_repo_from_url  # noqa: E402


class TestPRStateNormalization(unittest.TestCase):
    def test_normalize_ci_checks_mixed(self):
        check_runs = [
            {"name": "lint", "status": "completed", "conclusion": "success", "completed_at": "2026-08-25T10:00:00Z"},
            {"name": "unit-test", "status": "completed", "conclusion": "failure", "completed_at": "2026-08-25T10:05:00Z"},
            {"name": "e2e", "status": "in_progress", "conclusion": None},
            {"name": "build", "status": "queued", "conclusion": None},
        ]
        state = normalize_ci_checks(check_runs)
        self.assertEqual(state.total, 4)
        self.assertEqual(state.passing, 1)
        self.assertEqual(state.failing, 1)
        self.assertEqual(state.in_progress, 1)
        self.assertEqual(state.queued, 1)
        self.assertFalse(state.is_fully_completed)
        self.assertTrue(state.has_failures)
        self.assertIn("unit-test", state.failed_check_names)
        self.assertEqual(state.latest_completed_at, datetime(2026, 8, 25, 10, 5, tzinfo=timezone.utc))

    def test_normalize_ci_checks_explicit_null_status(self):
        check_runs = [
            {"name": "check1", "status": None, "conclusion": None},
        ]
        state = normalize_ci_checks(check_runs)
        self.assertEqual(state.total, 1)
        self.assertEqual(state.passing, 0)

    def test_normalize_ci_checks_legacy_statuses(self):
        statuses = [
            {"context": "ci/test", "state": "success"},
            {"context": "ci/lint", "state": "error"},
            {"context": "ci/build", "state": "pending"},
        ]
        state = normalize_ci_checks(statuses)
        self.assertEqual(state.total, 3)
        self.assertEqual(state.passing, 1)
        self.assertEqual(state.failing, 1)
        self.assertEqual(state.in_progress, 1)
        self.assertFalse(state.is_fully_completed)


class TestCICompletionBarrier(unittest.TestCase):
    def test_barrier_with_in_progress_checks(self):
        state = CheckState(total=2, passing=1, in_progress=1)
        ready, reason = evaluate_ci_completion_barrier(state, quiet_period_seconds=90)
        self.assertFalse(ready)
        self.assertIn("in progress", reason.lower())

    def test_barrier_with_debounce_active(self):
        now = datetime.now(timezone.utc)
        completed_recently = now - timedelta(seconds=30)
        state = CheckState(total=2, passing=1, failing=1, in_progress=0, queued=0, latest_completed_at=completed_recently)
        ready, reason = evaluate_ci_completion_barrier(state, quiet_period_seconds=90, current_time=now)
        self.assertFalse(ready)
        self.assertIn("debounce quiet period active", reason.lower())

    def test_barrier_satisfied_after_debounce(self):
        now = datetime.now(timezone.utc)
        completed_earlier = now - timedelta(seconds=120)
        state = CheckState(total=2, passing=1, failing=1, in_progress=0, queued=0, latest_completed_at=completed_earlier)
        ready, reason = evaluate_ci_completion_barrier(state, quiet_period_seconds=90, current_time=now)
        self.assertTrue(ready)
        self.assertIn("checks complete and debounce satisfied", reason.lower())


class TestAuthorTrustEvaluation(unittest.TestCase):
    def setUp(self):
        self.base_pr = PRState(
            repo="OpenShift-Fleet/agentic-sdlc",
            pr_number=100,
            title="Test PR",
            body="Desc",
            author_login="contributor-bob",
            author_association="MEMBER",
            is_draft=False,
            head_sha="abc1234",
            head_ref="feature",
            base_ref="main",
            mergeable_state="clean",
            is_fork=False,
        )

    def test_layer1_native_trusted_associations(self):
        policy = TrustPolicy(strategy=TrustStrategy.ORG_AND_COLLABORATORS)
        for assoc in ("OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"):
            self.base_pr.author_association = assoc
            res = evaluate_author_trust(self.base_pr, policy)
            self.assertTrue(res.is_trusted)
            self.assertEqual(res.matched_layer, "association")

    def test_layer1_native_untrusted_associations(self):
        policy = TrustPolicy(strategy=TrustStrategy.ORG_AND_COLLABORATORS)
        for assoc in ("FIRST_TIME_CONTRIBUTOR", "MANNEQUIN", "NONE"):
            self.base_pr.author_association = assoc
            res = evaluate_author_trust(self.base_pr, policy)
            self.assertFalse(res.is_trusted)
            self.assertEqual(res.matched_layer, "untrusted")

    def test_layer1_first_timer_is_untrusted(self):
        policy = TrustPolicy(strategy=TrustStrategy.ORG_AND_COLLABORATORS)
        self.base_pr.author_association = "FIRST_TIMER"
        res = evaluate_author_trust(self.base_pr, policy)
        self.assertFalse(res.is_trusted)
        self.assertEqual(res.matched_layer, "untrusted")

    def test_layer2_explicit_allowlist(self):
        policy = TrustPolicy(strategy=TrustStrategy.EXPLICIT_ALLOWLIST, allowlist={"alice", "bob"})
        self.base_pr.author_login = "bob"
        self.base_pr.author_association = "NONE"
        res = evaluate_author_trust(self.base_pr, policy)
        self.assertTrue(res.is_trusted)
        self.assertEqual(res.matched_layer, "allowlist")

        self.base_pr.author_login = "charlie"
        res = evaluate_author_trust(self.base_pr, policy)
        self.assertFalse(res.is_trusted)

    def test_layer2_github_team(self):
        policy = TrustPolicy(strategy=TrustStrategy.GITHUB_TEAM, team_slug="fleet-devs")
        self.base_pr.author_login = "team-member-sam"
        self.base_pr.author_association = "NONE"
        team_members = {"team-member-sam", "alice"}
        res = evaluate_author_trust(self.base_pr, policy, org_team_members=team_members)
        self.assertTrue(res.is_trusted)
        self.assertEqual(res.matched_layer, "github_team")

    def test_layer3_ok_to_review_label(self):
        policy = TrustPolicy(strategy=TrustStrategy.ORG_AND_COLLABORATORS, trusted_label="ok-to-review", require_label_applier_trusted=True)
        self.base_pr.author_association = "NONE"
        self.base_pr.labels = {"ok-to-review", "enhancement"}

        # Label present, verified with trusted applier event
        label_events = [
            {"name": "ok-to-review", "actor": {"login": "actor_collab_1", "author_association": "MEMBER"}}
        ]
        res = evaluate_author_trust(self.base_pr, policy, label_events=label_events)
        self.assertTrue(res.is_trusted)
        self.assertEqual(res.matched_layer, "trusted_label")

        # Label present, but no audit events available and applier verification is required
        res_no_events = evaluate_author_trust(self.base_pr, policy, label_events=None)
        self.assertFalse(res_no_events.is_trusted)
        self.assertEqual(res_no_events.matched_layer, "untrusted")

        # Label present, applier verification disabled
        policy_no_verify = TrustPolicy(strategy=TrustStrategy.ORG_AND_COLLABORATORS, trusted_label="ok-to-review", require_label_applier_trusted=False)
        res_no_verify = evaluate_author_trust(self.base_pr, policy_no_verify, label_events=None)
        self.assertTrue(res_no_verify.is_trusted)
        self.assertEqual(res_no_verify.matched_layer, "trusted_label")

    def test_layer3_untrusted_label_applier(self):
        policy = TrustPolicy(strategy=TrustStrategy.ORG_AND_COLLABORATORS, trusted_label="ok-to-review", require_label_applier_trusted=True)
        self.base_pr.author_association = "NONE"
        self.base_pr.labels = {"ok-to-review"}
        label_events = [
            {"name": "ok-to-review", "actor": {"login": "external-user", "author_association": "NONE"}}
        ]
        res = evaluate_author_trust(self.base_pr, policy, label_events=label_events)
        self.assertFalse(res.is_trusted)
        self.assertEqual(res.matched_layer, "untrusted")

    def test_bot_author_regex(self):
        self.assertFalse(is_bot_author("abbot"))
        self.assertFalse(is_bot_author("talbot"))
        self.assertTrue(is_bot_author("dependabot[bot]"))
        self.assertTrue(is_bot_author("openshift-bot"))
        self.assertTrue(is_bot_author("cve_bot"))
        self.assertTrue(is_bot_author("my-app.bot"))


class TestPRConditionEvaluation(unittest.TestCase):
    def test_draft_pr_has_no_conditions(self):
        pr = PRState(
            repo="org/repo",
            pr_number=1,
            title="WIP",
            body="",
            author_login="author-1",
            author_association="OWNER",
            is_draft=True,
            head_sha="111",
            head_ref="branch",
            base_ref="main",
            mergeable_state="dirty",
            is_fork=False,
        )
        self.assertEqual(evaluate_pr_conditions(pr, {"any_actionable"}), set())

    def test_merge_conflict_matches_condition_without_author_routing(self):
        pr = PRState(
            repo="org/repo",
            pr_number=2,
            title="My feature",
            body="",
            author_login="author-1",
            author_association="OWNER",
            is_draft=False,
            head_sha="222",
            head_ref="branch",
            base_ref="main",
            mergeable_state="dirty",
            is_fork=False,
        )
        self.assertEqual(
            evaluate_pr_conditions(pr, {"ci_fail_or_conflict", "new_pr_or_commit"}),
            {"ci_fail_or_conflict", "new_pr_or_commit"},
        )

    def test_fork_without_push_access_still_matches(self):
        pr = PRState(
            repo="org/repo",
            pr_number=3,
            title="External fix attempt",
            body="",
            author_login="author-1",
            author_association="OWNER",
            is_draft=False,
            head_sha="333",
            head_ref="branch",
            base_ref="main",
            mergeable_state="dirty",
            is_fork=True,
            fork_owner="fork-owner",
            raw_payload={"maintainer_can_modify": False},
        )
        self.assertIn(
            "ci_fail_or_conflict",
            evaluate_pr_conditions(pr, {"ci_fail_or_conflict"}),
        )

    def test_review_comments_match_condition(self):
        pr = PRState(
            repo="org/repo",
            pr_number=3,
            title="Needs comments addressed",
            body="",
            author_login="author-1",
            author_association="OWNER",
            is_draft=False,
            head_sha="333",
            head_ref="branch",
            base_ref="main",
            mergeable_state="clean",
            is_fork=False,
            unresolved_review_comments=2,
        )
        self.assertEqual(
            evaluate_pr_conditions(pr, {"review_comments"}),
            {"review_comments"},
        )

    def test_new_pr_or_commit_matches_unreviewed_head(self):
        pr = PRState(
            repo="org/repo",
            pr_number=4,
            title="Bump lodash",
            body="",
            author_login="teammate",
            author_association="MEMBER",
            is_draft=False,
            head_sha="444",
            head_ref="feature",
            base_ref="main",
            mergeable_state="clean",
            is_fork=False,
        )
        self.assertEqual(
            evaluate_pr_conditions(pr, {"new_pr_or_commit"}),
            {"new_pr_or_commit"},
        )

    def test_any_actionable_expands_to_matching_conditions(self):
        pr = PRState(
            repo="org/repo",
            pr_number=5,
            title="Team PR",
            body="",
            author_login="teammate-dan",
            author_association="MEMBER",
            is_draft=False,
            head_sha="555",
            head_ref="feature-dan",
            base_ref="main",
            mergeable_state="clean",
            is_fork=False,
            unresolved_review_comments=1,
        )
        self.assertEqual(
            evaluate_pr_conditions(pr, {"any_actionable"}),
            {"any_actionable", "new_pr_or_commit", "review_comments"},
        )


class TestExtractRepoFromUrl(unittest.TestCase):
    def test_extract_repo_from_url(self):
        self.assertEqual(extract_repo_from_url("https://github.com/org/repo.git"), "org/repo")
        self.assertEqual(extract_repo_from_url("git@github.com:org/repo.git"), "org/repo")
        self.assertEqual(extract_repo_from_url("org/repo"), "org/repo")
        self.assertEqual(extract_repo_from_url(""), "")


class TestAsyncPRWatcherStore(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from swarmer.database import Base

        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_async_state_store_record_and_reconcile(self):
        from swarmer.pr_watcher_store import (
            get_dispatch_state,
            get_etag,
            is_blocked,
            prune_etags,
            record_dispatch,
            reconcile_completed,
            save_etag,
        )

        async with self.session_factory() as db:
            # 1. Record first attempt
            attempts = await record_dispatch(
                db, repo="owner/repo", pr_number=10, head_sha="sha1", condition="ci_fail_or_conflict", session_id=1, status="dispatched"
            )
            self.assertEqual(attempts, 1)
            self.assertTrue(await is_blocked(db, "owner/repo", 10, "sha1", "ci_fail_or_conflict"))

            # 2. Check state
            row = await get_dispatch_state(db, "owner/repo", 10, "sha1", "ci_fail_or_conflict")
            self.assertIsNotNone(row)
            self.assertEqual(row.status, "dispatched")

            # 3. Second attempt increments the counter
            attempts2 = await record_dispatch(
                db, repo="owner/repo", pr_number=10, head_sha="sha1", condition="ci_fail_or_conflict", session_id=1, status="dispatched"
            )
            self.assertEqual(attempts2, 2)

            # 4. Reconcile completed (succeeded -> completed)
            await reconcile_completed(db, session_id=1, phase="succeeded")
            row_after = await get_dispatch_state(db, "owner/repo", 10, "sha1", "ci_fail_or_conflict")
            self.assertEqual(row_after.status, "completed")

            # 4b. Reconcile failed below max attempts -> status "failed"
            await record_dispatch(
                db, repo="owner/repo", pr_number=11, head_sha="sha_fail", condition="ci_fail_or_conflict", session_id=2, status="dispatched"
            )
            await reconcile_completed(db, session_id=2, phase="failed")
            row_failed = await get_dispatch_state(db, "owner/repo", 11, "sha_fail", "ci_fail_or_conflict")
            self.assertEqual(row_failed.status, "failed")
            self.assertIn("phase 'failed'", row_failed.last_error)

            # 4c. Reconcile failed at max attempts -> status "blocked"
            for _ in range(3):
                await record_dispatch(
                    db, repo="owner/repo", pr_number=12, head_sha="sha_cap", condition="ci_fail_or_conflict", session_id=3, status="dispatched"
                )
            await reconcile_completed(db, session_id=3, phase="failed")
            row_blocked = await get_dispatch_state(db, "owner/repo", 12, "sha_cap", "ci_fail_or_conflict")
            self.assertEqual(row_blocked.status, "blocked")
            self.assertIn("Max dispatch attempts", row_blocked.last_error)

            # 5. New head_sha resets the attempt counter
            attempts3 = await record_dispatch(
                db, repo="owner/repo", pr_number=10, head_sha="sha2", condition="ci_fail_or_conflict", session_id=1, status="dispatched"
            )
            self.assertEqual(attempts3, 1)

            # 6. ETag caching and pruning
            await save_etag(db, "owner/repo", "etag123")
            await save_etag(db, "owner/other", "etag456")
            self.assertEqual(await get_etag(db, "owner/repo"), "etag123")

            await prune_etags(db, {"owner/repo"})
            self.assertEqual(await get_etag(db, "owner/repo"), "etag123")
            self.assertIsNone(await get_etag(db, "owner/other"))

    async def test_resolve_event_triggers_db_query(self):
        from swarmer.models.session import Session
        from swarmer.models.session_repo import SessionRepo
        from swarmer.models.session_schedule import SessionSchedule
        from swarmer.models.workspace import Workspace
        from swarmer.pr_watcher_store import resolve_event_triggers

        async with self.session_factory() as db:
            ws = Workspace(display_name="Test WS", namespace="test-ws", description="")
            db.add(ws)
            await db.commit()
            await db.refresh(ws)

            session = Session(workspace_id=ws.id, name="s1", mode="prompt", provider="", agent_tool="opencode", instruction_prompt="")
            db.add(session)
            await db.commit()
            await db.refresh(session)

            repo = SessionRepo(session_id=session.id, repo_url="https://github.com/stolostron/agent-swarm.git", branch="main", local_path="agent-swarm")
            cron_sched = SessionSchedule(session_id=session.id, trigger_type="cron", cron_schedule="0 9 * * 1-5", label="cron only")
            event_sched = SessionSchedule(
                session_id=session.id,
                trigger_type="event",
                event_condition="any_actionable",
                author_scope="self",
                fix_authors="alice",
                label="event trigger",
            )
            db.add_all([repo, cron_sched, event_sched])
            await db.commit()

            fan_out = await resolve_event_triggers(db)
            self.assertIn(ws.id, fan_out)
            self.assertIn("stolostron/agent-swarm", fan_out[ws.id])
            items = fan_out[ws.id]["stolostron/agent-swarm"]
            self.assertEqual(len(items), 1)
            sched, sess = items[0]
            self.assertEqual(sched.trigger_type, "event")
            self.assertEqual(sched.fix_author_logins, {"alice"})
            self.assertEqual(sess.id, session.id)

    async def test_resolve_event_triggers_excludes_cron_only_sessions(self):
        from swarmer.models.session import Session
        from swarmer.models.session_repo import SessionRepo
        from swarmer.models.session_schedule import SessionSchedule
        from swarmer.models.workspace import Workspace
        from swarmer.pr_watcher_store import resolve_event_triggers

        async with self.session_factory() as db:
            ws = Workspace(display_name="Test WS 2", namespace="test-ws-2", description="")
            db.add(ws)
            await db.commit()
            await db.refresh(ws)

            session = Session(workspace_id=ws.id, name="s2", mode="prompt", provider="", agent_tool="opencode", instruction_prompt="")
            db.add(session)
            await db.commit()
            await db.refresh(session)

            repo = SessionRepo(session_id=session.id, repo_url="https://github.com/stolostron/cron-only-repo.git", branch="main", local_path="cron-only-repo")
            cron_sched = SessionSchedule(session_id=session.id, trigger_type="cron", cron_schedule="0 9 * * 1-5", label="cron only")
            db.add_all([repo, cron_sched])
            await db.commit()

            fan_out = await resolve_event_triggers(db)
            self.assertNotIn("stolostron/cron-only-repo", fan_out.get(ws.id, {}))

    async def test_resolve_github_token_for_workspace_repo(self) -> None:
        from unittest.mock import AsyncMock, patch
        from swarmer.models.github_pat import GitHubPAT
        from swarmer.models.session import Session
        from swarmer.models.session_schedule import SessionSchedule
        from swarmer.models.workspace import Workspace
        from swarmer.pr_watcher import _resolve_github_token_for_workspace_repo

        async with self.session_factory() as db:
            ws = Workspace(display_name="Test Token WS", namespace="test-token-ws", description="")
            db.add(ws)
            await db.commit()
            await db.refresh(ws)

            pat = GitHubPAT(workspace_id=ws.id, name="my-pat", github_username="fixture_user", pat_enc="enc_fixture")
            db.add(pat)
            await db.commit()
            await db.refresh(pat)

            session = Session(workspace_id=ws.id, name="s_pat", github_pat_id=pat.id, mode="prompt", provider="", agent_tool="opencode", instruction_prompt="")
            session.github_pat = pat
            sched = SessionSchedule(session_id=1, trigger_type="event", label="event")

            # 1. Resolves from session PAT
            with patch.object(GitHubPAT, "pat", "token_session_pat_val"):
                token = await _resolve_github_token_for_workspace_repo(ws.id, "stolostron/agent-swarm", [(sched, session)], db)
                self.assertEqual(token, "token_session_pat_val")

            # 2. Resolves from GitHub App if session has no PAT
            session_no_pat = Session(workspace_id=ws.id, name="s_app", mode="prompt", provider="", agent_tool="opencode", instruction_prompt="")
            with patch("swarmer.github_app.get_workspace_github_app", new_callable=AsyncMock) as mock_get_app, \
                 patch("swarmer.github_auth.mint_installation_token", new_callable=AsyncMock) as mock_mint:
                mock_get_app.return_value = object()
                mock_mint.return_value = "token_app_iat_val"
                token = await _resolve_github_token_for_workspace_repo(ws.id, "stolostron/agent-swarm", [(sched, session_no_pat)], db)
                self.assertEqual(token, "token_app_iat_val")

            # 3. Resolves from environment variable fallback
            with patch("swarmer.github_app.get_workspace_github_app", new_callable=AsyncMock) as mock_get_app, \
                 patch.dict("os.environ", {"GH_TOKEN_STOLOSTRON": "token_org_env_val"}):
                mock_get_app.return_value = None
                token = await _resolve_github_token_for_workspace_repo(ws.id, "stolostron/agent-swarm", [(sched, session_no_pat)], db)
                self.assertEqual(token, "token_org_env_val")


class TestPRWatcherNetworkDetails(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_pr_details_and_reviews(self) -> None:
        import httpx
        from swarmer.pr_watcher import _fetch_pr_details, _fetch_reviews_and_threads

        async def handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "/pulls/10" in url_str:
                return httpx.Response(200, json={"number": 10, "mergeable_state": "dirty", "maintainer_can_modify": True})
            if "/graphql" in url_str:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "reviewThreads": {
                                        "nodes": [
                                            {
                                                "isResolved": False,
                                                "isOutdated": False,
                                                "comments": {"nodes": [{"author": {"login": "coderabbitai[bot]"}, "body": "fix this"}]},
                                            },
                                            {
                                                "isResolved": True,
                                                "isOutdated": False,
                                                "comments": {"nodes": [{"author": {"login": "alice"}, "body": "resolved"}]},
                                            },
                                        ]
                                    },
                                    "reviews": {
                                        "nodes": [
                                            {"author": {"login": "coderabbitai[bot]"}, "state": "COMMENTED", "commit": {"oid": "sha123"}}
                                        ]
                                    },
                                }
                            }
                        }
                    },
                )
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            detail = await _fetch_pr_details(client, "stolostron/agent-swarm", 10, "dummy-token")
            self.assertEqual(detail.get("mergeable_state"), "dirty")
            self.assertTrue(detail.get("maintainer_can_modify"))

            unresolved, cr_count = await _fetch_reviews_and_threads(
                client, "stolostron/agent-swarm", 10, "dummy-token"
            )
            self.assertEqual(unresolved, 1)
            self.assertEqual(cr_count, 1)


class TestPRWatcherSignalRouting(unittest.TestCase):
    def _make_pr(self, **overrides):
        base = dict(
            repo="stolostron/agent-swarm",
            pr_number=153,
            title="Test PR",
            body="",
            author_login="author-1",
            author_association="MEMBER",
            is_draft=False,
            head_sha="abc123def",
            head_ref="feature",
            base_ref="main",
            mergeable_state="clean",
            is_fork=False,
            unresolved_review_comments=2,
            coderabbit_unresolved_comments=2,
            check_state=CheckState(total=1, passing=1),
            raw_payload={},
        )
        base.update(overrides)
        return PRState(**base)

    def _make_schedule(self, schedule_id: int, event_condition: str, author_scope: str, fix_authors: str = ""):
        return SimpleNamespace(
            id=schedule_id,
            enabled=True,
            trigger_type="event",
            event_condition=event_condition,
            author_scope=author_scope,
            fix_author_logins={a.strip().lower() for a in fix_authors.split(",") if a.strip()},
        )

    def _make_session(self, session_id: int):
        return SimpleNamespace(id=session_id, name=f"s{session_id}", phase="idle", is_active=False)

    def test_review_comments_match_specific_and_catchall(self):
        from swarmer.pr_state import evaluate_pr_conditions
        from swarmer.pr_watcher import _match_triggers_for_pr

        pr = self._make_pr()
        matched = evaluate_pr_conditions(pr, {"review_comments", "any_actionable"})

        sched_hygiene = self._make_schedule(25, "review_comments", "all")
        sched_catchall = self._make_schedule(17, "any_actionable", "self", fix_authors="author-1")
        matches = _match_triggers_for_pr(
            pr,
            matched,
            [(sched_hygiene, self._make_session(19)), (sched_catchall, self._make_session(19))],
        )

        self.assertEqual(len(matches), 2)
        self.assertEqual(matches[0][0].id, 25)
        self.assertEqual(matches[1][0].id, 17)

    def test_any_actionable_maps_new_pr_to_match(self):
        from swarmer.pr_state import evaluate_pr_conditions
        from swarmer.pr_watcher import _match_triggers_for_pr

        pr = self._make_pr(unresolved_review_comments=0, coderabbit_unresolved_comments=0)
        matched = evaluate_pr_conditions(pr, {"any_actionable", "new_pr_or_commit"})
        self.assertIn("any_actionable", matched)

        sched_catchall = self._make_schedule(44, "any_actionable", "team")
        matches = _match_triggers_for_pr(pr, matched, [(sched_catchall, self._make_session(77))])
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][0].id, 44)

    def test_extract_event_pr_numbers(self):
        from swarmer.pr_watcher import _extract_event_pr_numbers

        events = [
            {"type": "PullRequestEvent", "payload": {"pull_request": {"number": 153}}},
            {"type": "IssueCommentEvent", "payload": {"issue": {"number": 153, "pull_request": {"url": "x"}}}},
            {"type": "CheckRunEvent", "payload": {"check_run": {"pull_requests": [{"number": 153}, {"number": 154}]}}},
            {"type": "PushEvent", "payload": {}},
        ]

        self.assertEqual(_extract_event_pr_numbers(events), {153, 154})


class TestPRWatcherDispatchFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from swarmer.database import Base

        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _seed_session_with_schedules(self):
        from swarmer.models.session import Session
        from swarmer.models.session_schedule import SessionSchedule
        from swarmer.models.workspace import Workspace

        async with self.session_factory() as db:
            ws = Workspace(display_name="Watcher WS", namespace="watcher-ws", description="")
            db.add(ws)
            await db.commit()
            await db.refresh(ws)

            session = Session(
                workspace_id=ws.id,
                name="watcher-session",
                mode="prompt",
                provider="",
                agent_tool="opencode",
                instruction_prompt="",
                phase="idle",
            )
            db.add(session)
            await db.commit()
            await db.refresh(session)

            sched_hygiene = SessionSchedule(
                session_id=session.id,
                trigger_type="event",
                event_condition="review_comments",
                author_scope="all",
                label="Review comments",
            )
            sched_catchall = SessionSchedule(
                session_id=session.id,
                trigger_type="event",
                event_condition="any_actionable",
                author_scope="self",
                fix_authors="author-1",
                label="Catch all",
            )
            db.add_all([sched_hygiene, sched_catchall])
            await db.commit()
            return session.id

    async def _load_session_with_schedules(self, db, session_id: int):
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        from swarmer.models.session import Session

        q = await db.execute(
            select(Session)
            .options(
                selectinload(Session.workspace),
                selectinload(Session.github_pat),
                selectinload(Session.repos),
                selectinload(Session.schedules),
            )
            .where(Session.id == session_id)
        )
        loaded = q.scalar_one()
        by_condition = {s.event_condition: s for s in loaded.schedules}
        return loaded, by_condition["review_comments"], by_condition["any_actionable"]

    async def test_evaluate_dispatches_one_and_queues_second_same_session(self):
        import httpx
        from unittest.mock import AsyncMock, patch

        from swarmer.pr_state import CheckState, PRState
        from swarmer.pr_watcher import _evaluate_and_dispatch_prs
        from swarmer.pr_watcher_store import get_dispatch_state

        session_id = await self._seed_session_with_schedules()

        pr_state = PRState(
            repo="stolostron/agent-swarm",
            pr_number=153,
            title="PR 153",
            body="",
            author_login="author-1",
            author_association="MEMBER",
            is_draft=False,
            head_sha="sha153",
            head_ref="feature",
            base_ref="main",
            mergeable_state="clean",
            is_fork=False,
            unresolved_review_comments=2,
            coderabbit_unresolved_comments=2,
            check_state=CheckState(total=1, passing=1),
            raw_payload={},
        )

        async def _fake_launch(sess, _ws, _db):
            sess.phase = "running"

        async with self.session_factory() as db:
            session, sched_hygiene, sched_catchall = await self._load_session_with_schedules(db, session_id)

            with patch("swarmer.pr_watcher._fetch_open_prs", new=AsyncMock(return_value=[{"number": 153}])), \
                 patch("swarmer.pr_watcher._build_pr_state", new=AsyncMock(return_value=(pr_state, []))), \
                 patch("swarmer.routers.sessions._do_launch", new=AsyncMock(side_effect=_fake_launch)) as launch_mock:
                async with httpx.AsyncClient() as client:
                    await _evaluate_and_dispatch_prs(
                        client,
                        "stolostron/agent-swarm",
                        [(sched_hygiene, session), (sched_catchall, session)],
                        None,
                        db,
                    )

                self.assertEqual(launch_mock.await_count, 1)

                hygiene_row = await get_dispatch_state(db, "stolostron/agent-swarm", 153, "sha153", "review_comments", session_id=session.id)
                catchall_row = await get_dispatch_state(db, "stolostron/agent-swarm", 153, "sha153", "any_actionable", session_id=session.id)

                self.assertIsNotNone(hygiene_row)
                self.assertEqual(hygiene_row.status, "dispatched")
                self.assertIsNotNone(catchall_row)
                self.assertEqual(catchall_row.status, "queued")

    async def test_drain_queued_dispatch_launches_next(self):
        from unittest.mock import AsyncMock, patch

        from swarmer.pr_watcher import _drain_queued_dispatches
        from swarmer.pr_watcher_store import get_dispatch_state, record_dispatch

        session_id = await self._seed_session_with_schedules()

        async with self.session_factory() as db:
            session, sched_hygiene, _sched_catchall = await self._load_session_with_schedules(db, session_id)
            event_ctx = {
                "trigger_type": "event",
                "schedule_id": sched_hygiene.id,
                "repo": "stolostron/agent-swarm",
                "pr_number": 153,
                "head_sha": "sha153",
                "event_condition": "review_comments",
                "cause": "2 unresolved review comment(s) found on PR",
            }
            await record_dispatch(
                db,
                repo="stolostron/agent-swarm",
                pr_number=153,
                head_sha="sha153",
                condition="review_comments",
                session_id=session.id,
                status="queued",
                event_context=json.dumps(event_ctx),
            )

            async def _fake_launch(sess, _ws, _db):
                sess.phase = "running"

            with patch("swarmer.routers.sessions._do_launch", new=AsyncMock(side_effect=_fake_launch)) as launch_mock:
                await _drain_queued_dispatches(db)
                self.assertEqual(launch_mock.await_count, 1)

            row = await get_dispatch_state(db, "stolostron/agent-swarm", 153, "sha153", "review_comments", session_id=session.id)
            self.assertIsNotNone(row)
            self.assertEqual(row.status, "dispatched")
            self.assertEqual(row.attempts, 1)

    async def test_fix_author_pr_can_still_dispatch_review_signal(self):
        import httpx
        from unittest.mock import AsyncMock, patch

        from swarmer.pr_state import CheckState, PRState
        from swarmer.pr_watcher import _evaluate_and_dispatch_prs
        from swarmer.pr_watcher_store import get_dispatch_state

        session_id = await self._seed_session_with_schedules()
        from swarmer.models.session_schedule import SessionSchedule

        pr_state = PRState(
            repo="stolostron/agent-swarm",
            pr_number=153,
            title="PR 153",
            body="",
            author_login="author-1",
            author_association="MEMBER",
            is_draft=False,
            head_sha="sha154",
            head_ref="feature",
            base_ref="main",
            mergeable_state="clean",
            is_fork=False,
            unresolved_review_comments=0,
            coderabbit_unresolved_comments=0,
            check_state=CheckState(total=1, passing=1),
            raw_payload={},
        )

        async with self.session_factory() as db:
            session, _sched_hygiene, _sched_catchall = await self._load_session_with_schedules(db, session_id)

            sched_review = SessionSchedule(
                session_id=session.id,
                trigger_type="event",
                event_condition="new_pr_or_commit",
                author_scope="all",
                label="Review",
            )
            db.add(sched_review)
            await db.commit()
            await db.refresh(sched_review)

            with patch("swarmer.pr_watcher._fetch_open_prs", new=AsyncMock(return_value=[{"number": 153}])), \
                 patch("swarmer.pr_watcher._build_pr_state", new=AsyncMock(return_value=(pr_state, []))), \
                 patch("swarmer.routers.sessions._do_launch", new=AsyncMock()):
                async with httpx.AsyncClient() as client:
                    await _evaluate_and_dispatch_prs(
                        client,
                        "stolostron/agent-swarm",
                        [(sched_review, session)],
                        None,
                        db,
                    )

            review_row = await get_dispatch_state(db, "stolostron/agent-swarm", 153, "sha154", "new_pr_or_commit", session_id=session.id)
            self.assertIsNotNone(review_row)
            self.assertEqual(review_row.status, "dispatched")

    async def test_evaluate_dispatches_multiple_independent_sessions_same_condition(self):
        import httpx
        from unittest.mock import AsyncMock, patch
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from swarmer.models.session import Session
        from swarmer.models.session_repo import SessionRepo
        from swarmer.models.session_schedule import SessionSchedule
        from swarmer.models.workspace import Workspace
        from swarmer.pr_state import CheckState, PRState
        from swarmer.pr_watcher import _evaluate_and_dispatch_prs
        from swarmer.pr_watcher_store import get_dispatch_state

        async with self.session_factory() as db:
            ws = Workspace(display_name="Multi-session WS", namespace="multi-ws", description="")
            db.add(ws)
            await db.commit()
            await db.refresh(ws)

            session1 = Session(workspace_id=ws.id, name="s1", mode="prompt", provider="", agent_tool="opencode", instruction_prompt="")
            session2 = Session(workspace_id=ws.id, name="s2", mode="prompt", provider="", agent_tool="opencode", instruction_prompt="")
            db.add_all([session1, session2])
            await db.commit()
            await db.refresh(session1)
            await db.refresh(session2)

            repo1 = SessionRepo(session_id=session1.id, repo_url="https://github.com/stolostron/agent-swarm.git", local_path="agent-swarm")
            repo2 = SessionRepo(session_id=session2.id, repo_url="https://github.com/stolostron/agent-swarm.git", local_path="agent-swarm")
            sched1 = SessionSchedule(
                session_id=session1.id,
                trigger_type="event",
                event_condition="new_pr_or_commit",
                author_scope="all",
                label="Reviewer 1",
            )
            sched2 = SessionSchedule(
                session_id=session2.id,
                trigger_type="event",
                event_condition="new_pr_or_commit",
                author_scope="all",
                label="Reviewer 2",
            )
            db.add_all([repo1, repo2, sched1, sched2])
            await db.commit()
            await db.refresh(sched1)
            await db.refresh(sched2)

            # Reload with relationships
            q1 = await db.execute(
                select(Session)
                .options(selectinload(Session.workspace), selectinload(Session.github_pat), selectinload(Session.repos), selectinload(Session.schedules))
                .where(Session.id == session1.id)
            )
            s1 = q1.scalar_one()
            q2 = await db.execute(
                select(Session)
                .options(selectinload(Session.workspace), selectinload(Session.github_pat), selectinload(Session.repos), selectinload(Session.schedules))
                .where(Session.id == session2.id)
            )
            s2 = q2.scalar_one()

            pr_state = PRState(
                repo="stolostron/agent-swarm",
                pr_number=200,
                title="PR 200",
                body="",
                author_login="author-1",
                author_association="MEMBER",
                is_draft=False,
                head_sha="sha200",
                head_ref="feature",
                base_ref="main",
                mergeable_state="clean",
                is_fork=False,
                unresolved_review_comments=0,
                coderabbit_unresolved_comments=0,
                check_state=CheckState(total=1, passing=1),
                raw_payload={},
            )

            with patch("swarmer.pr_watcher._fetch_open_prs", new=AsyncMock(return_value=[{"number": 200}])), \
                 patch("swarmer.pr_watcher._build_pr_state", new=AsyncMock(return_value=(pr_state, []))), \
                 patch("swarmer.routers.sessions._do_launch", new=AsyncMock()) as launch_mock:
                async with httpx.AsyncClient() as client:
                    await _evaluate_and_dispatch_prs(
                        client,
                        "stolostron/agent-swarm",
                        [(sched1, s1), (sched2, s2)],
                        None,
                        db,
                    )

                self.assertEqual(launch_mock.await_count, 2)

            row1 = await get_dispatch_state(db, "stolostron/agent-swarm", 200, "sha200", "new_pr_or_commit", session_id=s1.id)
            row2 = await get_dispatch_state(db, "stolostron/agent-swarm", 200, "sha200", "new_pr_or_commit", session_id=s2.id)

            self.assertIsNotNone(row1)
            self.assertEqual(row1.status, "dispatched")
            self.assertEqual(row1.session_id, s1.id)

            self.assertIsNotNone(row2)
            self.assertEqual(row2.status, "dispatched")
            self.assertEqual(row2.session_id, s2.id)


if __name__ == "__main__":
    unittest.main()
