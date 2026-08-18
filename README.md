# SARAL candidate ingest, identity & enrichment

One row per real person, created once, updated forever, with provenance.
See `DECISIONS.md` for the normalization/identity/merge policy this code
obeys, `INFRA.md` for the scale-up story, and `WRITEUP.md` for the summary.

## Prerequisites

- Docker Desktop (or Docker Engine + Compose v2), running.
- Nothing else — Python/Postgres both run inside containers.

## Get the code

```bash
git clone https://github.com/vasaniprince/data_ingestion_pipline.git
cd data_ingestion_pipline
```

## Run it

```bash
docker compose up -d --build
```

This brings up Postgres (`db`, host port `55432`) and the API (`api`, host
port `8000`). The API container talks to Postgres over the compose network
at `db:5432`; anything you run on the host instead (e.g. `pytest` outside
Docker) should use `DATABASE_URL=postgresql://saral:saral@localhost:55432/saral`.

Wait for both to be healthy, then run the pipeline inside the `api`
container:

```bash
docker compose exec api bash scripts/run_pipeline.sh
```

That's `ingest batch_01` → `ingest batch_02` → `enrich` → `export`, in order,
and it leaves `out/candidates_enriched.csv` on your host (the `out/` volume
is mounted). Equivalent step-by-step, if you want to run (or re-run) any one
stage individually:

```bash
docker compose exec api python -m src.ingest batch_01
docker compose exec api python -m src.ingest batch_02
docker compose exec api python -m src.enrich
docker compose exec api python -m src.export
```

## Prove the double-run guarantee

Run the whole pipeline again and diff the artifacts — the table must not
change and enrichment must not spend anything the second time:

```bash
docker compose exec api bash scripts/run_pipeline.sh
```

Look at the printed `spend_inr` in the enrichment step (should be `0.0` on
the second run) and diff `out/candidates_enriched.csv` against a saved copy
from the first run (should be byte-identical). This is also asserted directly
in the test suite (`tests/test_idempotency.py`, `tests/test_enrichment.py`).

## Hit the API

```bash
curl "http://localhost:8000/candidates?q=backend&location=bengaluru&open_to_work=true"
curl "http://localhost:8000/candidates/<a-candidate-id-from-the-response-above>"
curl "http://localhost:8000/stats"
```

- `GET /candidates` — search/filter (`q`, `role`, `location`, `open_to_work`,
  `min_experience` in years, `limit`/`offset`), paginated.
- `GET /candidates/{id}` — the canonical record plus its identities,
  per-field provenance (with supersession history), and full change log —
  the "why does this row say this" debug view.
- `GET /stats` — the latest run report as JSON (optionally `?batch=batch_01`
  / `batch_02` / `enrichment`).

## Run the tests

```bash
docker compose exec api pytest tests/ -v
```

(or, from the host, with `DATABASE_URL` pointed at `localhost:55432` as above.)

## Deliverables

- `out/candidates_enriched.csv` — the dataset (22 columns, see Appendix A.3
  of the brief and `DECISIONS.md` §9 for exactly how each is derived).
- `out/run_report_batch01.json`, `run_report_batch02.json`,
  `run_report_enrichment.json` — one per pipeline stage, also readable live
  via `GET /stats`.
- `DECISIONS.md`, `INFRA.md`, `WRITEUP.md` — written policy, scale-up plan,
  and summary.
