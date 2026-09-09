# Changelog

## Unreleased

### Changed: `hrms-v2` installs from `2.0`, not `dev/v2.0`

`horillasetup build hrms-v2` cloned the development branch. That is where work
lands first and can carry migrations and schema changes no release has shipped,
so every install was a checkout ahead of every published version -- not
something a setup tool should hand anyone by default. It now clones `2.0`, the
branch releases are cut from.

Changing the clone alone would have fixed only new installs. `upgrade` runs a
bare `git pull`, which follows whatever branch the checkout already tracks, so
existing installs would have stayed on `dev/v2.0` indefinitely. `upgrade` now
moves a checkout to the configured branch first -- and refuses, leaving the tree
untouched, when there are uncommitted changes, rather than switching branches
under someone's work.

CI migrates into `2.0` for the same reason: testing the migration against a
branch users are not on proves the wrong thing. The weekly schedule still
catches upstream drift.

## 1.1.2

### Fixed: three uniqueness rules were missing from the pre-flight check

1.1.1 claimed to check every uniqueness rule v2 introduces. It checked the 13
`unique_together` rules and one of the four `UniqueConstraint`s. Now added:

* `unique_badge_id` -- two employees sharing a badge id. The likeliest of the
  three to bite: v1 never enforced it and duplicates accumulate quietly in a
  long-lived database.
* `unique_company_id_when_not_null_facedetection`
* `unique_company_id_when_not_null_geofencing`

All three are conditional on NOT NULL, which the existing NULL filter already
matches, so they needed no special handling -- only listing. Sixteen rules are
now checked.

### Fixed: stage 6 could not see the holiday loss it was written to catch

The check compared rows in the source before the migration against rows in the
destination after it:

    lost = count(leave_holiday) - count(base_holidays)

On a v1 that already stores holidays in `base` the destination arrives holding
its own rows, so the subtraction read `3 - 11 = -8` and reported success while
three holidays were silently dropped. The 1.1.1 copy bug therefore passed
through both the copy *and* the verification written specifically to catch it;
it was found by counting rows by hand.

Both sides now count distinct natural keys -- `(name, start_date)` for holidays,
`(based_on_week, based_on_week_day)` for company leaves -- with the "before"
spanning the source and the destination together. Distinct keys rather than a
sum, because the copy legitimately skips a source row whose key already exists
in the destination, and summing would report that correct de-duplication as
data loss. Verified both ways against the customer reconstruction: the silent
drop reports 3 lost, a correct copy reports 0.

### Fixed: pre-flight crashed on a database without the recruitment app

The duplicate-company and orphaned-user checks queried their tables
unconditionally, so `preflight` raised `UndefinedTable` -- aborting the whole
migration -- on any v1 where the recruitment app was not installed. A check that
cannot run is now skipped rather than fatal. Found while writing tests for the
above.


## 1.1.1

### Fixed: the holiday copy silently dropped rows when the target already had data

The copy that carries Holiday and CompanyLeave from `leave` into `base` was
idempotent on `id`:

```sql
where not exists (select 1 from base_holidays t where t.id = leave_holiday.id)
```

That is correct only when `base_holidays` is empty or holds rows previously
copied from `leave_holiday`. On a v1 where holidays already live in `base` --
which is the case from some 1.x versions onward -- `base_holidays` holds
unrelated rows at ids 1..n, every source id collides, and every source row is
discarded as "already present". The run reports 0 carried, prints nothing, and
the migration reports success. Silent loss of exactly the data this copy exists
to protect.

Measured on a reconstruction of a real customer database: 3 rows in
`leave_holiday`, 0 copied, no warning.

The copy now matches on a natural key -- `(name, start_date)` for holidays and
`(based_on_week, based_on_week_day)` for company leaves, the latter being v2's
own `unique_together` -- and no longer copies `id`, letting the target assign
fresh ones. `is not distinct from` is used rather than `=` so that nullable
columns match instead of never matching.

Dropping `id` is safe: the v1 schema has no inbound foreign keys to
`leave_holiday` or `leave_companyleave`, and v2's M2M join tables are created by
the migration and empty when the copy runs. Both were verified rather than
assumed.

