# /home/appuser/TKR_Machine_Management/wsgi.py
from dotenv import load_dotenv
load_dotenv()

from app import create_app

app = create_app()

print(f"DEBUG: Connecting with SQLALCHEMY_DATABASE_URI = {app.config.get('SQLALCHEMY_DATABASE_URI')}")
# Note: No need for if __name__ == "__main__": app.run() here
# Gunicorn imports the 'app' object directly.