# tkr_system/app/__init__.py
import os  # <--- Add this import statement
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from config import Config  # Import the Config class from config.py
import calendar

# Initialize extensions
db = SQLAlchemy()
migrate = Migrate()

from app.api.routes import api_bp # Import the new API blueprint

def create_app(config_class=Config):
    """Flask application factory."""
    app = Flask(__name__, instance_relative_config=True) # Enable instance folder configuration

    # Load configuration from Config object
    app.config.from_object(config_class) 
    
    # Ensure the instance folder exists 
    # This check is good practice within the factory too
    try:
        os.makedirs(app.instance_path) # Now 'os' is defined
    except OSError:
        pass # Already exists or other error creating it

    # Initialize Flask extensions with the app instance
    db.init_app(app)
    migrate.init_app(app, db)

   # --- Define and Register Jinja Filter ---
    # Option A: Define directly here
    @app.template_filter('month_name') # The name used in the template
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
    # Import within the factory function to avoid circular imports
    from app.planned_maintenance import bp as pm_bp
    from app.inventory import bp as inv_bp

    app.register_blueprint(pm_bp, url_prefix='/planned-maintenance')
    app.register_blueprint(inv_bp, url_prefix='/inventory')
    app.register_blueprint(api_bp)

    # Optional: Add a simple root route for testing if the app runs
    @app.route('/hello')
    def hello():
        return "Hello, TKR System!"

    return app

# Import models here after db is initialized, 


def format_month_name(month_number):
    """Converts a month number (1-12) to its full name."""
    try:
        month_num = int(month_number)
        if 1 <= month_num <= 12:
            return calendar.month_name[month_num]
        else:
            # Return the original value if it's not a valid month number
            return month_number
    except (ValueError, TypeError):
        # Handle cases where month_number isn't an integer
        return month_number
# helps Flask-Migrate detect models easily.
# Putting it here avoids circular imports with blueprints potentially needing models.
from app import models 