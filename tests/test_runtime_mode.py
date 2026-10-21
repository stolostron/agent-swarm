"""Tests for selecting platform integrations by Swarmer runtime mode."""

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from swarmer.config import settings
from swarmer.main import _initialize_platform_runtime


@pytest.mark.asyncio
async def test_openshell_runtime_skips_kubernetes_initialization_and_migration(monkeypatch):
    monkeypatch.setattr(settings, "swarmer_runtime_mode", "openshell")
    with (
        patch("swarmer.main.k8s.init_k8s") as init_k8s,
        patch("swarmer.main._sync_k8s_workspace_members", new=AsyncMock()) as sync_members,
    ):
        await _initialize_platform_runtime()

    init_k8s.assert_not_called()
    sync_members.assert_not_awaited()


@pytest.mark.asyncio
async def test_kubernetes_runtime_keeps_existing_initialization(monkeypatch):
    monkeypatch.setattr(settings, "swarmer_runtime_mode", "kubernetes")
    with (
        patch("swarmer.main.k8s.init_k8s") as init_k8s,
        patch("swarmer.main._sync_k8s_workspace_members", new=AsyncMock()) as sync_members,
    ):
        await _initialize_platform_runtime()

    init_k8s.assert_called_once_with(settings.k8s_in_cluster)
    sync_members.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_openshell_runtime_disables_kubernetes_user_discovery(monkeypatch):
    from swarmer.workspace_acl import _k8s_known_users

    monkeypatch.setattr(settings, "swarmer_runtime_mode", "openshell")
    with (
        patch("swarmer.k8s.list_openshift_users") as list_users,
        patch("swarmer.k8s.list_user_service_accounts") as list_accounts,
    ):
        assert await _k8s_known_users() == set()

    list_users.assert_not_called()
    list_accounts.assert_not_called()


@pytest.mark.asyncio
async def test_openshell_runtime_disables_kubernetes_pull_secret_operations(monkeypatch):
    from swarmer.api.v1.secrets import (
        create_pull_secret,
        delete_pull_secret,
        get_pull_secret,
    )

    monkeypatch.setattr(settings, "swarmer_runtime_mode", "openshell")
    workspace = SimpleNamespace(k8s_namespace="workspace")
    body = SimpleNamespace()

    with (
        patch("swarmer.api.v1.secrets.k8s.get_pull_secret_info") as get_info,
        patch("swarmer.api.v1.secrets.k8s.apply_pull_secret") as save_secret,
        patch("swarmer.api.v1.secrets.k8s.delete_pull_secret") as remove_secret,
    ):
        result = await get_pull_secret(ws_id=1, ws=workspace)
        assert result.exists is False
        with pytest.raises(HTTPException) as create_error:
            await create_pull_secret(ws_id=1, body=body, ws=workspace)
        with pytest.raises(HTTPException) as delete_error:
            await delete_pull_secret(ws_id=1, ws=workspace)

    assert create_error.value.status_code == 409
    assert delete_error.value.status_code == 409
    get_info.assert_not_called()
    save_secret.assert_not_called()
    remove_secret.assert_not_called()
