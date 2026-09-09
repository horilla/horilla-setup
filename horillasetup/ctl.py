"""
Horilla command-line setup tool for managing project setup, dependencies, migrations, and upgrades
across multiple Horilla products (e.g., HRMS, CRM, and future modules).
"""

import platform
import subprocess
import sys
from pathlib import Path
import shutil
import argparse

from horillasetup import migrate_v1

# -------------------------------------------------------------------------
# Horilla repositories configuration
# -------------------------------------------------------------------------
PYTHON_CMD = "python" if platform.system() == "Windows" else "python3"
HORILLA_REPOS = {
    "hrms-v1": {
        "url": "https://github.com/horilla-opensource/horilla.git",
        "branch": "1.0",
    },
    "hrms-v2": {
        "url": "https://github.com/horilla-opensource/horilla.git",
        # Stable, not dev/v2.0. This branch is what a released version is cut
        # from; dev/v2.0 is where work lands first and can carry migrations and
        # schema changes that no release has shipped yet. Installing from it
        # gave customers a checkout ahead of every published version, which is
        # not something a setup tool should hand anyone by default.
        "branch": "2.0",
    },
    "crm": {
        "url": "https://github.com/horilla-opensource/horilla-crm",
        "branch": "master",
    },
}


# -------------------------------------------------------------------------
# INSTALLATION HELPERS
# -------------------------------------------------------------------------
def install_packages():
    """Install Python dependencies from requirements.txt."""
    current_dir = Path.cwd()
    requirements_file = current_dir / "requirements.txt"

    if not requirements_file.exists():
        print("⚠️  requirements.txt not found. Skipping dependency installation.\n")
        sys.exit(1)

    print("📦 Installing Python dependencies...\n")
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(requirements_file)],
            check=True,
        )
        print("✅ Dependencies installed successfully.\n")
    except subprocess.CalledProcessError:
        print("❌ Failed to install dependencies.\n")
        sys.exit(1)


# -------------------------------------------------------------------------
# MIGRATIONS
# -------------------------------------------------------------------------
def apply_migrations_hrms(existing=False, from_v1=False, backup_dir=None,
                          skip_backup=False, assume_yes=False):
    """Apply Django migrations for HRMS v2, or migrate an existing v1 database."""
    current_dir = Path.cwd()
    manage_py = current_dir / "manage.py"

    if not manage_py.exists():
        print("⚠️  manage.py not found. Make sure you are in a Horilla project directory.\n")
        sys.exit(1)

    if existing:
        # This flow deleted every row in django_migrations and then faked the
        # whole graph, which left the ledger claiming v2 while the schema was
        # still v1 -- new tables were never created, and there was no backup.
        # It cannot be made safe, so it is refused rather than quietly mapped
        # onto the new path: anyone with it in a script must see why.
        print(
            "\n❌ --existing has been removed. It deleted the migration ledger\n"
            "   and faked every migration, which left the database claiming to\n"
            "   be v2 while the schema was still v1.\n\n"
            "   Use this instead:\n\n"
            "       horillasetup migrate hrms-v2 --from-v1\n\n"
            f"   It migrates a v1 database ({migrate_v1.SUPPORTED_RANGE}) in place,\n"
            "   taking a backup first and verifying the data afterwards.\n"
        )
        sys.exit(2)

    if from_v1:
        sys.exit(migrate_v1.run(
            current_dir,
            backup_dir=Path(backup_dir) if backup_dir else None,
            skip_backup=skip_backup,
            assume_yes=assume_yes,
        ))

    print("🔍 Checking Horilla HRMS database setup...\n")
    print("🆕 Running standard migrations...\n")
    commands = [
        f"{PYTHON_CMD} manage.py makemigrations",
        f"{PYTHON_CMD} manage.py migrate",
        f"{PYTHON_CMD} manage.py collectstatic --noinput",
    ]

    for cmd in commands:
        print(f"▶️  {cmd}")
        try:
            subprocess.run(cmd, shell=True, check=True)
        except subprocess.CalledProcessError:
            print(f"❌ Failed while executing: {cmd}\n")
            sys.exit(1)

    print("\n✅ Migration process completed successfully!\n")


def apply_migrations_hrms_v1():
    """Standard migration flow for HRMS v1."""
    current_dir = Path.cwd()
    manage_py = current_dir / "manage.py"

    if not manage_py.exists():
        print("⚠️  manage.py not found. Make sure you are in a Horilla v1 project directory.\n")
        sys.exit(1)

    print("🆕 Running standard HRMS v1 migrations...\n")
    commands = [
        f"{PYTHON_CMD} manage.py makemigrations",
        f"{PYTHON_CMD} manage.py migrate",
        f"{PYTHON_CMD} manage.py collectstatic --noinput",
    ]

    for cmd in commands:
        print(f"▶️  {cmd}")
        try:
            subprocess.run(cmd, shell=True, check=True)
        except subprocess.CalledProcessError:
            print(f"❌ Failed while executing: {cmd}\n")
            sys.exit(1)

    print("\n✅ HRMS v1 migration completed successfully!\n")


