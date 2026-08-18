"""Per-source adapters: turn a raw crawl row (each source has a totally
different field layout) into one canonical `Record`. All source-specific
weirdness is isolated here.

Field semantics for null-protection: a field is emitted (possibly with value
None) only when the source's schema *has* that key. "Key present but null"
(e.g. a linkedin re-crawl with current_company: null) is meaningful -- it must
reach the merge engine so it can be refused as a deletion. "Key absent" (e.g.
github has no salary) simply isn't observed and is skipped.
"""
from dataclasses import dataclass, field

from . import normalize as n
from .config import SOURCE_CONFIDENCE

FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "outlook.com",
    "hotmail.com", "rediffmail.com", "protonmail.com", "icloud.com",
}


def classify_email_type(display_email: str) -> str:
    domain = display_email.partition("@")[2]
    return "personal" if domain in FREE_EMAIL_DOMAINS else "work"


@dataclass
class Identifier:
    itype: str          # linkedin_handle | email | phone | github_login
    value: str          # canonical display value
    match_key: str      # dedup key
    raw_form: str        # original spelling as it appeared in the source
    confidence: float
    email_type: str | None = None


@dataclass
class Record:
    source: str
    batch: str
    source_row_id: int | None       # the source's own _row number
    crawled_at: str | None
    raw_payload: dict
    display_name: str | None
    normalized_name: str | None
    identifiers: list[Identifier] = field(default_factory=list)
    fields: dict = field(default_factory=dict)    # canonical_field -> normalized value (may be None)
    skills: list[str] = field(default_factory=list)
    confidence: float = 0.0
    source_row_pk: int | None = None  # raw_source_rows.id, set by ingest after insert

    @property
    def has_usable_identifier(self) -> bool:
        return len(self.identifiers) > 0


def _email_identifier(raw_value, conf) -> Identifier | None:
    display, match_key = n.normalize_email(raw_value)
    if display is None:
        return None
    return Identifier("email", display, match_key, str(raw_value).strip(),
                      conf, classify_email_type(display))


def _phone_identifier(raw_value, conf) -> Identifier | None:
    val = n.normalize_phone(raw_value)
    if val is None:
        return None
    return Identifier("phone", val, val, str(raw_value).strip(), conf)


def _linkedin_identifier(raw_value, conf) -> Identifier | None:
    handle = n.normalize_linkedin(raw_value)
    if handle is None:
        return None
    return Identifier("linkedin_handle", handle, handle, str(raw_value).strip(), conf)


def map_row(raw: dict, batch: str) -> Record:
    source = raw.get("_src")
    conf = SOURCE_CONFIDENCE.get(source, 0.5)
    rec = Record(
        source=source,
        batch=batch,
        source_row_id=raw.get("_row"),
        crawled_at=raw.get("_crawled_at"),
        raw_payload=raw,
        display_name=None,
        normalized_name=None,
        confidence=conf,
    )

    if source == "linkedin_scraper":
        _map_linkedin(raw, rec, conf)
    elif source == "naukri_export":
        _map_naukri(raw, rec, conf)
    elif source == "github_crawl":
        _map_github(raw, rec, conf)
    else:
        rec.source = source or "unknown"  # ingest will reject unknown sources
    return rec


def _add(rec: Record, ident: Identifier | None):
    if ident is not None:
        rec.identifiers.append(ident)


def _map_linkedin(raw, rec, conf):
    rec.display_name, rec.normalized_name = n.normalize_name(raw.get("full_name"))
    _add(rec, _linkedin_identifier(raw.get("profile_url"), conf))
    if "email" in raw:
        _add(rec, _email_identifier(raw.get("email"), conf))
    if "phone" in raw:
        _add(rec, _phone_identifier(raw.get("phone"), conf))

    if "full_name" in raw:
        rec.fields["full_name"] = rec.display_name
    if "current_title" in raw:
        rec.fields["current_title"] = n.clean_str(raw.get("current_title"))
    if "current_company" in raw:
        rec.fields["current_company"] = n.clean_str(raw.get("current_company"))
    if "location" in raw:
        rec.fields["location"] = n.normalize_location(raw.get("location"))
    if "experience_years" in raw:
        rec.fields["experience_months"] = n.normalize_experience_months(raw.get("experience_years"))
    if "open_to_work" in raw:
        rec.fields["open_to_work"] = n.normalize_open_to_work(raw.get("open_to_work"))
    rec.skills = n.normalize_skills(raw.get("skills"))


def _map_naukri(raw, rec, conf):
    rec.display_name, rec.normalized_name = n.normalize_name(raw.get("candidate_name"))
    _add(rec, _linkedin_identifier(raw.get("linkedin"), conf))
    if "email_id" in raw:
        _add(rec, _email_identifier(raw.get("email_id"), conf))
    if "mobile" in raw:
        _add(rec, _phone_identifier(raw.get("mobile"), conf))

    if "candidate_name" in raw:
        rec.fields["full_name"] = rec.display_name
    if "curr_designation" in raw:
        rec.fields["current_title"] = n.clean_str(raw.get("curr_designation"))
    if "curr_employer" in raw:
        rec.fields["current_company"] = n.clean_str(raw.get("curr_employer"))
    if "pref_location" in raw:
        rec.fields["location"] = n.normalize_location(raw.get("pref_location"))
    if "total_exp" in raw:
        rec.fields["experience_months"] = n.normalize_experience_months(raw.get("total_exp"))
    if "annual_salary" in raw:
        rec.fields["annual_salary_inr"] = n.normalize_salary_inr(raw.get("annual_salary"))
    if "notice_period" in raw:
        rec.fields["notice_period_days"] = n.normalize_notice_period_days(raw.get("notice_period"))
    rec.skills = n.normalize_skills(raw.get("key_skills"))


def _map_github(raw, rec, conf):
    rec.display_name, rec.normalized_name = n.normalize_name(raw.get("name"))
    login = n.clean_str(raw.get("login"))
    if login:
        login = login.lower()
        _add(rec, Identifier("github_login", login, login, str(raw.get("login")), conf))
    # a github `blog` field often holds the person's LinkedIn URL
    if "blog" in raw:
        _add(rec, _linkedin_identifier(raw.get("blog"), conf))
    if "email" in raw:
        _add(rec, _email_identifier(raw.get("email"), conf))

    if "name" in raw:
        rec.fields["full_name"] = rec.display_name
    if "company" in raw:
        rec.fields["current_company"] = n.clean_str(raw.get("company"))  # leading '@' handled below
        if rec.fields["current_company"] and rec.fields["current_company"].startswith("@"):
            rec.fields["current_company"] = rec.fields["current_company"][1:]
    if "location" in raw:
        rec.fields["location"] = n.normalize_location(raw.get("location"))
    if "hireable" in raw:
        rec.fields["open_to_work"] = n.normalize_open_to_work(raw.get("hireable"))
    rec.skills = n.normalize_skills(raw.get("top_languages"))
