"""Per-user OAuth token persistence.

Single JSON file under the gitignored data/ directory, keyed by Google account
email. Deliberately boring: the spec is a personal-use tool, and a file keeps
the failure modes obvious. Swap for SQLite if this ever goes multi-tenant.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

from backend.config import get_settings
from backend.models import UserTokens


def _path() -> Path:
    return get_settings().token_store_path


def _read_all() -> dict[str, dict]:
    path = _path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A corrupted store should not brick the app — the user can reconnect.
        return {}


def _write_all(data: dict[str, dict]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file in the same directory, then atomically replace, so a
    # crash mid-write can never leave a half-written token file behind.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tokens-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    _restrict_permissions(path)


def _restrict_permissions(path: Path) -> None:
    """Best-effort owner-only permissions (spec 6: file-permission-restricted)."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def save(tokens: UserTokens) -> None:
    data = _read_all()
    data[tokens.email] = json.loads(tokens.model_dump_json())
    _write_all(data)


def load(email: str) -> UserTokens | None:
    raw = _read_all().get(email)
    if raw is None:
        return None
    try:
        return UserTokens.model_validate(raw)
    except ValueError:
        return None


def delete(email: str) -> None:
    data = _read_all()
    if data.pop(email, None) is not None:
        _write_all(data)


def list_emails() -> list[str]:
    return sorted(_read_all().keys())
