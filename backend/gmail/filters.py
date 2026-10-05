"""Pre-LLM filtering (spec 3.3).

Two layers:

1. `build_query()` — pushes the cheap, broad exclusions into the Gmail search
   query itself (categories *and* known-noise senders), so junk never costs us
   an API round-trip, let alone an LLM call.
2. `is_noise()` — a local second pass for the marketing and transactional mail
   that still lands in the Primary category.

Deliberately biased toward *letting mail through*. A false positive here
silently loses a real rejection email; a false negative costs a fraction of a
cent in LLM tokens. When in doubt, pass it to the model and let
`is_job_related` decide.
"""

from __future__ import annotations

import re
from datetime import date

from backend.models import EmailRecord

# Applicant tracking systems and recruiting platforms. These almost always mail
# from `no-reply@`/`donotreply@` addresses, which would otherwise trip the noise
# heuristics below — so an ATS match short-circuits all filtering.
ATS_DOMAINS = frozenset(
    {
        "greenhouse.io",
        # Greenhouse sends transactional application mail from a separate domain
        # than their marketing ("MyGreenhouse") one, which is noise below.
        "greenhouse-mail.io",
        "lever.co",
        "myworkday.com",
        "myworkdayjobs.com",
        "workday.com",
        "icims.com",
        "smartrecruiters.com",
        "ashbyhq.com",
        "taleo.net",
        "successfactors.com",
        "jobvite.com",
        "workable.com",
        "workablemail.com",
        "breezy.hr",
        "bamboohr.com",
        "recruitee.com",
        "teamtailor.com",
        "dover.com",
        "ripplematch.com",
        "eightfold.ai",
        "phenompeople.com",
        "avature.net",
        "brassring.com",
        "amazon.jobs",
        # Online assessment / interview scheduling platforms
        "hackerrank.com",
        "codesignal.com",
        "hackerearth.com",
        "karat.io",
        "codility.com",
        "coderpad.io",
        "hirevue.com",
        "hirevue.net",
        "modernhire.com",
        "goodtime.io",
        "calendly.com",
    }
)

# Domains that are never about a specific application of yours. Matched by
# domain *suffix*, so `match.indeed.com` is caught by `indeed.com`.
# Note: `amazon.com` here does NOT shadow `amazon.jobs` above — different TLD.
NOISE_DOMAINS = frozenset(
    {
        # Banking / payments / money movement
        "chase.com",
        "bankofamerica.com",
        "wellsfargo.com",
        "citi.com",
        "citibank.com",
        "capitalone.com",
        "discover.com",
        "americanexpress.com",
        "aexp.com",
        "paypal.com",
        "venmo.com",
        "zellepay.com",
        "cash.app",
        "squareup.com",
        "ubi.bank.in",
        "sbi.co.in",
        "hdfcbank.net",
        "icicibank.com",
        "coinbase.com",
        "robinhood.com",
        "fidelity.com",
        "vanguard.com",
        # Shopping / food / rides / receipts
        "amazon.com",
        "uber.com",
        "ubereats.com",
        "lyft.com",
        "doordash.com",
        "grubhub.com",
        "instacart.com",
        "target.com",
        "walmart.com",
        "bestbuy.com",
        "ebay.com",
        "etsy.com",
        # Social / content / forums
        "facebookmail.com",
        "twitter.com",
        "x.com",
        "instagram.com",
        "reddit.com",
        "quora.com",
        "medium.com",
        "substack.com",
        "pinterest-mail.com",
        "discord.com",
        "nextdoor.com",
        # Subscriptions / media
        "netflix.com",
        "spotify.com",
        "hulu.com",
        "disneyplus.com",
        "youtube.com",
        # Job boards sending *alerts* (not your own applications)
        "indeed.com",
        "ziprecruiter.com",
        "glassdoor.com",
        "monster.com",
        "dice.com",
        "simplyhired.com",
        "careerbuilder.com",
        "handshake.com",
        "joinhandshake.com",
        # Job-board alert/marketing platforms (as opposed to an employer's ATS):
        # these push openings at you rather than updating your own applications.
        "jobs2web.com",
        "greenhouse-jobs.com",
        "wayup.com",
        "builtin.com",
        "em.linkedin.com",
        # Newsletters and unrelated services observed in this inbox
        "apexearlycareers.com",
        "getsquire.com",
        # Government / visa / passport services. Keyword-heavy ("appointment",
        # "confirm your identity") but never about a job application.
        "vfsglobal.com",
        "vfshelpline.com",
        "passportindia.gov.in",
    }
)

