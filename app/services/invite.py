"""Agent invites (012) — the only way to sign up. docs/DECISIONS.md §21.

A TEAM_ADMIN creates an invite for their own agency with a fixed role. The
token goes to them once; whoever redeems it registers themselves and becomes an
agent of that agency with that role. Nothing the person sends at sign-up can
choose either — both are read from the invite row.

The token is a bearer secret (an admin invite makes whoever holds it an admin),
so only its sha256 is stored. 32 random bytes: hashing without a salt is fine,
there is nothing to brute-force.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import Agency, Agent, AgentInvite, Person
from app.services import auth_admin
from app.services.events import emit

log = logging.getLogger(__name__)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def invite_status(invite: AgentInvite, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if invite.redeemed_at is not None:
        return "REDEEMED"
    if invite.revoked_at is not None:
        return "REVOKED"
    if invite.expires_at <= now:
        return "EXPIRED"
    return "PENDING"


async def create_invite(
    session: AsyncSession,
    settings: Settings,
    *,
    admin: Agent,
    role: str,
    email: str | None,
    expires_in_hours: int | None,
) -> tuple[AgentInvite, str]:
    """Returns (invite, token). The token exists only in this return value."""
    token = secrets.token_urlsafe(32)
    invite = AgentInvite(
        agency_id=admin.agency_id,
        role=role,
        email=email.lower() if email else None,
        token_hash=hash_token(token),
        created_by=admin.id,
        expires_at=datetime.now(timezone.utc)
        + timedelta(hours=expires_in_hours or settings.invite_ttl_hours),
    )
    session.add(invite)
    await session.commit()
    await session.refresh(invite)
    return invite, token


def invite_url(settings: Settings, token: str) -> str | None:
    if not settings.frontend_url:
        return None
    return f"{settings.frontend_url.rstrip('/')}/invite#{token}"


async def list_invites(
    session: AsyncSession, *, agency_id: uuid.UUID, limit: int = 100, offset: int = 0
) -> list[AgentInvite]:
    q = (
        select(AgentInvite)
        .where(AgentInvite.agency_id == agency_id)
        .order_by(AgentInvite.created_at.desc(), AgentInvite.id)
        .limit(limit).offset(offset)
    )
    return list((await session.execute(q)).scalars().all())


async def revoke_invite(
    session: AsyncSession, *, agency_id: uuid.UUID, invite_id: uuid.UUID
) -> None:
    """Idempotent. 404 outside the caller's agency, 409 once redeemed."""
    invite = (
        await session.execute(
            select(AgentInvite)
            .where(AgentInvite.id == invite_id, AgentInvite.agency_id == agency_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if invite is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invite not found")
    if invite.redeemed_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Invite already redeemed; deactivate the agent instead")
    if invite.revoked_at is None:
        invite.revoked_at = datetime.now(timezone.utc)
    await session.commit()


async def _usable_invite(
    session: AsyncSession, token: str, *, lock: bool
) -> AgentInvite:
    """404 for a token that matches nothing, 410 for one that did but no longer
    works. Distinguishing them leaks nothing: the token cannot be guessed."""
    q = select(AgentInvite).where(AgentInvite.token_hash == hash_token(token))
    if lock:
        # Two people redeeming one token: the second waits here, then sees it used.
        q = q.with_for_update()
    invite = (await session.execute(q)).scalar_one_or_none()
    if invite is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invite not found")
    state = invite_status(invite)
    if state != "PENDING":
        raise HTTPException(
            status.HTTP_410_GONE,
            f"Invite {state.lower()}; ask your agency admin for a new one",
        )
    return invite


async def preview_invite(session: AsyncSession, *, token: str) -> dict:
    invite = await _usable_invite(session, token, lock=False)
    agency_name = await session.scalar(select(Agency.name).where(Agency.id == invite.agency_id))
    return {"agency_name": agency_name, "role": invite.role,
            "email": invite.email, "expires_at": invite.expires_at}


async def accept_invite(
    session: AsyncSession,
    settings: Settings,
    *,
    token: str,
    full_name: str,
    email: str,
    password: str,
    phone: str | None,
) -> Agent:
    """Register the person as an agent of the invite's agency, with its role.

    The invite row stays locked while Supabase creates the Auth user, so one
    token can never produce two agents. If our commit fails after that, the
    Auth user is deleted again (best effort) so a retry is not blocked by
    "email already registered".

    Under DEV_AUTH_BYPASS there is no Supabase: the agent is created unbound
    and acts through X-Dev-Agent-Id like every other local agent.
    """
    invite = await _usable_invite(session, token, lock=True)
    email = email.lower()
    if invite.email and invite.email != email:
        await session.rollback()
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "This invite is for a different email address")

    person = Person(full_name=full_name, email=email, phone=phone)
    session.add(person)
    await session.flush()
    agent = Agent(person=person, agency_id=invite.agency_id, role=invite.role)
    session.add(agent)
    await session.flush()

    auth_user_id: uuid.UUID | None = None
    if not settings.dev_auth_bypass:
        try:
            auth_user_id = await auth_admin.create_user(
                settings, email=email, password=password,
                metadata={"agent_id": str(agent.id), "agency_id": str(agent.agency_id),
                          "role": agent.role, "full_name": full_name},
            )
        except auth_admin.EmailTaken:
            await session.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "An account with this email already exists")
        except auth_admin.AuthAdminUnavailable as exc:
            await session.rollback()
            log.warning("invite accept: auth admin unavailable: %s", exc)
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                "Sign-up is temporarily unavailable")

    agent.auth_user_id = auth_user_id
    invite.redeemed_at = datetime.now(timezone.utc)
    invite.redeemed_agent_id = agent.id
    emit(session, event_type="agent.joined", aggregate_type="agent",
         aggregate_id=agent.id, agency_id=agent.agency_id, actor_id=agent.id,
         payload={"role": agent.role, "invite_id": str(invite.id)})
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        if auth_user_id is not None:
            try:
                await auth_admin.delete_user(settings, auth_user_id)
            except Exception as exc:  # noqa: BLE001 — the original error matters more
                log.error("invite accept: orphaned auth user %s: %s", auth_user_id, exc)
        raise
    return agent
