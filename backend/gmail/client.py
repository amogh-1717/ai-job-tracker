"""Gmail API wrapper: list and fetch candidate threads (spec 3.3).

Works at **thread** granularity, per the spec's recommendation: a single thread
can span "application received" through "unfortunately" weeks later. We hand the
LLM the whole thread text (so a terse rejection reply still has context) and
treat the newest message's timestamp as the thread date.

Preview what would be sent to the LLM, without calling one:

    uv run python -m backend.gmail.client
"""

from __future__ import annotations

import base64
import binascii
import html
import logging
import re
import sys
import time
from datetime import date, datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from backend.auth import oauth
from backend.config import get_settings
from backend.gmail import filters
from backend.models import EmailRecord

logger = logging.getLogger(__name__)

# Keep per-thread text bounded. LLM cost scales with it, and the signal for
# classification is almost always in the first screenful of each message.
MAX_CHARS_PER_MESSAGE = 1500
MAX_CHARS_PER_THREAD = 6000

# Safety rail so a first scan of a busy mailbox cannot silently balloon.
DEFAULT_MAX_THREADS = 1200

# Gmail bills "Total Query Cost" units per user per second *and* per minute, and
# threads.get costs 10 apiece. An unthrottled loop trips `rateLimitExceeded`
# immediately; a 403 mistaken for "unreadable" would silently drop real
# application emails. Deliberately conservative — a rescan is a background
# action triggered by a button, so slow-and-complete beats fast-and-lossy.
_MIN_SECONDS_BETWEEN_CALLS = 1 / 6
_last_call_at = 0.0

# When retries are exhausted, pause this long before giving the thread one last
# chance. Quota windows are per-minute, so a cooldown this size usually clears.
_COOLDOWN_SECONDS = 45.0

# googleapiclient retries 429/5xx and 403-rateLimitExceeded with exponential
# backoff when given num_retries.
_NUM_RETRIES = 6

RETRYABLE_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "backendError"})


class RateLimited(Exception):
    """Gmail kept rate-limiting us even after retries."""


def build_service(credentials):
    """Gmail API client for an already-authorized user."""
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def _throttle() -> None:
    global _last_call_at
    wait = _MIN_SECONDS_BETWEEN_CALLS - (time.monotonic() - _last_call_at)
    if wait > 0:
        time.sleep(wait)
    _last_call_at = time.monotonic()


def _is_rate_limit(exc: HttpError) -> bool:
    if exc.resp is None:
        return False
    status = exc.resp.status
    if status == 429:
        return True
    if status != 403:
        return False
    # A 403 is only transient when Google says it is a rate limit; a genuine
    # permission problem must not be retried or swallowed.
    detail = getattr(exc, "reason", "") or ""
    body = ""
    try:
        body = exc.content.decode("utf-8", errors="replace")
    except (AttributeError, UnicodeDecodeError):
        pass
    haystack = f"{detail} {body}"
    return any(reason in haystack for reason in RETRYABLE_REASONS)


def _execute(request, label: str):
    """Execute an API request with throttling and rate-limit retries."""
    _throttle()
    try:
        return request.execute(num_retries=_NUM_RETRIES)
    except HttpError as exc:
        if _is_rate_limit(exc):
            raise RateLimited(f"Gmail rate limit persisted on {label}") from exc
        raise


# --- MIME helpers ---------------------------------------------------------


def _decode_b64url(data: str) -> str:
    """Gmail returns base64url with the padding stripped."""
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return ""


_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_BREAK_RE = re.compile(r"<br\s*/?>|</p>|</div>|</tr>|</h[1-6]>", re.IGNORECASE)
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANKLINES_RE = re.compile(r"\n{3,}")


def _strip_html(raw: str) -> str:
    text = _SCRIPT_STYLE_RE.sub(" ", raw)
    text = _BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    return html.unescape(text)


def _tidy(text: str) -> str:
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANKLINES_RE.sub("\n\n", text).strip()


