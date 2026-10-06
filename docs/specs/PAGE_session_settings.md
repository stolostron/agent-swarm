# Session Settings Component

## Purpose

Session settings determine what runs at launch: agent tool, provider/model,
working branch, prompt, GitHub credential, and enabled MCP servers.

## Sources

- Templates: `swarmer/templates/sessions/detail.html`, `_provider_select.html`,
  `_prompt_picker.html`, `_pat_select.html`, `_launch_dialog.html`
- Router: `swarmer/routers/sessions.py:session_edit`, `session_launch_dialog`,
  and `session_launch`
- REST API: session `PUT`, `set-mode`, and `set-provider` operations
- Runtime: `swarmer/agent_tools.py`, `swarmer/openshell_client.py`

## Methods and tooling

- Form autosave via browser `fetch` and `FormData`.
- Serialized save promise prevents overlapping configuration writes.
- Jinja2 renders available tools/providers and disables active-session fields.
- Agent tool strategy controls supported modes and launch command.
- Manual launch selections are confirmed in a shared modal; cancel/dismiss does
  not autosave dialog changes.

## Algorithm

1. Change events on selects, checkboxes, radios, and blur events on text fields
   invoke one save operation.
2. Server verifies session ownership and rejects active-session edits.
3. Validate prompt ownership, normalize agent tool, validate mode/tool support,
   and persist provider, branch, PAT, MCP IDs, and instruction text.
4. The next launch translates persisted settings into OpenShell providers,
   policy, repo setup, and agent command.

### Agent Swarm MCP provider

When the persisted MCP selection includes `agent-swarm`, launch derives the
provider name `swarmer-ws-{workspace_id}-agent-swarm-s{session_id}`. The
provider is created or updated with a one-hour, workspace-bound session token
and attached to the new sandbox. The provider profile must also contain the
internal Swarmer REST endpoint, bearer credential mapping, and MCP binary
attribution; the sandbox `swarm_mcp` network rule is separate and only permits
network access.

For `tui` and `server` sessions, the token is re-minted every 50 minutes and
the existing provider is updated in place. Prompt sessions do not need a
refresh loop because they are one-shot. Provider cleanup occurs after sandbox
deletion on stop, delete, and successful prompt completion.

## Model semantics

- Provider is a preset/identifier used by OpenCode; the exact model format is
  resolved by the selected provider configuration.
- Shell executes a raw command and has no AI token cost; it does not support
  server mode.
- Working branch may be explicit or generated at launch.
- Prompt source and inline instruction can be combined according to launch
  prompt assembly rules.
- `Session.instruction_prompt` remains the session default for scheduled/event
  fallback. Additional Instructions entered in the launch dialog override that
  default for the manual run only. Queued runs persist the nullable
  `queued_instruction_prompt` until dispatch; NULL means no override, while an
  empty string is an explicit blank override.
- Confirming a manual launch saves prompt/provider/mode to the existing Session,
  but never saves its run-only instruction text to Session or SessionSchedule.

## Acceptance checks

- Active sessions cannot be changed through direct POST requests.
- Invalid prompt IDs from another workspace are rejected.
- Manual provider selections are checked against current workspace availability.
- A blank manual instruction override does not inherit the session instruction
  default, and scheduled prompt fallback remains unchanged.
- Server mode rejects unsupported agent tools server-side.
- Autosave reports failure without silently losing the selected value.
- Agent Swarm provider creation is driven by persisted `Session.mcp_server_ids`,
  not by the local OpenCode configuration.
