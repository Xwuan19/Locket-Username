import os
from datetime import datetime, timezone

from flask import (
    current_app, jsonify, render_template, request, send_file, session, redirect,
)

from .. import db, site_settings, unlock
from ..auth_utils import check_site_password, is_authenticated, auth_required
from . import bp


def _mobileconfig_path():
    static_dir = os.path.join(current_app.root_path, "static")
    return os.path.join(static_dir, "locket.mobileconfig")


def _mask_username(name):
    if not name:
        return "—"
    s = str(name)
    return s[0] + "*" * min(4, max(0, len(s) - 1))


def _no_accounts_response():
    return jsonify({
        "success": False,
        "msg": "Chưa có tài khoản Locket nào. Admin hãy thêm qua /admin.",
    }), 503


def _maintenance_active():
    m = site_settings.get_maintenance()
    if not m.get("enabled"):
        return None
    if m.get("allow_admin", True) and session.get("admin"):
        return None
    return m


def _maintenance_json_response():
    m = _maintenance_active()
    if m is None:
        return None
    return jsonify({
        "success": False,
        "maintenance": True,
        "msg": m.get("message") or "Hệ thống đang bảo trì.",
        "end_at": m.get("end_at") or None,
    }), 503


@bp.route("/")
def index():
    # If not authenticated, show login page
    if not is_authenticated():
        return render_template("login.html")

    # Check maintenance
    m = _maintenance_active()
    if m is not None:
        return render_template("maintenance.html", settings=m), 503

    theme = site_settings.get_theme().get("name", "gold")
    layout = site_settings.get_layout().get("name", "stacked")
    return render_template("index.html", theme=theme, layout=layout)


@bp.route("/auth/login", methods=["POST"])
def login():
    """Site-wide login endpoint."""
    data = request.json or {}
    password = data.get("password")

    if not password:
        return jsonify({"success": False, "error": "Password is required"}), 400

    if check_site_password(password):
        session["authenticated"] = True
        return jsonify({"success": True})

    return jsonify({"success": False, "error": "Invalid password"}), 401


@bp.route("/auth/logout", methods=["POST"])
def logout():
    """Site-wide logout endpoint."""
    session.clear()
    return jsonify({"success": True})


@bp.route("/api/mobileconfig", methods=["GET"])
@auth_required
def mobileconfig_download():
    """Serve the mobileconfig with the exact headers iOS needs to trigger the
    'Install Profile' system dialog (instead of saving as a regular download).

    - Content-Type: application/x-apple-aspen-config — required by iOS Safari.
    - Content-Disposition: inline — keeps Safari from offering "Save to Files".
    - No-cache — admins can re-upload and clients see the new version.
    """
    path = _mobileconfig_path()
    if not os.path.exists(path):
        return jsonify({"success": False, "msg": "Profile not configured"}), 404
    resp = send_file(
        path,
        mimetype="application/x-apple-aspen-config",
        as_attachment=False,
        download_name="locket.mobileconfig",
    )
    resp.headers["Content-Disposition"] = 'inline; filename="locket.mobileconfig"'
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@bp.route("/api/site-settings", methods=["GET"])
@auth_required
def site_settings_public():
    payload = site_settings.public_view()
    # Whether the current visitor is actually under maintenance (after admin
    # bypass). FE uses this to decide whether to redirect to the maintenance
    # page — `maintenance.enabled` alone would loop admins.
    payload["maintenance_active"] = _maintenance_active() is not None
    return jsonify({"success": True, **payload})


@bp.route("/api/get-user-info", methods=["POST"])
@auth_required
def get_user_info():
    blocked = _maintenance_json_response()
    if blocked is not None:
        return blocked
    rotator = current_app.rotator
    if rotator is None or rotator.size() == 0:
        return _no_accounts_response()

    data = request.json or {}
    username = data.get("username")
    if not username:
        return jsonify({"success": False, "msg": "Username is required"}), 400

    try:
        print(f"Looking up user: {username}")
        # Use round-robin slot selection
        slot_id = rotator.next_slot_round_robin()
        if not slot_id:
            return _no_accounts_response()

        api = rotator.ensure_fresh(slot_id)
        account_info = api.getUserByUsername(username)

        if not account_info or "result" not in account_info:
            return jsonify({"success": False, "msg": "User not found or API error"}), 404

        user_data = account_info.get("result", {}).get("data")
        if not user_data:
            return jsonify({"success": False, "msg": "User data not found"}), 404

        return jsonify({
            "success": True,
            "data": {
                "uid": user_data.get("uid"),
                "username": user_data.get("username"),
                "first_name": user_data.get("first_name", ""),
                "last_name": user_data.get("last_name", ""),
                "profile_picture_url": user_data.get("profile_picture_url", ""),
            },
        })

    except Exception as e:
        print(f"Error in get user info: {e}")
        return jsonify({"success": False, "msg": f"An error occurred: {str(e)}"}), 500


@bp.route("/api/unlock", methods=["POST"])
@auth_required
def unlock_gold():
    """Synchronous unlock endpoint. Blocks until unlock completes (10-30s)."""
    blocked = _maintenance_json_response()
    if blocked is not None:
        return blocked
    rotator = current_app.rotator
    if rotator is None or rotator.size() == 0:
        return _no_accounts_response()

    data = request.json or {}
    username = data.get("username")
    if not username:
        return jsonify({"success": False, "msg": "Username is required"}), 400

    try:
        print(f"Unlocking Gold for user: {username}")
        # Use round-robin slot selection
        slot_id = rotator.next_slot_round_robin()
        if not slot_id:
            return _no_accounts_response()

        api = rotator.get(slot_id)
        result = unlock.unlock_user(username, api, rotator, slot_id)

        return jsonify(result)

    except Exception as e:
        print(f"Error unlocking user: {e}")
        return jsonify({
            "success": False,
            "message": f"An error occurred: {str(e)}",
            "duration": 0
        }), 500


@bp.route("/api/recent-history", methods=["GET"])
@auth_required
def recent_history():
    """Public-safe recent history. Username is masked (a**** style).
    Returns up to 30 newest entries from last 24h."""
    client = db.get_client()
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(days=1)).isoformat()

    response = client.table("recent_log") \
        .select("username, status, duration, completed_at") \
        .gte("completed_at", cutoff) \
        .order("id", desc=True) \
        .limit(30) \
        .execute()

    rows = response.data if response.data else []
    items = []
    for r in rows:
        items.append({
            "username": _mask_username(r.get("username")),
            "status": r.get("status"),
            "duration": r.get("duration"),
            "completed_at": r.get("completed_at"),
        })
    return jsonify({"success": True, "items": items})


@bp.route("/api/mobileconfig/history", methods=["GET"])
@auth_required
def mobileconfig_history_public():
    """Public-safe profile update history. Filenames are stripped (admins only
    see those); we expose action + size + signed flag + timestamp so users
    know when the profile was last refreshed."""
    client = db.get_client()
    response = client.table("mobileconfig_history") \
        .select("action, size, signed, created_at") \
        .order("id", desc=True) \
        .limit(10) \
        .execute()

    rows = response.data if response.data else []
    items = [
        {
            "action": r.get("action"),
            "size": r.get("size"),
            "signed": bool(r.get("signed")),
            "created_at": r.get("created_at"),
        }
        for r in rows
    ]
    return jsonify({"success": True, "items": items})
