"""Tests for swarmer.k8s helpers.

Covers list_swarmer_user_role_binding_identities() — the ACM-41659 migration
helper that reads back legacy `make grant-workspace-access` RoleBindings —
and the Add Member / Add Admin candidate discovery helpers (app_namespace,
list_openshift_users, list_user_service_accounts).
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from swarmer.k8s import (
    app_namespace,
    list_openshift_users,
    list_swarmer_user_role_binding_identities,
    list_user_service_accounts,
)


def _role_binding(role_kind, role_name, subjects):
    # NOTE: MagicMock(name=...) sets the mock's debug repr, not a `.name`
    # attribute — set attributes after construction instead.
    rb = MagicMock()
    rb.role_ref = MagicMock()
    rb.role_ref.kind = role_kind
    rb.role_ref.name = role_name
    rb.subjects = subjects
    return rb


def _subject(kind, name, namespace=None):
    subject = MagicMock()
    subject.kind = kind
    subject.name = name
    subject.namespace = namespace
    return subject


class TestListSwarmerUserRoleBindingIdentities:
    def test_extracts_service_account_and_user_subjects(self):
        bindings = MagicMock()
        bindings.items = [
            _role_binding(
                "ClusterRole",
                "swarmer-user",
                [
                    _subject("ServiceAccount", "alice", namespace="swarmer"),
                    _subject("User", "bob"),
                ],
            )
        ]
        with patch("kubernetes.client") as mock_k8s:
            mock_k8s.RbacAuthorizationV1Api.return_value.list_namespaced_role_binding.return_value = bindings
            result = list_swarmer_user_role_binding_identities("team-a")

        assert result == ["system:serviceaccount:swarmer:alice", "bob"]

    def test_ignores_bindings_for_other_cluster_roles(self):
        bindings = MagicMock()
        bindings.items = [
            _role_binding("ClusterRole", "some-other-role", [_subject("User", "eve")]),
        ]
        with patch("kubernetes.client") as mock_k8s:
            mock_k8s.RbacAuthorizationV1Api.return_value.list_namespaced_role_binding.return_value = bindings
            result = list_swarmer_user_role_binding_identities("team-a")

        assert result == []

    def test_service_account_defaults_to_binding_namespace_when_subject_namespace_missing(self):
        bindings = MagicMock()
        bindings.items = [
            _role_binding(
                "ClusterRole", "swarmer-user", [_subject("ServiceAccount", "alice", namespace=None)]
            ),
        ]
        with patch("kubernetes.client") as mock_k8s:
            mock_k8s.RbacAuthorizationV1Api.return_value.list_namespaced_role_binding.return_value = bindings
            result = list_swarmer_user_role_binding_identities("team-a")

        assert result == ["system:serviceaccount:team-a:alice"]

    def test_returns_empty_list_and_never_raises_on_k8s_error(self):
        with patch("kubernetes.client") as mock_k8s:
            mock_k8s.RbacAuthorizationV1Api.return_value.list_namespaced_role_binding.side_effect = RuntimeError("boom")
            result = list_swarmer_user_role_binding_identities("team-a")

        assert result == []

    def test_no_bindings_returns_empty_list(self):
        bindings = MagicMock()
        bindings.items = []
        with patch("kubernetes.client") as mock_k8s:
            mock_k8s.RbacAuthorizationV1Api.return_value.list_namespaced_role_binding.return_value = bindings
            result = list_swarmer_user_role_binding_identities("team-a")

        assert result == []


class TestAppNamespace:
    def test_prefers_settings_k8s_namespace(self):
        with patch("swarmer.config.settings") as mock_settings:
            mock_settings.k8s_namespace = "shared-ns"
            assert app_namespace() == "shared-ns"

    def test_falls_back_to_incluster_file(self):
        with patch("swarmer.config.settings") as mock_settings:
            mock_settings.k8s_namespace = ""
            with patch("builtins.open", MagicMock(return_value=MagicMock(
                __enter__=MagicMock(return_value=MagicMock(read=MagicMock(return_value="my-ns\n"))),
                __exit__=MagicMock(return_value=False),
            ))):
                assert app_namespace() == "my-ns"

    def test_falls_back_to_default_when_file_missing(self):
        with patch("swarmer.config.settings") as mock_settings:
            mock_settings.k8s_namespace = ""
            with patch("builtins.open", side_effect=OSError()):
                assert app_namespace() == "swarmer"


class TestListOpenshiftUsers:
    def test_extracts_user_names(self):
        result = {"items": [{"metadata": {"name": "alice"}}, {"metadata": {"name": "bob"}}]}
        with patch("kubernetes.client") as mock_k8s:
            mock_k8s.CustomObjectsApi.return_value.list_cluster_custom_object.return_value = result
            assert list_openshift_users() == ["alice", "bob"]

    def test_returns_empty_list_on_error(self):
        with patch("kubernetes.client") as mock_k8s:
            mock_k8s.CustomObjectsApi.return_value.list_cluster_custom_object.side_effect = RuntimeError("not OpenShift")
            assert list_openshift_users() == []

    def test_skips_items_missing_name(self):
        result = {"items": [{"metadata": {}}, {"metadata": {"name": "alice"}}]}
        with patch("kubernetes.client") as mock_k8s:
            mock_k8s.CustomObjectsApi.return_value.list_cluster_custom_object.return_value = result
            assert list_openshift_users() == ["alice"]


class TestListUserServiceAccounts:
    def _sa(self, name):
        sa = MagicMock()
        sa.metadata.name = name
        return sa

    def test_formats_service_account_identities(self):
        result = MagicMock()
        result.items = [self._sa("alice"), self._sa("bob")]
        with patch("kubernetes.client") as mock_k8s:
            mock_k8s.CoreV1Api.return_value.list_namespaced_service_account.return_value = result
            assert list_user_service_accounts("swarmer") == [
                "system:serviceaccount:swarmer:alice",
                "system:serviceaccount:swarmer:bob",
            ]

    def test_excludes_system_service_accounts(self):
        result = MagicMock()
        result.items = [self._sa("default"), self._sa("swarmer"), self._sa("alice")]
        with patch("kubernetes.client") as mock_k8s:
            mock_k8s.CoreV1Api.return_value.list_namespaced_service_account.return_value = result
            assert list_user_service_accounts("swarmer") == ["system:serviceaccount:swarmer:alice"]

    def test_returns_empty_list_on_error(self):
        with patch("kubernetes.client") as mock_k8s:
            mock_k8s.CoreV1Api.return_value.list_namespaced_service_account.side_effect = RuntimeError("boom")
            assert list_user_service_accounts("swarmer") == []

    def test_defaults_to_app_namespace_when_omitted(self):
        result = MagicMock()
        result.items = []
        with patch("swarmer.k8s.app_namespace", return_value="detected-ns"):
            with patch("kubernetes.client") as mock_k8s:
                mock_core = mock_k8s.CoreV1Api.return_value
                mock_core.list_namespaced_service_account.return_value = result
                list_user_service_accounts()
                mock_core.list_namespaced_service_account.assert_called_once_with("detected-ns")
