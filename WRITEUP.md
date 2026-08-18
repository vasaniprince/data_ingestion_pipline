# WRITEUP.md

## AI tool usage

I used **Claude Code** conversationally across all seven parts, in an
explicit plan → approve → implement → verify rhythm — one plan file per
part under `plan/`, each reviewed and approved before any code was written.

**Where it genuinely sped things up**: the schema (`migrations/001_init.sql`)
was validated by two independent review passes before a line of ingest code
existed, catching a non-unique `field_provenance` current-row index and a
few missing columns before they became a runtime bug. For batch_02, I had
Claude hand-trace the expected outcome (which candidates get created vs.
attached, which merge rule fires for each, the exact `out_of_order_records`
and `nulls_ignored` counts) against the raw data *before* running the
ingest, then treated any mismatch between that trace and the actual run as
a bug signal rather than trusting the first successful-looking run. The
DECISIONS.md policy prose was written in step with the code implementing
it, so the two never drifted apart — each merge rule and normalization
choice got its reasoning written down at the same time it was coded, not
reconstructed afterward.

**The concrete case where it produced something plausible and wrong**: the
combination-merge rule in `src/identity.py`. Early in Part 3, Claude's first
proposal for `_combination_match` — the fallback rule for attaching a row
with no shared strong identifier — keyed on **name + company + city only**
(two signals). It looked sound: it matched the brief's own decline example
("different linkedin_handle, company **and city**" implying the converse),
and it passed every test that existed at that point, including correctly
resolving batch_02's Vikram Singh row. I noticed the problem on review, not
from a failing test: two-signal agreement (a shared name, employer, *and*
city) is a real coincidence risk at any meaningful volume, and a false merge
is explicitly the worse failure the brief grades against. I had Claude
strengthen the rule to require a **third** signal (`current_title`) before
attaching, and added a `combination_match_applied` flag so every candidate
this rule touches is auditable on the record itself, not just inferable
from a docstring. The residual risk (three matching signals is still not
zero) is written down honestly in DECISIONS.md §7 rather than hidden — I did
not let the fix stand in for "solved."

**What this means**: the actual work here was not typing speed — Claude
generated the first draft of nearly every merge rule, trust ranking, and
normalization function in this repo — it was deciding whether each
generated answer was actually *right*, catching the 2-signal case above
being the sharpest example of that evaluation doing real work rather than
rubber-stamping a plausible-looking first draft.

## What I built, and what I'm least sure about

An ingest → identity-resolution → enrichment pipeline over two messy
crawled batches, backed by Postgres, served through a small FastAPI service,
exported to a flat CSV — one row per real person, created once, updated
forever, with every field traceable to the source that won it. `README.md`
has the copy-pasteable run commands; `DECISIONS.md` and `INFRA.md` own the
detailed policy and scale-up reasoning respectively, so I won't re-derive
either here.

The decisions I'm **least** sure about, in order of how much they worry me:

1. **The combination-merge rule, even at three signals.** This is the
   single highest-consequence unresolved call in the whole pipeline. Three
   independent weak signals agreeing is *much* less likely to be a
   coincidence than two, but it is not zero, and the mitigation I'd actually
   trust — a review queue that flags a combination-only match for human or
   higher-confidence confirmation instead of auto-attaching — is designed
   in DECISIONS.md §7 but not built. If I had to bet on where this pipeline
   would produce its first real false merge at scale, it's here.
2. **Enrichment cost accounting.** The CSV shows a paid `not_found` miss
   (Nikhil Reddy) as `enriched=false, enrichment_cost_inr=0.09` — real
   spend, not zero — because hiding a paid miss behind a boolean felt like
   the less honest choice. But the brief's own two-row example only shows
   the found/never-called cases, so this was a judgment call I made, not
   one the spec settled for me.
3. **`open_to_work` NULL rendering as `false` in the CSV.** An unobserved
   signal isn't evidence the person is closed to offers, but the brief's
   CSV example never shows a blank boolean cell, so I chose a concrete
   value over an ambiguous empty one. I'm not confident that's what a
   recruiter reading the sheet would actually want.
4. **The `FIELD_SOURCE_TRUST` table as a whole (`src/config.py`) is reasoned,
   not measured.** Every rank in it — LinkedIn over Naukri for title/company,
   Naukri over LinkedIn for salary/experience/notice, LinkedIn over Naukri
   over Naukri for location, enrichment over everyone for email/phone — is a
   plausibility argument about how each platform's data likely got there
   (self-maintained profile vs. structured HR form vs. a stated preference
   vs. a paid verification call), not a result checked against any ground
   truth. There's no way to know, from this dataset alone, whether LinkedIn's
   title actually turns out more accurate than Naukri's more often; I ranked
   by "which source is closest to the primary fact," not by measured
   outcomes, because no outcome data exists here to measure against. A real
   system would want to validate these rankings against actual accuracy
   before trusting them at volume.
