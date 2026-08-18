import os

DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://saral:saral@localhost:5432/saral",
)

DATA_DIR = os.environ.get("SARAL_DATA_DIR", "data")
OUT_DIR = os.environ.get("SARAL_OUT_DIR", "out")

ENRICHMENT_FILE = os.path.join(DATA_DIR, "enrichment_api.json")
ENRICHMENT_COST_INR = 0.09
ENRICHMENT_BUDGET_CALLS = 15
ENRICHMENT_NEGATIVE_TTL_DAYS = 30
# not_found is a billed call, but "this key currently has no discoverable
# contact info" can change as people update public profiles. 30 days balances
# "don't re-bill for the same miss next week" against "don't treat a miss as
# permanent". Positive (found) results are cached effectively forever: a
# verified email/phone doesn't go stale within our run horizon.

# ---------------------------------------------------------------------------
# Per-source base confidence (0..1). Used as field_provenance.confidence and as
# the final merge tiebreaker. Enrichment carries its own per-record confidence
# from the API, so it is not listed here. Values are defensible defaults, not
# reproductions of the (illustrative) Appendix numbers.
# ---------------------------------------------------------------------------
SOURCE_CONFIDENCE = {
    "linkedin_scraper": 0.90,
    "naukri_export": 0.80,
    "github_crawl": 0.70,
    "enrichment": 0.90,   # fallback only; real value comes from the API row
}

# ---------------------------------------------------------------------------
# Per-field source-trust ranks: LOWER rank wins a cross-source conflict.
# Fields/sources not listed fall back to DEFAULT_TRUST_RANK, then to recency.
# Rationale in DECISIONS.md.
#   title/company : linkedin is the person's own current self-description
#   experience/salary/notice : naukri is structured HR data, most precise
#   location : naukri is `pref_location` (aspirational) -> least trusted for
#              *current* location, so linkedin < github < naukri
#   email/phone : enrichment is a paid verified provider -> most trusted
# ---------------------------------------------------------------------------
FIELD_SOURCE_TRUST = {
    "full_name":          {"linkedin_scraper": 0, "naukri_export": 1, "github_crawl": 2, "enrichment": 1},
    "current_title":      {"linkedin_scraper": 0, "enrichment": 0, "naukri_export": 1, "github_crawl": 2},
    "current_company":    {"linkedin_scraper": 0, "enrichment": 0, "naukri_export": 1, "github_crawl": 2},
    "location":           {"linkedin_scraper": 0, "github_crawl": 1, "naukri_export": 2},
    "experience_months":  {"naukri_export": 0, "linkedin_scraper": 1, "github_crawl": 2},
    "annual_salary_inr":  {"naukri_export": 0, "linkedin_scraper": 1, "github_crawl": 1},
    "notice_period_days": {"naukri_export": 0, "linkedin_scraper": 1},
    "open_to_work":       {"linkedin_scraper": 0, "naukri_export": 0, "github_crawl": 0, "enrichment": 0},
    # email/phone are identities, not scalar fields; ranks here pick the
    # canonical/primary identifier + its stored confidence.
    "email":              {"enrichment": 0, "naukri_export": 1, "linkedin_scraper": 2, "github_crawl": 3},
    "phone":              {"enrichment": 0, "naukri_export": 1, "linkedin_scraper": 2, "github_crawl": 3},
}
DEFAULT_TRUST_RANK = 99

# Scalar fields resolved by the merge engine (single winner each).
MERGE_SCALAR_FIELDS = [
    "full_name", "current_title", "current_company", "location",
    "experience_months", "annual_salary_inr", "notice_period_days", "open_to_work",
]
# Fields whose value is a UNION across sources rather than a single winner.
UNION_FIELDS = {"skills"}

# Fields for which an incoming explicit null against a populated value is worth
# a REJECTED change_log row (null_from_source_is_not_deletion). Volatile flags
# like open_to_work are excluded (absence there is not "data we'd hate to lose").
NULL_PROTECTED_FIELDS = {
    "full_name", "current_title", "current_company", "location",
    "experience_months", "annual_salary_inr", "notice_period_days",
}