def apply_migrations_crm():
    """Apply migrations for CRM project."""
    current_dir = Path.cwd()
    manage_py = current_dir / "manage.py"

    if not manage_py.exists():
        print("⚠️  manage.py not found. Make sure you are in a Horilla CRM project directory.\n")
        sys.exit(1)

    print("🆕 Running CRM migrations...\n")
    commands = [
        f"{PYTHON_CMD} manage.py makemigrations",
        f"{PYTHON_CMD} manage.py migrate",
        f"{PYTHON_CMD} manage.py collectstatic --noinput",
    ]

    for cmd in commands:
        print(f"▶️  {cmd}")
        try:
            subprocess.run(cmd, shell=True, check=True)
        except subprocess.CalledProcessError:
            print(f"❌ Failed while executing: {cmd}\n")
            sys.exit(1)

    print("\n✅ CRM migration completed successfully!\n")


# -------------------------------------------------------------------------
# BUILD PROJECTS
# -------------------------------------------------------------------------
def build_project(version_key):
    """Clone and initialize a Horilla HRMS/CRM project."""
    current_dir = Path.cwd()
    git_dir = current_dir / ".git"
    repo_info = HORILLA_REPOS[version_key]

    print(f"🚀 Starting Horilla {version_key.upper()} project setup...\n")

    if git_dir.exists():
        print("⚠️  A Git repository already exists here.")
        print(f"👉  Use 'horillasetup upgrade {version_key}' instead.\n")
        sys.exit(1)

    allowed_names = {"venv", "horillavenv", ".venv"}
    other_items = [item for item in current_dir.iterdir() if item.name not in allowed_names]
    if other_items:
        print("⚠️  Directory is not empty. Please use an empty folder or one containing only a venv.\n")
        sys.exit(1)

    tmp_dir = current_dir / ".horilla_tmp"
    print(f"📦 Step 1: Cloning {version_key} into '{tmp_dir.name}'...")

    try:
        subprocess.run(
            ["git", "clone", "-b", repo_info["branch"], repo_info["url"], str(tmp_dir)],
            check=True,
        )
        print("✅ Repository successfully cloned.\n")
    except subprocess.CalledProcessError:
        print("❌ Git clone failed. Make sure Git is installed and accessible.\n")
        sys.exit(1)

    print("📁 Step 2: Moving project files...")
    for item in tmp_dir.iterdir():
        target = current_dir / item.name
        if not target.exists():
            shutil.move(str(item), str(target))
    tmp_dir.rmdir()
    print("✅ Files moved successfully.\n")

    install_packages()

    print("⚙️  Environment setup note:")
    print("   - For production: rename `.env.dist` to `.env` and update DB + host settings manually.")
    print("   - For development (SQLite): you can continue directly.\n")

    print(f"🎉 Horilla {version_key.upper()} setup complete!")
    print("👉 Next steps:")
    print(f"   1️⃣ Configure your `.env` file.")
    print(f"   2️⃣ Run migrations using: horillasetup migrate {version_key}")
    if version_key == "hrms-v2":
        print(f"       or migrate from v1 using: horillasetup migrate hrms-v2 --existing")
    print(f"   3️⃣ Start server with: {PYTHON_CMD} manage.py runserver\n")


# -------------------------------------------------------------------------
# UPGRADE PROJECT
# -------------------------------------------------------------------------
def upgrade_project(version_key):
    """Pull latest changes for a given Horilla project."""
    current_dir = Path.cwd()
    git_dir = current_dir / ".git"

    if not git_dir.exists():
        print(f"⚠️  No existing {version_key} project found.")
        print(f"👉  Use 'horillasetup build {version_key}' first.")
        sys.exit(1)

    target_branch = HORILLA_REPOS.get(version_key, {}).get("branch")
    if target_branch:
        # A bare `git pull` follows whatever branch the checkout already tracks.
        # Installs built before this tool switched to the stable branch are on
        # dev/v2.0, and would keep pulling the development branch indefinitely --
        # the checkout silently stays ahead of every released version. Move it
        # once, and only when there is nothing local to lose.
        current = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        if current and current != target_branch:
            dirty = subprocess.run(
                ["git", "status", "--porcelain"], capture_output=True, text=True
            ).stdout.strip()
            if dirty:
                print(
                    f"⚠️  This checkout is on '{current}', but {version_key} now "
                    f"tracks '{target_branch}'."
                )
                print("    There are uncommitted changes, so it was left alone.")
                print(f"    Commit or stash them, then: git checkout {target_branch}")
                sys.exit(1)
            print(f"🔀 Switching from '{current}' to '{target_branch}'...")
            try:
                subprocess.run(["git", "fetch", "origin", target_branch], check=True)
                subprocess.run(["git", "checkout", target_branch], check=True)
            except subprocess.CalledProcessError:
                print(f"❌ Could not switch to '{target_branch}'.")
                sys.exit(1)

    print(f"🔄 Pulling latest updates from {version_key} branch...")
    try:
        subprocess.run(["git", "pull"], check=True)
        print(f"\n✅ {version_key.upper()} successfully updated to the latest version!")
    except subprocess.CalledProcessError:
        print("❌ Git pull failed. Make sure your repo is clean and accessible.")
        sys.exit(1)


