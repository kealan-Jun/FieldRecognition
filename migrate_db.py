#!/usr/bin/env python3
"""Apply database migrations."""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from database import Database, apply_migrations, get_migration_status


def main():
    """Main migration script."""
    import argparse

    parser = argparse.ArgumentParser(description='Database migration tool')
    parser.add_argument('--db', default='Data/Demo.sqlite3',
                       help='Database path (default: Data/Demo.sqlite3)')
    parser.add_argument('--dry-run', action='store_true',
                       help='Show pending migrations without applying')
    parser.add_argument('--status', action='store_true',
                       help='Show migration status only')
    args = parser.parse_args()

    db = Database(args.db)

    if args.status or args.dry_run:
        status = get_migration_status(db)
        print(f"Database: {args.db}")
        print(f"Current version: {status['current_version']}")
        print(f"Latest version: {status['latest_version']}")
        print(f"Applied: {status['applied_count']}/{status['total_count']}")

        if status['pending']:
            print(f"\nPending migrations ({len(status['pending'])}):")
            for migration in status['pending']:
                print(f"  v{migration['version']}: {migration['name']}")
                if migration.get('description'):
                    print(f"    {migration['description']}")
        else:
            print("\n✓ All migrations applied")

        if args.status:
            return 0

    if args.dry_run:
        print("\nDry run mode - no changes made")
        return 0

    # Apply migrations
    print(f"\nApplying migrations to {args.db}...")
    try:
        applied = apply_migrations(db)

        if not applied:
            print("✓ No pending migrations")
            return 0

        print(f"\n✓ Applied {len(applied)} migrations:")
        for migration in applied:
            if migration['status'] == 'applied':
                duration = migration.get('duration_ms', 0)
                print(f"  ✓ v{migration['version']}: {migration['name']} ({duration}ms)")
            elif migration['status'] == 'failed':
                print(f"  ✗ v{migration['version']}: {migration['name']}")
                print(f"    Error: {migration['error']}")
                return 1

        print("\n✓ All migrations completed successfully")
        return 0

    except Exception as e:
        print(f"\n✗ Migration failed: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
