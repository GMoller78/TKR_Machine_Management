# config.py
import os

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'your-very-secret-key-in-development'

    # PostgreSQL Database Configuration
    # Option 1: Using a single DATABASE_URL environment variable (Recommended for production)
    # Example: postgresql://user:password@host:port/dbname
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        f"postgresql://{os.environ.get('PGUSER', 'postgres')}:{os.environ.get('PGPASSWORD', 'anikaM0404')}@" \
        f"{os.environ.get('PGHOST', 'localhost')}:{os.environ.get('PGPORT', '5432')}/" \
        f"{os.environ.get('PGDATABASE', 'tkr_pm_db')}"

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Optional: If you want to see the SQL queries SQLAlchemy executes (good for debugging)
    # SQLALCHEMY_ECHO = True

    # Ensure your other configurations (if any) are also here
    # For example, if you had mail server settings, etc.