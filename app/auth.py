"""Supabase Auth JWT validation.

Supports both of Supabase's signing modes, chosen by whether
SUPABASE_JWT_SECRET is set:

  asymmetric (default on new projects) — the project publishes a JWKS endpoint
      and signs with RS256/ES256. Nothing secret is needed by this service; it
      fetches and caches the public keys.

  legacy HS256 — the project has one shared secret. Set SUPABASE_JWT_SECRET.

Validation is deliberately strict: signature, expiry, audience and issuer.
Dropping `aud`/`iss` verification would let a token minted by *any* Supabase
project authenticate here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import jwt
from fastapi import HTTPException, status
from jwt import PyJWKClient

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

_ASYMMETRIC_ALGOS = ["RS256", "ES256"]
_jwks_client: PyJWKClient | None = None


def _get_jwks_client(settings: Settings) -> PyJWKClient:
    """One cached client per process — it caches signing keys internally."""
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(settings.jwks_url, cache_keys=True, lifespan=600)
    return _jwks_client


@dataclass(frozen=True)
class TokenClaims:
    """The parts of a validated Supabase token this service cares about."""
    sub: str                  # auth.users.id -> core.agent.auth_user_id
    email: str | None
    role: str | None          # Supabase's own role claim ("authenticated"), NOT agent.role
    raw: dict


class InvalidToken(Exception):
    """Raised for any token that fails validation. Never leaks the reason to the client."""


def decode_token(token: str, settings: Settings | None = None) -> TokenClaims:
    settings = settings or get_settings()

    options = {"require": ["exp", "sub"]}
    common = dict(
        audience=settings.supabase_jwt_audience,
        issuer=settings.jwt_issuer,
        options=options,
    )

    try:
        if settings.supabase_jwt_secret:
            payload = jwt.decode(
                token, settings.supabase_jwt_secret, algorithms=["HS256"], **common
            )
        else:
            signing_key = _get_jwks_client(settings).get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token, signing_key.key, algorithms=_ASYMMETRIC_ALGOS, **common
            )
    except jwt.ExpiredSignatureError as exc:
        raise InvalidToken("token expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise InvalidToken("wrong audience") from exc
    except jwt.InvalidIssuerError as exc:
        raise InvalidToken("wrong issuer — token is from a different Supabase project") from exc
    except jwt.PyJWTError as exc:
        raise InvalidToken(str(exc)) from exc
    except Exception as exc:  # JWKS fetch failures land here
        log.warning("JWKS/keys unavailable: %s", exc)
        raise InvalidToken("unable to verify token signature") from exc

    return TokenClaims(
        sub=payload["sub"],
        email=payload.get("email"),
        role=payload.get("role"),
        raw=payload,
    )


def unauthorized(detail: str = "Not authenticated") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )
