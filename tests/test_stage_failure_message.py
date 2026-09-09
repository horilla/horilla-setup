"""A stage failure must say what actually failed.

Every stage runs a probe script inside the project, and every failure was
reported as a problem with what the stage was *doing*. A customer's migration
died because Django could not start at all -- an emoji in a warning that a
Windows cp1252 console could not encode -- and the tool said:

    MigrationError: could not inspect the database:
    OM "horilla_l...

Both halves misled. The database was never reached, and the tail was sliced by
character mid-token: `OM "horilla_l` is the end of
`FROM "horilla_ldap_ldapsettings"`. The operator went looking at their data,
which was fine all along.
"""

from types import SimpleNamespace

from horillasetup.migrate_v1 import _stage_failure


def result(stderr):
    return SimpleNamespace(returncode=1, stderr=stderr, stdout="")


DJANGO_STARTUP_CRASH = '''Traceback (most recent call last):
  File "_horillasetup_stage.py", line 2, in <module>
    django.setup()
  File "django/apps/registry.py", line 124, in populate
    app_config.ready()
  File "horilla_ldap/apps.py", line 22, in ready
    ldap_config = config.load_ldap_settings()
  File "encodings/cp1252.py", line 19, in encode
    return codecs.charmap_encode(input, self.errors, encoding_table)[0]
UnicodeEncodeError: 'charmap' codec can't encode characters in position 0-1
'''


def test_a_startup_crash_is_not_blamed_on_the_database():
    msg = _stage_failure(result(DJANGO_STARTUP_CRASH), "could not inspect the database")
    assert "while Django was starting up" in msg
    assert "before the database was reached" in msg
    assert "nothing has been changed" in msg


def test_an_encoding_failure_offers_the_fix():
    msg = _stage_failure(result(DJANGO_STARTUP_CRASH), "could not inspect the database")
    assert "PYTHONUTF8=1" in msg
    assert "console encoding" in msg


def test_the_tail_is_not_cut_mid_token():
    """
    The character slice produced `OM "horilla_l...`. Slicing by line keeps
    whole lines, so a reader sees the actual error.
    """
    msg = _stage_failure(result(DJANGO_STARTUP_CRASH), "could not inspect the database")
    assert 'UnicodeEncodeError' in msg
    for line in msg.splitlines():
        assert not line.startswith('OM "')


def test_a_real_database_error_is_still_reported_plainly():
    """A genuine DB failure must not acquire a misleading startup explanation."""
    stderr = 'psycopg2.errors.UndefinedTable: relation "employee_employee" does not exist'
    msg = _stage_failure(result(stderr), "could not inspect the database")
    assert msg.startswith("could not inspect the database")
    assert "while Django was starting up" not in msg
    assert "PYTHONUTF8" not in msg
    assert "employee_employee" in msg


def test_long_output_is_trimmed_but_keeps_the_end():
    stderr = "\n".join(f"line {i}" for i in range(200)) + "\nFinalError: boom"
    msg = _stage_failure(result(stderr), "pre-flight failed")
    assert "FinalError: boom" in msg
    assert "line 0" not in msg
