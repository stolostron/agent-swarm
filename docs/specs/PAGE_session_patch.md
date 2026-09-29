# Session Patch Component

## Purpose

Patch export turns changes made in repository-backed sessions into a downloadable
multi-repository patch and local application instructions.

## Sources

- Template: `swarmer/templates/sessions/detail.html` Patch panel
- Router: `swarmer/routers/sessions.py` patch handlers
- REST API: `POST .../{sid}/generate-patch`, `GET .../{sid}/download-patch`

## Methods and tooling

- OpenShell/git commands inspect the configured working repositories.
- Diff paths are prefixed with each repository local path.
- Safe filename generation replaces unsafe session-name characters.
- Browser clipboard API copies apply commands and generated commit message.
- Gemini-based commit-message generation has a safe changed-file fallback.

## Algorithm

1. Require configured repositories and a running session for generation.
2. Collect repository diffs from the session working branches.
3. Prefix paths to prevent collisions across repositories.
4. Persist patch output, base reference, and optional commit message.
5. Render copyable clone/checkout/apply commands.
6. Return the patch with `text/x-patch` and a safe download filename.

## Acceptance checks

- Patch generation is disabled when the session is not running.
- Download is unavailable when no patch exists.
- Generated paths apply to the intended repository subdirectories.
- Commit-message fallback works without an in-process provider credential.
