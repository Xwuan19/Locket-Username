"""Supabase-backed persistence for accounts, tokens, recent log, and site settings.

Replaces SQLite with Supabase Postgres. All state now lives in Supabase cloud database.
The client is initialized once from environment variables and reused across requests.
"""

import os
from supabase import create_client, Client
from typing import Optional

_client: Optional[Client] = None


def get_client() -> Client:
    """Return the singleton Supabase client, creating it on first call."""
    global _client
    if _client is None:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_KEY")

        if not url or not key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_KEY environment variables are required. "
                "Please set them in your .env file or Vercel environment settings."
            )

        _client = create_client(url, key)
        print(f"db: Supabase client initialized for {url}")

    return _client


def init():
    """Initialize database connection. Idempotent - safe to call multiple times.

    Note: Schema must be created manually in Supabase SQL Editor using supabase_migration.sql
    This function only verifies the connection works.
    """
    try:
        client = get_client()
        # Simple health check - try to query site_settings
        client.table("site_settings").select("key").limit(1).execute()
        print("db: Supabase connection verified")
    except Exception as e:
        print(f"db: Warning - Supabase connection check failed: {e}")
        print("db: Make sure you've run supabase_migration.sql in your Supabase SQL Editor")
