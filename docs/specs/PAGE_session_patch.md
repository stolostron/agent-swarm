# Session Patch Component

## Purpose

Patch export turns changes made in a running OpenShell session into a downloadable
patch and local application instructions.

## Sources

- Template: `swarmer/templates/sessions/detail.html` Patch panel
- Router: `swarmer/routers/sessions.py` patch handlers
- REST API: `POST .../{sid}/generate-patch`, `GET .../{sid}/download-patch`

## Methods and tooling

- OpenShell runs a single `git diff` command in the session sandbox.
- The command uses `origin/{patch_base_ref}` when a base reference is configured;
  otherwise it compares the current working tree.
- Safe filename generation replaces unsafe session-name characters.
- Browser clipboard API copies apply commands and generated commit message.
- Gemini-based commit-message generation has a safe changed-file fallback.

## Algorithm

1. Require an OpenShell sandbox and a running session for generation.
2. Run `git diff` in the sandbox, optionally against the configured base ref.
3. Persist patch output and an optional generated commit message.
4. Render copyable clone/checkout/apply commands.
5. Return the patch with `text/x-patch` and a safe download filename.

## Acceptance checks

- Patch generation is disabled when the session is not running.
- Download is unavailable when no patch exists.
- Generated paths reflect the working tree returned by the sandbox git diff.
- Commit-message fallback works without an in-process provider credential.
