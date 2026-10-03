"""Two gaps found by re-reading the 1.1.1 fixes.

1. The pre-flight uniqueness list covered the 13 `unique_together` rules and one
   of the four `UniqueConstraint`s. `unique_badge_id` was missing, and duplicate
   badge ids accumulate easily in a long-lived HR database that never enforced
   them -- it would abort stage 5 exactly like the work-record duplicates did.

2. Stage 6's holiday check could not see the loss it was written to catch. It
   compared rows in the source before against rows in the destination after:

       lost = count(leave_holiday) - count(base_holidays)

   On a v1 that already stores holidays in `base`, the destination arrives with
   its own rows, so the subtraction reads 3 - 11 = -8 and reports success while
   three holidays are silently dropped. Measured on a reconstruction of a real
   customer database, which is how the underlying copy bug was found -- by
   counting rows by hand, because the verification said nothing.

   Counting distinct natural keys across both tables fixes it without
   false-positiving on the row the copy legitimately skips as a duplicate.
"""

import pytest

from horillasetup.migration.fingerprint import V2_UNIQUENESS, preflight


def _scalar(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
        return row[0] if row else 0


def test_the_conditional_constraints_are_in_the_list():
    """All four UniqueConstraints v2 adds, not just the work-record one."""
    tables = {t for t, _, _ in V2_UNIQUENESS}
    for expected in ("attendance_workrecords", "employee_employee",
                     "facedetection_facedetection", "geofencing_geofencing"):
        assert expected in tables, f"{expected} is not checked by pre-flight"


def test_duplicate_badge_ids_are_reported(pg_connection):
    with pg_connection.cursor() as cur:
        cur.execute(
            "create table employee_employee ("
            "  id serial primary key,"
            "  employee_first_name varchar(200),"
            "  employee_last_name varchar(200),"
            "  email varchar(200),"
            "  badge_id varchar(50))"
        )
        cur.execute(
            "insert into employee_employee "
            "(employee_first_name, employee_last_name, email, badge_id) values "
            "('A','X','a@x','EMP001'),"
            "('B','Y','b@y','EMP001'),"
            "('C','Z','c@z','EMP002')"
        )

    problems = preflight(pg_connection)
    badge = [p for p in problems if "badge_id" in p]
    assert badge, f"duplicate badge ids were not reported: {problems}"
    assert "1 duplicate" in badge[0]


def test_null_badge_ids_are_not_reported(pg_connection):
    """
    The constraint is conditional on badge_id IS NOT NULL, so rows without one
    cannot collide. Reporting them would send the operator hunting a duplicate
    the migration is going to accept.
    """
    with pg_connection.cursor() as cur:
        cur.execute(
            "create table employee_employee ("
            "  id serial primary key,"
            "  employee_first_name varchar(200),"
            "  employee_last_name varchar(200),"
            "  email varchar(200),"
            "  badge_id varchar(50))"
        )
        cur.execute(
            "insert into employee_employee "
            "(employee_first_name, employee_last_name, email, badge_id) values "
            "('A','X','a@x',null), ('B','Y','b@y',null), ('C','Z','c@z',null)"
        )

    assert not [p for p in preflight(pg_connection) if "badge_id" in p]


@pytest.fixture
def holiday_tables(pg_connection):
    """A v1 where `base` already holds holidays -- the shape that hid the loss."""
    with pg_connection.cursor() as cur:
        for name in ("leave_holiday", "base_holidays"):
            cur.execute(
                f"create table {name} ("
                "  id serial primary key,"
                "  name varchar(200) not null,"
                "  start_date date not null)"
            )
        cur.execute(
            "insert into base_holidays (name, start_date) values "
            "('New Year','2025-01-01'),('Republic Day','2025-01-26'),"
            "('Christmas','2025-12-25')"
        )
        cur.execute(
            "insert into leave_holiday (name, start_date) values "
            "('Onam','2024-09-15'),('Diwali','2026-11-08'),"
            "('New Year','2025-01-01')"   # already in the target, by key
        )
    return pg_connection


BEFORE_SQL = """
select count(*) from (select distinct * from (
    select name, start_date from leave_holiday
    union all select name, start_date from base_holidays) u) d
"""
AFTER_SQL = "select count(*) from (select distinct name, start_date from base_holidays) d"


def test_the_before_count_spans_both_tables(holiday_tables):
    """3 in the target + 3 in the source, one shared by key = 5 distinct."""
    assert _scalar(holiday_tables, BEFORE_SQL) == 5


def test_a_silent_drop_is_caught(holiday_tables):
    """The regression: the copy does nothing, target keeps its own 3 rows."""
    before = _scalar(holiday_tables, BEFORE_SQL)
    after = _scalar(holiday_tables, AFTER_SQL)  # copy skipped everything
    assert before - after == 2, "should report the two unique source holidays lost"

    # what the old check would have concluded
    old_before = _scalar(holiday_tables, "select count(*) from leave_holiday")
    assert old_before - after <= 0, (
        "this test is pointless unless the old check really was blind here"
    )


def test_a_correct_copy_reports_no_loss(holiday_tables):
    """The two unique rows carried across, the duplicate skipped."""
    with holiday_tables.cursor() as cur:
        cur.execute(
            "insert into base_holidays (name, start_date) "
            "select name, start_date from leave_holiday s "
            "where not exists (select 1 from base_holidays t "
            "  where t.name is not distinct from s.name "
            "    and t.start_date is not distinct from s.start_date)"
        )

    before = _scalar(holiday_tables, BEFORE_SQL)
    after = _scalar(holiday_tables, AFTER_SQL)
    assert before - after == 0, "a correct copy must not be reported as a loss"


def test_duplicate_payslip_period_is_reported(pg_connection):
    """unique_payslip_per_employee_period (discussion #1127)."""
    with pg_connection.cursor() as cur:
        cur.execute(
            "create table payroll_payslip (id serial primary key, "
            "employee_id_id int, start_date date, end_date date)"
        )
        cur.execute(
            "insert into payroll_payslip (employee_id_id, start_date, end_date) "
            "values (1,'2026-05-01','2026-05-31'),(1,'2026-05-01','2026-05-31'),"
            "(2,'2026-05-01','2026-05-31')"
        )
    pg_connection.commit()
    assert any("payroll_payslip" in p for p in preflight(pg_connection))
