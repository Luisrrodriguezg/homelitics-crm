"""Supabase Auth Admin API — the only code that holds the service-role key.

Used by one flow: redeeming an invite creates the person's Auth user
(docs/DECISIONS.md §21). The key bypasses RLS and can create or delete any
user, so nothing else imports this module and nothing here logs a request.
"""
from __future__ import annotations

import uuid

import httpx

from app.config import Settings

_TIMEOUT_S = 10.0


class AuthAdminUnavailable(Exception):
    """No key configured, or Supabase did not answer as expected."""


class EmailTaken(Exception):
    """Supabase Auth already has a user with this email."""


def _client(settings: Settings) -> httpx.AsyncClient:
    key = settings.supabase_service_role_key
    if not key:
        raise AuthAdminUnavailable("SUPABASE_SERVICE_ROLE_KEY is not set")
    return httpx.AsyncClient(
        base_url=settings.supabase_url, timeout=_TIMEOUT_S,
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
    )


async def create_user(
    settings: Settings, *, email: str, password: str, metadata: dict
) -> uuid.UUID:
    """A confirmed user (no email is sent; the invite already proved intent)."""
    async with _client(settings) as client:
        try:
            r = await client.post("/auth/v1/admin/users", json={
                "email": email, "password": password,
                "email_confirm": True, "user_metadata": metadata,
            })
        except httpx.HTTPError as exc:
            raise AuthAdminUnavailable(exc.__class__.__name__) from exc
    if r.status_code in (200, 201):
        return uuid.UUID(r.json()["id"])
    body = r.text.lower()
    if r.status_code == 422 and ("already" in body or "email_exists" in body):
        raise EmailTaken(email)
    # Not the body: it can echo the request back.
    raise AuthAdminUnavailable(f"Supabase answered {r.status_code}")


async def delete_user(settings: Settings, user_id: uuid.UUID) -> None:
    """Best effort, for undoing create_user when our own commit fails."""
    async with _client(settings) as client:
        await client.delete(f"/auth/v1/admin/users/{user_id}")
