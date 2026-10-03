# Changelog

## Unreleased

### Fixed: pre-flight missed duplicate payslips

v2 enforces `unique_payslip_per_employee_period` on
`payroll_payslip (employee_id, start_date, end_date)`. It was not in the
pre-flight list, so two identical draft payslips passed pre-flight and then
failed stage 5 while building the index. It is now checked.

A sweep of every v2 `unique_together` / `UniqueConstraint` against 1.6.1 found
no other constraint on a table that already exists in v1 and was missing.

### Changed: accept an untagged build between 1.4.x and 1.5.0

A database with the 1.5 attendance columns (`approved_by_id`) but a Google
Drive backup table still in the pre-1.5 shape was refused as "unrecognised
schema". It is now classified as 1.5+, with a note, when that table has no
OAuth columns and no rows -- `horilla_backup/0002` converts it either way, so
nothing can be lost. Any other mixed shape is still refused.

## 1.1.5

### Fixed: an adopted table kept whatever v1 gave it, and nothing else

A customer's upgrade reported success. Days later the dashboard failed for
non-admin users -- whoever first hit a query that touched the column:

    ProgrammingError: column project_project.company_id_id does not exist

Adoption is what lets v2's `0001_initial` run over a database that already has
the tables: it returned before Django's `create_model` rather than letting the
CREATE TABLE fail. But that statement carries more than the table. It carries
every column, the indexes for those columns, and -- at the end of Django's own
`create_model` -- the join table for each auto-created many-to-many field.

Returning early skipped all of it. A column v2 introduced on a table v1 already
had was never created, while `django_migrations` recorded the migration as
applied. The database looked migrated and could not serve a page.

An adopted table is now reconciled against the model. Missing columns go
through Django's own `add_field`, so each arrives with the constraints, foreign
key and index Django would have given it, and auto-created join tables are
built rather than skipped.

A NOT NULL column with no usable default cannot be added to a table that
already has rows. Guessing a value would be inventing customer data, and
carrying on would leave exactly the half-built table this change prevents, so
that case stops with a message naming the table, the column and the decision
the operator has to make.

Stage 6 now also compares every column Django expects against
`information_schema` and reports what is absent. Reconciliation should leave it
with nothing to find; it exists because the failure being fixed is silence -- a
migration that reports success over a database that cannot serve a page. If
anything still slips through, the tool says so while the operator is watching.

**This does not repair databases already migrated by an earlier version.** A
migration run before 1.1.5 can still be carrying columns that were never
created. The stage 6 check reports them on a re-run, and the columns it names
have to be added deliberately.

## 1.1.4

### Fixed: a stage failure blamed the stage, not the actual error

Every stage runs a probe script inside the project, and any failure was
reported as a problem with what that stage was doing. A customer migration died
because Django could not start at all, and the tool said:

    MigrationError: could not inspect the database:
    OM "horilla_l...

Both halves misled. The database was never reached -- an emoji in a warning
that a Windows cp1252 console could not encode killed `django.setup()`. And the
tail was sliced by character mid-token: `OM "horilla_l` is the end of
`FROM "horilla_ldap_ldapsettings"`. The operator went looking at their data,
which was fine.

Failures are now attributed. A crash during Django startup says so, and says
that nothing has been changed. Output is trimmed by line rather than by
character, so the error stays readable. A `UnicodeEncodeError` -- which is
cosmetic on Linux and fatal on Windows -- carries the `PYTHONUTF8=1` fix.

A genuine database error is still reported plainly, with no invented startup
explanation.

## 1.1.3

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
