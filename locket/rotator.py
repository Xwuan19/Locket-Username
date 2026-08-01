"""Pool of Locket accounts keyed by stable slot_id (uuid).

Persistence lives in Supabase (`accounts` table). The rotator caches Auth +
LocketAPI instances in memory keyed by slot_id — those are runtime-only and
must be rebuilt after a restart.

Single-operator sync flow calls `get(slot_id)` to obtain a LocketAPI instance.
`refresh(slot_id)` re-runs login after a 401. Mutators (`add`, `remove`) write
to Supabase and update the in-memory cache atomically under `self._lock`.

Falls back to a single account derived from EMAIL/PASSWORD env vars when the
database is empty.
"""

import os
import threading
import time
import uuid
import random

from . import db
from .locket_auth import Auth
from .locket_api import LocketAPI


class _Slot:
    __slots__ = ("email", "password", "auth", "api", "token_at")

    def __init__(self, email, password):
        self.email = email
        self.password = password
        self.auth = Auth(email, password)
        self.api = None
        self.token_at = 0.0  # epoch when current token was minted


class AccountRotator:
    def __init__(self):
        db.init()
        self._lock = threading.Lock()
        self._slots = {}  # slot_id -> _Slot
        self._order = []  # slot_id list, sorted by added_at
        self._round_robin_idx = 0  # For round-robin slot selection

        # Load accounts from Supabase
        client = db.get_client()
        response = client.table("accounts").select("slot_id, email, password").order("added_at").execute()
        rows = response.data if response.data else []

        if not rows:
            self._seed_from_env()
            response = client.table("accounts").select("slot_id, email, password").order("added_at").execute()
            rows = response.data if response.data else []

        for r in rows:
            self._slots[r["slot_id"]] = _Slot(r["email"], r["password"])
            self._order.append(r["slot_id"])

        if not self._slots:
            print(
                "AccountRotator: 0 accounts configured. App will start but unlock "
                "endpoints will return 503 until an account is added via /admin."
            )
            return

        # Eagerly initialize the first slot so startup fails loudly on bad creds.
        try:
            self._init_slot_locked(self._order[0])
        except Exception as e:
            print(f"AccountRotator: warning, first slot init failed: {e}")

        print(f"AccountRotator: loaded {len(self._slots)} account(s)")

    def ensure_fresh(self, slot_id):
        """Return the slot's LocketAPI, refreshing the token first if it's
        older than TOKEN_TTL_SEC. Used by sync endpoints right before they
        hit getUserByUsername so the call always carries a fresh token."""
        with self._lock:
            if slot_id not in self._slots:
                raise KeyError(f"Unknown slot_id: {slot_id}")
            slot = self._slots[slot_id]
            stale = (time.time() - slot.token_at) >= self.TOKEN_TTL_SEC or slot.api is None
        if stale:
            print(f"AccountRotator: ensure_fresh refreshing {slot_id[:8]}")
            api = self.refresh(slot_id)
            if api is not None:
                return api
        with self._lock:
            return self._slots[slot_id].api

    def next_slot_round_robin(self):
        """Return the next slot_id in round-robin order for load distribution."""
        with self._lock:
            if not self._order:
                return None
            slot_id = self._order[self._round_robin_idx % len(self._order)]
            self._round_robin_idx += 1
            return slot_id

    def random_slot(self):
        """Return a random slot_id."""
        with self._lock:
            if not self._order:
                return None
            return random.choice(self._order)

    def _seed_from_env(self):
        email = os.getenv("EMAIL")
        password = os.getenv("PASSWORD")
        if not email or not password:
            return
        slot_id = str(uuid.uuid4())
        client = db.get_client()
        client.table("accounts").insert({
            "slot_id": slot_id,
            "email": email,
            "password": password,
            "added_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }).execute()
        print(f"AccountRotator: seeded one account from EMAIL env var")

    # Firebase id tokens technically last ~1 hour, but Locket's edge starts
    # 502-ing on tokens that are even slightly stale during their incidents.
    # Keep tokens fresh: anything older than 5 min is refreshed on-demand.
    TOKEN_TTL_SEC = 5 * 60

    def _init_slot_locked(self, slot_id):
        """Caller must hold self._lock."""
        slot = self._slots[slot_id]
        token = slot.auth.get_token()
        slot.api = LocketAPI(token)
        slot.token_at = time.time()

    # --- Read API ---

    def size(self):
        with self._lock:
            return len(self._slots)

    def list_ids(self):
        with self._lock:
            return list(self._order)

    def list_accounts(self):
        """Admin view: [{id, email}] in insertion order. Password never exposed."""
        with self._lock:
            return [{"id": sid, "email": self._slots[sid].email} for sid in self._order]

    def has(self, slot_id):
        with self._lock:
            return slot_id in self._slots

    def email(self, slot_id):
        with self._lock:
            return self._slots[slot_id].email

    def get(self, slot_id):
        """Return the LocketAPI bound to one slot, lazy-initializing on first use."""
        with self._lock:
            if slot_id not in self._slots:
                raise KeyError(f"Unknown slot_id: {slot_id}")
            slot = self._slots[slot_id]
            if slot.api is None:
                self._init_slot_locked(slot_id)
            return slot.api

    # --- Mutators ---

    def add(self, email, password):
        """Append a new account, persist, return the new slot_id."""
        slot_id = str(uuid.uuid4())
        client = db.get_client()
        with self._lock:
            client.table("accounts").insert({
                "slot_id": slot_id,
                "email": email,
                "password": password,
                "added_at": time.strftime("%Y-%m-%d %H:%M:%S")
            }).execute()
            self._slots[slot_id] = _Slot(email, password)
            self._order.append(slot_id)
        print(f"AccountRotator: added slot {slot_id} ({email})")
        return slot_id

    def remove(self, slot_id):
        """Remove an account, persist. Returns True if removed."""
        with self._lock:
            if slot_id not in self._slots:
                return False
            email = self._slots[slot_id].email
            client = db.get_client()
            client.table("accounts").delete().eq("slot_id", slot_id).execute()
            del self._slots[slot_id]
            self._order.remove(slot_id)
        print(f"AccountRotator: removed slot {slot_id} ({email})")
        return True

    def refresh(self, slot_id):
        """Force a fresh login for one slot (after a 401)."""
        with self._lock:
            if slot_id not in self._slots:
                return None
            slot = self._slots[slot_id]
            print(f"AccountRotator: refreshing token for slot {slot_id} ({slot.email})")
            try:
                new_token = slot.auth.create_token()
                slot.api = LocketAPI(new_token)
                slot.token_at = time.time()
                return slot.api
            except Exception as e:
                print(f"AccountRotator: refresh failed for slot {slot_id}: {e}")
                return None

    @staticmethod
    def test_login(email, password):
        """Validate creds without touching the pool. Returns (ok, error)."""
        try:
            Auth(email, password).create_token()
            return True, None
        except Exception as e:
            return False, str(e)
