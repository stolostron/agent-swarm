"""Tests for the in-process Swarm PR Events Watcher (swarmer/pr_watcher.py,
swarmer/pr_state.py, swarmer/pr_watcher_store.py)."""

from datetime import datetime, timedelta, timezone
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from swarmer.pr_state import (  # noqa: E402
    CheckState,
    PRAction,
    PRState,
    TrustPolicy,
    TrustStrategy,
    classify_pr_action,
    evaluate_author_trust,
    evaluate_ci_completion_barrier,
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
        policy = TrustPolicy(strategy=TrustStrategy.EXPLICIT_ALLOWLIST, allowlist={"alice", "jnpacker"})
        self.base_pr.author_login = "jnpacker"
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


class TestClassifyPRAction(unittest.TestCase):
    def setUp(self):
        self.fix_authors = {"jnpacker"}
        self.bot_logins = {"dependabot[bot]", "renovate[bot]"}
        self.policy = TrustPolicy()

    def test_draft_pr_ignored(self):
        pr = PRState(
            repo="org/repo",
            pr_number=1,
            title="WIP",
            body="",
            author_login="jnpacker",
            author_association="OWNER",
            is_draft=True,
            head_sha="111",
            head_ref="branch",
            base_ref="main",
            mergeable_state="dirty",
            is_fork=False,
        )
        action, reason = classify_pr_action(pr, self.fix_authors, self.bot_logins, self.policy)
        self.assertEqual(action, PRAction.IGNORE)

    def test_fix_author_merge_conflict(self):
        pr = PRState(
            repo="org/repo",
            pr_number=2,
            title="My feature",
            body="",
            author_login="jnpacker",
            author_association="OWNER",
            is_draft=False,
            head_sha="222",
            head_ref="branch",
            base_ref="main",
            mergeable_state="dirty",
            is_fork=False,
        )
        action, reason = classify_pr_action(pr, self.fix_authors, self.bot_logins, self.policy)
        self.assertEqual(action, PRAction.FIX)
        self.assertIn("merge conflict", reason.lower())

    def test_fix_author_fork_skipped(self):
        pr = PRState(
            repo="org/repo",
            pr_number=3,
            title="External fix attempt",
            body="",
            author_login="jnpacker",
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
        action, reason = classify_pr_action(pr, self.fix_authors, self.bot_logins, self.policy)
        self.assertEqual(action, PRAction.IGNORE)
        self.assertIn("without maintainer push permissions", reason.lower())

    def test_fix_author_fork_allowed_when_maintainer_can_modify(self):
        pr = PRState(
            repo="org/repo",
            pr_number=3,
            title="Maintainer editable fork fix",
            body="",
            author_login="jnpacker",
            author_association="OWNER",
            is_draft=False,
            head_sha="333",
            head_ref="branch",
            base_ref="main",
            mergeable_state="dirty",
            is_fork=True,
            fork_owner="fork-owner",
            raw_payload={"maintainer_can_modify": True},
        )
        action, reason = classify_pr_action(pr, self.fix_authors, self.bot_logins, self.policy)
        self.assertEqual(action, PRAction.FIX)
        self.assertIn("merge conflict", reason.lower())

    def test_bot_author_deferred_to_auto_merge(self):
        pr = PRState(
            repo="org/repo",
            pr_number=4,
            title="Bump lodash",
            body="",
            author_login="dependabot[bot]",
            author_association="CONTRIBUTOR",
            is_draft=False,
            head_sha="444",
            head_ref="dependabot/npm_and_yarn/lodash",
            base_ref="main",
            mergeable_state="clean",
            is_fork=False,
        )
        action, reason = classify_pr_action(pr, self.fix_authors, self.bot_logins, self.policy)
        self.assertEqual(action, PRAction.AUTO_MERGE_DEFER)

    def test_team_pr_routed_to_review(self):
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
            has_agent_review_on_head=False,
        )
        action, reason = classify_pr_action(pr, self.fix_authors, self.bot_logins, self.policy)
        self.assertEqual(action, PRAction.REVIEW)


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
            get_action_state,
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
                db, repo="owner/repo", pr_number=10, head_sha="sha1", action="pr-fix", session_id=1, status="dispatched"
            )
            self.assertEqual(attempts, 1)
            self.assertTrue(await is_blocked(db, "owner/repo", 10, "sha1", "pr-fix"))

            # 2. Check state
            row = await get_action_state(db, "owner/repo", 10, "sha1", "pr-fix")
            self.assertIsNotNone(row)
            self.assertEqual(row.status, "dispatched")

            # 3. Second attempt increments the counter
            attempts2 = await record_dispatch(
                db, repo="owner/repo", pr_number=10, head_sha="sha1", action="pr-fix", session_id=1, status="dispatched"
            )
            self.assertEqual(attempts2, 2)

            # 4. Reconcile completed (succeeded -> completed)
            await reconcile_completed(db, session_id=1, phase="succeeded")
            row_after = await get_action_state(db, "owner/repo", 10, "sha1", "pr-fix")
            self.assertEqual(row_after.status, "completed")

            # 4b. Reconcile failed below max attempts -> status "failed"
            await record_dispatch(
                db, repo="owner/repo", pr_number=11, head_sha="sha_fail", action="pr-fix", session_id=2, status="dispatched"
            )
            await reconcile_completed(db, session_id=2, phase="failed")
            row_failed = await get_action_state(db, "owner/repo", 11, "sha_fail", "pr-fix")
            self.assertEqual(row_failed.status, "failed")
            self.assertIn("phase 'failed'", row_failed.last_error)

            # 4c. Reconcile failed at max attempts -> status "blocked"
            for _ in range(3):
                await record_dispatch(
                    db, repo="owner/repo", pr_number=12, head_sha="sha_cap", action="pr-fix", session_id=3, status="dispatched"
                )
            await reconcile_completed(db, session_id=3, phase="failed")
            row_blocked = await get_action_state(db, "owner/repo", 12, "sha_cap", "pr-fix")
            self.assertEqual(row_blocked.status, "blocked")
            self.assertIn("Max dispatch attempts", row_blocked.last_error)

            # 5. New head_sha resets the attempt counter
            attempts3 = await record_dispatch(
                db, repo="owner/repo", pr_number=10, head_sha="sha2", action="pr-fix", session_id=1, status="dispatched"
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
                fix_authors="jnpacker",
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
            self.assertEqual(sched.fix_author_logins, {"jnpacker"})
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

            unresolved, cr_count, has_review = await _fetch_reviews_and_threads(
                client, "stolostron/agent-swarm", 10, "sha123", "dummy-token"
            )
            self.assertEqual(unresolved, 1)
            self.assertEqual(cr_count, 1)
            self.assertTrue(has_review)


if __name__ == "__main__":
    unittest.main()
