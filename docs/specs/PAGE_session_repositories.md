# Session Repositories Component

## Purpose

The repository component defines the git inputs cloned into a session sandbox,
including URL, branch, local path, and credential source.

## Sources

- Templates: `swarmer/templates/sessions/_repo_list.html`, `_repo_items.html`,
  `_repo_picker.html`, `_pat_select.html`
- Router: `swarmer/routers/sessions.py` repository handlers
- REST API: `swarmer/api/v1/repos.py`
- Runtime: OpenShell sandbox setup and GitHub auth helpers

## Methods and tooling

- HTMX add/delete actions return `HX-Trigger: repoListChanged`.
- Stable list shell refreshes `/repos/items` without replacing the card.
- Repository picker uses PAT or GitHub App installation-token discovery.
- URL validation rejects embedded credentials/tokens.
- OpenShell performs clone/setup inside the sandbox.

## Algorithm

1. Reject repository changes for active sessions.
2. Validate URL and normalize branch and local path.
3. Default local path to the repository name when omitted.
4. Persist the `SessionRepo` relationship.
5. On launch, clone each repo into its requested path and checkout branch.
6. Refresh the list and repository health information after mutation.

## API references

- `GET /api/v1/workspaces/{ws_id}/sessions/{sid}/repos`
- `POST /api/v1/workspaces/{ws_id}/sessions/{sid}/repos`
- `DELETE /api/v1/workspaces/{ws_id}/sessions/{sid}/repos/{rid}`
- HTML `/repos/items` and `/repos/pick` partial endpoints

## Security and acceptance

- No token may occur in a repository URL.
- PAT/App selection controls discovery and launch authentication but is never
  rendered as a token.
- Duplicate or invalid repository state must not create a partial launch.
- Multiple repositories must have distinct usable local paths. The current
  implementation does not yet enforce this requirement; equivalent paths such
  as `foo` and `./foo` can be persisted.
