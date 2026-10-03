"""Identify what a database actually is, before migrating it.

No Horilla v1 release ships committed migrations -- `.gitignore` excludes
`**/migrations/**` in every tag from 1.3.2 to 1.6.1 -- so a v1 database's
schema is whatever `makemigrations` generated on the day it was installed.
There is no version string in the database to read, and `django_migrations`
records locally-generated migration names that say nothing about the release.

So the schema itself is the only evidence. This module introspects it and
classifies the database, refusing anything it does not recognise rather than
migrating on a guess.

Measured against five real fixture databases built from tags 1.3.2, 1.4.0,
1.5.0, 1.6.0 and 1.6.1 (see migration_tests/): all five have an identical
341-table set, and differ only in 7 columns. That yields exactly two variants
across the whole supported range.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- what a v1 database looks like ---------------------------------------

V1_TABLE_COUNT = 341

# Tables that must be present for this to be a Horilla v1 database at all.
# Chosen to span several apps so a partial or hand-pruned database fails the
# check rather than sliding through on one lucky match.
V1_MARKER_TABLES = frozenset({
    "auth_user",
    "base_company",
    "employee_employee",
    "attendance_attendance",
    "payroll_payslip",
    "leave_leaverequest",
    "recruitment_recruitment",
    "django_migrations",
})

# Present only from 1.5.0 onward.
POST_15_COLUMNS = frozenset({
    ("attendance_attendance", "approved_by_id"),
    ("attendance_historicalattendance", "approved_by_id"),
    ("horilla_backup_googledrivebackup", "access_token"),
    ("horilla_backup_googledrivebackup", "oauth_credentials_file"),
    ("horilla_backup_googledrivebackup", "refresh_token"),
    ("horilla_backup_googledrivebackup", "token_expiry"),
})

# Present only before 1.5.0; replaced by the OAuth fields above.
PRE_15_COLUMNS = frozenset({
    ("horilla_backup_googledrivebackup", "service_account_file"),
})

# Tables that exist only in v2. Any of them means the database has already
# been migrated, at least partially.
#
# Several markers rather than one, so a partially-migrated database (where an
# earlier attempt died midway) is still recognised.
#
# horilla_auth_horillauser is deliberately NOT among them. Under the rename
# approach it is the FIRST thing the migration produces -- auth_user is renamed
# to it before any other app runs -- so a run that died immediately afterwards
# would look fully migrated and be refused, leaving the operator stuck with a
# database no tool will touch. The four below all come from later migrations,
# so they mean real progress rather than "the very first step happened".
#
# An earlier version used horilla_auth_horillauser as the sole marker under the
# previous db_table design, where it was never created at all. That silently
# failed to detect an already-migrated database, which then had 177 ledger rows
# unapplied and every migration re-run. Both mistakes are the same mistake:
# picking a marker without checking when it appears.
V2_MARKER_TABLES = frozenset({
    "base_roster",
    "base_integrationapps",
    "base_companygroupassignment",
    "attendance_attendanceconflictresolution",
})

VARIANT_PRE_15 = "v1-pre-1.5"      # tags 1.3.2, 1.4.0
VARIANT_15_PLUS = "v1-1.5-plus"    # tags 1.5.0, 1.6.0, 1.6.1
VARIANT_ALREADY_V2 = "already-v2"
VARIANT_UNKNOWN = "unknown"


@dataclass
class Fingerprint:
    variant: str
    table_count: int
    missing_markers: set = field(default_factory=set)
    unexpected_state: list = field(default_factory=list)

    @property
    def supported(self) -> bool:
        return self.variant in (VARIANT_PRE_15, VARIANT_15_PLUS)

    def describe(self) -> str:
        if self.variant == VARIANT_PRE_15:
            return "Horilla v1 (1.3.2-1.4.0 schema)"
        if self.variant == VARIANT_15_PLUS:
            return "Horilla v1 (1.5.0-1.6.1 schema)"
        if self.variant == VARIANT_ALREADY_V2:
            return "already migrated to v2"
        return "unrecognised schema"


def _tables(connection) -> set:
    with connection.cursor() as cur:
        cur.execute(
            "select table_name from information_schema.tables "
            "where table_schema = 'public'"
        )
        return {row[0] for row in cur.fetchall()}


def _columns(connection) -> set:
    with connection.cursor() as cur:
        cur.execute(
            "select table_name, column_name from information_schema.columns "
            "where table_schema = 'public'"
        )
        return {(row[0], row[1]) for row in cur.fetchall()}


def _is_empty_pre15_backup_leftover(connection, columns) -> bool:
    """True for the one hybrid we accept: 1.5 attendance columns, but a Google
    Drive table that is wholly pre-1.5 (no OAuth columns) and has no rows, so
    converting it cannot lose anything."""
    attendance_15 = {
        ("attendance_attendance", "approved_by_id"),
        ("attendance_historicalattendance", "approved_by_id"),
    }
    oauth = {c for c in POST_15_COLUMNS if c[0] == "horilla_backup_googledrivebackup"}
    if not attendance_15 <= columns or oauth & columns:
        return False
    with connection.cursor() as cur:
        cur.execute("select count(*) from horilla_backup_googledrivebackup")
        return cur.fetchone()[0] == 0


def fingerprint(connection) -> Fingerprint:
    """Classify the connected database.

    Never raises on an odd schema -- an unrecognised database is a result to
    report, not an exception, so the caller can print something useful.
    """
    tables = _tables(connection)
    columns = _columns(connection)
    count = len(tables)

    if V2_MARKER_TABLES & tables:
        return Fingerprint(variant=VARIANT_ALREADY_V2, table_count=count)

    missing = V1_MARKER_TABLES - tables
    if missing:
        return Fingerprint(
            variant=VARIANT_UNKNOWN, table_count=count, missing_markers=missing
        )

    has_post15 = bool(POST_15_COLUMNS & columns)
    has_pre15 = bool(PRE_15_COLUMNS & columns)

    hybrid_note = None
    if has_post15 and has_pre15 and _is_empty_pre15_backup_leftover(
        connection, columns
    ):
        # An untagged master build between 1.4.x and 1.5.0: it has the 1.5
        # attendance columns but its (empty) Google Drive table was never
        # converted. horilla_backup/0002 handles that table either way, so
        # classify by the attendance columns instead of refusing.
        variant = VARIANT_15_PLUS
        hybrid_note = (
            "horilla_backup_googledrivebackup is still in the pre-1.5 shape "
            "(empty); treated as 1.5+ -- the migration converts that table"
        )
    elif has_post15 and not has_pre15:
        variant = VARIANT_15_PLUS
    elif has_pre15 and not has_post15:
        variant = VARIANT_PRE_15
    else:
        # Both or neither: a half-upgraded database, or one whose GoogleDrive
        # table was altered by hand. Refuse rather than guess which set of
        # columns the migration should target.
        return Fingerprint(
            variant=VARIANT_UNKNOWN,
            table_count=count,
            unexpected_state=[
                "GoogleDriveBackup columns match neither the pre-1.5 nor the "
                "1.5+ shape exactly; the database may be partially upgraded"
            ],
        )

    fp = Fingerprint(variant=variant, table_count=count)
    if hybrid_note:
        fp.unexpected_state.append(hybrid_note)
    if count != V1_TABLE_COUNT:
        # Not fatal: a customer may legitimately have extra tables from a
        # plugin. Recorded so the operator sees it before proceeding.
        fp.unexpected_state.append(
            f"expected {V1_TABLE_COUNT} tables, found {count}"
        )
    return fp


# --- pre-flight checks ----------------------------------------------------
# Conditions that would make the migration fail partway or corrupt data.
# Checked up front so the operator learns now, not at 60%.


# Uniqueness that v2 introduces and v1 never enforced. Every one of these is a
# migration that fails at the moment the index is built, part-way through
# stage 5, leaving a half-changed schema and a restore.
#
# Found the hard way: a customer's migration died on
# unique_work_record_per_employee_per_date, and Horilla's own demo data turns
# out to contain 111 colliding (employee, date) pairs across 225 work records --
# so any v1 install that loaded the sample data hits it. The rest of this list
# is every other unique_together and UniqueConstraint v2 adds, gathered in one
# pass rather than discovered one restore at a time.
#
# (table, [model field names], what a duplicate means)
V2_UNIQUENESS = [
    ("attendance_workrecords", ["employee_id", "date"],
     "more than one work record for the same employee on the same date"),
    ("attendance_attendance", ["employee_id", "attendance_date"],
     "more than one attendance row for the same employee on the same date"),
    ("payroll_payslip", ["employee_id", "start_date", "end_date"],
     "more than one payslip for the same employee and period"),
    ("attendance_attendanceovertime", ["employee_id", "month", "year"],
     "more than one overtime row for the same employee in the same month"),
    ("attendance_attendancelatecomeearlyout", ["attendance_id", "type"],
     "more than one late-come/early-out row of the same type per attendance"),
    ("base_company", ["company", "address"],
     "two companies with the same name and address"),
    ("base_companyleaves", ["based_on_week", "based_on_week_day"],
     "two company-leave rules for the same week and weekday"),
    ("base_employeeshiftschedule", ["shift_id", "day"],
     "two schedules for the same shift and day"),
    ("base_jobrole", ["job_position_id", "job_role"],
     "two job roles with the same name under one position"),
    ("employee_employee", ["employee_first_name", "employee_last_name", "email"],
     "two employees with the same name and email"),
    ("leave_availableleave", ["leave_type_id", "employee_id"],
     "more than one available-leave row per employee per leave type"),
    ("offboarding_employeetask", ["employee_id", "task_id"],
     "the same offboarding task assigned twice to one employee"),
    ("pms_employeeobjective", ["employee_id", "objective_id"],
     "the same objective assigned twice to one employee"),
    ("recruitment_candidate", ["email", "recruitment_id"],
     "the same email applying twice to one recruitment"),
    # Conditional UniqueConstraints. Each is declared with a condition that
    # excludes NULL -- badge_id__isnull=False, company_id not null -- which is
    # exactly what the NULL filter below already does, so they need no special
    # handling. badge_id is the one most likely to bite: duplicate badge ids
    # accumulate easily in a long-lived HR database and v1 never stopped them.
    ("employee_employee", ["badge_id"],
     "two employees sharing a badge id"),
    ("facedetection_facedetection", ["company_id"],
     "two face-detection configurations for one company"),
    ("geofencing_geofencing", ["company_id"],
     "two geofencing configurations for one company"),
]


def _resolve_column(cur, table, field):
    """The real column for a model field: `field` or, for a FK, `field_id`.

    v1 column names cannot be derived from the v2 field list by rule -- Django
    appends _id to a ForeignKey, and several of these fields are already named
    *_id in the model, giving columns like employee_id_id. Asking the database
    is shorter than encoding that per field, and it cannot drift.
    """
    for candidate in (field, f"{field}_id"):
        cur.execute(
            "select 1 from information_schema.columns "
            "where table_schema='public' and table_name=%s and column_name=%s",
            [table, candidate],
        )
        if cur.fetchone():
            return candidate
    return None


def _uniqueness_violations(connection) -> list:
    """Rows that v2's new unique constraints would reject."""
    problems = []
    with connection.cursor() as cur:
        for table, fields, description in V2_UNIQUENESS:
            cur.execute(
                "select 1 from information_schema.tables "
                "where table_schema='public' and table_name=%s",
                [table],
            )
            if not cur.fetchone():
                continue  # optional app not installed on this database

            columns = [_resolve_column(cur, table, f) for f in fields]
            if any(c is None for c in columns):
                continue  # this v1 predates the field; nothing to collide

            quoted = ", ".join(f'"{c}"' for c in columns)
            # Postgres treats NULLs as distinct in a unique index, so rows with
            # a NULL in any of these columns cannot collide and must not be
            # counted -- reporting them would send the operator hunting for a
            # duplicate the migration is going to accept.
            not_null = " and ".join(f'"{c}" is not null' for c in columns)
            cur.execute(
                f'select count(*) from ('
                f'  select {quoted} from "{table}" where {not_null}'
                f'  group by {quoted} having count(*) > 1) d'
            )
            duplicates = cur.fetchone()[0]
            if duplicates:
                problems.append(
                    f"{table}: {duplicates} duplicate value(s) of "
                    f"({', '.join(columns)}) -- {description}. v2 makes this "
                    "combination unique, so the migration would fail when it "
                    "builds the index. De-duplicate before migrating."
                )
    return problems


