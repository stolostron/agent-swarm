# Swarm PR Events Watcher & Session Dispatcher

The **Swarm PR Events Watcher** (`scripts/swarm-pr-watcher.py`) is an autonomous, firewall-safe daemon for Agent Swarm that monitors GitHub repositories for Pull Request state changes and dispatches Swarm sessions only when actionable work is needed from trusted authors.

---

## 1. Core Architecture

```
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
               │  - SQLite (repo, pr, sha, act) │
               │  - Max 3 attempts per SHA      │
               └────────────────┬───────────────┘
                                │
                                ▼
                    Dispatch Swarm Session
             (POST /api/v1/workspaces/.../launch)
```

### Fast Path (Event-Driven ETag Polling)
- Polls `GET https://api.github.com/repos/{owner}/{repo}/events` with `If-None-Match: <etag>`.
- `304 Not Modified` returns immediately with **0 rate limit cost**.
- `200 OK` wakes up the evaluator to scan open PRs in that repository.

### Slow Path (Hybrid Safety Net)
- Runs a full periodic sweep every 30–60 minutes across event-scoped repos.
- Catches untracked state transitions such as `mergeable: dirty` (GitHub computes mergeability asynchronously and does not emit event stream payloads for backend merge conflict calculations).

### Scoped Watched-Repo Resolution
- The watcher **only polls repositories that have at least one enabled `event` trigger**.
- Repositories configured with cron schedules (e.g. daily CVE scans or weekly package audits) are handled in-process by Swarmer's existing `swarmer/scheduler.py` and are **never polled** by the watcher daemon.

---

## 2. Trigger Model & Author Routing Taxonomy

Triggers are modeled as first-class items supporting both `event` and `cron` types:

| Category | Author Scope | Condition / Trigger | Target Action | Prompt |
| :--- | :--- | :--- | :--- | :--- |
| **Fix Authors** | `fix_authors` (Self / Laptop) | CI Failure, Merge Conflict, Review Comments | `pr-fix` | `prompts/auto-pr-fix-agent.md` |
| **Team PRs** | Trusted Collaborators | New PR opened, new commit pushed | `pr-review` | `prompts/auto-pr-review-agent.md` |
| **Automated Bots** | `dependabot`, `renovate`, `app/*` | Any | `auto-merge-defer` | Defer to repo's GitHub Actions |
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
   - **RBAC Protected:** Label application on GitHub requires Triage, Write, or Admin permissions on the base repository; external fork authors cannot apply this label.
   - **Defense-in-depth:** The watcher verifies that the user who added the label is a trusted collaborator.
   - **Invalidation:** New commits pushed to an external PR automatically invalidate prior approval and require re-review.

---

## 4. Resilience & Circuit Breaker

- **CI Completion Barrier:** Checks must have 0 `in_progress` or `queued` check runs plus a 90-second quiet debounce period before `pr-fix` is dispatched.
- **Circuit Breaker:** Maximum 3 fix attempts per `head_sha`. If the agent fails to resolve CI after 3 attempts, the status is marked `blocked`. A new human commit to the branch resets the counter.
- **Self-Trigger Guard:** Events generated by the bot agent's own commits are ignored to prevent recursive dispatch loops.
- **Fork Push Guard:** `pr-fix` is skipped for PRs originating from forks where push access is unavailable.

---

## 5. Operations & CLI Usage

### Running Standalone
```bash
# Run a single evaluation cycle
python3 scripts/swarm-pr-watcher.py --config config/swarm-watcher.json --once

# Run continuous daemon
python3 scripts/swarm-pr-watcher.py --config config/swarm-watcher.json --poll-interval 30

# Run in dry-run mode (evaluates PRs without dispatching Swarm sessions)
python3 scripts/swarm-pr-watcher.py --dry-run -v
```

### Environment Variables
- `SWARM_API_URL`: Swarmer server endpoint (e.g. `http://localhost:8090`).
- `SWARM_API_TOKEN`: Bearer token for Swarmer REST API.
- `GH_TOKEN_<ORG>`: Multi-org GitHub API tokens (e.g. `GH_TOKEN_OPENSHIFT_FLEET`, `GH_TOKEN_STOLOSTRON`).
- `GITHUB_TOKEN`: Fallback GitHub token.
