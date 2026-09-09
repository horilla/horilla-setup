# 🛠️ Horilla Setup CLI (`horillasetup`)

[![migration tests](https://github.com/horilla/horilla-setup/actions/workflows/migration-tests.yml/badge.svg)](https://github.com/horilla/horilla-setup/actions/workflows/migration-tests.yml)

The **Horilla Setup CLI** is a lightweight, cross-platform command-line tool designed to streamline the **initialization**, **migration**, **upgrade**, and **dependency management** processes across the **Horilla ecosystem** — including **HRMS v1**, **HRMS v2**, and the newly released **Horilla CRM**.

It automates repetitive setup tasks like environment preparation, Git cloning, dependency installation, and migration handling — ensuring a smooth, consistent workflow for developers and deployment teams.

---

## 🚀 Key Features

✓ **Quick project setup** for HRMS (v1 & v2) and CRM
✓ **Version-aware migrations** including HRMS v1 → v2 upgrade
✓ **Automated dependency installation** from `requirements.txt`
✓ **Seamless project upgrades** via Git pull
✓ **Cross-platform support** (Windows, Linux, macOS)
✓ **Single command workflow** for setup, migration, and updates

---

## ⚙️ Installation

### 📦 Global Installation (Recommended)

```bash
pip install horillasetup
```

### 🧩 Local Development Installation

If you’re improving or modifying the tool:

```bash
git clone https://github.com/horilla-opensource/setup.git
cd horilla-ctl
pip install -e .
```

> `-e` installs the package in *editable mode*, so changes take effect instantly.

---

## 🧭 Usage Guide

Show all available commands:

```bash
horillasetup --help
```

---

# 🏗️ 1. Build a New Horilla Project

### HRMS v1

```bash
horillasetup build hrms-v1
```

### HRMS v2

```bash
horillasetup build hrms-v2
```

### CRM (Newly Released 🚀)

```bash
horillasetup build crm
```

**The build command will:**

* Clone the correct Horilla repo (branch-specific)
* Copy project files into the working directory
* Install Python dependencies
* Provide environment setup instructions

---

# 🧱 2. Run Migrations

### HRMS v1

```bash
horillasetup migrate hrms-v1
```

### HRMS v2

```bash
horillasetup migrate hrms-v2
```

### CRM

```bash
horillasetup migrate crm
```

**Migration steps include:**

* Running `makemigrations`
* Applying migrations
* Collecting static files

---

# 🔄 3. Upgrade an Existing Project

Pull latest code updates from Git:

### HRMS v1

```bash
horillasetup upgrade hrms-v1
```

### HRMS v2

```bash
horillasetup upgrade hrms-v2
```

### CRM

```bash
horillasetup upgrade crm
```

---

# 🔁 4. HRMS v1 → HRMS v2 Database Upgrade

Migrates an existing Horilla v1 database to v2 **in place**, keeping every
user, password and record.

```bash
cd /path/to/your/horilla-v2
horillasetup migrate hrms-v2 --from-v1
```

**Supported source versions: 1.3.2 through 1.6.1.** Anything else is refused
rather than attempted — across that whole range there are only two physical
schema shapes, and the tool detects which one it is looking at.

### What it does

Six stages, each protecting the next:

| Stage | What it does |
|---|---|
| 1. Fingerprint | Confirms this really is a supported v1 database |
| 2. Pre-flight | Finds data that would fail partway through, while it is still safe to stop |
| 3. Backup | `pg_dump` of the whole database, before anything is written |
| 4. Ledger | Reconciles `django_migrations` with v2's migrations |
| 5. Migrate | Applies v2's schema over v1's, adopting the tables that already exist, and carries across data v2 would otherwise drop |
| 6. Verify | Checks users, passwords, holidays and relationships actually survived |

Nothing before stage 3 writes to the database, so a refusal in stage 1 or 2
leaves your database exactly as it was.

Your users keep their existing passwords — the stored hashes are carried
across untouched, not reset.

Holidays and company leave rules are carried across too. v2 moves both from the
`leave` app to `base` without a data step of its own, so a plain `migrate`
destroys them while reporting success; the tool copies them in the window
between the new table being created and the old one being dropped.

### Options

```bash
--from-v1              Migrate an existing v1 database
--backup-dir PATH      Where to write the backup (default: ./horilla-migration-backups)
--skip-backup          Do not back up first. The migration then cannot be undone
-y, --yes              Do not prompt for confirmation
```

### If something goes wrong

Restore the backup from stage 3:

```bash
dropdb horilla && createdb horilla
pg_restore -d horilla --no-owner --no-privileges \
    horilla-migration-backups/horilla-v1-<timestamp>.dump
```

Running the command twice is safe: an already-migrated database is detected
and refused.

### `--existing` has been removed

The old `--existing` flag deleted the migration ledger and faked every
migration. That left the database recording itself as v2 while the schema was
still v1, so tables that v2 added were never created — with no backup and no
way to notice. It now exits with a pointer to `--from-v1` rather than running.

---

# 📦 5. Install Dependencies Only

```bash
horillasetup install-deps
```

Installs all packages from `requirements.txt`.

---

## 💡 Example Setup Workflow

```bash
# Build a fresh CRM project
horillasetup build crm

# Run CRM migrations
horillasetup migrate crm

# Upgrade project later
horillasetup upgrade crm
```

---

## 🧪 Development

The v1 → v2 migration has a test suite that runs against **real Postgres** and
**real v1 databases** built from real Horilla release tags. Nothing is mocked:
the thing under test is a schema migration, and an ORM-level fixture would
abstract away exactly what can break.

```bash
python -m venv .venv
.venv/bin/pip install pytest psycopg2-binary
.venv/bin/pip install -e .
.venv/bin/python -m pytest tests -q          # skips what it cannot reach
```

Tests skip cleanly when their inputs are absent, so that always runs. To
exercise the migration itself you need the fixtures and a v2 checkout:

```bash
# ~15 min the first time: each tag is a full clone and pip install
for tag in 1.3.2 1.4.0 1.5.0 1.6.0 1.6.1; do
    HORILLA_SEED_HR_DATA=1 tests/fixtures/build_v1.sh "$tag"
done

HORILLA_V2_ROOT=/path/to/horilla-hr \
HORILLA_V2_PYTHON=/path/to/horilla-hr/.venv/bin/python \
  .venv/bin/python -m pytest tests -q
```

See `tests/README.md` for the environment variables and what each module covers.

CI runs the same suite on every push, and weekly against Horilla v2's
`2.0`. The weekly run is the point: v2 moves independently of this tool, so
a green run today says nothing about tomorrow — most sharply for the migration
ordering that the holiday data copy depends on, which would break silently.

---

## 📤 Releasing

Publishing is driven by a git tag, and the tag must match `setup.py`:

```bash
# 1. bump the version in setup.py and add a CHANGELOG entry
# 2. commit, then tag with a leading v
git tag v1.2.0 && git push origin v1.2.0
```

The workflow refuses to publish if the tag and `setup.py` disagree, or if that
version already exists on PyPI. `workflow_dispatch` builds and verifies the same
way without publishing, so the path can be rehearsed.

This is deliberate. Versions 1.0.2 – 1.0.4 were published from a working copy
while `setup.py` in git still said `1.0.1`, so for seven months you could not
tell what was in a release by reading the repository. The tag check makes the
two structurally unable to drift again.

Publishing uses [Trusted Publishing](https://docs.pypi.org/trusted-publishers/)
where configured — no long-lived token in repository secrets — and falls back to
a `PYPI_API_TOKEN` secret otherwise.

---

## 🛣️ Future Roadmap

* 🔌 Plugin-based scaffolding for new Horilla modules
* 🔍 Automated version & dependency conflict detection
* 📦 Project template generator
* 🧰 Extended DevOps tools integration