"""Client registration.

The one service function with no agency_id. `core.client` is a global role row —
a lead is what ties a client to an agency — so there is nothing to filter on.
The tenancy guard is the response shape instead (schemas.ClientOut carries no
PII). docs/DECISIONS.md §18.
"""
from __future__ import annotations

from sqlalchemy import select
# postgresql.insert, not sqlalchemy.insert: on_conflict_do_nothing is dialect-specific.
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Client, Person


async def create_or_get_client(
    session: AsyncSession,
    *,
    full_name: str,
    phone: str | None,
    email: str | None,
    telegram_user_id: int | None,
) -> tuple[Client, bool]:
    """Returns (client, created).

    With a telegram_user_id, a returning contact is found through the UNIQUE
    index on pii.person, reached via ON CONFLICT DO NOTHING — race-free for the
    same reason lead dedup is: the loser of two simultaneous first messages
    waits on the winner's row and then reads it. The stored name and phone are
    not overwritten; first write wins.

    Without one there is no stable key (names, emails and phones all collide or
    get reformatted in the live data), so every call creates a new client. With
    NULL the ON CONFLICT never fires, so both cases share this one insert.
    """
    person_id = (
        await session.execute(
            insert(Person)
            .values(full_name=full_name, phone=phone, email=email,
                    telegram_user_id=telegram_user_id)
            .on_conflict_do_nothing(index_elements=[Person.telegram_user_id])
            .returning(Person.id)
        )
    ).scalar_one_or_none()

    if person_id is None:
        # Conflict: this Telegram account is already a person. Its client row
        # was inserted in the same transaction as the person, so it is visible.
        existing = (
            await session.execute(
                select(Client)
                .join(Person, Person.id == Client.person_id)
                .where(Person.telegram_user_id == telegram_user_id)
                .order_by(Client.created_at)
            )
        ).scalars().first()
        if existing is not None:
            await session.commit()
            return existing, False
        # Defensive: a person with this id but no client row (never written by
        # this API). Give them one rather than 500.
        person_id = await session.scalar(
            select(Person.id).where(Person.telegram_user_id == telegram_user_id)
        )

    client = Client(person_id=person_id)
    session.add(client)
    await session.commit()
    await session.refresh(client)
    return client, True
