# Session Patch Component

## Purpose

Patch export turns changes made in a running session sandbox into a downloadable
patch and local application instructions.

## Sources

- Template: `swarmer/templates/sessions/detail.html` Patch panel
- Router: `swarmer/routers/sessions.py` patch handlers
- REST API: `POST .../{sid}/generate-patch`, `GET .../{sid}/download-patch`

## Methods and tooling

- OpenShell/git commands inspect the session sandbox.
- A single `git diff` captures the current sandbox changes.
- Safe filename generation replaces unsafe session-name characters.
- Browser clipboard API copies apply commands and generated commit message.
- Gemini-based commit-message generation has a safe changed-file fallback.

## Algorithm

1. Require an OpenShell sandbox and a running session for generation.
2. Run one `git diff` in the session sandbox.
3. Persist patch output, base reference, and optional commit message.
4. Render copyable clone/checkout/apply commands.
5. Return the patch with `text/x-patch` and a safe download filename.

## Acceptance checks

- Patch generation is disabled when the session is not running.
- Download is unavailable when no patch exists.
- Generated paths reflect the sandbox working directory.
- Commit-message fallback works without an in-process provider credential.
