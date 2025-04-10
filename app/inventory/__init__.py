# tkr_system/app/inventory/__init__.py
from flask import Blueprint

# Create a Blueprint instance for Inventory
# 'inventory' = blueprint name
# __name__ = import name (Flask uses this to locate resources)
# template_folder = specifies location for this blueprint's templates
# static_folder = specifies location for this blueprint's static files
# url_prefix = All routes defined in this blueprint will be prefixed with '/inventory'
bp = Blueprint(
    'inventory', 
    __name__, 
    template_folder='templates', 
    static_folder='static',
    url_prefix='/inventory'
)

# Import routes after blueprint creation to avoid circular imports
# Routes will use the 'bp' instance defined above
from app.inventory import routes