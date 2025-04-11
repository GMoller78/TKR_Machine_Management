# /home/appuser/TKR_Machine_Management/wsgi.py
from app import create_app

app = create_app()

# Note: No need for if __name__ == "__main__": app.run() here
# Gunicorn imports the 'app' object directly.