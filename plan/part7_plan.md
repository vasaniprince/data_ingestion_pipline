# Part 7 Plan — WRITEUP.md (~1-2 pages, four required sections)

## Context

Part 7 is the final deliverable: `WRITEUP.md`, repo root, "about 1 to 2
pages." Unlike Parts 1-6, no judging criterion names it directly — but the
brief is explicit that its AI-usage section **"is the one we read first,"**
and it's the only place several standing promises get discharged:

- `DECISIONS.md:231` — "See WRITEUP.md for the full list of hostile cases
  caught vs. not handled."
- `src/normalize.py:19` — "(see INFRA.md / WRITEUP.md)" for the geocoder gap.
- `README.md:5,82` — frames WRITEUP.md as **"the summary"** — it should
  synthesize/point back into DECISIONS.md and INFRA.md, not re-derive them.

Exactly four sub-questions, verbatim from the brief:
1. What you built, and the decisions you are least sure about.
2. Which hostile cases (section 1 of the brief) you caught, and which you
   know you didn't handle — "listing what you missed scores better than
   pretending you caught everything."
3. What you'd build next with one more week.
4. **AI tool usage (required)** — specific tools, where they genuinely sped
   things up, and **one concrete case where the AI produced something
   plausible and wrong, how you noticed, and what you did about it.**

## Confirmed content for the required AI-mistake case (§4)

The 2-signal combination-merge rule. Early in Part 3, I (Claude) proposed
`_combination_match` keyed on **name + company + city only**. It looked
sound, matched the brief's own decline example, and passed the tests that
existed at the time. On reflection the residual false-merge risk (two
different people sharing name, employer, AND city) was too high to leave as
the final design, so it was strengthened to require **three** signals
(+ `current_title`), with a `combination_match_applied` flag added so the
decision is auditable rather than silent. This is a real, checkable
before/after in `src/identity.py` and `DECISIONS.md` §2.2/§7 — a case where
the first AI-proposed answer was plausible, shipped, and then revised once
its risk was reasoned through more carefully, not before.

## Structure of `WRITEUP.md`

Given the brief's own instruction that the AI-usage section is read first,
lead with it, then cover the other three in the brief's listed order —
prose is fine, headed sections keep it scannable.

### 0. What you built (brief orientation, 1 short paragraph)

One paragraph: an ingest+identity-resolution+enrichment pipeline over two
messy batches, backed by Postgres, served via FastAPI, exported to CSV —
one row per person, created once, updated forever, with provenance. Points
to README.md for how to run it. Not a re-derivation of the architecture —
DECISIONS.md and INFRA.md already own that detail.

### 1. AI tool usage (read first — leads the document)

- **Tools**: Claude Code, used conversationally across all 7 parts in a
  plan → approve → implement → verify rhythm (explicit plan files under
  `plan/`, one per part).
- **Where it genuinely sped things up**: schema design validated by two
  independent review passes before writing code; tracing the exact expected
  batch_02 counters (entities_created/updated/merges/out_of_order_records)
  by hand-simulating the merge logic against the raw data *before* running
  it, then treating any mismatch as a bug signal rather than trusting the
  first run; generating the full DECISIONS.md policy prose in step with the
  code that implements it, so neither drifted from the other.
- **The required concrete case**: the 2-signal → 3-signal combination-merge
  rule (above), stated plainly as "here is what was plausible-but-wrong,
  here is how it was caught, here is the fix" — not softened.
- **Judgment, not typing speed**: frame this as the actual point of the
  section — every merge decision, trust ranking, and null-handling rule in
  DECISIONS.md was a judgment call that needed *evaluating*, not just
  generating; the AI-mistake case above is the sharpest example of that
  evaluation actually catching something.

### 2. Decisions least sure about

Own judgment call (user deferred): foreground the ones with genuine,
unresolved residual risk — not a restatement of DECISIONS.md's confident
rules.
- **Combination-merge rule, even at 3 signals** — still non-zero false-merge
  risk at volume; the review-queue mitigation (flag instead of auto-attach
  on combination-only matches) was designed in DECISIONS.md §7 but never
  built. This is the single highest-consequence unresolved call in the
  pipeline, since a false merge is explicitly graded as the worse failure.
- **Enrichment cost accounting** (`enriched=false` but real spend shown,
  e.g. Nikhil Reddy `0.09` not `0.00`) — a defensible transparency choice,
  not an obviously-correct one; the brief's own two-row example never shows
  this exact case.
- **`open_to_work` NULL → `false` in the CSV** — an unobserved signal
  rendered as a concrete negative could read as a false claim to a
  recruiter; chosen only because the brief's CSV example never shows a
  blank boolean cell.
- Briefly note the location-trust ranking (naukri's `pref_location` ranked
  lowest for *current* location despite naukri being the trusted source for
  salary/notice) as a smaller, secondary uncertainty.

### 3. Hostile cases: caught vs. not handled

This is the section `DECISIONS.md:231` explicitly defers to. The brief's
own section 1 enumerates **exactly 12** planted hostile cases (verbatim
list pulled from the docx and verified) — cross-checked one by one against
this codebase:

