"""Normalisation rules for every messy field shape in the raw crawl data.

Every function here is pure and side-effect free: raw value in, canonical
value out (or None for anything that means "empty"). See DECISIONS.md for
the reasoning behind each rule.
"""
import re
from urllib.parse import urlparse

EMPTY_ISH = {"", "-", "n/a", "na", "null", "none", "not disclosed", "unknown", "linkedin member"}

GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}

# Placeholder / test identifiers that must never become real identities.
JUNK_EMAIL_DOMAINS = {"test.com", "example.com", "example.org", "example.net"}

# alias -> (canonical_city, state, country). Deliberately small: covers only
# what appears in this dataset. A real system would use a geocoding service
# (see INFRA.md / WRITEUP.md). Aliases collapse to one canonical city name so
# "Bangalore" and "Bengaluru" and "BLR" are the same place for search + merge.
CITY_GAZETTEER = {
    "bengaluru": ("bengaluru", "karnataka", "IN"),
    "bangalore": ("bengaluru", "karnataka", "IN"),
    "blr": ("bengaluru", "karnataka", "IN"),
    "pune": ("pune", "maharashtra", "IN"),
    "hyderabad": ("hyderabad", "telangana", "IN"),
    "mumbai": ("mumbai", "maharashtra", "IN"),
    "surat": ("surat", "gujarat", "IN"),
    "chennai": ("chennai", "tamil nadu", "IN"),
    "kochi": ("kochi", "kerala", "IN"),
    "jaipur": ("jaipur", "rajasthan", "IN"),
    "noida": ("noida", "uttar pradesh", "IN"),
    "delhi ncr": ("delhi", "delhi", "IN"),
    "delhi": ("delhi", "delhi", "IN"),
}

STATE_ABBREV = {
    "mh": "maharashtra",
    "ka": "karnataka",
    "tn": "tamil nadu",
    "up": "uttar pradesh",
}

COMPANY_SUFFIXES = [
    " software pvt ltd", " pvt ltd", " private limited", " ltd", " inc.", " inc",
    " india", " technologies", " labs",
]


def is_empty_ish(value):
    if value is None:
        return True
    if isinstance(value, str) and value.strip().lower() in EMPTY_ISH:
        return True
    return False


def clean_str(value):
    """Collapse every empty-ish spelling to None; otherwise strip whitespace."""
    if is_empty_ish(value):
        return None
    if isinstance(value, str):
        v = re.sub(r"\s+", " ", value).strip()
        return v if v and v.lower() not in EMPTY_ISH else None
    return value


# ---------------------------------------------------------------- names ----

def normalize_name(raw_name):
    name = clean_str(raw_name)
    if name is None:
        return None, None
    # "Kulkarni, Sneha" -> "Sneha Kulkarni"
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            name = f"{parts[1]} {parts[0]}"
    name = re.sub(r"\s+", " ", name).strip()
    display = " ".join(w.capitalize() if w.isupper() or w.islower() else w for w in name.split(" "))
    normalized = display.lower()
    return display, normalized


# ----------------------------------------------------------- linkedin URL --

def normalize_linkedin(raw_url):
    """Collapse every LinkedIn profile URL spelling to a bare handle."""
    url = clean_str(raw_url)
    if url is None:
        return None
    u = url.strip().lower()
    if not u.startswith("http"):
        u = "https://" + u
    parsed = urlparse(u)
    host = parsed.netloc
    path = parsed.path.rstrip("/")
    if "linkedin.com" not in host:
        return None
    if path.startswith("/in/"):
        handle = path[len("/in/"):]
        handle = handle.strip("/")
        return handle or None
    if path.startswith("/pub/"):
        # old style /pub/name/33/2b1/a4 -> stable synthetic handle
        segs = [s for s in path[len("/pub/"):].split("/") if s]
        return ("pub-" + "-".join(segs)) if segs else None
    return None


# --------------------------------------------------------------- emails ----

def normalize_email(raw_email):
    """Canonicalise an email into (display, match_key).

    display   : lowercased, plus-tag stripped, domain normalised, DOTS KEPT.
    match_key : display, but with dots stripped for gmail/googlemail ONLY.

    The Gmail dot rule is gmail-specific -- applying it to every domain is a
    bug (see brief). Returns (None, None) for empty-ish or junk/test emails.
    """
    email = clean_str(raw_email)
    if email is None:
        return None, None
    email = email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return None, None
    local, _, domain = email.partition("@")
    # strip the +tag on every domain (RFC-common subaddressing convention)
    local_display = local.split("+", 1)[0]
    if domain in GMAIL_DOMAINS:
        domain = "gmail.com"
    if domain in JUNK_EMAIL_DOMAINS:
        return None, None
    display = f"{local_display}@{domain}"
    if domain == "gmail.com":
        match_key = f"{local_display.replace('.', '')}@{domain}"
    else:
        match_key = display
    return display, match_key


# --------------------------------------------------------------- phones ----

def normalize_phone(raw_phone, default_country="91"):
    phone = clean_str(raw_phone)
    if phone is None:
        return None
    digits = re.sub(r"[^\d+]", "", phone)
    has_plus = digits.startswith("+")
    digits = digits.lstrip("+")
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 10:
        digits = default_country + digits
    elif len(digits) == 11 and digits.startswith("0"):
        digits = default_country + digits[1:]
    elif len(digits) == 12 and digits.startswith(default_country):
        pass
    elif has_plus:
        pass  # already had an explicit country code, trust it as-is
    if len(digits) < 11 or len(digits) > 15:
        return None
    # junk filter: all-zeros or a single repeated digit (e.g. 0000000000)
    national = digits[2:] if digits.startswith(default_country) else digits
    if len(set(national)) <= 1:
        return None
    return "+" + digits


