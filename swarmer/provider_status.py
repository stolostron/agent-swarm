"""Live status for workspace-scoped OpenShell AI providers."""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer import openshell_client
from swarmer.models.opencode_secret import OpencodeSecret

_PROVIDERS = {
    "Vertex AI": "google-cloud",
    "Gemini": "google-ai-studio",
    "OpenAI": "openai",
}


async def get_missing_provider_names_bulk(
    ws_ids: list[int], db: AsyncSession, concurrency: int = 10
) -> dict[int, list[str]]:
    if not ws_ids:
        return {}

    result = await db.execute(
        select(OpencodeSecret).where(OpencodeSecret.workspace_id.in_(ws_ids))
    )
    secrets = result.scalars().all()
    expected_by_ws: dict[int, set[str]] = {wid: set() for wid in ws_ids}
    for secret in secrets:
        if secret.workspace_id not in expected_by_ws:
            continue
        if secret.has_vertex and secret.has_adc:
            expected_by_ws[secret.workspace_id].add("Vertex AI")
        if secret.has_gemini:
            expected_by_ws[secret.workspace_id].add("Gemini")
        if secret.has_openai:
            expected_by_ws[secret.workspace_id].add("OpenAI")

    sem = asyncio.Semaphore(concurrency)

    async def _check_provider(wid: int, label: str, suffix: str) -> tuple[int, str, bool]:
        async with sem:
            try:
                present = await openshell_client.provider_exists(
                    f"swarmer-ws-{wid}-{suffix}"
                )
            except Exception:
                present = True  # Gateway outage is not proof that provider is missing
            return wid, label, present

    tasks = [
        _check_provider(wid, label, _PROVIDERS[label])
        for wid, expected in expected_by_ws.items()
        for label in expected
    ]

    missing_by_ws: dict[int, list[str]] = {wid: [] for wid in ws_ids}
    if tasks:
        results = await asyncio.gather(*tasks)
        for wid, label, present in results:
            if not present:
                missing_by_ws[wid].append(label)

    return missing_by_ws


async def get_missing_provider_names(ws_id: int, db: AsyncSession) -> list[str]:
    res = await get_missing_provider_names_bulk([ws_id], db)
    return res.get(ws_id, [])


def session_provider_label(provider: str) -> str | None:
    return {"claude": "Vertex AI", "gemini": "Gemini", "openai": "OpenAI"}.get(provider)
