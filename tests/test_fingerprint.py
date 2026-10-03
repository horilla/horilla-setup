"""Fingerprinting must classify every supported source and refuse everything else.

The refusal cases matter more than the acceptance cases: migrating an
unrecognised schema on a guess is how a database gets corrupted, and the
operator has no way to tell it happened until much later.
"""

import contextlib
import subprocess
import sys
from pathlib import Path

import psycopg2
import pytest

# The tool's own package, one level up from tests/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import (  # noqa: E402
    SCHEMA_VARIANTS,
    find_dump,
    restore_dump,
)

from horillasetup.migration.fingerprint import (  # noqa: E402
    VARIANT_15_PLUS,
    VARIANT_ALREADY_V2,
    VARIANT_PRE_15,
    VARIANT_UNKNOWN,
    fingerprint,
    preflight,
)

EXPECTED_VARIANT = {
    "pre-1.5": VARIANT_PRE_15,
    "1.5-plus": VARIANT_15_PLUS,
}


@contextlib.contextmanager
def connect(db):
    """psycopg2's own context manager commits but does not close, which leaves
    the session open and makes dropdb fail during teardown."""
    conn = psycopg2.connect(dbname=db)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def scratch_db(request):
    """An empty database that is dropped afterwards."""
    name = f"fp_scratch_{request.node.name[:30].replace('[','_').replace(']','')}"
    subprocess.run(["dropdb", "--if-exists", name], check=True)
    subprocess.run(["createdb", name], check=True)
    yield name
    subprocess.run(["dropdb", "--if-exists", name], check=True)


def restore_v1(db, tag="1.6.1"):
    """Restore whichever fixture variant exists for `tag`.

    find_dump rather than a hardcoded name: CI seeds HR data, so it produces
    only v1_<tag>_full.dump and the plain name is never written there.
    """
    path = find_dump(tag)
    if path is None:
        pytest.skip(f"no fixture for {tag}; run fixtures/build_v1.sh {tag}")
    restore_dump(db, path)


# --- acceptance -----------------------------------------------------------

def test_every_supported_tag_is_classified(v1_db, v1_tag):
    with connect(v1_db) as conn:
        fp = fingerprint(conn)
    assert fp.supported, f"{v1_tag} was not recognised as a supported source"
    assert fp.variant == EXPECTED_VARIANT[SCHEMA_VARIANTS[v1_tag]]
    assert fp.table_count == 341


def test_supported_fixtures_pass_preflight(v1_db):
    with connect(v1_db) as conn:
        assert preflight(conn) == []


# --- refusal --------------------------------------------------------------

def test_empty_database_is_refused(scratch_db):
    with connect(scratch_db) as conn:
        fp = fingerprint(conn)
    assert not fp.supported
    assert fp.variant == VARIANT_UNKNOWN
    assert "auth_user" in fp.missing_markers


def test_partial_schema_is_refused(scratch_db):
    """A couple of matching tables must not be enough to pass."""
    with connect(scratch_db) as conn:
        with conn.cursor() as cur:
            cur.execute("create table auth_user(id int); create table base_company(id int);")
        conn.commit()
        fp = fingerprint(conn)
    assert not fp.supported
    assert fp.variant == VARIANT_UNKNOWN


def test_already_migrated_database_is_refused(scratch_db):
    """Re-running the migration must not be possible.

    Detected by v2-only tables from LATER migrations, not by
    horilla_auth_horillauser. Under the rename approach that table is the very
    first thing produced, so a run that died immediately after would look fully
    migrated and be refused -- leaving the operator with a database no tool
    will touch.
    """
    restore_v1(scratch_db)
    with connect(scratch_db) as conn:
        with conn.cursor() as cur:
            cur.execute("create table base_roster(id int)")
        conn.commit()
        fp = fingerprint(conn)
    assert fp.variant == VARIANT_ALREADY_V2
    assert not fp.supported


def test_v2_user_table_name_is_not_the_marker(scratch_db):
    """Guards the regression directly: horilla_auth_horillauser must not be
    what identifies a migrated database. The rename creates it first, before
    any other app has migrated, so treating it as proof of completion would
    refuse a database that has barely started and still needs the tool."""
    restore_v1(scratch_db)
    with connect(scratch_db) as conn:
        with conn.cursor() as cur:
            cur.execute("create table horilla_auth_horillauser(id int)")
        conn.commit()
        fp = fingerprint(conn)
    assert fp.variant != VARIANT_ALREADY_V2


def test_half_upgraded_googledrive_table_is_refused(scratch_db):
    """Pre-1.5 and 1.5+ columns present together means someone has already
    altered the schema by hand. Refuse rather than guess which shape to target."""
    restore_v1(scratch_db)
    with connect(scratch_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "alter table horilla_backup_googledrivebackup "
                "add column service_account_file varchar(100)"
            )
        conn.commit()
        fp = fingerprint(conn)
    assert not fp.supported
    assert fp.unexpected_state


