# tkr_system/config.py
import os

# Determine the base directory of the project
basedir = os.path.abspath(os.path.dirname(__file__))
# Define the path to the instance folder
instance_path = os.path.join(basedir, 'instance')

# Ensure the instance folder exists
os.makedirs(instance_path, exist_ok=True)

class Config:
    """Base configuration settings."""
    SECRET_KEY = os.environ.get('SECRET_KEY', 'a-very-secure-dev-key-replace-me') # Use environment variable in production!
    
    # Define the SQLite database URI relative to the instance folder
    # Flask-SQLAlchemy automatically prefixes this with 'instance/' when instance_relative_config=True
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        'sqlite:///' + os.path.join(instance_path, 'tkr.db')
        
    SQLALCHEMY_TRACK_MODIFICATIONS = False # Disable modification tracking to save resources