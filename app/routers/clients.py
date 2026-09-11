"""Client registration — the step before a lead can exist."""
from fastapi import APIRouter, Depends, Response, status

from app.deps import CurrentAgent, DbSession, require_scope
from app.schemas import ClientCreate, ClientOut
from app.services import client as svc

router = APIRouter(prefix="/clients", tags=["clients"])


@router.post(
    "",
    response_model=ClientOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a client, or return the one with this Telegram account",
    description=(
        "Creates the `client_id` that `POST /leads` needs.\n\n"
        "**With `telegram_user_id`** (the bot): a Telegram account is one client. "
        "Posting the same id again returns the existing client with **200** instead of "
        "**201**, and does not overwrite the stored name or phone. Enforced by a UNIQUE "
        "index via `ON CONFLICT DO NOTHING`, so two simultaneous first messages cannot "
        "create two clients.\n\n"
        "**Without it** (a walk-in, a call, an in-app contact): always creates a new "
        "client. Name, email and phone are never matched on — they collide between "
        "different people.\n\n"
        "The response carries only the id: clients are shared across agencies, so it "
        "never echoes stored contact details."
    ),
    responses={
        200: {"description": "This Telegram account is already a client; that client is returned"},
        201: {"description": "New client created"},
    },
    dependencies=[Depends(require_scope("clients:create"))],
)
async def create_client(
    payload: ClientCreate, agent: CurrentAgent, session: DbSession, response: Response
):
    client, created = await svc.create_or_get_client(
        session,
        full_name=payload.full_name,
        phone=payload.phone,
        email=payload.email,
        telegram_user_id=payload.telegram_user_id,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return client