def preflight(connection) -> list:
    """Return a list of blocking problems. Empty means clear to proceed."""
    problems = list(_uniqueness_violations(connection))

    with connection.cursor() as cur:
        def table_exists(name):
            """Optional apps leave their tables absent.

            Both checks below used to query their table unconditionally, so
            preflight raised UndefinedTable -- and therefore aborted the whole
            migration -- on any v1 without the recruitment app installed. A
            check that cannot run should be skipped, not fatal.
            """
            cur.execute(
                "select 1 from information_schema.tables "
                "where table_schema = 'public' and table_name = %s",
                [name],
            )
            return cur.fetchone() is not None

        # v2 makes RecruitmentGeneralSetting.company_id a OneToOneField.
        # Duplicates make the unique constraint unsatisfiable, so the
        # migration would fail partway through with the schema half-changed.
        # Column is company_id_id: the model field is named company_id, and
        # Django appends _id to a ForeignKey's database column.
        if table_exists("recruitment_recruitmentgeneralsetting"):
            cur.execute("""
                select count(*) from (
                    select company_id_id from recruitment_recruitmentgeneralsetting
                    where company_id_id is not null
                    group by company_id_id having count(*) > 1
                ) dupes
            """)
            dupes = cur.fetchone()[0]
            if dupes:
                problems.append(
                    f"{dupes} company_id value(s) appear more than once in "
                    "recruitment_recruitmentgeneralsetting. v2 makes this field "
                    "one-to-one; de-duplicate before migrating."
                )

        # Orphaned user references already present in v1 would be blamed on
        # the migration afterwards. Establish that they predate it.
        if table_exists("employee_employee") and table_exists("auth_user"):
            cur.execute("""
                select count(*) from employee_employee e
                where e.employee_user_id_id is not null
                  and not exists (
                    select 1 from auth_user u where u.id = e.employee_user_id_id
                  )
            """)
            orphans = cur.fetchone()[0]
            if orphans:
                problems.append(
                    f"{orphans} employee row(s) reference a user that does not "
                    "exist. Fix these before migrating; they are not caused by "
                    "the migration but will look like it afterwards."
                )

    return problems