Every existing test for this copy started from an empty `base_holidays`, which
is why the defect shipped.

### Fixed: uniqueness v2 adds that v1 data violates aborted the migration at stage 5

A customer's migration died building `unique_work_record_per_employee_per_date`.
Horilla's own demo data contains 111 colliding `(employee, date)` pairs across
225 work records, so any v1 install that loaded the sample data hits it -- and
hits it part-way through stage 5, with a half-changed schema and a restore.

Pre-flight now checks the uniqueness rules v2 introduces against the source database
before the backup is taken, and names the table, the columns and the number of
duplicates. Columns are resolved by asking the database whether the model field
is `field` or `field_id`, because Django's `_id` suffixing is not derivable from
the field list. NULLs are excluded, since Postgres treats them as distinct in a
unique index and reporting them would send the operator hunting a duplicate the
migration will accept.

### Fixed: a migration could abort part-way on a database with a stale id sequence

A customer migration failed in stage 5 with

```
psycopg2.errors.UniqueViolation: duplicate key value violates unique constraint
"django_content_type_pkey"
DETAIL:  Key (id)=(219) already exists.
```

Their `django_content_type` sequence was sitting below the highest id the table
held. Nothing notices such a sequence until something inserts without naming an
id, and `migrate`'s `post_migrate` signal does exactly that -- it runs
`create_contenttypes` and `create_permissions`, which insert a row per model v2
adds.

The stale sequence predates the migration: it is what a database looks like
after rows have been inserted with explicit primary keys, via a `loaddata`, a
copy between environments, or a restore that did not reset sequences. The
migration is simply the first thing to insert enough rows to hit it, so it took
the blame and the operator took a restore.

Stage 4 now moves every `id` sequence up to its table's true maximum before
`migrate` runs, and reports how many it reset. Empty tables and healthy
sequences are left alone. `setval` to `max(id)` is idempotent and can only move
a sequence forward to a value that was already correct.

Every table is checked rather than only `django_content_type` -- that is merely
the one `post_migrate` reaches first, and any table whose sequence is behind
would fail the same way the moment v2 inserts into it.


## 1.1.0

### Migrate a Horilla v1 database to v2

```bash
cd /path/to/your/horilla-v2
horillasetup migrate hrms-v2 --from-v1
```

Upgrades an existing v1 database (**1.3.2 – 1.6.1**) in place, keeping every user,
password, holiday and record. Six stages — fingerprint, pre-flight, backup,
ledger reconciliation, migrate, verify — where nothing before the backup writes to
the database, so a refusal leaves it exactly as it was.

Users keep their existing passwords: the stored hashes are carried across
untouched, not reset.

The Horilla codebase itself is not modified. The migration is served from this
tool via Django's `MIGRATION_MODULES`.

### Fixed: holidays were silently destroyed

v2 moves `Holiday` and `CompanyLeave` from the `leave` app to `base` with no data
step between creating the new tables and dropping the old ones. Upgrading without
this tool destroys every holiday and company-leave rule a customer configured,
while reporting success. Confirmed against a real 1.6.1 database: 1 row in, 0 out.

### Removed: `migrate hrms-v2 --existing`

It deleted every row in `django_migrations` and then faked the whole migration
graph, leaving the database recording itself as v2 while the schema was still v1 —
so tables v2 added were never created, with no backup and no way to notice. It now
exits with a pointer to `--from-v1` rather than running.

If you have `--existing` in a script, replace it with `--from-v1`.

### Testing

101 tests against real Postgres and real v1 databases built from real release
tags, run in CI on every push and weekly against Horilla v2's `dev/v2.0`. The
weekly run matters because v2 moves independently of this tool.

---

## 1.0.2 – 1.0.4

Published to PyPI in January and February 2026. The version bumps were not
committed, so this repository still recorded 1.0.1 until 1.1.0; the released code
was otherwise the same as 1.0.1.

## 1.0.0 – 1.0.1

Initial release: `build`, `migrate`, `upgrade` and `install-deps` for HRMS v1,
HRMS v2 and CRM.
