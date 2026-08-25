# Swarm PR Events Watcher & Session Dispatcher

The **Swarm PR Events Watcher** (`swarmer/pr_watcher.py`) is an in-process, firewall-safe background loop that runs inside the Swarmer pod's FastAPI lifespan (alongside `swarmer/scheduler.py`). It monitors GitHub repositories for Pull Request state changes and dispatches Swarm sessions only when actionable work is needed from trusted authors — with **zero standalone configuration files**. All triggers, repos, conditions, and author scopes are configured entirely through the Web UI's Scheduling section and stored in `swarmer.db`.

---

## 1. Core Architecture

```text
GitHub Events API (Outbound ETag Polling)
                 │
  ┌──────────────┴──────────────┐
  ▼                             ▼
304 Not Modified              200 OK (New Events Detected)
(0 rate-limit cost)             │
                                ▼
                       Scan Open PRs for Repo
                                │
                                ▼
               ┌────────────────────────────────┐
               │    Author & Trust Filtering    │
               │  - Exclude bot PRs & forks     │
               │  - Check author trust layers   │
               └────────────────┬───────────────┘
                                │
                                ▼
               ┌────────────────────────────────┐
               │  CI Completion & Debounce Bar  │
               │  - 0 IN_PROGRESS / QUEUED      │
               │  - 90–120s quiet period        │
               └────────────────┬───────────────┘
                                │
                                ▼
               ┌────────────────────────────────┐
               │   Circuit Breaker & Dedup DB   │
               │  - swarmer.db (repo, pr, sha)  │
               │  - Max 3 attempts per SHA      │
               └────────────────┬───────────────┘
                                │
                                ▼
                    Dispatch Swarm Session
                (in-process via _do_launch())
```

### Fast Path (Event-Driven ETag Polling)
- Polls `GET https://api.github.com/repos/{owner}/{repo}/events` with `If-None-Match: <etag>`.
- `304 Not Modified` returns immediately with **0 rate limit cost**.
- `200 OK` wakes up the evaluator to scan open PRs in that repository.

### Slow Path (Hybrid Safety Net)
- Runs a full periodic sweep every 30–60 minutes across event-scoped repos.
- Catches untracked state transitions such as `mergeable: dirty` (GitHub computes mergeability asynchronously and does not emit event stream payloads for backend merge conflict calculations).

### Scoped Watched-Repo Resolution
- The watcher **only polls repositories that have at least one enabled `event` trigger**, discovered by directly querying `SessionSchedule` rows joined to `SessionRepo` in the database (`swarmer/pr_watcher_store.py:resolve_event_triggers`).
- Repositories configured with cron schedules (e.g. daily CVE scans or weekly package audits) are handled in-process by Swarmer's existing `swarmer/scheduler.py` and are **never polled** by the watcher loop.

---

## 2. Trigger Model & Author Routing Taxonomy

Triggers are modeled as first-class items supporting both `event` and `cron` types, configured entirely via the Web UI Scheduling section on a session's detail page:

| Category | Author Scope | Condition / Trigger | Target Action | Prompt |
| :--- | :--- | :--- | :--- | :--- |
| **Fix Authors** | `self` (per-schedule `fix_authors` GitHub logins) | CI Failure, Merge Conflict, Review Comments | `pr-fix` | `prompts/auto-pr-fix-agent.md` |
| **Team PRs** | `team` (Trusted Collaborators) | New PR opened, new commit pushed | `pr-review` | Session's configured prompt |
| **Automated Bots** | `bots` (`dependabot`, `renovate`, `app/*`) | Any | `auto-merge-defer` | Defer to repo's GitHub Actions |
| **Untrusted / External** | Non-members, first-time contributors | No `ok-to-review` label | `ignore` | Ignored |

---

## 3. Team-PR Trust & Security Guardrails

To prevent arbitrary code execution and resource exhaustion from malicious or drive-by external pull requests, the watcher enforces a 3-layer trust model:

1. **Layer 1: Native GitHub Author Association (Default)**
   - Automatically trusts PR authors with `OWNER`, `MEMBER`, or `COLLABORATOR` associations.
   - Treats `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, and `NONE` as untrusted.
2. **Layer 2: Workspace Policy**
   - Configurable explicit allowlists or GitHub organization team memberships (`GET /orgs/{org}/teams/{slug}/members`).
3. **Layer 3: The `ok-to-review` Label Gate**
   - External PRs remain ignored until a repository collaborator adds the `ok-to-review` label.
   - **RBAC Protected:** Label application on GitHub requires Triage, Write, or Admin permissions on the base repository; external fork authors cannot apply this label. When label timeline events are available, the watcher verifies the label applier's identity and falls back to base repository RBAC when timeline data is unavailable.
   - **Invalidation:** New commits pushed to an external PR automatically invalidate prior approval and require re-review.

---

## 4. Resilience & Circuit Breaker

- **CI Completion Barrier:** Checks must have 0 `in_progress` or `queued` check runs plus a 90-second quiet debounce period before `pr-fix` is dispatched.
- **Circuit Breaker:** Maximum 3 fix attempts per `head_sha`. If the agent fails to resolve CI after 3 attempts, the status is marked `blocked`. A new human commit to the branch resets the counter.
- **Self-Trigger Guard:** Events generated by the bot agent's own commits are ignored to prevent recursive dispatch loops.
- **Fork Push Guard:** `pr-fix` is only dispatched for fork PRs when `maintainer_can_modify` is true (i.e. the maintainer/App can actually push to the fork branch); otherwise the PR is ignored for fixes.

---

## 5. Configuration (Web UI Only)

There is no configuration file. Everything is configured per-session in the Web UI's **Scheduling** section:

1. Navigate to a session's detail page → **Schedule** card → **+ Add trigger / schedule**.
2. Select **⚡ GitHub Event** as the Trigger Type.
3. Choose the **Event Condition** (`ci_fail_or_conflict`, `new_pr_or_commit`, `review_comments`, `any_actionable`).
4. Choose the **Author Scope** (`self`, `team`, `bots`, `all`).
5. If `self` is selected, provide a comma-separated list of GitHub logins in **Fix Authors** (e.g. `<github-login-1>, <github-login-2>`).
6. Save. The in-process watcher automatically picks up the new trigger on its next repo-refresh cycle — no restart required.

### Operational Tuning (ENV / ConfigMap)

| Env Var | Default | Purpose |
|---|---|---|
| `PR_WATCHER_ENABLED` | `true` | Enable/disable the in-process background loop |
| `PR_WATCHER_POLL_INTERVAL` | `30` | Seconds between outbound ETag polls |
| `PR_WATCHER_SWEEP_INTERVAL` | `1800` | Seconds between full sweeps (merge-conflict safety net) |
| `PR_WATCHER_DEBOUNCE_SECONDS` | `90` | Quiet-period debounce after CI checks finish |
| `PR_WATCHER_MAX_FIX_ATTEMPTS` | `3` | Circuit breaker limit per commit SHA |

### GitHub API Credentials

The watcher resolves a GitHub token per repository in priority order:
1. A GitHub PAT explicitly attached to a session watching that repo.
2. The workspace's configured GitHub App (mints a short-lived Installation Access Token).
3. Fallback environment variables: `GH_TOKEN_<ORG>`, `GITHUB_TOKEN`, or `GH_TOKEN`.

See [docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md#debugging--log-retrieval-reference) for log retrieval and debugging instructions.
