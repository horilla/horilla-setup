"""The migration runs from the tool, without the HR codebase being edited.

This is the core claim of the whole design, so it is asserted directly: a real
v1 database, migrated by a genuinely unmodified v2 checkout, using only
MIGRATION_MODULES and a settings wrapper.

The guard that matters most is test_v2_checkout_is_unmodified. An early
verification of this approach was invalid because the checkout still carried
the previous design's `db_table = "auth_user"`, which made the migration look
like it worked when it was not being exercised at all. Every other assertion
here is worthless if that one does not hold.
"""

import os
import subprocess
from pathlib import Path

import pytest

from conftest import database_url, dump_path, psql, restore_dump

TOOL_ROOT = Path(__file__).resolve().parents[1]
V2_ROOT = Path(os.environ.get("HORILLA_V2_ROOT", ""))
V2_PYTHON = os.environ.get("HORILLA_V2_PYTHON", "")

pytestmark = pytest.mark.skipif(
    not (V2_PYTHON and Path(V2_PYTHON).exists()
         and str(V2_ROOT) and (V2_ROOT / "manage.py").exists()),
    reason="set HORILLA_V2_ROOT and HORILLA_V2_PYTHON to a v2 checkout",
)

FK_TO = """
select count(*)
from information_schema.table_constraints tc
join information_schema.constraint_column_usage ccu
  on tc.constraint_name = ccu.constraint_name
where tc.constraint_type = 'FOREIGN KEY' and ccu.table_name = '{}';
"""


def _run(db, script=None, args=None):
    """Run a script or manage.py command against `db` with the tool injected.

    Scripts are written into the project directory rather than a temp dir:
    Django's settings import needs the project on sys.path, and running from
    elsewhere fails with ModuleNotFoundError: No module named 'horilla'.
    """
    env = {
        **os.environ,
        "PYTHONPATH": str(TOOL_ROOT),
        "DJANGO_SETTINGS_MODULE": "horillasetup.migration_settings",
        "HORILLA_ADOPT_EXISTING_SCHEMA": "1",
    }
    env_file = V2_ROOT / ".env"
    original = env_file.read_text()
    env_file.write_text("\n".join(
        f"DATABASE_URL={database_url(db)}"
        if line.startswith("DATABASE_URL=") else line
        for line in original.splitlines()
    ))
    tmp = V2_ROOT / "_pytest_tmp.py"
    try:
        if script is not None:
            tmp.write_text("import django;django.setup()\n" + script)
            cmd = [V2_PYTHON, str(tmp)]
        else:
            cmd = [V2_PYTHON, "manage.py", *args]
        return subprocess.run(cmd, cwd=V2_ROOT, env=env,
                              capture_output=True, text=True)
    finally:
        env_file.write_text(original)
        tmp.unlink(missing_ok=True)


def _restore(db, tag="1.6.1"):
    """These tests need the HR-seeded fixture (companies, employees, payroll);
    the user-only one would leave most assertions with nothing to check."""
    path = dump_path(tag, "_full")
    if not os.path.exists(path):
        pytest.skip(f"no HR fixture at {path}; "
                    f"run HORILLA_SEED_HR_DATA=1 tests/fixtures/build_v1.sh {tag}")
    subprocess.run(["dropdb", "--if-exists", db], check=True)
    subprocess.run(["createdb", db], check=True)
    restore_dump(db, path)


def _migrate(db):
    """Reconcile the ledger, then migrate -- what the Phase 2 entrypoint does."""
    prep = _run(db, script=(
        "from django.db import connection\n"
        "from horillasetup.migration.adopt import ("
        "unapply_colliding_ledger_rows, clear_auth_ordering_conflicts)\n"
        "print('COLL', len(unapply_colliding_ledger_rows(connection)))\n"
        "print('ORD', len(clear_auth_ordering_conflicts(connection)))\n"
    ))
    assert prep.returncode == 0, prep.stderr[-2000:]
    result = _run(db, args=["migrate", "--noinput"])
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
    return prep.stdout


