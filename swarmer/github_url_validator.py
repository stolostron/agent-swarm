"""GitHub URL validation: reject URLs that embed authentication tokens.

Tokens embedded in URLs are a well-documented security anti-pattern.  They
appear in server access logs, browser history, proxy traces, and—in a
multi-agent system—may be serialised to message queues or traces.

GitHub's own guidance and OWASP both recommend tokens be passed exclusively
via HTTP headers (``Authorization: Bearer <token>``) and never as URL
components.

Usage::

    from swarmer.github_url_validator import validate_github_url, GitHubURLError

    try:
        validate_github_url(url)
    except GitHubURLError as exc:
        log.warning("Rejected GitHub URL: %s", exc.redacted_url)
        raise

The function raises :class:`GitHubURLError` on any detected violation.  For
clean URLs it returns ``None`` silently so callers can treat it as a no-op
guard.
"""

import re
from urllib.parse import parse_qs, urlparse, urlunparse


# ---------------------------------------------------------------------------
# Known token formats (compiled once at import time)
# ---------------------------------------------------------------------------

# Fine-grained PAT:  github_pat_XXXX…
_RE_GITHUB_PAT = re.compile(r"^github_pat_[A-Za-z0-9_]{1,}")

# Classic PAT / OAuth token / app token:  ghp_XXX, gho_XXX, ghs_XXX, ghu_XXX
_RE_GITHUB_CLASSIC = re.compile(r"^gh[pousr]_[A-Za-z0-9]{1,}")

# Git config credential helper tokens / SHA-1 style tokens: 40 hex chars
_RE_HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")

# Broad heuristic for any gh*_ prefixed token
_RE_GH_PREFIX = re.compile(r"^gh[a-z]_[A-Za-z0-9]{1,}")

# Query-parameter names that are never legitimate in a GitHub URL
_TOKEN_PARAM_DENYLIST: frozenset[str] = frozenset(
    {"token", "access_token", "api_key", "auth", "authorization", "bearer"}
)


def _looks_like_token(value: str) -> bool:
    """Return True if *value* resembles a GitHub / OAuth token format."""
    return bool(
        _RE_GITHUB_PAT.match(value)
        or _RE_GITHUB_CLASSIC.match(value)
        or _RE_GH_PREFIX.match(value)
        or _RE_HEX40.match(value)
    )


def _redact(value: str) -> str:
    """Replace all but the first 4 characters of *value* with asterisks."""
    if len(value) <= 4:
        return "****"
    return value[:4] + "****"


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class GitHubURLError(ValueError):
    """Raised when a GitHub URL contains an embedded authentication token.

    Attributes
    ----------
    reason:
        A human-readable description of the violation.
    redacted_url:
        A version of the URL safe for logging (token values replaced with
        ``****``).
    """

    def __init__(self, reason: str, redacted_url: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.redacted_url = redacted_url


# ---------------------------------------------------------------------------
# Core validator
# ---------------------------------------------------------------------------


def validate_github_url(url: str) -> None:
    """Raise :class:`GitHubURLError` if *url* contains an embedded auth token.

    Checks (in order):

    1. **Userinfo credentials** — ``https://user:TOKEN@github.com/…``
       URL parsers expose these as ``parsed.username`` / ``parsed.password``.

    2. **Denylisted query parameters** — any parameter whose *name* is in
       :data:`_TOKEN_PARAM_DENYLIST` (e.g. ``?token=``, ``?access_token=``,
       ``?api_key=``, ``?auth=``).

    3. **Token-shaped query parameter values** — any query parameter whose
       *value* matches a known GitHub token format, regardless of the
       parameter name (catches obfuscated parameter names).

    4. **Token-shaped path segments** — any ``/``-delimited path component
       matching a known GitHub token format.

    SSH URLs (``git@github.com:owner/repo``) cannot carry credentials in the
    same way as HTTPS URLs, but are still checked for token-shaped path
    components after the colon.

    Parameters
    ----------
    url:
        The raw URL string to validate.

    Raises
    ------
    GitHubURLError
        If an embedded token is detected.  The exception carries a
        :attr:`~GitHubURLError.redacted_url` safe for logging.
    """
    if not url:
        return

    # ------------------------------------------------------------------
    # SSH URLs: git@github.com:owner/repo — limited surface, check path
    # ------------------------------------------------------------------
    if url.startswith("git@"):
        # Extract the part after the colon as a pseudo-path
        colon_idx = url.find(":")
        path_part = url[colon_idx + 1:] if colon_idx != -1 else url
        for segment in re.split(r"[/:]", path_part):
            seg = segment.removesuffix(".git")
            if seg and _looks_like_token(seg):
                redacted = url.replace(seg, _redact(seg))
                raise GitHubURLError(
                    f"SSH GitHub URL contains a token-shaped path segment: {_redact(seg)}",
                    redacted_url=redacted,
                )
        return  # SSH URLs have no query strings or userinfo

    # ------------------------------------------------------------------
    # HTTPS / HTTP URLs
    # ------------------------------------------------------------------
    try:
        parsed = urlparse(url)
    except Exception:
        # Malformed URL — let callers deal with it; not our job here.
        return

    # 1. Userinfo credentials (user:password@host)
    if parsed.username or parsed.password:
        redacted_netloc = (
            f"{parsed.hostname}" if not parsed.port
            else f"{parsed.hostname}:{parsed.port}"
        )
        redacted = urlunparse(
            parsed._replace(netloc=redacted_netloc)
        )
        raise GitHubURLError(
            "GitHub URL contains embedded credentials in the userinfo field; "
            "use header-based auth instead",
            redacted_url=redacted,
        )

    # 2 & 3. Query parameters: denied names and token-shaped values
    if parsed.query:
        params = parse_qs(parsed.query, keep_blank_values=True)
        redacted_params: dict[str, list[str]] = {}
        violations: list[str] = []

        for name, values in params.items():
            name_lower = name.lower()
            for value in values:
                if name_lower in _TOKEN_PARAM_DENYLIST:
                    violations.append(
                        f"query parameter '{name}' is on the auth token denylist"
                    )
                    redacted_params.setdefault(name, []).append(_redact(value))
                elif _looks_like_token(value):
                    violations.append(
                        f"query parameter '{name}' has a token-shaped value"
                    )
                    redacted_params.setdefault(name, []).append(_redact(value))
                else:
                    redacted_params.setdefault(name, []).append(value)

        if violations:
            # Reconstruct a redacted query string for the error message
            redacted_qs = "&".join(
                f"{k}={v}"
                for k, vs in redacted_params.items()
                for v in vs
            )
            redacted = urlunparse(parsed._replace(query=redacted_qs))
            raise GitHubURLError(
                "GitHub URL contains an auth token in query parameters: "
                + "; ".join(violations),
                redacted_url=redacted,
            )

    # 4. Path segments
    for segment in parsed.path.split("/"):
        seg = segment.removesuffix(".git")
        if seg and _looks_like_token(seg):
            redacted_path = parsed.path.replace(segment, _redact(seg))
            redacted = urlunparse(parsed._replace(path=redacted_path))
            raise GitHubURLError(
                f"GitHub URL contains a token-shaped path segment: {_redact(seg)}",
                redacted_url=redacted,
            )
