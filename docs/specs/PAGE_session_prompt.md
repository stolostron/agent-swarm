# Session Prompt Component

## Purpose

Prompt mode runs a one-shot instruction against the selected agent tool and
records the result without maintaining an interactive terminal or chat service.

## Sources

- Templates: `swarmer/templates/sessions/_prompt_picker.html`,
  `detail.html`
- Router: `swarmer/routers/sessions.py` launch/edit paths
- Runtime: agent strategies, `swarmer/openshell_client.py`, scheduler pollers

## Methods and tooling

- Prompt source selection plus inline instruction text.
- Shared `_do_launch()` for manual and scheduled execution.
- OpenShell one-shot command execution.
- Prompt completion polling and run persistence.
- Automatic sandbox deletion after successful prompt execution.

## Algorithm

1. Resolve selected workspace prompt and inline instructions.
2. Assemble the launch prompt; for OpenCode sessions it is written as
   `AGENTS.md` according to launch rules.
3. Apply tool/provider, repository, branch, MCP, environment, and policy setup.
4. Execute once, stream output, and persist processed/raw values and run state.
5. On completion, expose output/history; on success, clean up the sandbox.
6. On process restart, resume monitoring for surviving prompt executions.

## API references

- Session create/update and launch endpoints.
- `GET .../{sid}/output`
- `GET .../{sid}/runs`

## Acceptance checks

- Missing or cross-workspace prompts fail before launch.
- Prompt runs cannot become an orphaned active session after completion.
- Output and history distinguish manual prompt runs from scheduled runs.
