"""Flask app factory.

The app is composed in `create_app()`:

1. Load .env, build Flask config, ProxyFix when behind HTTPS.
2. Initialize the Supabase connection (idempotent).
3. Build the AccountRotator. If the DB has zero accounts and EMAIL/PASSWORD
   env vars are unset, the rotator boots empty — public endpoints will return
   503 until an account is added through the admin panel, but admin login
   still works.
4. Register the public + admin blueprints.

Singleton (rotator) is attached to the Flask `app` object so request handlers
can reach it via `current_app.rotator`.
"""

import dotenv
from flask import Flask

from . import config, db
from .admin import bp as admin_bp
from .public_routes import bp as public_bp
from .rotator import AccountRotator


def create_app():
    dotenv.load_dotenv()

    app = Flask(__name__, instance_relative_config=False)
    config.configure(app)
    db.init()

    try:
        app.rotator = AccountRotator()
    except Exception as e:
        # Rotator now boots with 0 accounts gracefully, so this only fires on
        # truly unexpected init errors (e.g. corrupted DB). Keep the app alive
        # so admin can still log in and see logs.
        print(f"Error initializing AccountRotator: {e}")
        app.rotator = None

    app.register_blueprint(public_bp)
    app.register_blueprint(admin_bp)

    return app