def _hybrid_db(conn, backup_rows=0):
    """Minimal v1 shape: 1.5 attendance columns, pre-1.5 Google Drive table."""
    from horillasetup.migration.fingerprint import V1_MARKER_TABLES
    with conn.cursor() as cur:
        for table in V1_MARKER_TABLES:
            cur.execute(f"create table {table} (id serial primary key)")
        for table in ("attendance_attendance", "attendance_historicalattendance"):
            if table == "attendance_historicalattendance":
                cur.execute(f"create table {table} (id serial primary key)")
            cur.execute(f"alter table {table} add column approved_by_id int")
        cur.execute(
            "create table horilla_backup_googledrivebackup "
            "(id serial primary key, service_account_file varchar(100))"
        )
        for _ in range(backup_rows):
            cur.execute(
                "insert into horilla_backup_googledrivebackup "
                "(service_account_file) values ('x')"
            )
    conn.commit()


def test_empty_pre15_backup_table_with_15_attendance_is_accepted(scratch_db):
    """Untagged master build between 1.4.x and 1.5.0 (discussion #1127)."""
    with connect(scratch_db) as conn:
        _hybrid_db(conn)
        fp = fingerprint(conn)
    assert fp.variant == VARIANT_15_PLUS
    assert any("treated as 1.5+" in m for m in fp.unexpected_state)


def test_hybrid_with_backup_rows_is_still_refused(scratch_db):
    with connect(scratch_db) as conn:
        _hybrid_db(conn, backup_rows=1)
        fp = fingerprint(conn)
    assert not fp.supported


# --- pre-flight blocking --------------------------------------------------

def test_duplicate_company_setting_blocks_migration(scratch_db):
    """v2 makes RecruitmentGeneralSetting.company_id one-to-one. Duplicates
    would make the constraint unsatisfiable and fail the migration partway,
    leaving the schema half-changed."""
    restore_v1(scratch_db)
    with connect(scratch_db) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                insert into base_company(id,is_active,company,hq,address,country,state,city,zip)
                values (901,true,'A',false,'x','x','x','x','1')
            """)
            cur.execute("""
                insert into recruitment_recruitmentgeneralsetting
                    (id,company_id_id,is_active,candidate_self_tracking,show_overall_rating)
                values (901,901,true,false,false),(902,901,true,false,false)
            """)
        conn.commit()

        fp = fingerprint(conn)
        assert fp.supported, "should still be a valid v1 database"
        problems = preflight(conn)

    assert problems, "duplicate company_id must be reported"
    assert "recruitment_recruitmentgeneralsetting" in problems[0]


def test_orphaned_employee_user_blocks_migration(scratch_db):
    """Orphans cannot occur while the FK constraint holds -- Postgres rejects
    the insert. They appear only when the constraint is absent, which happens
    with `pg_restore --disable-triggers` or a data-only restore.

    Reproduced by dropping the constraint first, which is exactly the state a
    badly-restored database arrives in.
    """
    restore_v1(scratch_db)
    with connect(scratch_db) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                select tc.constraint_name
                from information_schema.table_constraints tc
                join information_schema.key_column_usage kcu
                  on tc.constraint_name = kcu.constraint_name
                where tc.table_name = 'employee_employee'
                  and tc.constraint_type = 'FOREIGN KEY'
                  and kcu.column_name = 'employee_user_id_id'
            """)
            row = cur.fetchone()
            assert row, "expected an FK on employee_employee.employee_user_id_id"
            cur.execute(f"alter table employee_employee drop constraint {row[0]}")
            cur.execute("""
                insert into employee_employee
                    (id, employee_first_name, email, phone,
                     employee_user_id_id, is_active,
                     is_from_onboarding, is_directly_converted)
                values (9001, 'Orphan', 'orphan@example.com', '0000000000',
                        99999, true, false, false)
            """)
        conn.commit()
        problems = preflight(conn)

    assert any("employee row" in p for p in problems)


def test_healthy_v1_cannot_contain_orphans(v1_db):
    """The FK constraint is the real guard; preflight is a backstop for
    databases that arrive without it."""
    with connect(v1_db) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                select count(*) from information_schema.table_constraints tc
                join information_schema.key_column_usage kcu
                  on tc.constraint_name = kcu.constraint_name
                where tc.table_name = 'employee_employee'
                  and tc.constraint_type = 'FOREIGN KEY'
                  and kcu.column_name = 'employee_user_id_id'
            """)
            assert cur.fetchone()[0] == 1