@pytest.fixture(scope="module")
def upgraded():
    """One migrated 1.6.1 database, shared: a full run takes ~2 minutes."""
    db = "ext_upgrade"
    _restore(db)
    before = {
        "users": psql(db, "select count(*) from auth_user"),
        "fks": int(psql(db, FK_TO.format("auth_user"))),
        "hashes": psql(db, "select md5(string_agg(username||password, ',' "
                           "order by username)) from auth_user"),
        "groups": psql(db, "select count(*) from auth_user_groups"),
    }
    _migrate(db)
    yield db, before
    subprocess.run(["dropdb", "--if-exists", db], check=True)


@pytest.fixture(scope="module")
def fresh():
    """The same migration onto an empty database."""
    db = "ext_fresh"
    subprocess.run(["dropdb", "--if-exists", db], check=True)
    subprocess.run(["createdb", db], check=True)
    result = _run(db, args=["migrate", "--noinput"])
    assert result.returncode == 0, result.stdout[-3000:]
    yield db
    subprocess.run(["dropdb", "--if-exists", db], check=True)


# --- the precondition every other test depends on -------------------------

def test_v2_checkout_is_unmodified():
    """No edit to the HR codebase is what this design exists to guarantee.

    Asserted against the remote rather than `git status`, because a local
    commit would leave the tree clean while still not being upstream -- which
    is exactly how an earlier verification of this approach fooled me.
    """
    subprocess.run(["git", "fetch", "-q", "origin", "2.0"],
                   cwd=V2_ROOT, capture_output=True)
    diff = subprocess.run(
        ["git", "diff", "--stat", "origin/2.0", "--",
         "horilla_auth/", "horilla/settings/", "base/migrations/"],
        cwd=V2_ROOT, capture_output=True, text=True,
    )
    assert diff.stdout.strip() == "", (
        "the v2 checkout differs from origin/2.0, so this suite is not "
        f"testing upstream:\n{diff.stdout}"
    )


def test_migration_is_served_from_the_tool(upgraded):
    """MIGRATION_MODULES, not a file in the app directory."""
    db, _ = upgraded
    out = _run(db, script=(
        "from django.db.migrations.loader import MigrationLoader\n"
        "from django.db import connection\n"
        "m = MigrationLoader(connection).disk_migrations[('horilla_auth','0001_initial')]\n"
        "print('MODULE', type(m).__module__)\n"
    ))
    assert "MODULE horillasetup.migrations.horilla_auth" in out.stdout


def test_settings_wrapper_inherits_the_project(upgraded):
    """A wrapper that dropped INSTALLED_APPS would migrate almost nothing."""
    db, _ = upgraded
    out = _run(db, script=(
        "from django.conf import settings\n"
        "print('APPS', len(settings.INSTALLED_APPS))\n"
        "print('USER', settings.AUTH_USER_MODEL)\n"
    ))
    assert "USER horilla_auth.HorillaUser" in out.stdout
    assert int(out.stdout.split("APPS ")[1].split()[0]) > 40


# --- the upgrade path -----------------------------------------------------

def test_tables_are_renamed_not_copied(upgraded):
    db, before = upgraded
    assert psql(db, "select count(*) from horilla_auth_horillauser") == before["users"]
    assert psql(db, "select count(*) from information_schema.tables "
                    "where table_name='auth_user'") == "0"


def test_all_foreign_keys_follow_the_rename(upgraded):
    """The reason for renaming rather than copying: ~328 inbound keys.

    The count afterwards is higher than before, because v2's own new tables
    add their own user references -- so this asserts none are LEFT BEHIND,
    which is the actual failure mode.
    """
    db, before = upgraded
    assert before["fks"] > 300
    assert int(psql(db, FK_TO.format("auth_user"))) == 0
    assert int(psql(db, FK_TO.format("horilla_auth_horillauser"))) >= before["fks"]


def test_no_employee_is_detached_from_its_user(upgraded):
    db, _ = upgraded
    assert psql(db, """
        select count(*) from employee_employee e
        where e.employee_user_id_id is not null and not exists (
          select 1 from horilla_auth_horillauser u where u.id = e.employee_user_id_id)
    """) == "0"


def test_password_hashes_are_byte_identical(upgraded):
    """Not 'a user can log in' -- the stored bytes must be untouched.

    Compared as a hash of the whole table, so a single altered row fails.
    """
    db, before = upgraded
    assert psql(db, "select md5(string_agg(username||password, ',' order by "
                    "username)) from horilla_auth_horillauser") == before["hashes"]


