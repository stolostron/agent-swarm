"""Tests for Swarm PR Events Watcher and PR State Library."""

from datetime import datetime, timedelta, timezone
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.lib.pr_state import (  # noqa: E402
    CheckState,
    PRAction,
    PRState,
    TrustPolicy,
    TrustStrategy,
    classify_pr_action,
    evaluate_author_trust,
    evaluate_ci_completion_barrier,
    normalize_ci_checks,
)
from scripts.swarm_pr_watcher import (  # noqa: E402
    GitHubClient,
    StateStore,
    SwarmPRWatcher,
    TriggerConfig,
    WatcherConfig,
    extract_repo_from_url,
    resolve_event_scoped_repos,
)


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
        for assoc in ("OWNER", "MEMBER", "COLLABORATOR"):
            self.base_pr.author_association = assoc
            res = evaluate_author_trust(self.base_pr, policy)
            self.assertTrue(res.is_trusted)
            self.assertEqual(res.matched_layer, "association")

    def test_layer1_native_untrusted_associations(self):
        policy = TrustPolicy(strategy=TrustStrategy.ORG_AND_COLLABORATORS)
        for assoc in ("CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "NONE"):
            self.base_pr.author_association = assoc
            res = evaluate_author_trust(self.base_pr, policy)
            self.assertFalse(res.is_trusted)
            self.assertEqual(res.matched_layer, "untrusted")

    def test_layer2_explicit_allowlist(self):
        policy = TrustPolicy(strategy=TrustStrategy.EXPLICIT_ALLOWLIST, allowlist={"alice", "jpacker"})
        self.base_pr.author_login = "jpacker"
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
        policy = TrustPolicy(strategy=TrustStrategy.ORG_AND_COLLABORATORS, trusted_label="ok-to-review")
        self.base_pr.author_association = "NONE"
        self.base_pr.labels = {"ok-to-review", "enhancement"}

        # Label present, verified with applier event
        label_events = [
            {"name": "ok-to-review", "actor": {"login": "maintainer-alice", "author_association": "MEMBER"}}
        ]
        res = evaluate_author_trust(self.base_pr, policy, label_events=label_events)
        self.assertTrue(res.is_trusted)
        self.assertEqual(res.matched_layer, "trusted_label")


class TestClassifyPRAction(unittest.TestCase):
    def setUp(self):
        self.fix_authors = {"jpacker"}
        self.bot_logins = {"dependabot[bot]", "renovate[bot]"}
        self.policy = TrustPolicy()

    def test_draft_pr_ignored(self):
        pr = PRState(
            repo="org/repo",
            pr_number=1,
            title="WIP",
            body="",
            author_login="jpacker",
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
            author_login="jpacker",
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
            author_login="jpacker",
            author_association="OWNER",
            is_draft=False,
            head_sha="333",
            head_ref="branch",
            base_ref="main",
            mergeable_state="dirty",
            is_fork=True,
            fork_owner="fork-owner",
        )
        action, reason = classify_pr_action(pr, self.fix_authors, self.bot_logins, self.policy)
        self.assertEqual(action, PRAction.IGNORE)
        self.assertIn("skipping pr-fix", reason.lower())

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


class TestEventScopedRepos(unittest.TestCase):
    def test_resolve_event_scoped_repos_excludes_cron(self):
        triggers = [
            TriggerConfig(
                id="t1",
                name="Event Fix",
                trigger_type="event",
                condition="ci_fail_or_conflict",
                author_scope="self",
                workspace_id=1,
                session_id=1,
                repos=["https://github.com/OpenShift-Fleet/agentic-sdlc.git", "stolostron/agent-swarm"],
                enabled=True,
            ),
            TriggerConfig(
                id="t2",
                name="Cron CVE",
                trigger_type="cron",
                condition="0 9 * * 1-5",
                author_scope="all",
                workspace_id=1,
                session_id=2,
                repos=["https://github.com/stolostron/cron-only-repo.git"],
                enabled=True,
            ),
            TriggerConfig(
                id="t3",
                name="Disabled Event",
                trigger_type="event",
                condition="new_pr_or_commit",
                author_scope="team",
                workspace_id=1,
                session_id=3,
                repos=["https://github.com/stolostron/disabled-repo.git"],
                enabled=False,
            ),
        ]

        scoped = resolve_event_scoped_repos(triggers)
        self.assertIn("OpenShift-Fleet/agentic-sdlc", scoped)
        self.assertIn("stolostron/agent-swarm", scoped)
        self.assertNotIn("stolostron/cron-only-repo", scoped)
        self.assertNotIn("stolostron/disabled-repo", scoped)
        self.assertEqual(len(scoped), 2)

    def test_extract_repo_from_url(self):
        self.assertEqual(extract_repo_from_url("https://github.com/org/repo.git"), "org/repo")
        self.assertEqual(extract_repo_from_url("git@github.com:org/repo.git"), "org/repo")
        self.assertEqual(extract_repo_from_url("org/repo"), "org/repo")


class TestStateStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self.tmp.close()
        self.store = StateStore(self.tmp.name)

    def tearDown(self):
        if os.path.exists(self.tmp.name):
            os.unlink(self.tmp.name)

    def test_record_dispatch_and_circuit_breaker(self):
        repo = "org/repo"
        pr_number = 12
        head_sha = "abc111"
        action = "pr-fix"

        # Attempt 1
        att1 = self.store.record_dispatch(repo, pr_number, head_sha, action, session_id=10, status="dispatched")
        self.assertEqual(att1, 1)
        state = self.store.get_action_state(repo, pr_number, head_sha, action)
        self.assertIsNotNone(state)
        self.assertEqual(state["status"], "dispatched")
        self.assertEqual(state["attempts"], 1)

        # Attempt 2
        att2 = self.store.record_dispatch(repo, pr_number, head_sha, action, session_id=10, status="dispatched")
        self.assertEqual(att2, 2)

        # Attempt 3 -> Mark blocked
        self.store.record_dispatch(repo, pr_number, head_sha, action, session_id=10, status="blocked")
        state = self.store.get_action_state(repo, pr_number, head_sha, action)
        self.assertEqual(state["status"], "blocked")

        # New SHA resets attempt counter for the new SHA
        new_sha = "def222"
        new_att = self.store.record_dispatch(repo, pr_number, new_sha, action, session_id=10, status="dispatched")
        self.assertEqual(new_att, 1)

    def test_etags_storage_and_cleanup(self):
        self.store.save_etag("org/repo1", "W/'etag1'")
        self.store.save_etag("org/repo2", "W/'etag2'")

        self.assertEqual(self.store.get_etag("org/repo1"), "W/'etag1'")
        self.assertEqual(self.store.get_etag("org/repo2"), "W/'etag2'")

        # Clean stale etags when repo2 is removed from active set
        self.store.clean_stale_etags({"org/repo1"})
        self.assertEqual(self.store.get_etag("org/repo1"), "W/'etag1'")
        self.assertIsNone(self.store.get_etag("org/repo2"))


class TestGitHubClientTokens(unittest.TestCase):
    def test_resolve_token_priority(self):
        client = GitHubClient(token_map={"MyOrg": "custom_token"})
        self.assertEqual(client.resolve_token("MyOrg/my-repo"), "custom_token")

        with patch.dict(os.environ, {"GH_TOKEN_TEST_ORG": "env_token"}):
            self.assertEqual(client.resolve_token("test-org/repo"), "env_token")

        with patch.dict(os.environ, {"GITHUB_TOKEN": "global_fallback"}, clear=True):
            self.assertEqual(client.resolve_token("unknown-org/repo"), "global_fallback")


class TestSwarmPRWatcherLifecycle(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self.tmp.close()
        self.config = WatcherConfig(
            api_url="http://mock-swarmer:8090",
            api_token="test-token",
            db_path=self.tmp.name,
            fix_authors={"jpacker"},
            triggers=[
                TriggerConfig(
                    id="tr1",
                    name="Fix My PRs",
                    trigger_type="event",
                    condition="ci_fail_or_conflict",
                    author_scope="self",
                    workspace_id=1,
                    session_id=10,
                    repos=["https://github.com/org/repo.git"],
                    enabled=True,
                )
            ],
        )
        self.watcher = SwarmPRWatcher(self.config)

    def tearDown(self):
        if os.path.exists(self.tmp.name):
            os.unlink(self.tmp.name)

    async def test_evaluate_and_dispatch_pr_dry_run(self):
        pr = PRState(
            repo="org/repo",
            pr_number=50,
            title="Test PR",
            body="Desc",
            author_login="jpacker",
            author_association="OWNER",
            is_draft=False,
            head_sha="head123",
            head_ref="my-branch",
            base_ref="main",
            mergeable_state="dirty",
            is_fork=False,
        )
        success, reason = await self.watcher.evaluate_and_dispatch_pr(pr, dry_run=True)
        self.assertTrue(success)
        self.assertIn("[DRY-RUN]", reason)

    async def test_evaluate_and_dispatch_pr_success(self):
        pr = PRState(
            repo="org/repo",
            pr_number=51,
            title="Test PR 2",
            body="Desc",
            author_login="jpacker",
            author_association="OWNER",
            is_draft=False,
            head_sha="head456",
            head_ref="my-branch",
            base_ref="main",
            mergeable_state="dirty",
            is_fork=False,
        )

        with patch.object(self.watcher.dispatcher, "launch_session", new_callable=AsyncMock) as mock_launch:
            mock_launch.return_value = (True, "Session launched successfully", {"status": "ok"})
            success, reason = await self.watcher.evaluate_and_dispatch_pr(pr, dry_run=False)
            self.assertTrue(success)
            self.assertIn("Dispatched session 10", reason)
            mock_launch.assert_called_once()

    async def test_poll_repo_events_304_and_200(self):
        repo = "org/repo"
        # 304 Not Modified
        with patch.object(self.watcher.gh, "fetch_events", return_value=(304, [], "W/'etag1'")):
            rescan = await self.watcher.poll_repo_events(repo)
            self.assertFalse(rescan)

        # 200 OK
        with patch.object(self.watcher.gh, "fetch_events", return_value=(200, [{"type": "PushEvent"}], "W/'etag2'")), \
             patch.object(self.watcher, "scan_repo_prs", new_callable=AsyncMock) as mock_scan:
            mock_scan.return_value = 1
            rescan = await self.watcher.poll_repo_events(repo)
            self.assertTrue(rescan)
            mock_scan.assert_called_once_with(repo, dry_run=False)
            self.assertEqual(self.watcher.store.get_etag(repo), "W/'etag2'")

    async def test_refresh_watched_repos_removes_stale_etags(self):
        self.watcher.store.save_etag("org/repo", "etag1")
        self.watcher.store.save_etag("org/old-repo", "etag2")

        # Disable the only trigger
        self.config.triggers[0].enabled = False
        active = self.watcher.refresh_watched_repos()
        self.assertEqual(len(active), 0)
        # ETag for org/repo should have been removed
        self.assertIsNone(self.watcher.store.get_etag("org/repo"))

    async def test_circuit_breaker_tripped(self):
        pr = PRState(
            repo="org/repo",
            pr_number=52,
            title="Test PR 3",
            body="Desc",
            author_login="jpacker",
            author_association="OWNER",
            is_draft=False,
            head_sha="head789",
            head_ref="my-branch",
            base_ref="main",
            mergeable_state="dirty",
            is_fork=False,
        )

        # Pre-set attempts to 3 and status to blocked
        self.watcher.store.record_dispatch("org/repo", 52, "head789", "pr-fix", session_id=10, status="blocked")
        success, reason = await self.watcher.evaluate_and_dispatch_pr(pr, dry_run=False)
        self.assertFalse(success)
        self.assertIn("circuit breaker tripped", reason.lower())

    async def test_run_sweep_handles_dirty_prs(self):
        raw_prs = [{"number": 99, "head": {"sha": "sha99", "ref": "feat"}, "base": {"ref": "main"}, "user": {"login": "jpacker"}, "author_association": "OWNER", "draft": False, "title": "Sweep PR"}]
        detailed_pr = {**raw_prs[0], "mergeable_state": "dirty"}

        with patch.object(self.watcher.gh, "fetch_open_prs", return_value=raw_prs), \
             patch.object(self.watcher.gh, "fetch_pr_details", return_value=detailed_pr), \
             patch.object(self.watcher.gh, "fetch_check_runs", return_value=[]), \
             patch.object(self.watcher.gh, "fetch_review_comments", return_value=[]), \
             patch.object(self.watcher.dispatcher, "launch_session", new_callable=AsyncMock) as mock_launch:
            mock_launch.return_value = (True, "Session launched", {})
            await self.watcher.run_sweep(dry_run=False)
            mock_launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
