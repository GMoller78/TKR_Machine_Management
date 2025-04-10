# tkr_system/run.py
import os
from app import create_app

# Create the Flask app instance using the factory
app = create_app()

# Run the development server
if __name__ == '__main__':
    # Use port 5000 by default, allow configuration via environment variable
    port = int(os.environ.get('PORT', 5000)) 
    # Run in debug mode for development (auto-reloads, detailed errors)
    # Set debug=False in production!
    app.run(debug=True, host='0.0.0.0', port=port) 