"""Google OAuth flow: consent URL, code exchange, token refresh, session cookies.

Tokens land in `token_store`, keyed by the Google account email, so every later
request can look up whose Gmail/Sheets to touch (spec 3.1).
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timezone

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from backend.config import GOOGLE_SCOPES, get_settings
from backend.models import UserTokens

logger = logging.getLogger(__name__)

# Google hands back the scopes it granted, which include `openid` and the fully
# qualified userinfo scope — not always byte-identical to what we asked for.
# Without this, oauthlib rejects the token response over a cosmetic mismatch.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

if get_settings().oauth_redirect_uri.startswith(("http://localhost", "http://127.0.0.1")):
    # oauthlib refuses plain-http redirects unless told the transport is local.
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

# Imported after the env vars above are set — google_auth_oauthlib pulls in oauthlib.
from google.auth.transport.requests import Request  # noqa: E402
from google.oauth2.credentials import Credentials  # noqa: E402
from google_auth_oauthlib.flow import Flow  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402

from backend.auth import token_store  # noqa: E402


class ReauthRequired(Exception):
    """Raised when a user's stored tokens are missing or no longer refreshable.

    Callers should surface a "reconnect Gmail" prompt rather than crashing
    (spec 3.2: tokens expire every 7 days in testing mode).
    """


class MissingCredentialsFile(Exception):
    """credentials.json is absent — the app cannot start an OAuth flow."""


def _serializer() -> URLSafeTimedSerializer:
    settings = get_settings()
    if not settings.session_secret:
        raise RuntimeError(
            "SESSION_SECRET is not set. Generate one with:\n"
            '  python -c "import secrets; print(secrets.token_urlsafe(32))"\n'
            "and add it to .env"
        )
    return URLSafeTimedSerializer(settings.session_secret, salt="job-tracker-session")


# --- Session cookie -------------------------------------------------------


def make_session_value(email: str) -> str:
    return _serializer().dumps({"email": email})


def read_session_value(cookie: str | None) -> str | None:
    """Return the email inside a signed session cookie, or None if unusable."""
    if not cookie:
        return None
    settings = get_settings()
    try:
        payload = _serializer().loads(
            cookie, max_age=settings.session_max_age_days * 24 * 3600
        )
    except (BadSignature, SignatureExpired):
        return None
    email = payload.get("email") if isinstance(payload, dict) else None
    return email or None


# --- OAuth flow -----------------------------------------------------------


def _build_flow(state: str | None = None, code_verifier: str | None = None) -> Flow:
    settings = get_settings()
    creds_file = settings.google_credentials_file
    if not creds_file.is_absolute():
        from backend.config import PROJECT_ROOT

        creds_file = PROJECT_ROOT / creds_file
    if not creds_file.exists():
        raise MissingCredentialsFile(
            f"Expected Google OAuth client file at {creds_file}. Download it from "
            "Google Cloud Console (APIs & Services > Credentials) and place it there."
        )
    flow = Flow.from_client_secrets_file(
        str(creds_file), scopes=GOOGLE_SCOPES, state=state, code_verifier=code_verifier
    )
    flow.redirect_uri = settings.oauth_redirect_uri
    return flow


def authorization_url() -> tuple[str, str, str]:
    """Return (consent_screen_url, state, code_verifier).

    google-auth-oauthlib auto-generates a PKCE code_verifier per Flow instance
    (default since 1.5.0). The callback builds a brand-new Flow, so the caller
    must round-trip both `state` and `code_verifier` (we stash both in the
    signed oauth-state cookie) or the token exchange fails with
    "Missing code verifier".
    """
    flow = _build_flow()
    url, state = flow.authorization_url(
        access_type="offline",  # ask for a refresh token
        include_granted_scopes="true",
        prompt="consent",  # force a refresh token even on repeat connects
    )
    return url, state, flow.code_verifier


def new_state_token() -> str:
    return secrets.token_urlsafe(24)


def make_state_value(state: str, code_verifier: str) -> str:
    """Pack state + PKCE code_verifier into one signed, short-lived cookie."""
    return URLSafeTimedSerializer(
        get_settings().session_secret, salt="job-tracker-oauth-state"
    ).dumps({"state": state, "code_verifier": code_verifier})


def read_state_value(cookie: str | None) -> dict | None:
    """Return {"state": ..., "code_verifier": ...} from the cookie, or None."""
    if not cookie:
        return None
    try:
        payload = URLSafeTimedSerializer(
            get_settings().session_secret, salt="job-tracker-oauth-state"
        ).loads(cookie, max_age=600)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict) or "state" not in payload:
        return None
    return payload


def exchange_code(code: str, state: str, code_verifier: str) -> UserTokens:
    """Swap an authorization code for credentials and persist them."""
    flow = _build_flow(state=state, code_verifier=code_verifier)
    flow.fetch_token(code=code)
    creds = flow.credentials
    email = _fetch_email(creds)

    existing = token_store.load(email)
    tokens = _to_user_tokens(email, creds)
    if existing:
        # Google omits the refresh token on some repeat grants — keep the old one,
        # and never lose the Sheet we already created for this user.
        tokens.refresh_token = tokens.refresh_token or existing.refresh_token
        tokens.sheet_id = existing.sheet_id
        tokens.sheet_url = existing.sheet_url
    token_store.save(tokens)
    return tokens


def _fetch_email(creds: Credentials) -> str:
    service = build("oauth2", "v2", credentials=creds, cache_discovery=False)
    info = service.userinfo().get().execute()
    email = info.get("email")
    if not email:
        raise ReauthRequired("Google did not return an email address for this account.")
    return email


def _to_user_tokens(email: str, creds: Credentials) -> UserTokens:
    return UserTokens(
        email=email,
        token=creds.token,
        refresh_token=creds.refresh_token,
        token_uri=creds.token_uri,
        client_id=creds.client_id,
        client_secret=creds.client_secret,
        scopes=list(creds.scopes or GOOGLE_SCOPES),
        expiry=creds.expiry.replace(tzinfo=timezone.utc) if creds.expiry else None,
    )


def get_credentials(email: str) -> Credentials:
    """Load a user's credentials, refreshing them if needed.

    Raises ReauthRequired if there is nothing usable on file — callers turn that
    into a "reconnect" prompt instead of a 500.
    """
    stored = token_store.load(email)
    if stored is None:
        raise ReauthRequired(f"No stored credentials for {email}.")

    creds = Credentials(
        token=stored.token,
        refresh_token=stored.refresh_token,
        token_uri=stored.token_uri,
        client_id=stored.client_id,
        client_secret=stored.client_secret,
        scopes=stored.scopes,
        expiry=stored.expiry.replace(tzinfo=None) if stored.expiry else None,
    )

    if creds.valid:
        return creds
    if not creds.refresh_token:
        raise ReauthRequired(f"No refresh token stored for {email}; reconnect needed.")
    try:
        creds.refresh(Request())
    except Exception as exc:  # google raises a family of RefreshErrors
        logger.warning("Token refresh failed for %s: %s", email, type(exc).__name__)
        raise ReauthRequired(f"Could not refresh credentials for {email}.") from exc

    refreshed = _to_user_tokens(email, creds)
    refreshed.sheet_id = stored.sheet_id
    refreshed.sheet_url = stored.sheet_url
    token_store.save(refreshed)
    return creds


def connected_since(email: str) -> datetime | None:
    stored = token_store.load(email)
    return stored.expiry if stored else None
