"""Compare expected opportunity_processing stage columns against hosted Supabase.

Same idea as check_schema.py for Airtable. Never prints credentials.
Run after applying supabase_migration_opportunity_stages.sql.
"""
from database.supabase_client import (
    REQUIRED_STAGE_COLUMNS,
    check_opportunity_stage_schema,
    reset_opportunity_ledger_status,
)


def main() -> int:
    reset_opportunity_ledger_status()
    print("=" * 60)
    print("  opportunity_processing stage columns")
    print("=" * 60)
    missing = check_opportunity_stage_schema()
    if not missing:
        print(f"  All {len(REQUIRED_STAGE_COLUMNS)} expected columns exist:")
        for col in REQUIRED_STAGE_COLUMNS:
            print(f"     - {col}")
        return 0
    print(f"  MISSING ({len(missing)}):")
    for col in missing:
        print(f"     - {col}")
    print("  Apply supabase_migration_opportunity_stages.sql and rerun.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
