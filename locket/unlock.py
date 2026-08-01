"""Synchronous unlock flow for single-operator use.

Extracted from the old queue_manager._process_request logic.
Handles the complete unlock flow: getUserByUsername -> restorePurchase -> validate -> notify.
Includes retry logic for 401/5xx errors with exponential backoff.
"""

import time
import random
from typing import Dict, Any, Optional
from . import db
from .notifications import send_telegram_notification

# RevenueCat product identifiers that count as successful Gold unlock
SUBSCRIPTION_IDS = [
    "locket_1600_1y",
    "locket_199_1m",
    "locket_199_1m_only",
    "locket_3600_1y",
    "locket_399_1m_only"
]

# Retry backoff delays (in seconds) for transient errors
_BACKOFF_DELAYS = (0.3, 0.7, 1.2, 2.0, 3.0, 5.0, 8.0)

# Substrings that identify transient errors worth retrying
_TRANSIENT_MARKERS = [
    "status code 500", "status code 502", "status code 503", "status code 504",
    "Internal Server Error", "Bad Gateway", "Service Unavailable", "Gateway Timeout",
    "ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout",
    "RemoteDisconnected", "ProtocolError"
]


def _is_transient(e: Exception) -> bool:
    """Check if an exception looks like a transient network/server error."""
    msg = str(e).lower()
    return any(marker.lower() in msg for marker in _TRANSIENT_MARKERS)


def unlock_user(username: str, api, rotator, slot_id: str) -> Dict[str, Any]:
    """
    Perform the complete unlock flow for a username using the given account slot.

    Args:
        username: Locket username to unlock
        api: LocketAPI instance (from rotator.get(slot_id))
        rotator: AccountRotator instance (for refresh on 401)
        slot_id: Account slot ID to use

    Returns:
        {
            "success": bool,
            "message": str,
            "duration": float,
            "uid": str (optional, on success),
            "product_id": str (optional, on success)
        }
    """
    start_time = time.time()

    try:
        # Step 1: Get user info with retry logic
        account_info = _call_with_retry(api, "getUserByUsername", username, rotator, slot_id)

        if not account_info or "result" not in account_info:
            raise Exception("User not found or API error")

        user_data = account_info.get("result", {}).get("data")
        if not user_data:
            raise Exception("User data not found")

        uid_target = user_data.get("uid")
        if not uid_target:
            raise Exception("UID not found for user")

        # Step 2: Restore purchase with retry logic
        restore_result = _call_with_retry(api, "restorePurchase", uid_target, rotator, slot_id)

        # Step 3: Validate Gold entitlement
        entitlements = restore_result.get("subscriber", {}).get("entitlements", {})
        gold_entitlement = entitlements.get("Gold", {})
        product_id = gold_entitlement.get("product_identifier")

        if product_id not in SUBSCRIPTION_IDS:
            raise Exception(f"Restore purchase failed. Gold entitlement not found for {username}.")

        # Step 4: Success! Send notification and log
        duration = time.time() - start_time

        # Send Telegram notification
        try:
            send_telegram_notification(username, uid_target, product_id, restore_result)
        except Exception as notif_err:
            print(f"unlock: Telegram notification failed: {notif_err}")

        # Insert into recent_log
        try:
            _insert_recent_log(username, "completed", None, duration)
        except Exception as log_err:
            print(f"unlock: Failed to insert recent_log: {log_err}")

        return {
            "success": True,
            "message": f"Purchase {product_id} for {username} successfully!",
            "duration": round(duration, 2),
            "uid": uid_target,
            "product_id": product_id
        }

    except Exception as e:
        duration = time.time() - start_time
        error_msg = str(e)

        # Log error
        try:
            _insert_recent_log(username, "error", error_msg, duration)
        except Exception as log_err:
            print(f"unlock: Failed to insert error log: {log_err}")

        return {
            "success": False,
            "message": error_msg,
            "duration": round(duration, 2)
        }


def _call_with_retry(api, method_name: str, *args, rotator, slot_id: str):
    """
    Call an API method with retry logic for 401/5xx errors.

    - 401/Unauthenticated: refresh token and retry once
    - 5xx/transient errors: exponential backoff with jitter, up to 8 attempts
    - Other errors: fail immediately
    """
    last_err = None
    transient_streak = 0
    token_refreshed = False

    # Try with no delay first, then with backoff delays
    for attempt, delay in enumerate([(0.0,) + _BACKOFF_DELAYS][0]):
        # Sleep with jitter (±25%)
        if delay > 0:
            jittered_delay = delay * (0.75 + random.random() * 0.5)
            time.sleep(jittered_delay)

        try:
            method = getattr(api, method_name)
            result = method(*args)
            return result

        except Exception as e:
            last_err = e
            msg = str(e)

            # Handle 401 - refresh token and retry
            if "401" in msg or "Unauthenticated" in msg.lower():
                print(f"unlock: 401 error on {method_name}, refreshing token for slot {slot_id[:8]}...")
                api = rotator.refresh(slot_id)
                if api is None:
                    raise Exception(f"Token refresh failed: {e}")
                transient_streak = 0
                continue

            # Handle transient errors - backoff and retry
            if _is_transient(e):
                transient_streak += 1

                # After 3 consecutive transient errors with 5xx, try refreshing token once
                if (transient_streak >= 3 and not token_refreshed and
                    any(x in msg for x in ["502", "503", "504", "Bad Gateway"])):
                    print(f"unlock: Multiple 5xx errors, trying token refresh...")
                    try:
                        new_api = rotator.refresh(slot_id)
                        if new_api:
                            api = new_api
                            token_refreshed = True
                    except Exception as refresh_err:
                        print(f"unlock: Token refresh failed: {refresh_err}")

                print(f"unlock: Transient error on {method_name} (attempt {attempt + 1}): {e}")
                continue

            # Non-transient, non-401 error - fail immediately
            raise e

    # Exhausted all retries
    raise last_err


def _insert_recent_log(username: str, status: str, error: Optional[str], duration: float):
    """Insert a record into recent_log table."""
    client = db.get_client()
    client.table("recent_log").insert({
        "username": username,
        "status": status,
        "error": error,
        "duration": duration,
        "completed_at": time.strftime("%Y-%m-%d %H:%M:%S")
    }).execute()
