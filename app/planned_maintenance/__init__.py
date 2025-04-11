# tkr_system/app/planned_maintenance/__init__.py
from flask import Blueprint
import calendar

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
    static_folder='static'
)

# Import routes after blueprint creation to avoid circular imports
# Routes will use the 'bp' instance defined above
from app.planned_maintenance import routes 