"""Tests for swarmer.k8s RBAC helpers."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from swarmer.k8s import _swarmer_user_role_binding_name


def test_role_binding_name_is_stable():
    identity = "system:serviceaccount:swarmer:alice"
    assert _swarmer_user_role_binding_name(identity) == _swarmer_user_role_binding_name(identity)


def test_role_binding_name_differs_for_colliding_normalizations():
    a = "user@example.com/with/a/very/long/identity/segment/that/truncates"
    b = "user@example.com/with/a/very/long/identity/segment/that/truncates-extra"
    assert _swarmer_user_role_binding_name(a) != _swarmer_user_role_binding_name(b)
