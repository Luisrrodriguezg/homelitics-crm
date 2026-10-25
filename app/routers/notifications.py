"""The in-app inbox (014): each agent's own messages, and the admin monitor.

A message appears when the AI agent or a TEAM_ADMIN assigns a lead to an agent
or books a visit on their calendar. docs/DECISIONS.md §23, docs/NOTIFICATIONS.md.
"""
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.deps import CurrentAgent, DbSession, TeamAdmin, require_scope
from app.schemas import MarkedRead, Message, NotificationPage, NotificationType
from app.services import notification as svc

router = APIRouter(tags=["notifications"])

_PAGE = Query(svc.PAGE_SIZE, ge=1, le=svc.PAGE_SIZE, description="At most 20 per page")
_BEFORE = Query(None, description="`next_before` from the previous page: older messages")
_TYPE = Query(None, description="LEAD_ASSIGNED or VISIT_SCHEDULED")
_READ = Query(None, description="true = only read, false = only unread")


def _check_days(created_from: date | None, created_to: date | None) -> None:
    if created_from and created_to and created_from > created_to:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "created_from is after created_to")


@router.get(
    "/me/notifications",
    response_model=NotificationPage,
    summary="Your inbox",
    description=(
        "Your messages, newest first, 20 per page: a lead the AI agent or an admin "
        "assigned to you (`LEAD_ASSIGNED`) and a visit they booked on your calendar "
        "(`VISIT_SCHEDULED`). `read` tells the frontend how to draw each one. "
        "`unread_count` with no filters is the bell badge. Reading the list marks "
        "nothing read."
    ),
)
async def my_notifications(
    agent: CurrentAgent,
    session: DbSession,
    type: NotificationType | None = _TYPE,
    read: bool | None = _READ,
    before: int | None = _BEFORE,
    limit: int = _PAGE,
):
    return await svc.list_notifications(
        session, agency_id=agent.agency_id, recipient_id=agent.id,
        type=type, read=read, before=before, limit=limit,
    )


@router.post(
    "/me/notifications/read-all",
    response_model=MarkedRead,
    summary="Mark all your messages read",
    dependencies=[Depends(require_scope("notifications:manage"))],
)
async def mark_all_read(agent: CurrentAgent, session: DbSession):
    return MarkedRead(marked=await svc.mark_all_read(session, agent=agent))


@router.post(
    "/me/notifications/{notification_id}/read",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mark one message read",
    description="Call it when the agent opens the message. Repeating it is harmless: "
                "`read_at` keeps the first time. **404** if the message is not yours.",
    responses={404: {"model": Message}},
    dependencies=[Depends(require_scope("notifications:manage"))],
)
async def mark_read(notification_id: int, agent: CurrentAgent, session: DbSession):
    if await svc.mark_read(session, notification_id=notification_id, agent=agent) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/notifications",
    response_model=NotificationPage,
    summary="Every message in your agency (TEAM_ADMIN only)",
    description=(
        "The admin monitor: what was sent to whom, by whom, and whether they have "
        "read it. Newest first, 20 per page. Viewing it never marks anything read. "
        "`created_from`/`created_to` are inclusive days in the agency's timezone."
    ),
    responses={403: {"model": Message}, 422: {"model": Message}},
)
async def agency_notifications(
    admin: TeamAdmin,
    session: DbSession,
    recipient_agent_id: uuid.UUID | None = Query(None, description="Who received it"),
    actor_agent_id: uuid.UUID | None = Query(None, description="Who caused it"),
    by_bot: bool | None = Query(None, description="true = caused by the AI agent, "
                                                  "false = by an admin"),
    type: NotificationType | None = _TYPE,
    read: bool | None = _READ,
    lead_id: uuid.UUID | None = Query(None, description="Messages about one lead"),
    created_from: date | None = Query(None, description="Created on or after this day"),
    created_to: date | None = Query(None, description="Created on or before this day"),
    before: int | None = _BEFORE,
    limit: int = _PAGE,
):
    _check_days(created_from, created_to)
    return await svc.list_notifications(
        session, agency_id=admin.agency_id, recipient_id=recipient_agent_id,
        actor_id=actor_agent_id, by_bot=by_bot, type=type, read=read, lead_id=lead_id,
        created_from=created_from, created_to=created_to, before=before, limit=limit,
    )
