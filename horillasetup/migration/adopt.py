"""Let v2's initial migrations run against a database that already has v1's tables.

Every v2 app's `0001_initial` is a plain `CreateModel` set, generated against
an empty database. On a v1 database those tables already exist, so the first
`CREATE TABLE` raises:

    relation "base_announcementcomment" already exists

That single fact is what forced the old tool into `migrate --fake`, which
marked all 28 apps applied without applying anything -- leaving the migration
state wrong for every migration that followed.

The fix is neither rewriting 185 migration files nor faking them. It is to
make `CREATE TABLE` idempotent for the duration of the initial migrations:
when the table is already there, skip creating it and let Django's state
advance normally. Later migrations (AlterField, AddField, RunPython) then
apply for real, on top of correct state.

Enabled only when HORILLA_ADOPT_EXISTING_SCHEMA is set, so a normal install
and a normal test run are completely unaffected.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

ENV_FLAG = "HORILLA_ADOPT_EXISTING_SCHEMA"


def adoption_enabled() -> bool:
    return os.environ.get(ENV_FLAG, "").lower() in ("1", "true", "yes", "on")


# Postgres SQLSTATEs for "this object is already here". Matched on the code
# rather than the message so the check is not locale-dependent.
_DUPLICATE_SQLSTATES = frozenset({
    "42P07",  # duplicate_table (also covers indexes: they share a namespace)
    "42710",  # duplicate_object (constraints)
    "42701",  # duplicate_column
})


def _is_duplicate_object_error(exc) -> bool:
    """True only for 'already exists', never for any other database error."""
    cause = getattr(exc, "__cause__", None) or exc
    sqlstate = getattr(cause, "pgcode", None)
    if sqlstate in _DUPLICATE_SQLSTATES:
        return True
    # SQLite has no SQLSTATE; fall back to its message shape.
    return "already exists" in str(exc).lower()


def _is_additive_ddl(statement: str) -> bool:
    """True for statements that create something new.

    Restricting the guard to these keeps it from ever masking an UPDATE or an
    ALTER that changes an existing column's type -- those must still fail
    loudly.
    """
    head = statement.lstrip().upper()
    if head.startswith(("CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX")):
        return True
    if head.startswith("ALTER TABLE") and (
        " ADD CONSTRAINT " in head or " ADD COLUMN " in head
    ):
        return True
    return False


# Postgres SQLSTATEs for "the thing you asked me to remove is not there".
_MISSING_OBJECT_SQLSTATES = frozenset({
    "42703",  # undefined_column
    "42P01",  # undefined_table
    "42704",  # undefined_object (constraints, indexes)
})


def _is_missing_object_error(exc) -> bool:
    cause = getattr(exc, "__cause__", None) or exc
    if getattr(cause, "pgcode", None) in _MISSING_OBJECT_SQLSTATES:
        return True
    return "does not exist" in str(exc).lower()


def _is_subtractive_ddl(statement: str) -> bool:
    """True for statements that remove something.

    These need the mirror of the additive guard. v1's own releases made some
    of the same changes v2's migrations make: horilla_backup/0002 removes
    GoogleDriveBackup.service_account_file, but v1 1.5.0 already removed it,
    so on a 1.5+ source the column is gone before v2's migration runs. On a
    1.3.2/1.4.0 source it is still there and the removal must happen.

    Dropping something that is already gone is the intended end state either
    way, so this is safe -- unlike swallowing a failed ALTER ... TYPE, which
    would silently leave a column the wrong type.
    """
    head = statement.lstrip().upper()
    if head.startswith("DROP INDEX"):
        return True
    if head.startswith("ALTER TABLE") and (
        " DROP COLUMN " in head or " DROP CONSTRAINT " in head
    ):
        return True
    return False


def _reconcile_existing_table(editor, model):
    """Add what v1's copy of an adopted table does not already have.

    Adopting a table means skipping its CREATE TABLE, and that statement
    carries more than the table: every column, the indexes for those columns,
    and -- at the end of Django's own create_model -- the join table for each
    auto-created many-to-many field. Returning early skipped all of it, so any
    column v2 introduced on a table v1 already had was never created, while
    django_migrations recorded the migration as applied. Nothing failed until
    a query touched the column:

        ProgrammingError: column project_project.company_id_id does not exist

    reported from a real customer upgrade, on a dashboard page, days after a
    migration that reported success.

    Django's own add_field is used rather than hand-built DDL, so the column
    arrives with the constraints, foreign key and index Django would have
    given it.
    """
    table = model._meta.db_table
    with editor.connection.cursor() as cursor:
        existing = {
            column.name
            for column in editor.connection.introspection.get_table_description(
                cursor, table
            )
        }

    for field in model._meta.local_fields:
        column = getattr(field, "column", None)
        if not column or column in existing:
            continue
        logger.warning(
            "adopted table %s is missing column %s -- adding it", table, column
        )
        try:
            editor.add_field(model, field)
        except Exception as exc:
            # A NOT NULL column with no usable default cannot be added to a
            # table that already has rows, and guessing a value here would be
            # inventing customer data. Stop with something the operator can
            # act on rather than leaving a half-built table behind.
            raise RuntimeError(
                f"Cannot add the missing column {table}.{column} "
                f"({model._meta.label}.{field.name}): {exc}. "
                "It is NOT NULL with no default and the table already has "
                "rows, so a value has to be chosen deliberately. Add the "
                "column by hand with the default that is right for this "
                "data, then run the migration again."
            ) from exc

    # Join tables are separate relations and are created at the end of
    # Django's create_model, which the early return above skips. Recursing
    # through the patched create_model adopts one that already exists and
    # builds one that does not.
    for field in model._meta.local_many_to_many:
        through = field.remote_field.through
        if through._meta.auto_created:
            editor.create_model(through)


def install():
    """Patch BaseDatabaseSchemaEditor.create_model to skip existing tables.

    Called from horillasetup/migration_settings.py, which Django imports
    before it loads any migration, so the patch is in place for the whole
    `migrate` run. It used to be called from horilla/__init__.py -- that meant
    editing the HR codebase, which is precisely what this tool exists to
    avoid.

    Deliberately narrow: only `create_model` is touched, and only the
    already-exists case is changed. Anything else -- a genuinely new v2 table,
    any later AlterField -- behaves exactly as Django intends.
    """
    if not adoption_enabled():
        return False

    from django.db.backends.base.schema import BaseDatabaseSchemaEditor

    if getattr(BaseDatabaseSchemaEditor, "_horilla_adoption_installed", False):
        return True

    original_create_model = BaseDatabaseSchemaEditor.create_model

    def create_model(self, model):
        table = model._meta.db_table
        if table in self.connection.introspection.table_names():
            # The table came from v1. Django's in-memory state still advances,
            # because state is tracked separately from the physical schema --
            # which is the whole point.
            logger.info("adopting existing table %s", table)
            _reconcile_existing_table(self, model)
            return
        return original_create_model(self, model)

    BaseDatabaseSchemaEditor.create_model = create_model

    # Tables are not the only objects v1 already created: the 1.6.1 fixture
    # carries ~1,500 indexes and ~2,800 constraints, and CreateModel is not
    # the only operation that emits DDL (AddIndex, AddConstraint and
    # AlterUniqueTogether each do too).
    #
    # Guarding `execute` rather than each of those methods covers the paths
    # not enumerated here, and stays correct if Django adds more. The guard is
    # deliberately narrow: it swallows *only* a duplicate-object error, and
    # only for CREATE/ALTER-ADD statements. Every other failure still raises.
    original_execute = BaseDatabaseSchemaEditor.execute

    def execute(self, sql, params=()):
        statement = str(sql).strip()

        # Postgres aborts the whole transaction on a duplicate-object error --
        # verified: every subsequent statement then fails with
        # InFailedSqlTransaction. Catching the exception is therefore not
        # enough; the statement has to run inside a savepoint that can be
        # rolled back without losing the migration's other work.
        additive = _is_additive_ddl(statement)
        subtractive = _is_subtractive_ddl(statement)
        if not (additive or subtractive):
            return original_execute(self, sql, params)

        with self.connection.cursor() as cursor:
            cursor.execute("SAVEPOINT horilla_adopt")
        try:
            result = original_execute(self, sql, params)
        except Exception as exc:
            tolerated = (
                (additive and _is_duplicate_object_error(exc))
                or (subtractive and _is_missing_object_error(exc))
            )
            with self.connection.cursor() as cursor:
                cursor.execute("ROLLBACK TO SAVEPOINT horilla_adopt")
            if not tolerated:
                raise
            logger.info(
                "%s: %s",
                "adopting existing object" if additive else "already removed",
                statement[:120],
            )
            return None
        else:
            with self.connection.cursor() as cursor:
                cursor.execute("RELEASE SAVEPOINT horilla_adopt")
            return result

    BaseDatabaseSchemaEditor.execute = execute
    BaseDatabaseSchemaEditor._horilla_adoption_installed = True
    logger.warning(
        "%s is set: existing tables will be adopted rather than created. "
        "This is only correct when migrating a Horilla v1 database.",
        ENV_FLAG,
    )
    return True


def clear_auth_ordering_conflicts(connection) -> list:
    """Unapply every ledger row that must not precede horilla_auth.0001_initial.

    v2 swaps AUTH_USER_MODEL to horilla_auth.HorillaUser. That makes a large
    part of the graph depend on horilla_auth -- django.contrib.admin, auditlog,
    and anything else referencing settings.AUTH_USER_MODEL. A v1 ledger records
    those as applied already, so `migrate` refuses to start:

        InconsistentMigrationHistory: Migration admin.0001_initial is applied
        before its dependency horilla_auth.0001_initial

    An earlier version resolved this by INSERTING horilla_auth.0001_initial
    with a backdated timestamp, reasoning that auth_user already held exactly
    the rows that migration produces. True under the previous db_table design,
    where the migration had no physical work to do.

    False under the rename design, and dangerously so: marking it applied means
    the three table renames never run, so the tables keep v1's names while the
    recorded state claims otherwise, and every later migration fails against a
    schema that does not match. The migration has to actually execute.

    So the offending rows are REMOVED rather than one row added. They are then
    re-applied in the same run with schema adoption active, which makes their
    CreateModel operations idempotent against tables that already exist. No
    table is dropped and no row is deleted -- only ledger entries move.

    The set is computed from Django's own migration graph, including transitive
    dependents, rather than hardcoded. Hardcoding it was tried and is
    whack-a-mole: fixing admin.0001 surfaced admin.0002, and fixing all of
    admin surfaced auditlog. Deriving it also means a newly-installed
    third-party app needs no change here.

    Returns [(app, name), ...] removed, empty when there was nothing to do.
    """
    from django.db.migrations.loader import MigrationLoader

    with connection.cursor() as cursor:
        cursor.execute(
            "select count(*) from django_migrations "
            "where app = 'horilla_auth' and name = '0001_initial'"
        )
        if cursor.fetchone()[0]:
            # Already migrated, or a previous run got this far. Removing rows
            # now would re-run migrations whose dependency is truly satisfied.
            return []

    loader = MigrationLoader(connection, ignore_no_migrations=True)

    # Direct dependents: anything naming horilla_auth, plus anything naming the
    # swappable setting -- third-party apps use the latter form.
    blocked = {
        key
        for key, migration in loader.disk_migrations.items()
        for dep in migration.dependencies
        if dep[0] == "horilla_auth" or dep == ("__setting__", "AUTH_USER_MODEL")
    }

    # Transitive: a migration after a blocked one is equally unorderable.
    dependents = {}
    for key, migration in loader.disk_migrations.items():
        for dep in migration.dependencies:
            dependents.setdefault(dep, set()).add(key)

    stack = list(blocked)
    while stack:
        for child in dependents.get(stack.pop(), ()):
            if child not in blocked:
                blocked.add(child)
                stack.append(child)

    stale = sorted(blocked & set(loader.applied_migrations))
    if not stale:
        return []

    with connection.cursor() as cursor:
        for app, name in stale:
            cursor.execute(
                "delete from django_migrations where app = %s and name = %s",
                [app, name],
            )

    logger.info(
        "unapplied %d migration(s) that would precede horilla_auth.0001_initial "
        "(%s)",
        len(stale),
        ", ".join(sorted({app for app, _ in stale})),
    )
    return stale


def collides_with_v1_ledger(connection) -> list:
    """Ledger rows whose names match a shipped v2 migration file.

    v1 and v2 both auto-generated migrations with Django's default names, so a
    v1 database's ledger already contains `base.0001_initial`,
    `employee.0001_initial` and 24 others -- entirely different migrations that
    happen to share a name. Django sees the name as applied and skips v2's
    version, so any genuinely-new table inside one is never created while the
    ledger reports success. Measured on a real 1.6.1 database: 26 collisions,
    and `base_integrationapps` silently missing.

    Returns [(app, name), ...] found in both places.
    """
    from django.apps import apps as global_apps
    from django.db.migrations.loader import MigrationLoader

    # Only first-party apps. MigrationLoader.disk_migrations also includes
    # Django's own (contenttypes, auth, sessions, admin) and third-party ones,
    # whose v1 ledger rows are genuinely correct -- unapplying those would try
    # to recreate django_session and auth_permission, and auth in particular
    # is what keeps permission IDs stable across the migration.
    first_party = {
        config.label
        for config in global_apps.get_app_configs()
        if not str(config.path).replace("\\", "/").rstrip("/").endswith(
            ("site-packages/" + config.label, "dist-packages/" + config.label)
        )
        and "site-packages" not in str(config.path)
        and "dist-packages" not in str(config.path)
        and not config.name.startswith("django.")
    }

    shipped = {
        (app, name)
        for app, name in MigrationLoader(None, ignore_no_migrations=True).disk_migrations
        if app in first_party
    }
    with connection.cursor() as cursor:
        cursor.execute("select app, name from django_migrations")
        applied = set(cursor.fetchall())
    return sorted(applied & shipped)


def unapply_colliding_ledger_rows(connection) -> list:
    """Delete the colliding rows so v2's own migrations run.

    Safe only because `install()` makes DDL idempotent: the tables those v1
    migrations created still exist and are adopted rather than recreated. What
    is removed is a false claim that v2's same-named migration has run.

    This is the targeted replacement for the old tool's approach of deleting
    every row in django_migrations. Returns what was removed, so the caller
    can report it.
    """
    collisions = collides_with_v1_ledger(connection)
    if not collisions:
        return []
    with connection.cursor() as cursor:
        for app, name in collisions:
            cursor.execute(
                "delete from django_migrations where app = %s and name = %s",
                [app, name],
            )
    logger.info("unapplied %d colliding ledger rows", len(collisions))
    return collisions


def resync_sequences(connection) -> list:
    """Move every ``id`` sequence up to the highest id its table actually holds.

    Returns the sequences that were behind, as (table, sequence, was, now).

    A v1 database that has ever had rows inserted with explicit primary keys --
    a ``loaddata``, a data copy between environments, a restore that did not
    reset sequences -- carries a sequence sitting below ``max(id)``. Nothing
    notices until something inserts without naming an id.

    ``migrate`` does exactly that. Its ``post_migrate`` signal runs
    ``create_contenttypes`` and ``create_permissions``, which ``bulk_create``
    rows for every model v2 adds, taking ids from the sequence. When the
    sequence is behind, the insert lands on a row that already exists:

        psycopg2.errors.UniqueViolation: duplicate key value violates unique
        constraint "django_content_type_pkey"
        DETAIL:  Key (id)=(219) already exists.

    That aborted a real customer migration part-way through stage 5, with the
    schema half-changed. The condition predates the migration -- it is a latent
    fault in the source database -- but this is the first thing that inserts
    enough rows to hit it, so the migration gets the blame and the operator gets
    a restore.

    Repairing rather than refusing: ``setval`` to ``max(id)`` is the standard
    fix, it is idempotent, and it can only move a sequence forward to a value
    that was already correct. Refusing would hand the operator a manual SQL
    chore for a problem the tool can simply resolve.

    Every table is checked, not just ``django_content_type``. That one is what
    the customer hit because ``post_migrate`` inserts there first, but any table
    whose sequence is behind would fail the same way the moment v2 inserts into
    it, and there is no reason to find them one restore at a time.
    """
    repaired = []
    with connection.cursor() as cur:
        cur.execute(
            """
            select c.relname,
                   pg_get_serial_sequence(quote_ident(c.relname), a.attname)
            from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            join pg_attribute a on a.attrelid = c.oid
            where n.nspname = 'public'
              and c.relkind = 'r'
              and a.attname = 'id'
              and not a.attisdropped
              and pg_get_serial_sequence(quote_ident(c.relname), a.attname)
                  is not null
            """
        )
        candidates = cur.fetchall()

        for table, sequence in candidates:
            cur.execute(f'select max(id) from "{table}"')
            max_id = cur.fetchone()[0]
            if max_id is None:
                # Empty table: leave the sequence alone. Forcing it to 1 with
                # is_called=true would skip id 1 on the first insert.
                continue

            cur.execute("select last_value, is_called from %s" % sequence)
            last_value, is_called = cur.fetchone()
            current = last_value if is_called else last_value - 1
            if current >= max_id:
                continue

            cur.execute("select setval(%s, %s, true)", [sequence, max_id])
            repaired.append((table, sequence, current, max_id))
            logger.info(
                "sequence %s was at %s but %s holds ids up to %s -- reset",
                sequence, current, table, max_id,
            )

    logger.info("resynced %d sequences", len(repaired))
    return repaired