# -------------------------------------------------------------------------
# MAIN CLI HANDLER
# -------------------------------------------------------------------------
def main():
    """CLI entry point for horillasetup."""
    parser = argparse.ArgumentParser(
        description="Horilla setup tool for managing project setup, migrations, upgrades, and dependencies."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # -----------------------------
    # BUILD COMMAND
    # -----------------------------
    build_parser = subparsers.add_parser("build", help="Build project (HRMS v1/v2/CRM)")
    build_subparsers = build_parser.add_subparsers(dest="subcommand", required=True)
    build_subparsers.add_parser("hrms-v1", help="Clone and set up a new Horilla HRMS v1 project")
    build_subparsers.add_parser("hrms-v2", help="Clone and set up a new Horilla HRMS v2 project")
    build_subparsers.add_parser("crm", help="Clone and set up a new Horilla CRM project")

    # -----------------------------
    # MIGRATE COMMAND
    # -----------------------------
    migrate_parser = subparsers.add_parser("migrate", help="Apply migrations")
    migrate_subparsers = migrate_parser.add_subparsers(dest="subcommand", required=True)
    migrate_subparsers.add_parser("hrms-v1", help="Run HRMS v1 migrations")
    migrate_hrms_parser = migrate_subparsers.add_parser("hrms-v2", help="Run HRMS v2 migration flow")
    migrate_hrms_parser.add_argument(
        "--existing", action="store_true",
        help=argparse.SUPPRESS,  # removed; kept so it errors rather than "unrecognized"
    )
    migrate_hrms_parser.add_argument(
        "--from-v1", action="store_true",
        help=f"Migrate an existing Horilla v1 database ({migrate_v1.SUPPORTED_RANGE}) in place",
    )
    migrate_hrms_parser.add_argument(
        "--backup-dir", metavar="PATH",
        help="Where to write the pre-migration backup (default: ./horilla-migration-backups)",
    )
    migrate_hrms_parser.add_argument(
        "--skip-backup", action="store_true",
        help="Do not back up first. The migration then cannot be undone",
    )
    migrate_hrms_parser.add_argument(
        "-y", "--yes", action="store_true",
        help="Do not prompt for confirmation",
    )
    migrate_subparsers.add_parser("crm", help="Run CRM migrations")

    # -----------------------------
    # UPGRADE COMMAND
    # -----------------------------
    upgrade_parser = subparsers.add_parser("upgrade", help="Upgrade existing HRMS/CRM project")
    upgrade_subparsers = upgrade_parser.add_subparsers(dest="subcommand", required=True)
    upgrade_subparsers.add_parser("hrms-v1", help="Pull latest changes from HRMS v1 branch")
    upgrade_subparsers.add_parser("hrms-v2", help="Pull latest changes from HRMS v2 branch")
    upgrade_subparsers.add_parser("crm", help="Pull latest changes from CRM branch")

    # -----------------------------
    # INSTALL DEPS COMMAND
    # -----------------------------
    subparsers.add_parser("install-deps", help="Install dependencies from requirements.txt")

    # -----------------------------
    # COMMAND HANDLER
    # -----------------------------
    args = parser.parse_args()

    if args.command == "build":
        if args.subcommand in HORILLA_REPOS:
            build_project(args.subcommand)

    elif args.command == "migrate":
        if args.subcommand == "hrms-v2":
            apply_migrations_hrms(
                existing=args.existing,
                from_v1=args.from_v1,
                backup_dir=args.backup_dir,
                skip_backup=args.skip_backup,
                assume_yes=args.yes,
            )
        elif args.subcommand == "hrms-v1":
            apply_migrations_hrms_v1()
        elif args.subcommand == "crm":
            apply_migrations_crm()

    elif args.command == "upgrade":
        if args.subcommand in HORILLA_REPOS:
            upgrade_project(args.subcommand)

    elif args.command == "install-deps":
        install_packages()


if __name__ == "__main__":
    main()
