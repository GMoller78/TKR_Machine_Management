# tkr_system/app/__init__.py
import os
import calendar
import logging
from flask import Flask, request, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_login import LoginManager
from config import Config

# Initialize extensions
db = SQLAlchemy()
migrate = Migrate() # Uncomment if you use Flask-Migrate
login_manager = LoginManager()
login_manager.login_view = 'auth.login' # Route name for login page
login_manager.login_message = 'Please log in to access this page.'
login_manager.login_message_category = 'info' # Bootstrap class for flash message

def create_app(config_class=Config):
    """Flask application factory."""
    app = Flask(__name__, instance_relative_config=True)

    # Load configuration from Config object
    app.config.from_object(config_class)

    # Apply ProxyFix to handle reverse proxy headers
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    # Ensure the instance folder exists
    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except OSError:
        pass  # Already exists or other error creating it

    # Initialize Flask extensions with the app instance
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app) 

    # Define and Register Jinja Filter
    @app.template_filter('month_name')
    def format_month_name_filter(month_number):
        """Converts a month number (1-12) to its full name."""
        try:
            month_num = int(month_number)
            if 1 <= month_num <= 12:
                return calendar.month_name[month_num]
            return str(month_number)
        except (ValueError, TypeError):
            return str(month_number)

    def nl2br(value):
        """Convert newlines to <br> tags."""
        if not value:
            return ""
        return value.replace('\n', '<br>\n')

# Then register it with the app

    # Debug route for inspecting request and application state
    @app.route('/debug')
    def debug():
        """Debug route to inspect request headers, URLs, and environment."""
        try:
            headers = {k: v for k, v in request.headers.items()}
            example_url = "N/A"
            try:
                example_url = url_for('planned_maintenance.dashboard', _external=False)
            except Exception as url_err:
                example_url = f"Error generating URL: {url_err}"

            wsgi_environ = {
                k: v for k, v in request.environ.items()
                if k.startswith(('HTTP_', 'PATH_', 'REQUEST_', 'SERVER_', 'SCRIPT_', 'wsgi.'))
            }

            return f"""
            <h3>Debug Information</h3>
            <b>Request Path:</b> {request.path}<br>
            <b>Request Script Root:</b> {request.script_root}<br>
            <b>Request URL:</b> {request.url}<br>
            <b>Request Base URL:</b> {request.base_url}<br>
            <hr>
            <b>Generated URL ('planned_maintenance.dashboard'):</b> {example_url}<br>
            <hr>
            <b>Headers Seen by Flask:</b><br>
            <pre>{headers}</pre>
            <hr>
            <b>WSGI Environ (relevant parts):</b><br>
            <pre>{wsgi_environ}</pre>
            """
        except Exception as e:
            return f"Error in debug route: {str(e)}", 500

    # Import and register blueprints
    from app.planned_maintenance import bp as pm_bp
    from app.inventory import bp as inv_bp
    from app.api.routes import api_bp
    from app.auth import bp as auth_bp

    app.jinja_env.filters['nl2br'] = nl2br
    from app.planned_maintenance.routes import generate_whatsapp_share_url
    app.jinja_env.globals['generate_whatsapp_share_url'] = generate_whatsapp_share_url
    
    app.register_blueprint(pm_bp, url_prefix='/planned-maintenance')
    app.register_blueprint(inv_bp, url_prefix='/inventory')
    app.register_blueprint(api_bp)  # Assuming no prefix for API, adjust if needed
    app.register_blueprint(auth_bp, url_prefix='/auth')

    # Simple root route for testing
    @app.route('/hello')
    def hello():
        return "Hello, TKR System!"

    return app

# Import models after create_app to avoid circular imports
from app import models

# Helper function (outside create_app for potential reuse)
def format_month_name(month_number):
    """Converts a month number (1-12) to its full name."""
    try:
        month_num = int(month_number)
        if 1 <= month_num <= 12:
            return calendar.month_name[month_num]
        return str(month_number)
    except (ValueError, TypeError):
        return str(month_number)