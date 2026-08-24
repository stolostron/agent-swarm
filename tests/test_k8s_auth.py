"""Tests for k8s_auth token validation and fallback paths.

Covers:
- _username_from_jwt: JWT payload decoding
- _probe_with_user_token: returns TokenIdentity on 200/403, None on 401
- validate_token fallback on 401 (expired swarmer kubeconfig) and 403 (no RBAC)
"""

import base64
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from swarmer.k8s_auth import (
    TokenIdentity,
    _username_from_jwt,
    validate_token,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_jwt(sub: str) -> str:
    """Build a minimal unsigned JWT with the given sub claim."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload_bytes = json.dumps({"sub": sub}).encode()
    payload = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def _api_exception(status: int):
    from kubernetes.client.rest import ApiException
    exc = ApiException(status=status)
    exc.status = status
    return exc


# ---------------------------------------------------------------------------
# _username_from_jwt
# ---------------------------------------------------------------------------

class TestUsernameFromJwt:
    def test_extracts_sub_from_valid_jwt(self):
        token = _make_jwt("system:serviceaccount:swarmer:alice")
        assert _username_from_jwt(token) == "system:serviceaccount:swarmer:alice"

    def test_returns_empty_string_for_garbage(self):
        assert _username_from_jwt("not-a-jwt") == ""

    def test_returns_empty_string_for_missing_sub(self):
        header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(b'{"iss":"kubernetes"}').rstrip(b"=").decode()
        token = f"{header}.{payload}.sig"
        assert _username_from_jwt(token) == ""

    def test_handles_padding_correctly(self):
        # sub lengths that require different amounts of base64 padding
        for sub in ["a", "ab", "abc", "abcd"]:
            token = _make_jwt(sub)
            assert _username_from_jwt(token) == sub


# ---------------------------------------------------------------------------
# validate_token — fallback on 401 and 403
# ---------------------------------------------------------------------------

class TestValidateTokenFallback:
    @pytest.mark.asyncio
    async def test_401_from_tokenreview_triggers_fallback(self):
        """A 401 from the K8s API (swarmer kubeconfig expired) falls back to direct probe."""
        token = _make_jwt("system:serviceaccount:swarmer:alice")

        with patch("kubernetes.client") as mock_k8s:
            # TokenReview raises 401
            auth_api = MagicMock()
            auth_api.create_token_review.side_effect = _api_exception(401)
            mock_k8s.AuthenticationV1Api.return_value = auth_api

            # Direct probe returns 403 (authenticated, no list-ns permission)
            core_api = MagicMock()
            core_api.list_namespace.side_effect = _api_exception(403)
            api_client_instance = MagicMock()
            api_client_instance.__enter__ = MagicMock(return_value=api_client_instance)
            api_client_instance.__exit__ = MagicMock(return_value=False)
            mock_k8s.ApiClient.return_value = api_client_instance
            mock_k8s.CoreV1Api.return_value = core_api
            mock_k8s.Configuration.return_value = MagicMock()
            mock_k8s.V1TokenReview = MagicMock()
            mock_k8s.V1TokenReviewSpec = MagicMock()

            result = await validate_token(token, "https://localhost:6443", False)

        assert result is not None
        assert isinstance(result, TokenIdentity)
        assert result.username == "system:serviceaccount:swarmer:alice"

    @pytest.mark.asyncio
    async def test_403_from_tokenreview_triggers_fallback(self):
        """A 403 (no RBAC for tokenreviews) also falls back to direct probe."""
        token = _make_jwt("system:serviceaccount:swarmer:bob")

        with patch("kubernetes.client") as mock_k8s:
            auth_api = MagicMock()
            auth_api.create_token_review.side_effect = _api_exception(403)
            mock_k8s.AuthenticationV1Api.return_value = auth_api

            core_api = MagicMock()
            core_api.list_namespace.return_value = MagicMock()  # 200 success
            api_client_instance = MagicMock()
            api_client_instance.__enter__ = MagicMock(return_value=api_client_instance)
            api_client_instance.__exit__ = MagicMock(return_value=False)
            mock_k8s.ApiClient.return_value = api_client_instance
            mock_k8s.CoreV1Api.return_value = core_api
            mock_k8s.Configuration.return_value = MagicMock()
            mock_k8s.V1TokenReview = MagicMock()
            mock_k8s.V1TokenReviewSpec = MagicMock()

            result = await validate_token(token, "https://localhost:6443", False)

        assert result is not None
        assert result.username == "system:serviceaccount:swarmer:bob"

    @pytest.mark.asyncio
    async def test_invalid_user_token_in_probe_returns_none(self):
        """If the user's token is also invalid (401 from probe), login fails."""
        token = _make_jwt("system:serviceaccount:swarmer:eve")

        with patch("kubernetes.client") as mock_k8s:
            auth_api = MagicMock()
            auth_api.create_token_review.side_effect = _api_exception(401)
            mock_k8s.AuthenticationV1Api.return_value = auth_api

            core_api = MagicMock()
            core_api.list_namespace.side_effect = _api_exception(401)
            api_client_instance = MagicMock()
            api_client_instance.__enter__ = MagicMock(return_value=api_client_instance)
            api_client_instance.__exit__ = MagicMock(return_value=False)
            mock_k8s.ApiClient.return_value = api_client_instance
            mock_k8s.CoreV1Api.return_value = core_api
            mock_k8s.Configuration.return_value = MagicMock()
            mock_k8s.V1TokenReview = MagicMock()
            mock_k8s.V1TokenReviewSpec = MagicMock()

            result = await validate_token(token, "https://localhost:6443", False)

        assert result is None

    @pytest.mark.asyncio
    async def test_successful_tokenreview_returns_identity(self):
        """Happy path: TokenReview succeeds and returns a full TokenIdentity."""
        token = "some.valid.token"

        with patch("kubernetes.client") as mock_k8s:
            mock_status = MagicMock()
            mock_status.authenticated = True
            mock_status.user.username = "system:serviceaccount:swarmer:alice"
            mock_status.user.uid = "uid-123"
            mock_status.user.groups = ["system:serviceaccounts"]

            mock_resp = MagicMock()
            mock_resp.status = mock_status

            auth_api = MagicMock()
            auth_api.create_token_review.return_value = mock_resp
            mock_k8s.AuthenticationV1Api.return_value = auth_api
            mock_k8s.V1TokenReview = MagicMock()
            mock_k8s.V1TokenReviewSpec = MagicMock()

            result = await validate_token(token, "https://localhost:6443", False)

            assert result is not None
            assert result.username == "system:serviceaccount:swarmer:alice"
            assert result.uid == "uid-123"
            assert "system:serviceaccounts" in result.groups

    @pytest.mark.asyncio
    async def test_authenticated_tokenreview_with_none_user_returns_none(self):
        """TokenReview is authenticated but user object is None; must return None."""
        token = "some.token.with.none.user"

        with patch("kubernetes.client") as mock_k8s:
            mock_status = MagicMock()
            mock_status.authenticated = True
            mock_status.user = None

            mock_resp = MagicMock()
            mock_resp.status = mock_status

            auth_api = MagicMock()
            auth_api.create_token_review.return_value = mock_resp
            mock_k8s.AuthenticationV1Api.return_value = auth_api
            mock_k8s.V1TokenReview = MagicMock()
            mock_k8s.V1TokenReviewSpec = MagicMock()

            result = await validate_token(token, "https://localhost:6443", False)

            assert result is None

    @pytest.mark.asyncio
    async def test_authenticated_tokenreview_with_empty_username_returns_none(self):
        """TokenReview is authenticated but user username is empty; must return None."""
        token = "some.token.with.empty.user"

        with patch("kubernetes.client") as mock_k8s:
            mock_status = MagicMock()
            mock_status.authenticated = True
            mock_status.user.username = "  "
            mock_status.user.uid = "uid-123"
            mock_status.user.groups = []

            mock_resp = MagicMock()
            mock_resp.status = mock_status

            auth_api = MagicMock()
            auth_api.create_token_review.return_value = mock_resp
            mock_k8s.AuthenticationV1Api.return_value = auth_api
            mock_k8s.V1TokenReview = MagicMock()
            mock_k8s.V1TokenReviewSpec = MagicMock()

            result = await validate_token(token, "https://localhost:6443", False)

            assert result is None

    @pytest.mark.asyncio
    async def test_openshift_oauth_token_resolved_via_user_api(self):
        """An OpenShift OAuth token (sha256~...) has no JWT payload, but resolves via CustomObjectsApi."""
        token = "sha256~mockOpenShiftOauthBearerToken"

        with patch("kubernetes.client") as mock_k8s:
            # TokenReview fails with 403 (unprivileged user token)
            auth_api = MagicMock()
            auth_api.create_token_review.side_effect = _api_exception(403)
            mock_k8s.AuthenticationV1Api.return_value = auth_api

            # CustomObjectsApi returns user object from user.openshift.io
            custom_api = MagicMock()
            custom_api.get_cluster_custom_object.return_value = {
                "metadata": {"name": "alice", "uid": "uid-openshift-456"},
                "groups": ["system:authenticated", "system:authenticated:oauth", "developers"],
            }
            mock_k8s.CustomObjectsApi.return_value = custom_api

            api_client_instance = MagicMock()
            api_client_instance.__enter__ = MagicMock(return_value=api_client_instance)
            api_client_instance.__exit__ = MagicMock(return_value=False)
            mock_k8s.ApiClient.return_value = api_client_instance
            mock_k8s.Configuration.return_value = MagicMock()
            mock_k8s.V1TokenReview = MagicMock()
            mock_k8s.V1TokenReviewSpec = MagicMock()

            result = await validate_token(token, "https://localhost:6443", False)

        assert result is not None
        assert isinstance(result, TokenIdentity)
        assert result.username == "alice"
        assert result.uid == "uid-openshift-456"
        assert "developers" in result.groups

    @pytest.mark.asyncio
    async def test_non_jwt_token_resolved_via_self_subject_review(self):
        """A generic K8s non-JWT token resolves via SelfSubjectReview when OpenShift API 404s."""
        token = "opaque-k8s-token-123"

        with patch("kubernetes.client") as mock_k8s:
            auth_api = MagicMock()
            auth_api.create_token_review.side_effect = _api_exception(403)

            # OpenShift User API returns 404 (not an OpenShift cluster)
            custom_api = MagicMock()
            custom_api.get_cluster_custom_object.side_effect = _api_exception(404)
            mock_k8s.CustomObjectsApi.return_value = custom_api

            # SelfSubjectReview succeeds
            mock_ssr = MagicMock()
            mock_ssr.status.user_info.username = "k8s-user"
            mock_ssr.status.user_info.uid = "k8s-uid"
            mock_ssr.status.user_info.groups = ["system:authenticated"]
            auth_api.create_self_subject_review.return_value = mock_ssr
            mock_k8s.AuthenticationV1Api.return_value = auth_api

            api_client_instance = MagicMock()
            api_client_instance.__enter__ = MagicMock(return_value=api_client_instance)
            api_client_instance.__exit__ = MagicMock(return_value=False)
            mock_k8s.ApiClient.return_value = api_client_instance
            mock_k8s.Configuration.return_value = MagicMock()
            mock_k8s.V1TokenReview = MagicMock()
            mock_k8s.V1TokenReviewSpec = MagicMock()
            mock_k8s.V1SelfSubjectReview = MagicMock()

            result = await validate_token(token, "https://localhost:6443", False)

        assert result is not None
        assert result.username == "k8s-user"
        assert result.uid == "k8s-uid"
        assert result.groups == ["system:authenticated"]

    @pytest.mark.asyncio
    async def test_non_jwt_token_returns_none_when_both_identity_apis_fail(self):
        """A non-JWT token returns None when both OpenShift and SSR lookups fail to resolve identity."""
        token = "opaque-unresolvable-token"

        with patch("kubernetes.client") as mock_k8s:
            auth_api = MagicMock()
            auth_api.create_token_review.side_effect = _api_exception(403)

            # OpenShift User API returns 404
            custom_api = MagicMock()
            custom_api.get_cluster_custom_object.side_effect = _api_exception(404)
            mock_k8s.CustomObjectsApi.return_value = custom_api

            # SelfSubjectReview returns 404
            auth_api.create_self_subject_review.side_effect = _api_exception(404)
            mock_k8s.AuthenticationV1Api.return_value = auth_api

            # Even if list_namespace would return 200 or 403, non-JWT token without identity must return None
            core_api = MagicMock()
            core_api.list_namespace.return_value = MagicMock()
            mock_k8s.CoreV1Api.return_value = core_api

            api_client_instance = MagicMock()
            api_client_instance.__enter__ = MagicMock(return_value=api_client_instance)
            api_client_instance.__exit__ = MagicMock(return_value=False)
            mock_k8s.ApiClient.return_value = api_client_instance
            mock_k8s.Configuration.return_value = MagicMock()
            mock_k8s.V1TokenReview = MagicMock()
            mock_k8s.V1TokenReviewSpec = MagicMock()
            mock_k8s.V1SelfSubjectReview = MagicMock()

            result = await validate_token(token, "https://localhost:6443", False)

        assert result is None

    @pytest.mark.asyncio
    async def test_non_jwt_token_returns_none_when_identity_apis_yield_empty_username(self):
        """A non-JWT token returns None when identity APIs return objects with empty usernames."""
        token = "opaque-empty-user-token"

        with patch("kubernetes.client") as mock_k8s:
            auth_api = MagicMock()
            auth_api.create_token_review.side_effect = _api_exception(403)

            # OpenShift User API returns user with empty name
            custom_api = MagicMock()
            custom_api.get_cluster_custom_object.return_value = {
                "metadata": {"name": "", "uid": "uid-anon"},
                "groups": [],
            }
            mock_k8s.CustomObjectsApi.return_value = custom_api

            # SelfSubjectReview returns empty username
            mock_ssr = MagicMock()
            mock_ssr.status.user_info.username = ""
            mock_ssr.status.user_info.uid = ""
            mock_ssr.status.user_info.groups = []
            auth_api.create_self_subject_review.return_value = mock_ssr
            mock_k8s.AuthenticationV1Api.return_value = auth_api

            api_client_instance = MagicMock()
            api_client_instance.__enter__ = MagicMock(return_value=api_client_instance)
            api_client_instance.__exit__ = MagicMock(return_value=False)
            mock_k8s.ApiClient.return_value = api_client_instance
            mock_k8s.Configuration.return_value = MagicMock()
            mock_k8s.V1TokenReview = MagicMock()
            mock_k8s.V1TokenReviewSpec = MagicMock()
            mock_k8s.V1SelfSubjectReview = MagicMock()

            result = await validate_token(token, "https://localhost:6443", False)

        assert result is None
