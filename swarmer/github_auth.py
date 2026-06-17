"""GitHub auth helpers for session pods (App-first, PAT fallback)."""


def pat_injects_gh_token(has_github_app: bool) -> bool:
    """When a GitHub App is mounted, leave GH_TOKEN unset so gh-wrapper mints an IAT."""
    return not has_github_app


def build_git_credential_setup_shell(has_github_app: bool) -> str:
    """Shell prefix configuring git credentials before clone or agent startup."""
    if has_github_app:
        # System credential helper (GitHub App) is primary; PAT store is fallback.
        return (
            'if [ -n "${GITHUB_PAT}" ] && command -v git >/dev/null 2>&1; then '
            'git config --global --add credential.helper store && '
            'echo "https://${GITHUB_USERNAME}:${GITHUB_PAT}@github.com" '
            '> "${HOME}/.git-credentials"; '
            'fi && '
        )
    return (
        'if [ -n "${GITHUB_PAT}" ] && command -v git >/dev/null 2>&1; then '
        'git config --global credential.helper store && '
        'echo "https://${GITHUB_USERNAME}:${GITHUB_PAT}@github.com" '
        '> "${HOME}/.git-credentials" && '
        'git config --global user.name "${GITHUB_USERNAME}" && '
        'git config --global user.email "${GITHUB_USERNAME}@users.noreply.github.com"; '
        'fi && '
    )


def build_git_user_setup_shell(has_github_app: bool) -> str:
    """Optional git user.name/email when PAT username is available."""
    if not has_github_app:
        return ""
    return (
        'if [ -n "${GITHUB_USERNAME}" ] && command -v git >/dev/null 2>&1; then '
        'git config --global user.name "${GITHUB_USERNAME}" && '
        'git config --global user.email "${GITHUB_USERNAME}@users.noreply.github.com"; '
        'fi && '
    )