5. **Smaller and secondary**: ranking naukri lowest-trusted for *current*
   location (because its field is `pref_location`, a preference) while
   trusting it most for salary/notice is defensible but is an inversion
   worth someone double-checking against real recruiter behavior.

## Hostile cases: caught vs. not handled

The brief's section 1 plants exactly twelve hostile cases. I went back
through each one against this codebase rather than assume coverage:

1. Five-way LinkedIn URL variants (protocol, www./country subdomain, query
   param, trailing slash, case, plus the old `/pub/` form) — **caught**,
   `normalize_linkedin`.
2. Gmail dot/plus-tag variants of one mailbox, without over-applying the
   rule to non-Gmail domains — **caught**, `normalize_email`;
   `test_sneha_keeps_two_distinct_deloitte_emails` specifically proves the
   rule stays Gmail-scoped rather than merging Sneha's two genuinely
   different Deloitte addresses.
3. Four phone formats — **caught**, `normalize_phone` (E.164, India
   default).
4. Five experience formats, including `"6+"` and the `{"years","months"}`
   dict shape — **caught**, `normalize_experience_months`.
5. Four salary formats, including `"Not Disclosed"` — **caught**,
   `normalize_salary_inr`.
6. Location spelling variants (Bengaluru/Bangalore/Pune,MH/BLR) —
   **caught**, `CITY_GAZETTEER` + `STATE_ABBREV`.
7. Empty-ish value variants (`null`/`""`/`"-"`/`"N/A"`) — **caught**, the
   `EMPTY_ISH` set collapses all of them to `None`.
8. Skills arriving as a list on one row and a comma-string on another
   within the same source — **caught**, `normalize_skills` accepts both.
9. A pure-garbage row that must not crash the run and must not land in the
   table — **caught**, `test_garbage_rows_rejected_not_landed`.
10. Two different real people who share a name, where merging is worse
    than missing — **caught**, `test_two_aaravs_never_merge`; both survive
    as separate candidates, both flagged `name_collision_reviewed`.
11. A batch-2 record with an earlier `_crawled_at` than the batch-1 record
    for the same person — **caught structurally**: recency is compared
    against each field's own stored `observed_at`, never against
    file/batch arrival order, with source-trust checked first. Evidenced by
    batch_02's `out_of_order_records=1` (Vikram's naukri row). I'm being
    precise here rather than overclaiming: this is proven by mechanism plus
    a counter, not by one dedicated "nothing reverted" test the way case 10
    has a named assertion.
12. A batch-2 row for someone already on file that carries *less* data
    than we already had — **caught**, null-protection logs a
    `REJECTED:null_from_source_is_not_deletion` change row instead of
    silently deleting; `nulls_ignored=2` in batch_02 (Rohan's title and
    company).

All twelve are caught. Separately — and these are genuinely different from
the graded list above, not a hedge against it — a few known limitations
remain:

- The combination-merge residual false-merge risk described above.
- The skills alias map (`SKILL_ALIASES`) is a small, curated,
  dataset-informed list, not a real skills ontology — near-duplicate skills
  outside it still sit as separate tokens.
- The location gazetteer is scoped to this dataset's cities, not a general
  geocoder — `INFRA.md` names AWS Location Service/Google Geocoding as the
  production replacement, but it isn't built here.
- The per-row synchronous SQL pattern in `identity.process_record` is fine
  at this dataset's size but is the first thing `INFRA.md` §5 names as
  likely to break at 10x volume.
- The one metric that would catch dedup silently breaking overnight
  (`entities_created / rows_in` against its 7-day median, per `INFRA.md`
  §4) is named but not wired to any real alerting today.

## What I'd build next with one more week

In priority order: the review queue for combination-only matches (flag
instead of auto-attach, and let a human or a later enrichment call
confirm); the real geocoding provider swap in place of the dataset
gazetteer; chunked/checkpointed ingest so a 1M-row run survives a mid-run
crash without replaying from row one; batched identity lookups ahead of the
volume that would actually force the issue; and the `entities_created /
rows_in` alarm actually wired to a metrics pipeline instead of just
specified in a document.
