# v1 → v2 migration test harness

Every test here runs against **real Postgres** with **real v1 fixture
databases** restored from real Horilla releases. Nothing is mocked: the thing
under test is a schema migration, and an ORM-level fixture would abstract away
exactly what can break.

The harness is slow by design — a full run is several minutes, because each
end-to-end test performs an actual 185-migration run.

## Running

Nothing is required to collect the suite. Tests skip cleanly when their inputs
are absent, so `pytest tests` always runs.

```bash
python -m venv .venv && .venv/bin/pip install pytest psycopg2-binary
.venv/bin/python -m pytest tests -q
```

To run the tests that need a v2 checkout:

```bash
HORILLA_V2_ROOT=/path/to/horilla-hr \
HORILLA_V2_PYTHON=/path/to/horilla-hr/.venv/bin/python \
  .venv/bin/python -m pytest tests -q
```

| Variable | Purpose | Unset |
|---|---|---|
| `HORILLA_V1_WORKDIR` | where fixture dumps live | defaults to `/tmp/horilla-v1-fixtures` |
| `HORILLA_V2_ROOT` | a v2 checkout | those tests skip |
| `HORILLA_V2_PYTHON` | that checkout's interpreter | those tests skip |
| `HORILLA_V1_HR_DUMP` | the HR-data fixture | leave-copy tests skip |

## Building the fixtures

```bash
for t in 1.3.2 1.4.0 1.5.0 1.6.0 1.6.1; do tests/fixtures/build_v1.sh "$t"; done
```

`build_v1.sh` checks out the real tag, builds a venv and creates the schema.
Note it must run `migrate` → `makemigrations` → `migrate`: v1 ships **zero
committed migrations** (every tag gitignores `**/migrations/**`), so `migrate`
alone yields 13 tables instead of 341 and exits 0.

## Files

| File | Tests | Depends on the migration design? |
|---|---|---|
| `test_v1_fixtures.py` | 36 | no — asserts the v1 source shape |
| `test_fingerprint.py` | 22 | no — but imports `horillasetup.migration.fingerprint` |
| `test_auth_adoption.py` | 33 | **yes** — asserts how the user table is adopted |
| `test_migration_e2e.py` | 18 | **yes** — asserts post-migration table names |
| `test_leave_data_copy.py` | 14 | **yes** — drives the migration end to end |

## Current state — read this before trusting a red run

The harness was written against a **previous design** that set
`db_table = "auth_user"` on `HorillaUser`, adopting v1's table in place. That
design was abandoned: it required editing the HR codebase, which is
unmaintainable through Cybro `v2_dev`.

The replacement renames the physical tables from outside the codebase
(`auth_user` → `horilla_auth_horillauser`, plus both M2M join tables and their
`user_id` → `horillauser_id` columns).

So against an unmodified v2 checkout today:

- `test_v1_fixtures.py` — **36 pass**, design-neutral
- `test_fingerprint.py` — skipped until `horillasetup/migration/` lands (Phase 1)
- the other three — **fail**, because they still assert `auth_user` *after*
  migration and call `backdate_auth_migration`

Those failures are **expected and tracked**, not regressions. They are
rewritten in Phases 1–2 to expect the renamed tables. A test asserting the old
design passing today would mean the checkout is contaminated — which is worth
stating plainly, because it already happened once: an early verification run
was invalid because the checkout still carried the abandoned `db_table` change.
Always confirm `git diff origin/2.0` is empty before trusting a green run.
