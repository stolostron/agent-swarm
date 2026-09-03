"""Live status for workspace-scoped OpenShell AI providers."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer import openshell_client
from swarmer.models.opencode_secret import OpencodeSecret

_PROVIDERS = {
    "Vertex AI": "google-cloud",
    "Gemini": "google-ai-studio",
    "OpenAI": "openai",
}


async def get_missing_provider_names(ws_id: int, db: AsyncSession) -> list[str]:
    result = await db.execute(
        select(OpencodeSecret).where(OpencodeSecret.workspace_id == ws_id)
    )
    secrets = result.scalars().all()
    expected = set()
    for secret in secrets:
        if secret.has_adc or secret.has_vertex:
            expected.add("Vertex AI")
        if secret.has_gemini:
            expected.add("Gemini")
        if secret.has_openai:
            expected.add("OpenAI")

    missing: list[str] = []
    for label, suffix in _PROVIDERS.items():
        if label not in expected:
            continue
        try:
            present = await openshell_client.provider_exists(
                f"swarmer-ws-{ws_id}-{suffix}"
            )
        except Exception:
            # A gateway outage is not proof that a provider is missing.
            continue
        if not present:
            missing.append(label)
    return missing


def session_provider_label(provider: str) -> str | None:
    return {"claude": "Vertex AI", "gemini": "Gemini", "openai": "OpenAI"}.get(provider)
