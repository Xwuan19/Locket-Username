"""Site-wide authentication for single-operator use.

Simple password-based authentication using session cookies.
The entire site is protected behind one shared password (SITE_PASSWORD env var).
"""

import os
from functools import wraps
from flask import session, jsonify, redirect, request


def check_site_password(password):
    """Validate password against SITE_PASSWORD env var."""
    site_password = os.getenv("SITE_PASSWORD")
    if not site_password:
        # No password set = allow access (for dev/testing)
        return True
    return password == site_password


def is_authenticated():
    """Check if current session is authenticated."""
    return session.get("authenticated", False)


def auth_required(f):
    """Decorator to require site-wide authentication."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_authenticated():
            # API routes get JSON 401
            if request.path.startswith("/api/"):
                return jsonify({"success": False, "error": "Unauthorized"}), 401
            # HTML routes redirect to login
            return redirect("/")
        return f(*args, **kwargs)
    return decorated_function
