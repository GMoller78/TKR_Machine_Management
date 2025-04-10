# tkr_system/app/planned_maintenance/__init__.py
from flask import Blueprint

# Create a Blueprint instance for Planned Maintenance
# 'planned_maintenance' = blueprint name
# __name__ = import name (Flask uses this to locate resources)
# template_folder = specifies location for this blueprint's templates
# static_folder = specifies location for this blueprint's static files
# url_prefix = All routes defined in this blueprint will be prefixed with '/planned-maintenance'
bp = Blueprint(
    'planned_maintenance', 
    __name__, 
    template_folder='templates', 
    static_folder='static', 
    url_prefix='/planned-maintenance' 
)

# Import routes after blueprint creation to avoid circular imports
# Routes will use the 'bp' instance defined above
from app.planned_maintenance import routes 