**All 12 are caught.** Present as a literal numbered checklist mirroring
the brief's own list, each with its mechanism/proof, so it's directly
verifiable against the docx:
1. 5-way LinkedIn URL variants → `normalize_linkedin` (protocol/www/
   subdomain/query-string/trailing-slash/case, plus the old `/pub/` form).
2. Gmail dot/plus-tag variants of one mailbox → `normalize_email`, gmail-
   scoped dot rule (`test_sneha_keeps_two_distinct_deloitte_emails` proves
   the rule is domain-scoped, not universal — it's really testing case 2's
   *sibling* trap, that the rule must NOT over-apply).
3. 4-way phone format variants → `normalize_phone` (E.164, India default).
4. 5-way experience format variants (incl. `"6+"` and the dict shape) →
   `normalize_experience_months`.
5. 4-way salary format variants (incl. "Not Disclosed") →
   `normalize_salary_inr`.
6. Location spelling variants (Bengaluru/Bangalore/Pune,MH/BLR) →
   `CITY_GAZETTEER` + `STATE_ABBREV`.
7. Empty-ish value variants (`null`/`""`/`"-"`/`"N/A"`) → `EMPTY_ISH` set,
   all collapse to `None`.
8. Skills as list vs. comma-string within the same source →
   `normalize_skills` accepts both shapes.
9. Pure garbage row must not crash, must not land →
   `test_garbage_rows_rejected_not_landed` (rows 20, 21).
10. Two real people sharing a name must never merge →
    `test_two_aaravs_never_merge`, both flagged `name_collision_reviewed`.
11. A batch-2 record with an earlier `_crawled_at` than batch-1, for the
    same person → handled structurally: recency compares against each
    field's own stored `observed_at`, never file/batch order, with
    source-trust checked first — evidenced by batch_02's
    `out_of_order_records=1` counter (row 116, Vikram). Flagged honestly as
    proven by mechanism + counter, not by one dedicated "reverts nothing"
    assertion the way case 10 has a named test.
12. A batch-2 row with *less* data than already on file (nulls arriving) →
    null-protection, `REJECTED:null_from_source_is_not_deletion`,
    `nulls_ignored=2` in batch_02 (Rohan's title/company).

**Separately, known limitations beyond that graded list** (real gaps, but
a different category from the 12 above — worth stating precisely rather
than blurring "not on the checklist" with "missed the checklist," since
overclaiming misses that don't exist is its own kind of inaccuracy):
- Combination-merge rule still carries non-zero false-merge risk at scale
  even at 3 signals; the review-queue mitigation was designed
  (DECISIONS.md §7) but never built.
- Skills alias map is a small curated list, not a real ontology (ESCO or
  similar) — near-duplicate skills outside the list still sit separately.
- Location gazetteer is scoped to this dataset's cities, not a real
  geocoder — `INFRA.md` names the production replacement but it isn't built.
- Per-row synchronous SQL in `identity.process_record` — fine at this
  dataset's size, would be the first thing to break at 10x volume per
  `INFRA.md` §5; not rewritten to batched lookups here.
- No automated alerting wired up — `INFRA.md` names the one metric that
  would catch dedup silently breaking, but nothing actually pages anyone
  today.

### 4. What you'd build next with one more week

- The review queue for combination-only matches (flag, don't auto-attach;
  surface for human or higher-confidence confirmation).
- Real geocoding provider swap (per `INFRA.md` §1) instead of the dataset
  gazetteer.
- Chunked/checkpointed ingest (per `INFRA.md` §1/§3) so a 1M-row run
  survives a mid-run crash without a full-batch replay.
- Batched identity lookups (per `INFRA.md` §5) ahead of the volume that
  would actually require it.
- The `entities_created/rows_in` alarm (per `INFRA.md` §4) wired to a real
  metrics pipeline instead of just named as a plan.

## Files

- NEW `WRITEUP.md` (repo root)
- NEW `plan/part7_plan.md` (copy of this plan, per established convention)

No code changes — Part 7 is documentation only, the last deliverable.

## Verification

- Length: "about 1-2 pages" — target roughly 800-1,400 words (INFRA.md was
  ~1,022 words for a stricter "one page"; allow up to ~2x that here, not
  more).
- All four brief sub-questions present, in a natural reading order, with
  the AI-usage section leading per the brief's own "read first" instruction.
- The required AI-mistake case is concrete: names the specific code
  (`_combination_match`), the specific wrong version (2 signals), how it
  was noticed (residual false-merge risk reasoned through), and the fix
  (3 signals + `combination_match_applied` flag) — not vague ("AI
  sometimes makes mistakes").
- The hostile-cases section names specific tests/mechanisms as proof, not
  bare claims — grep the doc against `tests/` filenames to self-check every
  cited test actually exists and asserts what's claimed.
- The "not handled" list isn't padded to look longer than it is, but also
  isn't thinner than what DECISIONS.md §7 and INFRA.md already admit —
  cross-check against both before finalizing.
- Before finalizing, re-read the brief's own section-1 "12 hostile cases"
  list (extract it if not already captured) and confirm the caught/missed
  lists in WRITEUP.md actually match reality case-by-case, not just by
  category.