# Full-address or local-part fragments worth skipping regardless of domain.
NOISE_SENDER_SUBSTRINGS = (
    "noreply@linkedin.com",
    "jobalerts-noreply@linkedin.com",
    "jobs-listings@linkedin.com",
    "noreply-accounts@google.com",
    "news@",
    "newsletter@",
    "digest@",
    "marketing@",
    "promotions@",
    "deals@",
    "billing@",
    "receipts@",
    "invoice@",
    "statements@",
    "notifications@github.com",
)

# Subject lines that signal bulk or transactional mail, not an application update.
NOISE_SUBJECT_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bunsubscribe\b",
        r"\bnewsletter\b",
        r"\b(?:daily|weekly|monthly) digest\b",
        r"\b\d{1,3}%\s*off\b",
        r"\bflash sale\b",
        r"\blimited time offer\b",
        r"\bwebinar\b",
        # Job-board alerts rather than your own applications
        r"\bjobs? (?:alert|recommendation)s?\b",
        r"\bnew jobs? (?:for|matching)\b",
        r"\bjobs? you may be interested in\b",
        r"\b\d+\s+new jobs?\b",
        r"\bnew jobs? posted\b",
        r"\bjob matches\b",
        r"\bthis job is a match\b",
        r"\bdream job\b",
        r"\bsee who else is applying\b",
        r"\byour (?:trial|free trial)\b",
        r"\bthird-party oauth\b",
        r"\bpeople you may know\b",
        r"\bviewed your profile\b",
        # Transactional / account noise
        r"\bstatement of account\b",
        r"\byour (?:order|receipt|invoice|statement|subscription|refund)\b",
        r"\breceipt (?:for|from)\b",
        r"\bzelle\b",
        r"\bpayment (?:sent|received|due|confirmation)\b",
        r"\bfinish setting up\b",
        r"\byou shared some google account data\b",
        r"\bpassword reset\b",
        r"\bverify your email\b",
        r"\bsecurity alert\b",
        r"\btwo-?factor\b",
        r"\bhiring\b.*\bnear you\b",
    )
)


# Positive job signal. NOT used to filter by default — the LLM is the classifier
# per spec 3.4, and keyword gating would silently drop anything phrased unusually.
# Exposed so the preview can report how much an optional keyword pre-filter would
# save, and so it can be switched on deliberately if the LLM bill justifies it.
JOB_SIGNAL_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bapplicat(?:ion|ions)\b",
        r"\bapplied\b",
        r"\bapplying\b",
        r"\bthank you for (?:your interest|applying)\b",
        r"\binterview\b",
        r"\brecruit(?:er|ers|ing|ment)\b",
        r"\bcandidat(?:e|es|ure)\b",
        r"\bcandidac(?:y|ies)\b",
        r"\bapplicant\b",
        r"\breq(?:uisition)? ?id\b",
        r"\bhiring (?:team|manager|process)\b",
        r"\bnext steps?\b",
        r"\bphone screen\b",
        r"\bon-?site\b",
        r"\btechnical (?:screen|interview|assessment)\b",
        r"\bwe (?:regret|are unable) to\b",
        r"\bother candidates\b",
        r"\bpursue other\b",
        r"\bhiring\b",
        r"\bjob (?:opening|posting|offer)\b",
        r"\bonline assessment\b",
        r"\bcoding (?:challenge|assessment|test)\b",
        r"\btake-?home\b",
        r"\boffer letter\b",
        r"\bmove forward\b",
        r"\bnot (?:moving|move) forward\b",
        r"\bunfortunately\b",
        r"\bposition\b",
        r"\bthe role\b",
        r"\bintern(?:ship)?\b",
        r"\bnew grad\b",
        r"\bsoftware engineer\b",
        r"\btalent (?:team|acquisition)\b",
    )
)


