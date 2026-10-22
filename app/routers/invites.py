"""Agent invites — the only way to sign up (012, docs/DECISIONS.md §21).

Three routes are for a TEAM_ADMIN managing their agency's invites. Two are
public: the person holding an invite token has no account yet, so the token in
the body is their only credential. Neither public route takes the token in the
URL, which would put it in access logs.
"""
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.config import Settings, get_settings
from app.deps import DbSession, TeamAdmin
from app.schemas import (
    AgentOut, InviteAccept, InviteCreate, InviteCreated, InviteOut, InvitePreview,
    InviteToken, Message,
)
from app.services import invite as svc

router = APIRouter(prefix="/invites", tags=["invites"])

AppSettings = Annotated[Settings, Depends(get_settings)]


def _out(invite) -> dict:
    return {
        "id": invite.id, "role": invite.role, "email": invite.email,
        "status": svc.invite_status(invite), "created_by": invite.created_by,
        "created_at": invite.created_at, "expires_at": invite.expires_at,
        "redeemed_at": invite.redeemed_at, "redeemed_agent_id": invite.redeemed_agent_id,
    }


@router.post(
    "",
    response_model=InviteCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Invite someone to your agency (TEAM_ADMIN)",
    description=(
        "Creates a single-use invite into **your own agency** with a fixed `role` "
        "(`AGENT` or `TEAM_ADMIN`). Whoever redeems it becomes an agent of this agency "
        "with that role.\n\n"
        "The response is the **only** time the `token` is shown — it is stored as a "
        "hash. Send `invite_url` (or the token) to the person. Anyone holding it can "
        "join, so set `email` to lock it to one address, and prefer a short expiry for "
        "`TEAM_ADMIN` invites. Default expiry: `INVITE_TTL_HOURS` (72)."
    ),
    responses={403: {"model": Message, "description": "Caller is not a TEAM_ADMIN"}},
)
async def create_invite(
    payload: InviteCreate, admin: TeamAdmin, session: DbSession, settings: AppSettings
):
    invite, token = await svc.create_invite(
        session, settings, admin=admin, role=payload.role, email=payload.email,
        expires_in_hours=payload.expires_in_hours,
    )
    return {**_out(invite), "token": token, "invite_url": svc.invite_url(settings, token)}


@router.get(
    "",
    response_model=list[InviteOut],
    summary="Your agency's invites (TEAM_ADMIN)",
    description="Newest first, with their status. Tokens are never returned.",
)
async def list_invites(
    admin: TeamAdmin,
    session: DbSession,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    rows = await svc.list_invites(session, agency_id=admin.agency_id, limit=limit, offset=offset)
    return [_out(i) for i in rows]


@router.delete(
    "/{invite_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an invite (TEAM_ADMIN)",
    description="Idempotent. **404** for another agency's invite, **409** once redeemed.",
    responses={404: {"model": Message}, 409: {"model": Message}},
)
async def revoke_invite(invite_id: uuid.UUID, admin: TeamAdmin, session: DbSession):
    await svc.revoke_invite(session, agency_id=admin.agency_id, invite_id=invite_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/inspect",
    response_model=InvitePreview,
    summary="What an invite token is for (public)",
    description=(
        "No token required: for the sign-up page, before the person types anything — "
        "\"You're joining *Agency* as *Admin*\". Read-only. **404** unknown token, "
        "**410** expired, revoked or already used."
    ),
    responses={404: {"model": Message}, 410: {"model": Message}},
)
async def inspect_invite(payload: InviteToken, session: DbSession):
    return await svc.preview_invite(session, token=payload.token)


@router.post(
    "/accept",
    response_model=AgentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Redeem an invite and register (public)",
    description=(
        "No token required: the invite token is the credential. Creates the person's "
        "login (Supabase Auth, confirmed — no email is sent) and their agent row in the "
        "invite's agency with the invite's role; nothing in the body can choose either. "
        "Then sign in with `signInWithPassword` and the same email and password.\n\n"
        "**403** the invite is locked to another email · **404** unknown token · "
        "**409** that email already has an account · **410** expired, revoked or used · "
        "**503** sign-up is not configured (`SUPABASE_SERVICE_ROLE_KEY`)."
    ),
    responses={403: {"model": Message}, 404: {"model": Message}, 409: {"model": Message},
               410: {"model": Message}, 503: {"model": Message}},
)
async def accept_invite(payload: InviteAccept, session: DbSession, settings: AppSettings):
    agent = await svc.accept_invite(
        session, settings, token=payload.token, full_name=payload.full_name,
        email=payload.email, password=payload.password, phone=payload.phone,
    )
    return AgentOut(
        id=agent.id, agency_id=agent.agency_id, role=agent.role, active=agent.active,
        full_name=agent.person.full_name, email=agent.person.email,
    )