def _collect_bodies(payload: dict) -> tuple[list[str], list[str]]:
    """Walk the MIME tree, returning (plain_parts, html_parts)."""
    plain: list[str] = []
    html_parts: list[str] = []

    def walk(part: dict) -> None:
        mime = (part.get("mimeType") or "").lower()
        data = (part.get("body") or {}).get("data")
        if data:
            if mime == "text/plain":
                plain.append(_decode_b64url(data))
            elif mime == "text/html":
                html_parts.append(_decode_b64url(data))
        for sub in part.get("parts") or []:
            walk(sub)

    walk(payload or {})
    return plain, html_parts


def _message_text(message: dict) -> str:
    """Best-effort plain text for one message, preferring text/plain."""
    payload = message.get("payload") or {}
    plain, html_parts = _collect_bodies(payload)
    if plain:
        text = _tidy("\n".join(plain))
    elif html_parts:
        text = _tidy(_strip_html("\n".join(html_parts)))
    else:
        text = _tidy(message.get("snippet") or "")
    return text[:MAX_CHARS_PER_MESSAGE]


def _header(message: dict, name: str) -> str:
    headers = (message.get("payload") or {}).get("headers") or []
    target = name.lower()
    for header in headers:
        if (header.get("name") or "").lower() == target:
            return header.get("value") or ""
    return ""


def _received_at(message: dict) -> datetime:
    raw = message.get("internalDate")
    if raw:
        try:
            return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            pass
    return datetime.now(tz=timezone.utc)


# --- Listing and fetching -------------------------------------------------


def list_candidate_thread_ids(
    service, since: date, max_threads: int = DEFAULT_MAX_THREADS
) -> list[str]:
    """Thread IDs matching the pre-filtered Gmail query, newest first."""
    query = filters.build_query(since)
    logger.info("Gmail query: %s", query)

    thread_ids: list[str] = []
    page_token: str | None = None
    while True:
        remaining = max_threads - len(thread_ids)
        if remaining <= 0:
            break
        response = _execute(
            service.users()
            .threads()
            .list(
                userId="me",
                q=query,
                pageToken=page_token,
                maxResults=min(100, remaining),
            ),
            "threads.list",
        )
        thread_ids.extend(t["id"] for t in response.get("threads") or [])
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return thread_ids[:max_threads]


def fetch_thread(service, thread_id: str) -> EmailRecord | None:
    """Collapse one thread into a single EmailRecord for the LLM.

    Returns None only for threads that are genuinely unreadable. Rate limiting
    propagates as RateLimited so a throttled scan can never be mistaken for an
    empty inbox.
    """
    try:
        thread = _execute(
            service.users().threads().get(userId="me", id=thread_id, format="full"),
            f"threads.get({thread_id})",
        )
    except HttpError as exc:
        logger.warning("Could not fetch thread %s: %s", thread_id, exc)
        return None

    messages = thread.get("messages") or []
    if not messages:
        return None

    messages.sort(key=_received_at)
    newest = messages[-1]

    # Subject comes from the first message (the newest is often a bare "Re:"),
    # sender from the newest -- whoever last moved the application along.
    subject = _header(messages[0], "Subject") or _header(newest, "Subject")
    sender = _header(newest, "From") or _header(messages[0], "From")

    chunks: list[str] = []
    for index, message in enumerate(messages, start=1):
        text = _message_text(message)
        if not text:
            continue
        stamp = _received_at(message).strftime("%Y-%m-%d")
        who = _header(message, "From")
        chunks.append(f"--- message {index} | {stamp} | from: {who} ---\n{text}")

    body = "\n\n".join(chunks)[:MAX_CHARS_PER_THREAD]

    return EmailRecord(
        thread_id=thread_id,
        message_ids=[m["id"] for m in messages],
        subject=subject,
        sender=sender,
        received_at=_received_at(newest),
        body=body,
    )


