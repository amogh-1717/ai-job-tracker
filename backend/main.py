"""FastAPI entrypoint and route definitions (spec 3.1)."""

from __future__ import annotations

import logging

from fastapi import Cookie, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from oauthlib.oauth2.rfc6749.errors import OAuth2Error

from backend.auth import oauth, token_store
from backend.config import PROJECT_ROOT, get_settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Job Application Tracker")

STATE_COOKIE = "jobtracker_oauth_state"
FRONTEND_INDEX = PROJECT_ROOT / "frontend" / "index.html"


def _current_email(session: str | None) -> str | None:
    return oauth.read_session_value(session)


def _set_cookie(response, name: str, value: str, max_age: int) -> None:
    settings = get_settings()
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=True,
        # `lax` so the cookie still rides along on Google's redirect back to us.
        samesite="lax",
        secure=settings.app_base_url.startswith("https://"),
        path="/",
    )


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """Serve the landing page; the page itself asks /status which state to show."""
    if FRONTEND_INDEX.exists():
        return HTMLResponse(FRONTEND_INDEX.read_text(encoding="utf-8"))
    return HTMLResponse(
        "<h1>Job Application Tracker</h1>"
        '<p>Backend is up. <a href="/auth/login">Connect Gmail</a></p>'
    )


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/auth/login")
def auth_login() -> RedirectResponse:
    """Kick off the Google consent flow."""
    try:
        url, state, code_verifier = oauth.authorization_url()
    except oauth.MissingCredentialsFile as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    response = RedirectResponse(url, status_code=302)
    # Round-trip state + PKCE code_verifier in a signed cookie so the callback
    # can prove the response belongs to a flow this browser actually started
    # (CSRF guard) and complete the token exchange (PKCE requirement).
    _set_cookie(
        response, STATE_COOKIE, oauth.make_state_value(state, code_verifier), max_age=600
    )
    return response


@app.get("/auth/callback")
def auth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Handle Google's redirect: exchange the code, store tokens, start a session."""
    if error:
        return RedirectResponse(f"/?auth_error={error}", status_code=302)
    if not code or not state:
        return RedirectResponse("/?auth_error=missing_code", status_code=302)

    state_payload = oauth.read_state_value(request.cookies.get(STATE_COOKIE))
    if not state_payload or state_payload.get("state") != state:
        return RedirectResponse("/?auth_error=state_mismatch", status_code=302)

    try:
        tokens = oauth.exchange_code(
            code=code, state=state, code_verifier=state_payload["code_verifier"]
        )
    except (oauth.ReauthRequired, OAuth2Error) as exc:
        # OAuth2Error covers things like a reused/expired code (invalid_grant) —
        # surface as a redirect the user can retry from, not a 500.
        logger.warning("OAuth exchange failed: %s", exc)
        return RedirectResponse("/?auth_error=exchange_failed", status_code=302)

    # TODO(Phase 5): create the user's Sheet here if they don't have one yet.

    settings = get_settings()
    response = RedirectResponse("/?connected=1", status_code=302)
    _set_cookie(
        response,
        settings.session_cookie_name,
        oauth.make_session_value(tokens.email),
        max_age=settings.session_max_age_days * 24 * 3600,
    )
    response.delete_cookie(STATE_COOKIE, path="/")
    logger.info("Connected Google account %s", tokens.email)
    return response


@app.post("/auth/logout")
def auth_logout(request: Request) -> JSONResponse:
    """Clear the session cookie. Stored tokens are left alone."""
    response = JSONResponse({"ok": True})
    response.delete_cookie(get_settings().session_cookie_name, path="/")
    return response


@app.get("/status")
def status(session: str | None = Cookie(default=None, alias="jobtracker_session")) -> dict:
    """Tell the frontend whether this browser is connected, and to which account."""
    email = _current_email(session)
    if not email:
        return {"connected": False}

    stored = token_store.load(email)
    if stored is None:
        return {"connected": False}

    return {
        "connected": True,
        "email": email,
        "sheet_url": stored.sheet_url,
        "last_scan": None,  # populated from Phase 5
    }


@app.post("/rescan")
def rescan(session: str | None = Cookie(default=None, alias="jobtracker_session")) -> dict:
    """Full Gmail -> LLM -> Sheet pass. Wired up in Phase 5."""
    email = _current_email(session)
    if not email:
        raise HTTPException(status_code=401, detail="Not connected. Connect Gmail first.")
    raise HTTPException(status_code=501, detail="Rescan is not implemented yet (Phase 5).")
