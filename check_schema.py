"""Confirm the Supabase tables that replaced Airtable are present.

Does not call api.airtable.com and does not print credentials.
"""
from database.supabase_client import get_supabase

TABLES = ("consultants", "rate_cards", "agent_logs", "opportunity_processing")


def main() -> int:
    client = get_supabase()
    missing = []
    for name in TABLES:
        try:
            client.table(name).select("*").limit(1).execute()
            print(f"OK {name}")
        except Exception as exc:
            missing.append(name)
            print(f"MISSING {name}: {type(exc).__name__}")
    if missing:
        print("Apply supabase_migration_leave_airtable.sql")
        return 1
    print("Supabase CRM tables are present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
