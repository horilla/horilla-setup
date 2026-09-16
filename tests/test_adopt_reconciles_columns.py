"""Adopting a v1 table must not silently skip what that table lacks.

Adoption exists so v2's `0001_initial` can run over a database that already
has the tables. It did that by returning before Django's `create_model` ran --
but that statement carries more than the table. It carries every column, the
indexes for those columns, and the join table for each auto-created
many-to-many field.

So a column v2 introduced on a table v1 already had was never created, while
django_migrations recorded the migration as applied. The database looked
migrated. It failed later, on a page:

    ProgrammingError: column project_project.company_id_id does not exist

reported by a customer days after a migration that said it succeeded, on the
dashboard, and only for non-admin users -- whoever hit the query first.

These pin the reconciliation that now runs when a table is adopted.
"""

import pytest

from horillasetup.migration.adopt import _reconcile_existing_table


class FakeField:
    def __init__(self, name, column):
        self.name = name
        self.column = column


class FakeThrough:
    def __init__(self, table, auto_created=True):
        self._meta = FakeMeta(table, auto_created=auto_created)


class FakeM2M:
    def __init__(self, through):
        self.remote_field = type("R", (), {"through": through})()


class FakeMeta:
    def __init__(self, table, fields=(), m2m=(), auto_created=False, label=None):
        self.db_table = table
        self.local_fields = list(fields)
        self.local_many_to_many = list(m2m)
        self.auto_created = auto_created
        self.label = label or f"app.{table}"


class FakeModel:
    def __init__(self, table, fields=(), m2m=()):
        self._meta = FakeMeta(table, fields, m2m)


class FakeEditor:
    """Records what the reconciler asks Django to do."""

    def __init__(self, columns_by_table, add_field_error=None):
        self.columns_by_table = columns_by_table
        self.add_field_error = add_field_error
        self.added = []
        self.created_models = []
        editor = self

        class Introspection:
            def get_table_description(self, cursor, table):
                return [
                    type("Col", (), {"name": name})()
                    for name in editor.columns_by_table.get(table, [])
                ]

        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *a): return False

        class Connection:
            introspection = Introspection()
            def cursor(self): return Cursor()

        self.connection = Connection()

    def add_field(self, model, field):
        if self.add_field_error:
            raise self.add_field_error
        self.added.append((model._meta.db_table, field.column))

    def create_model(self, model):
        self.created_models.append(model._meta.db_table)


def test_missing_column_is_added():
    """The reported failure: v2 adds company_id to a table v1 already had."""
    model = FakeModel(
        "project_project",
        fields=[FakeField("id", "id"), FakeField("title", "title"),
                FakeField("company_id", "company_id_id")],
    )
    editor = FakeEditor({"project_project": ["id", "title"]})

    _reconcile_existing_table(editor, model)

    assert editor.added == [("project_project", "company_id_id")]


def test_columns_already_present_are_left_alone():
    model = FakeModel(
        "employee_employee",
        fields=[FakeField("id", "id"), FakeField("email", "email")],
    )
    editor = FakeEditor({"employee_employee": ["id", "email"]})

    _reconcile_existing_table(editor, model)

    assert editor.added == []


def test_auto_created_m2m_join_tables_are_still_built():
    """The other half of the early return.

    create_model builds a join table for every auto-created m2m at the end of
    its body, so returning early skipped those too -- a missing table, not
    just a missing column.
    """
    through = FakeThrough("project_project_members")
    model = FakeModel(
        "project_project",
        fields=[FakeField("id", "id")],
        m2m=[FakeM2M(through)],
    )
    editor = FakeEditor({"project_project": ["id"]})

    _reconcile_existing_table(editor, model)

    assert editor.created_models == ["project_project_members"]


def test_explicit_through_models_are_not_touched():
    """A through model the project declares itself is created by its own
    CreateModel operation, not as part of this table's."""
    through = FakeThrough("project_membership", auto_created=False)
    model = FakeModel(
        "project_project",
        fields=[FakeField("id", "id")],
        m2m=[FakeM2M(through)],
    )
    editor = FakeEditor({"project_project": ["id"]})

    _reconcile_existing_table(editor, model)

    assert editor.created_models == []


def test_a_column_that_cannot_be_added_stops_with_an_actionable_message():
    """NOT NULL with no default on a populated table needs a human.

    Guessing a value would be inventing customer data, and carrying on would
    leave exactly the half-built table this whole change exists to prevent.
    """
    model = FakeModel(
        "attendance_attendance",
        fields=[FakeField("id", "id"), FakeField("shift_id", "shift_id_id")],
    )
    editor = FakeEditor(
        {"attendance_attendance": ["id"]},
        add_field_error=Exception('null value in column "shift_id_id"'),
    )

    with pytest.raises(RuntimeError) as excinfo:
        _reconcile_existing_table(editor, model)

    message = str(excinfo.value)
    assert "attendance_attendance.shift_id_id" in message
    assert "by hand" in message