def test_group_membership_survives_the_join_table_rename(upgraded):
    """Django derives the join table AND its column from the model name, so
    this is what breaks if either rename is missed."""
    db, before = upgraded
    assert psql(db, "select count(*) from horilla_auth_horillauser_groups") \
        == before["groups"]
    assert psql(db, "select count(*) from information_schema.columns where "
                    "table_name='horilla_auth_horillauser_groups' and "
                    "column_name='horillauser_id'") == "1"


def test_a_v1_password_still_authenticates(upgraded):
    """The end-user-visible outcome. Checked before any login, since Django
    5.2 rehashes on success and would mask a broken hash."""
    db, _ = upgraded
    out = _run(db, script=(
        "from django.contrib.auth import get_user_model\n"
        "u = get_user_model().objects.get(username='v1user1')\n"
        "print('OK', u.check_password('FixturePassw0rd-1!'))\n"
        "print('REJECT', not u.check_password('wrong-password'))\n"
        "print('GROUPS', list(u.groups.values_list('name', flat=True)))\n"
    ))
    assert "OK True" in out.stdout
    assert "REJECT True" in out.stdout
    assert "HR Managers" in out.stdout


def test_user_flags_and_null_timestamps_survive(upgraded):
    """Flags decide who can log in and who is an administrator, and a NULL
    last_login must not become a timestamp -- a rewritten row would show up
    here rather than in the hash comparison, which only covers passwords."""
    db, _ = upgraded
    assert psql(db, "select count(*) from horilla_auth_horillauser "
                    "where is_superuser") == "1"
    assert psql(db, "select count(*) from horilla_auth_horillauser "
                    "where not is_active") == "1"
    assert psql(db, "select count(*) from horilla_auth_horillauser "
                    "where last_login is null") == "11"


def test_v2_only_tables_are_physically_created(upgraded):
    """Guards against a ledger that claims v2 over a v1 schema."""
    db, _ = upgraded
    for table in ("base_roster", "base_integrationapps",
                  "attendance_attendanceconflictresolution"):
        assert psql(db, "select count(*) from information_schema.tables "
                        f"where table_name='{table}'") == "1", table


def test_hr_data_and_money_are_unchanged(upgraded):
    db, _ = upgraded
    assert psql(db, "select count(*) from employee_employee") == "6"
    assert psql(db, "select count(*) from attendance_attendance") == "15"
    # Exact to the cent: v1 stores money as double precision, so a re-typed
    # column would show up here as drift.
    assert psql(db, "select sum(net_pay) from payroll_payslip") == "133752.93"


def test_state_matches_the_schema_afterwards(upgraded):
    """`migrate --check` is the honest test of SeparateDatabaseAndState: it
    fails if Django's recorded state and the physical schema disagree."""
    db, _ = upgraded
    assert _run(db, args=["migrate", "--check"]).returncode == 0


# --- the fresh-install path -----------------------------------------------

def test_fresh_install_gets_a_user_table(fresh):
    """The regression this test exists for: an earlier version advanced state
    while the physical operation no-op'd, leaving NO user table at all."""
    assert psql(fresh, "select count(*) from information_schema.tables "
                       "where table_name='horilla_auth_horillauser'") == "1"


def test_fresh_install_invents_no_legacy_table(fresh):
    """The rename must not run where there was nothing to adopt."""
    assert psql(fresh, "select count(*) from information_schema.tables "
                       "where table_name='auth_user'") == "0"


def test_fresh_install_join_table_has_djangos_column_name(fresh):
    assert psql(fresh, "select count(*) from information_schema.columns "
                       "where table_name='horilla_auth_horillauser_groups' "
                       "and column_name='horillauser_id'") == "1"


def test_a_user_can_be_created_on_a_fresh_install(fresh):
    out = _run(fresh, script=(
        "from django.contrib.auth import get_user_model\n"
        "U = get_user_model()\n"
        "U.objects.create_user(username='t', password='Test-Passw0rd!')\n"
        "print('OK', U.objects.get(username='t').check_password('Test-Passw0rd!'))\n"
    ))
    assert "OK True" in out.stdout


def test_fresh_state_matches_the_schema(fresh):
    assert _run(fresh, args=["migrate", "--check"]).returncode == 0
