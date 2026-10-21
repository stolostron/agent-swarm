# Authentication Component

## Purpose

Authenticate browser sessions and REST API requests using the identity backend
selected by `SWARMER_RUNTIME_MODE`. Authentication identifies a user; workspace
access and administration are enforced separately by the database ACL.

## Sources

- Login template: `swarmer/templates/login.html`
- HTML routes: `swarmer/routers/auth.py` (`login_page`, `login`,
  `_validate_and_login`, `oauth_callback`)
- Auth backend: `swarmer/k8s_auth.py` (`validate_token`,
  `validate_openshell_admin_token`, `validate_openshell_oidc_token`)
- API auth dependency: `swarmer/api/deps.py` (`require_api_auth`)
- Session dependency: `swarmer/deps.py` (`require_auth`, `get_user_token`)
- Admin bootstrap: `swarmer/api/v1/admins.py`, `swarmer/workspace_acl.py`
- Shared API contract: [REST API Authentication](rest-api.md#2-authentication)

## Behavior

1. `GET /login` renders the token form. Kubernetes mode labels it as a bearer
   token form and may offer the configured OpenShift OAuth link. OpenShell mode
   labels the form for an OIDC token or the configured bootstrap admin token;
   it does not show the OpenShift OAuth link.
2. `POST /login` and the OAuth callback pass the submitted token to
   `k8s_auth.validate_token`. Kubernetes mode uses TokenReview and its existing
   fallback identity probe. OpenShell mode first checks the mounted bootstrap
   token file, then validates an OIDC token.
3. On success, the browser stores the token encrypted in its signed session
   cookie, along with the validated username and groups. It then checks API
   reachability through the same bearer-authenticated REST API used by the UI.
4. REST requests independently validate their bearer token through the same
   `validate_token` selector. A session cookie alone is not REST API
   authentication.

## OpenShell validation

- OIDC requires an HTTPS `SWARMER_OIDC_ISSUER` and an explicit
  `SWARMER_OIDC_AUDIENCE`. Discovery issuer, signing key, signature, algorithm,
  audience, expiry, issued-at time, and subject are verified. The JWT `sub` is
  the stable ACL username; the groups claim defaults to `groups` and supports a
  configured dotted path through `SWARMER_OIDC_GROUPS_CLAIM`.
- JWKS must be served from the issuer origin unless its HTTPS origin is listed
  in `SWARMER_OIDC_JWKS_ALLOWED_ORIGINS`. Signing keys are cached for five
  minutes and refreshed for an unknown key ID with a per-issuer refresh limit.
- The bootstrap credential is read from `SWARMER_ADMIN_TOKEN_FILE` off the
  event loop and compared with `hmac.compare_digest`. Its identity has the
  reserved `swarmer-bootstrap-admin` group; OIDC group claims cannot grant that
  group. When the bootstrap file is configured, only that identity may create
  the first database administrator. The configured token remains an
  administrator credential until it is rotated or revoked.
- The first database administrator insert is atomic; concurrent bootstrap
  attempts cannot create multiple first-admin rows.
- When a bootstrap file is configured, `/api/v1/me` exposes the initial-admin
  action only to the bootstrap-token identity; the UI does not offer the action
  to other OIDC users.

## Security and failure handling

- Missing OIDC configuration, invalid discovery/JWKS responses, invalid claims,
  signature failures, missing token files, and token mismatches fail closed.
- Raw tokens are not logged. The OpenShell Gateway's machine identity is
  separate from these user credentials. Local Kind uses its gateway-generated
  client mTLS bundle; hosted deployments use the OpenShell SDK's renewable
  client-credentials flow and a mounted client-secret file configured through
  `SWARMER_GATEWAY_OIDC_*`. Neither mode uses a developer's personal CLI token
  or private key.
- `SWARMER_RUNTIME_MODE=openshell` skips Kubernetes initialization and legacy
  membership migration. Kubernetes-only namespace bypasses and directory
  lookups do not apply in this mode.
- Workspace authorization remains in `workspace_acl.py`; authentication does
  not itself grant workspace access except for validated static/database admin
  identities.

## Acceptance checks

- Kubernetes TokenReview and fallback tests remain green.
- OpenShell tests cover valid/invalid OIDC claims, issuer/JWKS trust, reserved
  bootstrap groups, bootstrap-token comparison, and runtime selection.
- API tests verify only the bootstrap identity can initialize the first admin
  when a bootstrap file is configured.
- `make test` covers the HTML/API authentication consumers and the related ACL
  behavior.