def fetch_candidates(
    email: str,
    since: date | None = None,
    max_threads: int = DEFAULT_MAX_THREADS,
    seen_message_ids: set[str] | None = None,
    progress: bool = False,
    keyword_prefilter: bool | None = None,
) -> tuple[list[EmailRecord], dict[str, int]]:
    """Fetch and pre-filter candidate threads for one connected account.

    `seen_message_ids` is the dedup hook from spec 3.3 -- Phase 5 passes in the
    message IDs already recorded in the Sheet so a rescan skips threads with
    nothing new in them. A thread is only skipped when *every* message is
    already known; one new reply makes the whole thread a candidate again.

    Returns (candidates, stats).
    """
    settings = get_settings()
    since = since or settings.email_since
    seen = seen_message_ids or set()
    if keyword_prefilter is None:
        keyword_prefilter = settings.use_keyword_prefilter

    credentials = oauth.get_credentials(email)  # raises ReauthRequired
    service = build_service(credentials)

    thread_ids = list_candidate_thread_ids(service, since=since, max_threads=max_threads)
    stats = {
        "threads_matched": len(thread_ids),
        "skipped_already_seen": 0,
        "skipped_noise": 0,
        "skipped_no_job_keywords": 0,
        "skipped_unreadable": 0,
        "rate_limited": 0,
        "candidates": 0,
    }

    candidates: list[EmailRecord] = []
    for index, thread_id in enumerate(thread_ids, start=1):
        if progress and index % 50 == 0:
            logger.info("  fetched %d/%d threads...", index, len(thread_ids))

        try:
            record = fetch_thread(service, thread_id)
        except RateLimited:
            # Never lose a whole scan to a quota blip: cool off, try once more,
            # and if it still fails, record it and keep going. Partial results
            # plus an explicit count beat an exception and nothing.
            logger.warning(
                "Rate limited at thread %d/%d; cooling down %.0fs",
                index,
                len(thread_ids),
                _COOLDOWN_SECONDS,
            )
            time.sleep(_COOLDOWN_SECONDS)
            try:
                record = fetch_thread(service, thread_id)
            except RateLimited:
                stats["rate_limited"] += 1
                continue

        if record is None:
            stats["skipped_unreadable"] += 1
            continue

        if seen and set(record.message_ids).issubset(seen):
            stats["skipped_already_seen"] += 1
            continue

        skip, reason = filters.is_noise(record)
        if skip:
            stats["skipped_noise"] += 1
            logger.debug("Skipping %r -- %s", record.subject[:60], reason)
            continue

        # Keyword gate: the last thing between a thread and a paid LLM call.
        # ATS senders bypass it inside looks_job_related(), so normal
        # application traffic is never gated on wording.
        if keyword_prefilter and not filters.looks_job_related(record):
            stats["skipped_no_job_keywords"] += 1
            logger.debug("Skipping %r -- no job keywords", record.subject[:60])
            continue

        candidates.append(record)

    stats["candidates"] = len(candidates)
    if stats["rate_limited"]:
        logger.warning(
            "%d thread(s) lost to Gmail rate limits - results are PARTIAL. "
            "Re-run to pick them up.",
            stats["rate_limited"],
        )
    return candidates, stats


# --- Local cache ----------------------------------------------------------
#
# Gmail's per-second quota makes a full scan slow, so cache fetched threads
# under the gitignored data/ dir. Later phases can then iterate on extraction
# prompts against a fixed set without re-fetching (and without the API bill).


def _cache_path(name: str):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    return get_settings().data_dir / f"{safe}.json"