# ------------------------------------------------------------- location ----

def normalize_location(raw_location):
    loc = clean_str(raw_location)
    if loc is None:
        return {"raw": None, "city": None, "state": None, "country": None}
    # Multi-location strings ("Hyderabad / Bangalore", "Bangalore, Delhi NCR"):
    # take the first token as primary; the raw string keeps everything.
    first_chunk = re.split(r"[/,]", loc)[0].strip()
    tokens = [t.strip() for t in loc.replace("/", ",").split(",") if t.strip()]

    city = None
    state = None
    country = None

    for tok in tokens:
        key = tok.lower()
        if key == "remote":
            continue
        if key == "india":
            country = "IN"
            continue
        if key in CITY_GAZETTEER:
            if city is None:
                city, state, country = CITY_GAZETTEER[key]
            continue
        if key in STATE_ABBREV:
            state = STATE_ABBREV[key]
            country = country or "IN"
            continue
        if len(key) <= 3 and key.upper() == tok and city is None:
            # short-form like "BLR" already covered by gazetteer; anything
            # else this short is ambiguous, skip rather than guess.
            continue

    if city is None and first_chunk.lower() in CITY_GAZETTEER:
        city, state, country = CITY_GAZETTEER[first_chunk.lower()]

    if country is None and (city is not None or "india" in loc.lower()):
        country = "IN"

    return {"raw": loc, "city": city, "state": state, "country": country}


# --------------------------------------------------------------- skills ----

# Collapse known spelling/formatting variants of the SAME skill to one
# canonical token before the cross-source union runs, so "Powerbi" (naukri)
# and "Power BI" (linkedin) don't sit side by side as two skills. Deliberately
# small and dataset-informed, not a general skills taxonomy -- see DECISIONS.md.
SKILL_ALIASES = {
    "powerbi": "power bi",
    "power-bi": "power bi",
    "ms excel": "excel",
    "msexcel": "excel",
    "advanced excel": "excel",  # treated as the same competency, not a distinct tier
    "postgres": "postgresql",
    "postgre sql": "postgresql",
    "js": "javascript",
    "node": "node.js",
    "nodejs": "node.js",
    "k8s": "kubernetes",
    "py": "python",
    "ml": "machine learning",
}


def normalize_skills(raw_skills):
    if raw_skills is None:
        return []
    if isinstance(raw_skills, str):
        items = raw_skills.split(",")
    elif isinstance(raw_skills, list):
        items = raw_skills
    else:
        return []
    out = []
    seen = set()
    for item in items:
        s = clean_str(item)
        if s is None:
            continue
        s = s.lower().strip()
        s = SKILL_ALIASES.get(s, s)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ----------------------------------------------------------- experience ----

_NUM = r"(\d+(?:\.\d+)?)"


def normalize_experience_months(raw_exp):
    """Accepts '6+', '6 Years 4 Months', '4.5', '7', {'years':6,'months':2}, None."""
    if raw_exp is None:
        return None
    if isinstance(raw_exp, dict):
        years = raw_exp.get("years") or 0
        months = raw_exp.get("months") or 0
        try:
            return round(float(years) * 12 + float(months))
        except (TypeError, ValueError):
            return None
    if isinstance(raw_exp, (int, float)):
        return round(float(raw_exp) * 12)
    text = clean_str(raw_exp)
    if text is None:
        return None
    text = text.strip().lower()
    years_match = re.search(_NUM + r"\s*(?:years?|yrs?)", text)
    months_match = re.search(_NUM + r"\s*(?:months?|mos?)", text)
    if years_match or months_match:
        years = float(years_match.group(1)) if years_match else 0
        months = float(months_match.group(1)) if months_match else 0
        return round(years * 12 + months)
    plain = re.match(r"^" + _NUM + r"\+?$", text)
    if plain:
        return round(float(plain.group(1)) * 12)
    return None


# ---------------------------------------------------------------- salary ---

def normalize_salary_inr(raw_salary):
    """Returns annual salary in INR, or None."""
    if raw_salary is None:
        return None
    if isinstance(raw_salary, (int, float)):
        return int(round(raw_salary))
    text = clean_str(raw_salary)
    if text is None:
        return None
    t = text.strip().lower()
    num_match = re.search(_NUM, t)
    if not num_match:
        return None
    num = float(num_match.group(1))
    if "lpa" in t or "lakh" in t:
        return int(round(num * 100_000))
    if "per month" in t or "/month" in t or "pm" in t.replace(" ", ""):
        return int(round(num * 12))
    return int(round(num))


# ------------------------------------------------------------ notice period-

def normalize_notice_period_days(raw_notice):
    text = clean_str(raw_notice)
    if text is None:
        return None
    t = text.strip().lower()
    if "immediate" in t:
        return 0
    num_match = re.search(_NUM, t)
    if not num_match:
        return None
    num = float(num_match.group(1))
    if "week" in t:
        return int(round(num * 7))
    if "month" in t:
        return int(round(num * 30))
    return int(round(num))


# --------------------------------------------------------------- company ---

def normalize_company_key(raw_company):
    """Lowercased, legal-suffix-stripped key used ONLY for soft identity
    matching (name+company rule). Display value keeps whatever the winning
    source said, verbatim."""
    c = clean_str(raw_company)
    if c is None:
        return None
    key = c.lower().strip()
    if key.startswith("@"):
        key = key[1:]
    for suffix in COMPANY_SUFFIXES:
        if key.endswith(suffix):
            key = key[: -len(suffix)].strip()
    return key or None


def normalize_open_to_work(raw_value):
    if raw_value is None:
        return None
    if isinstance(raw_value, bool):
        return raw_value
    return None
