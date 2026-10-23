"""Shared normalization and merge helpers for OpenShell session policies."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any


def load_policy_list(raw: str | None) -> list[dict[str, Any]]:
    """Decode a persisted policy JSON list, ignoring malformed legacy data."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def normalize_policy_endpoints(raw_endpoints: Any) -> list[dict[str, Any]]:
    """Normalize endpoint data before persistence, export, or OpenShell use.

    Empty host endpoints are invalid unless they explicitly target allowed IPs.
    L7 endpoints need an explicit access mode and enforcement policy; the UI has
    historically used full access for detected protocol endpoints.
    """
    if not isinstance(raw_endpoints, list):
        return []

    result: list[dict[str, Any]] = []
    for raw in raw_endpoints:
        if not isinstance(raw, dict):
            continue
        endpoint = dict(raw)
        host_value = endpoint.get("host")
        host = host_value.strip() if isinstance(host_value, str) else ""
        allowed_ips = endpoint.get("allowed_ips") or []
        if not host and not allowed_ips:
            continue
        endpoint["host"] = host
        if not endpoint.get("enforcement"):
            endpoint["enforcement"] = "enforce"
        if endpoint.get("protocol"):
            endpoint["access"] = "full"
            endpoint.pop("path", None)
            endpoint.pop("rules", None)
        if host == "registry.npmjs.org":
            endpoint["allow_encoded_slash"] = True
        result.append(endpoint)
    return result


def normalize_policy_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """Return a complete declarative rule while retaining persisted metadata."""
    result = dict(rule)
    result["name"] = str(rule.get("name") or "")
    result["endpoints"] = normalize_policy_endpoints(rule.get("endpoints"))
    binaries = rule.get("binaries")
    result["binaries"] = [dict(binary) for binary in binaries if isinstance(binary, dict)] if isinstance(binaries, list) else []
    return result


def is_chunk_promoted(chunk: dict[str, Any], policies: list[dict[str, Any]]) -> bool:
    """Match a detected chunk to persisted rules by name and binary coverage."""
    name = str(chunk.get("rule_name") or chunk.get("name") or "")
    if not name:
        return False
    by_name = {str(rule.get("name")): rule for rule in policies if rule.get("name")}
    rule = by_name.get(name)
    if rule is None:
        return False
    chunk_binaries = chunk.get("binaries") or []
    rule_binaries = rule.get("binaries") or []
    chunk_paths = {
        str(binary.get("path", ""))
        for binary in chunk_binaries
        if isinstance(binary, dict)
    }
    rule_paths = {
        str(binary.get("path", ""))
        for binary in rule_binaries
        if isinstance(binary, dict)
    }
    return chunk_paths.issubset(rule_paths)


def merge_policy_chunks(
    existing: list[dict[str, Any]], chunks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, list[str], list[dict[str, str]]]:
    """Promote detected chunks into policies without dropping existing coverage.

    Returns updated policies, the number of changed rule contributions, IDs of
    valid chunks eligible for live approval, and invalid chunks with
    machine-readable rejection reasons. Existing same-name rules retain all
    data while endpoint and binary coverage is unioned.
    """
    policies = [normalize_policy_rule(rule) for rule in existing]
    by_name = {str(rule.get("name")): rule for rule in policies if rule.get("name")}
    changed_count = 0
    promoted_chunk_ids: list[str] = []
    rejected: list[dict[str, str]] = []

    for chunk in chunks:
        chunk_id = str(chunk.get("id") or "")
        name = str(chunk.get("rule_name") or chunk.get("name") or "").strip()
        if not name:
            rejected.append({"chunk_id": chunk_id, "reason": "MISSING_RULE_NAME"})
            continue

        endpoints = normalize_policy_endpoints(chunk.get("endpoints"))
        if not endpoints:
            rejected.append({"chunk_id": chunk_id, "reason": "INVALID_ENDPOINTS"})
            continue
        if chunk_id:
            # Even an identical same-name contribution must have its own stable
            # OpenShell chunk approved, or it remains pending in the live view.
            promoted_chunk_ids.append(chunk_id)

        raw_binaries = chunk.get("binaries") or []
        binaries = [
            dict(binary)
            for binary in raw_binaries
            if isinstance(binary, dict) and str(binary.get("path") or "").strip()
        ] if isinstance(raw_binaries, list) else []

        rule = by_name.get(name)
        if rule is None:
            rule = {
                "name": name,
                "endpoints": endpoints,
                "binaries": binaries,
                "source": "chunk",
                "added_at": datetime.now(UTC).isoformat(),
                "chunk_id": chunk_id,
            }
            policies.append(rule)
            by_name[name] = rule
            changed = True
        else:
            known_paths = {
                str(binary.get("path") or "")
                for binary in (rule.get("binaries") or [])
                if isinstance(binary, dict)
            }
            merged_binaries = False
            for binary in binaries:
                path = str(binary.get("path") or "")
                if path not in known_paths:
                    rule.setdefault("binaries", []).append(binary)
                    known_paths.add(path)
                    merged_binaries = True

            known_endpoints = {
                json.dumps(endpoint, sort_keys=True, separators=(",", ":"))
                for endpoint in (rule.get("endpoints") or [])
                if isinstance(endpoint, dict)
            }
            merged_endpoints = False
            for endpoint in endpoints:
                key = json.dumps(endpoint, sort_keys=True, separators=(",", ":"))
                if key not in known_endpoints:
                    rule.setdefault("endpoints", []).append(endpoint)
                    known_endpoints.add(key)
                    merged_endpoints = True
            changed = merged_binaries or merged_endpoints

        if changed:
            changed_count += 1

    return policies, changed_count, promoted_chunk_ids, rejected
