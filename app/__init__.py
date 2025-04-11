# tkr_system/app/__init__.py
import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from config import Config
import calendar
from werkzeug.middleware.proxy_fix import ProxyFix # <--- Import ProxyFix

# Initialize extensions
db = SQLAlchemy()
migrate = Migrate()

from app.api.routes import api_bp # Import the new API blueprint

def create_app(config_class=Config):
    """Flask application factory."""
    app = Flask(__name__, instance_relative_config=True)

    # Load configuration from Config object
    app.config.from_object(config_class)

    # --- Apply ProxyFix AFTER app creation and config ---
    # This helps Flask understand headers like X-Forwarded-For, X-Forwarded-Proto, X-Forwarded-Host, X-Forwarded-Prefix
    # when running behind a reverse proxy like Nginx.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    # --- End ProxyFix ---

    # Ensure the instance folder exists
    try:
        os.makedirs(app.instance_path)
    except OSError:
        pass # Already exists or other error creating it

    # Initialize Flask extensions with the app instance
    db.init_app(app)
    migrate.init_app(app, db)

   # --- Define and Register Jinja Filter ---
    @app.template_filter('month_name')
    def format_month_name_filter(month_number):
        """Converts a month number (1-12) to its full name."""
        try:
            month_num = int(month_number)
            if 1 <= month_num <= 12:
                return calendar.month_name[month_num]
            else:
                return month_number
        except (ValueError, TypeError):
            return month_number

    # Import and register blueprints
    from app.planned_maintenance import bp as pm_bp
    from app.inventory import bp as inv_bp

    app.register_blueprint(pm_bp, url_prefix='/planned-maintenance')
    app.register_blueprint(inv_bp, url_prefix='/inventory')
    app.register_blueprint(api_bp) # Assuming api_bp doesn't need a prefix, adjust if it does

    # Optional: Add a simple root route for testing if the app runs
    @app.route('/hello')
    def hello():
        return "Hello, TKR System!"

    return app

# (Keep the rest of the file as is, including the bottom import)
from app import models

# Helper function (can remain outside create_app)
def format_month_name(month_number):
    """Converts a month number (1-12) to its full name."""
    try:
        month_num = int(month_number)
        if 1 <= month_num <= 12:
            return calendar.month_name[month_num]
        else:
            return month_number
    except (ValueError, TypeError):
        return month_number