def looks_job_related(record: EmailRecord) -> bool:
    """Keyword job-signal check used to gate LLM calls (spec 3.3 pre-filtering).

    Recruiting senders bypass the keyword test entirely, so an oddly worded
    message from an ATS or a `careers@` address is never gated out.
    """
    if is_recruiting_sender(record.sender):
        return True
    haystack = f"{record.subject or ''}\n{record.body or ''}"
    return any(p.search(haystack) for p in JOB_SIGNAL_PATTERNS)


def _domain_of(sender: str) -> str:
    """Extract the domain from a `Name <user@host>` style sender.

    Takes the *last* `@` group: a display name can itself contain an `@`
    (`"a@b" <real@example.com>`), and the address always comes last.
    """
    matches = re.findall(r"@([A-Za-z0-9.\-]+)", sender or "")
    return matches[-1].lower().rstrip(".").rstrip(">") if matches else ""


# Local-parts employers use for recruiting mail from their own domain. Treated
# like an ATS (bypassing the keyword gate) because these senders are
# unambiguously recruiting-related even when the wording is unusual.
CAREERS_LOCALPARTS = (
    "careers",
    "career",
    "recruiting",
    "recruitment",
    "recruiter",
    "talent",
    "talentacquisition",
    "universityrecruiting",
    "campusrecruiting",
    "earlycareers",
    "universityprograms",
    "jobs",
    "hiring",
    "candidatefeedback",
)


def is_recruiting_sender(sender: str) -> bool:
    """True for an ATS, or an employer address that exists to send recruiting mail."""
    if is_from_ats(sender):
        return True
    local = (sender or "").lower()
    # Narrow to the address, then to its local-part.
    if "<" in local and ">" in local:
        local = local[local.rfind("<") + 1 : local.rfind(">")]
    local = local.split("@")[0]
    normalized = re.sub(r"[^a-z]", "", local)
    return any(normalized.startswith(tag) or tag in normalized for tag in CAREERS_LOCALPARTS)


def _domain_matches(domain: str, domain_set: frozenset[str]) -> bool:
    """True if `domain` equals, or is a subdomain of, anything in `domain_set`."""
    if not domain:
        return False
    parts = domain.split(".")
    candidates = {".".join(parts[i:]) for i in range(len(parts))}
    return bool(candidates & domain_set)


def is_from_ats(sender: str) -> bool:
    """True if the sender is a known recruiting / assessment platform."""
    return _domain_matches(_domain_of(sender), ATS_DOMAINS)


def build_query(since: date) -> str:
    """Gmail search query: fixed start date, minus bulk categories and noise senders.

    Excluding senders server-side is the single biggest cost saver here: those
    threads never consume a `threads.get` call, which is what the per-second
    Gmail quota is actually spent on.
    """
    excluded_senders = " OR ".join(sorted(NOISE_DOMAINS))
    return " ".join(
        [
            f"after:{since.strftime('%Y/%m/%d')}",
            "-category:promotions",
            "-category:social",
            "-category:forums",
            "-in:chats",
            "-in:spam",
            "-in:trash",
            f"-from:({excluded_senders})",
        ]
    )


def is_noise(record: EmailRecord) -> tuple[bool, str]:
    """Return (should_skip, reason). Reason is for logging, not the user."""
    sender = (record.sender or "").lower()
    domain = _domain_of(sender)

    # An ATS is always worth reading, even from a no-reply address.
    if _domain_matches(domain, ATS_DOMAINS):
        return False, ""

    if _domain_matches(domain, NOISE_DOMAINS):
        return True, f"noise domain ({domain})"

    for needle in NOISE_SENDER_SUBSTRINGS:
        if needle in sender:
            return True, f"noise sender ({needle})"

    subject = record.subject or ""
    for pattern in NOISE_SUBJECT_PATTERNS:
        if pattern.search(subject):
            return True, f"noise subject (/{pattern.pattern}/)"

    if not subject.strip() and not (record.body or "").strip():
        return True, "empty subject and body"

    return False, ""