def save_cached(name: str, records: list[EmailRecord], merge: bool = True):
    """Write records to the cache, merging with whatever is already there.

    Merging makes a rate-limited scan resumable: each run contributes the
    threads it managed to fetch instead of replacing a fuller previous result
    with a thinner one.
    """
    import json

    path = _cache_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)

    by_thread: dict[str, EmailRecord] = {}
    if merge and path.exists():
        try:
            for record in load_cached(name):
                by_thread[record.thread_id] = record
        except (ValueError, OSError):
            logger.warning("Ignoring unreadable cache at %s", path)
    for record in records:
        by_thread[record.thread_id] = record  # fresher copy wins

    ordered = sorted(by_thread.values(), key=lambda r: r.received_at, reverse=True)
    payload = [json.loads(r.model_dump_json()) for r in ordered]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load_cached(name: str) -> list[EmailRecord]:
    import json

    path = _cache_path(name)
    if not path.exists():
        raise FileNotFoundError(f"No cached candidates at {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [EmailRecord.model_validate(item) for item in raw]


# --- Preview CLI (Phase 3 sanity check) -----------------------------------


def _safe(text: str) -> str:
    """Make text printable on consoles with a narrow codepage (Windows cp1252).

    Subject lines routinely contain emoji; without this the preview dies with
    UnicodeEncodeError instead of showing you your mail.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def _preview() -> int:
    """List candidate threads without calling any LLM."""
    import argparse

    from backend.auth import token_store

    parser = argparse.ArgumentParser(
        description="Preview Gmail candidate threads (no LLM calls)."
    )
    parser.add_argument("--email", help="connected account (default: the only one)")
    parser.add_argument("--max-threads", type=int, default=DEFAULT_MAX_THREADS)
    parser.add_argument("--limit", type=int, default=60, help="how many to list")
    parser.add_argument("--show-body", action="store_true", help="print a body excerpt")
    parser.add_argument("--show-skipped", action="store_true", help="log filter reasons")
    parser.add_argument(
        "--save",
        metavar="NAME",
        help="cache candidates to data/NAME.json so later phases can reuse "
        "them without re-hitting the Gmail API",
    )
    parser.add_argument("--load", metavar="NAME", help="read candidates from that cache")
    parser.add_argument(
        "--no-keyword-filter",
        action="store_true",
        help="ignore USE_KEYWORD_PREFILTER and keep every non-noise thread, so you "
        "can audit what the keyword gate would have dropped",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.show_skipped else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    accounts = token_store.list_emails()
    if not accounts:
        print("No connected accounts. Visit http://localhost:8000/auth/login first.")
        return 1
    email = args.email or accounts[0]
    if email not in accounts:
        print(f"{email} is not connected. Known accounts: {', '.join(accounts)}")
        return 1

    settings = get_settings()
    print(f"\nAccount:     {email}")
    print(f"Since:       {settings.email_since}  (EMAIL_SINCE)")
    print(f"Max threads: {args.max_threads}\n")

    if args.load:
        candidates = load_cached(args.load)
        stats = {"loaded_from_cache": len(candidates)}
        print(f"Loaded {len(candidates)} candidates from cache {args.load!r}\n")
    else:
        try:
            candidates, stats = fetch_candidates(
                email,
                max_threads=args.max_threads,
                progress=True,
                keyword_prefilter=False if args.no_keyword_filter else None,
            )
        except oauth.ReauthRequired as exc:
            print(f"Reconnect needed: {exc}")
            return 1
        except RateLimited as exc:
            print(f"Gmail rate limit: {exc}. Wait a minute and retry.")
            return 1

    if args.save:
        path = save_cached(args.save, candidates)
        print(f"Cached {len(candidates)} candidates to {path}")

    print("--- counts ---")
    for key, value in stats.items():
        print(f"  {key:24} {value}")

    gate = "OFF (--no-keyword-filter)" if args.no_keyword_filter else "ON"
    print(f"\n  keyword gate: {gate}")
    print(f"  -> {len(candidates)} LLM call(s) per rescan")

    print(f"\n--- candidate threads (showing up to {args.limit}) ---")
    for i, record in enumerate(candidates[: args.limit], start=1):
        ats = " [ATS]" if filters.is_from_ats(record.sender) else ""
        kw = " [kw]" if filters.looks_job_related(record) else "     "
        msgs = f" {len(record.message_ids)}msg" if len(record.message_ids) > 1 else ""
        print(
            f"{i:3}. {record.received_at:%Y-%m-%d}{kw}{ats}{msgs}  "
            f"{_safe(record.subject[:70])!r}"
        )
        print(f"     from: {_safe(record.sender[:76])}")
        if args.show_body:
            excerpt = _safe(record.body[:300]).replace("\n", " ")
            print(f"     body: {excerpt}...")
    if len(candidates) > args.limit:
        print(f"\n  ... and {len(candidates) - args.limit} more")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(_preview())
