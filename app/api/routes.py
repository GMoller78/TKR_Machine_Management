# app/api/routes.py
import logging
from flask import Blueprint, jsonify, request, abort
from app import db
# Import the models using the to_dict methods
from app.models import (
    Equipment, JobCard, MaintenancePlanEntry, UsageLog, Checklist, Part,
    JobCardPart, StockTransaction, MaintenanceTask # Ensure all needed models are imported
)
from sqlalchemy import desc
from datetime import datetime, date, timezone
from dateutil.parser import parse as parse_datetime
from calendar import monthrange

# Define the Blueprint
api_bp = Blueprint('api', __name__, url_prefix='/api')

# --- Helper Functions ---
# Removed model_to_dict helper as we'll use model methods directly

# --- Error Handlers for API ---
# ... (Keep existing error handlers: 404, 400, 500) ...
@api_bp.errorhandler(404)
def not_found_error(error):
    return jsonify({"error": "Not Found", "message": str(error.description if hasattr(error, 'description') else error)}), 404

@api_bp.errorhandler(400)
def bad_request_error(error):
    message = str(error.description) if hasattr(error, 'description') else "Invalid input."
    return jsonify({"error": "Bad Request", "message": message}), 400

@api_bp.errorhandler(500)
def internal_error(error):
    logging.error(f"API Internal Error: {error}", exc_info=True)
    db.session.rollback()
    return jsonify({"error": "Internal Server Error", "message": "An unexpected error occurred."}), 500


# ==============================================================================
# === Equipment API Routes ===
# ==============================================================================
# ... (Keep existing Equipment routes: GET list, GET id, POST, PUT, DELETE) ...
# Example using model's to_dict:
@api_bp.route('/equipment', methods=['GET'])
def get_equipment_list():
    """Returns a list of all equipment."""
    try:
        all_equipment = Equipment.query.order_by(Equipment.code).all()
        # Use the model's to_dict method
        return jsonify([eq.to_dict() for eq in all_equipment])
    except Exception as e:
        logging.error(f"Error retrieving equipment list: {e}", exc_info=True)
        abort(500, description="Error retrieving equipment list.")

@api_bp.route('/equipment/<int:id>', methods=['GET'])
def get_equipment(id):
    """Returns details for a specific equipment item."""
    equipment = Equipment.query.get_or_404(id, description=f"Equipment with ID {id} not found.")
    return jsonify(equipment.to_dict()) # Use model's to_dict

# ... other equipment routes (POST, PUT, DELETE) should also use to_dict() ...
@api_bp.route('/equipment', methods=['POST'])
def create_equipment():
    if not request.json: abort(400, "Request must be JSON.")
    data = request.get_json()
    required_fields = ['code', 'name', 'type']
    if not all(field in data for field in required_fields):
        abort(400, f"Missing required fields: {', '.join(required_fields)}")
    if Equipment.query.filter_by(code=data['code']).first():
        abort(400, f"Equipment with code '{data['code']}' already exists.")
    try:
        new_equipment = Equipment(
            code=data['code'], name=data['name'], type=data['type'],
            checklist_required=data.get('checklist_required', False)
        )
        db.session.add(new_equipment)
        db.session.commit()
        logging.info(f"API: Created equipment {new_equipment.id} - {new_equipment.code}")
        return jsonify(new_equipment.to_dict()), 201
    except Exception as e:
        db.session.rollback()
        logging.error(f"Database error creating equipment: {e}", exc_info=True)
        abort(500, "Database error creating equipment.")

@api_bp.route('/equipment/<int:id>', methods=['PUT'])
def update_equipment(id):
    equipment = Equipment.query.get_or_404(id, f"Equipment with ID {id} not found.")
    if not request.json: abort(400,"Request must be JSON.")
    data = request.get_json()
    if 'code' in data and data['code'] != equipment.code:
        existing = Equipment.query.filter(Equipment.code == data['code'], Equipment.id != id).first()
        if existing: abort(400, f"Another equipment item already uses code '{data['code']}'.")
        equipment.code = data['code']
    equipment.name = data.get('name', equipment.name)
    equipment.type = data.get('type', equipment.type)
    equipment.checklist_required = data.get('checklist_required', equipment.checklist_required)
    try:
        db.session.commit()
        logging.info(f"API: Updated equipment {equipment.id} - {equipment.code}")
        return jsonify(equipment.to_dict())
    except Exception as e:
        db.session.rollback()
        logging.error(f"Database error updating equipment: {e}", exc_info=True)
        abort(500, "Database error updating equipment.")

@api_bp.route('/equipment/<int:id>', methods=['DELETE'])
def delete_equipment(id):
    equipment = Equipment.query.get_or_404(id, f"Equipment with ID {id} not found.")
    try:
        # Check dependencies before deleting if necessary
        if equipment.job_cards.first() or equipment.maintenance_tasks.first() or equipment.checklists.first() or equipment.usage_logs.first() or equipment.plan_entries.first():
             abort(400, description=f"Cannot delete equipment {id} ({equipment.code}). It is linked to other records (job cards, tasks, logs, etc.).")

        db.session.delete(equipment)
        db.session.commit()
        logging.info(f"API: Deleted equipment {id} - {equipment.code}")
        return jsonify({"message": f"Equipment '{equipment.code}' deleted successfully."}), 200
    except Exception as e:
        db.session.rollback()
        if "IntegrityError" in str(e):
             abort(400, description=f"Cannot delete equipment {id}. It might be linked to other records.")
        logging.error(f"Database error deleting equipment: {e}", exc_info=True)
        abort(500, "Database error deleting equipment.")


# ==============================================================================
# === Job Card API Routes ===
# ==============================================================================

@api_bp.route('/job_cards', methods=['GET'])
def get_job_cards():
    """
    Returns a list of job cards.
    Optionally filter by 'status' query parameter (e.g., /api/job_cards?status=Done).
    Returns ALL job cards if 'status' parameter is omitted.
    """
    status_filter = request.args.get('status')
    # Start base query with eager loading for efficiency
    query = JobCard.query.options(
        db.joinedload(JobCard.equipment_ref) # Eager load equipment
    )

    if status_filter:
        # Apply filter if status parameter is provided
        query = query.filter(JobCard.status == status_filter)

    try:
        # Consider adding pagination for large numbers of job cards
        # Add ordering, e.g., by descending ID or due date
        job_cards = query.order_by(desc(JobCard.id)).all()

        # Use the model's to_dict method, including equipment details
        results = [jc.to_dict(include_equipment=True) for jc in job_cards]
        return jsonify(results)
    except Exception as e:
        logging.error(f"Error retrieving job cards: {e}", exc_info=True)
        abort(500, description="Error retrieving job cards.")

# ... (Keep existing GET id, POST, PUT, DELETE for Job Cards, using to_dict) ...
@api_bp.route('/job_cards/<int:id>', methods=['GET'])
def get_job_card(id):
    job_card = JobCard.query.options(
        db.joinedload(JobCard.equipment_ref),
        # Eager load parts via the association object then the part itself
        db.joinedload(JobCard.parts_used).joinedload(JobCardPart.part).joinedload(Part.supplier_ref)
    ).get_or_404(id, description=f"Job Card with ID {id} not found.")
    # Include equipment and parts details
    return jsonify(job_card.to_dict(include_equipment=True, include_parts=True))

@api_bp.route('/job_cards', methods=['POST'])
def create_job_card():
    if not request.json: abort(400, "Request must be JSON.")
    data = request.get_json()
    required_fields = ['equipment_id', 'description']
    if not all(field in data for field in required_fields):
        abort(400, f"Missing required fields: {', '.join(required_fields)}")
    equipment = Equipment.query.get(data['equipment_id'])
    if not equipment: abort(404, f"Equipment with ID {data['equipment_id']} not found.")

    try:
        year = datetime.now(timezone.utc).year
        count = JobCard.query.count() + 1 # Consider race condition potential
        job_number = f"JC-{year}-{count:04d}"
        due_date = None
        if data.get('due_date'):
            try:
                due_date = parse_datetime(data['due_date'])
                # Handle naive/aware based on model field type and app consistency
                # Assuming model stores naive UTC if aware is passed
                if isinstance(due_date, datetime) and due_date.tzinfo:
                    due_date = due_date.astimezone(timezone.utc).replace(tzinfo=None)
            except (ValueError, TypeError):
                abort(400, "Invalid due_date format. Use ISO 8601.")

        new_jc = JobCard(
            job_number=job_number, equipment_id=data['equipment_id'], description=data['description'],
            technician=data.get('technician'), status=data.get('status', 'To Do'),
            oem_required=data.get('oem_required', False), kit_required=data.get('kit_required', False),
            due_date=due_date, comments=data.get('comments')
        )
        db.session.add(new_jc)
        db.session.commit()
        logging.info(f"API: Created Job Card {new_jc.id} - {new_jc.job_number}")
        # Reload to get relationships populated correctly for the response
        db.session.refresh(new_jc)
        return jsonify(new_jc.to_dict(include_equipment=True)), 201
    except Exception as e:
        db.session.rollback()
        logging.error(f"Database error creating job card: {e}", exc_info=True)
        abort(500, "Database error creating job card.")

@api_bp.route('/job_cards/<int:id>', methods=['PUT'])
def update_job_card(id):
    job_card = JobCard.query.get_or_404(id, description=f"Job Card with ID {id} not found.")
    if not request.json: abort(400, "Request must be JSON.")
    data = request.get_json()

    # Update fields... (similar logic as before)
    job_card.description = data.get('description', job_card.description)
    job_card.technician = data.get('technician', job_card.technician)
    job_card.status = data.get('status', job_card.status)
    job_card.oem_required = data.get('oem_required', job_card.oem_required)
    job_card.kit_required = data.get('kit_required', job_card.kit_required)
    job_card.comments = data.get('comments', job_card.comments)
    # Handle due_date update...
    if 'due_date' in data:
        if data['due_date'] is None: job_card.due_date = None
        else:
            try:
                due_date = parse_datetime(data['due_date'])
                if isinstance(due_date, datetime) and due_date.tzinfo:
                     due_date = due_date.astimezone(timezone.utc).replace(tzinfo=None)
                job_card.due_date = due_date
            except (ValueError, TypeError): abort(400, "Invalid due_date format.")
    # Handle completion times if status is 'Done'...
    if job_card.status == 'Done':
        if 'start_datetime' in data and data['start_datetime']:
            try:
                start_dt = parse_datetime(data['start_datetime'])
                if start_dt.tzinfo: start_dt = start_dt.astimezone(timezone.utc).replace(tzinfo=None)
                job_card.start_datetime = start_dt
            except (ValueError, TypeError): abort(400, "Invalid start_datetime format.")
        if 'end_datetime' in data and data['end_datetime']:
             try:
                 end_dt = parse_datetime(data['end_datetime'])
                 if end_dt.tzinfo: end_dt = end_dt.astimezone(timezone.utc).replace(tzinfo=None)
                 if job_card.start_datetime and end_dt <= job_card.start_datetime:
                     abort(400, "End datetime must be after start datetime.")
                 job_card.end_datetime = end_dt
             except (ValueError, TypeError): abort(400, "Invalid end_datetime format.")
        # NOTE: Updating parts used via PUT is complex. Consider a separate endpoint or specific logic.

    try:
        db.session.commit()
        logging.info(f"API: Updated Job Card {job_card.id} - {job_card.job_number}")
        # Reload to get relationships updated for response
        db.session.refresh(job_card)
        return jsonify(job_card.to_dict(include_equipment=True)) # Maybe include parts if updated?
    except Exception as e:
        db.session.rollback()
        logging.error(f"Database error updating job card: {e}", exc_info=True)
        abort(500, "Database error updating job card.")


@api_bp.route('/job_cards/<int:id>', methods=['DELETE'])
def delete_job_card(id):
    job_card = JobCard.query.get_or_404(id, description=f"Job Card with ID {id} not found.")
    try:
        # Delete associated parts first if cascade delete isn't fully reliable or specific logic needed
        # JobCardPart.query.filter_by(job_card_id=id).delete()
        # The cascade='all, delete-orphan' on the relationship should handle this.

        db.session.delete(job_card)
        db.session.commit()
        logging.info(f"API: Deleted Job Card {id} - {job_card.job_number}")
        return jsonify({"message": f"Job Card '{job_card.job_number}' deleted successfully."}), 200
    except Exception as e:
        db.session.rollback()
        logging.error(f"Database error deleting job card: {e}", exc_info=True)
        abort(500, "Database error deleting job card.")


# ==============================================================================
# === Maintenance Plan API Routes ===
# ==============================================================================

@api_bp.route('/maintenance_plan', methods=['GET'])
def get_maintenance_plan():
    """
    Returns the generated maintenance plan entries for a specific year and month.
    Requires 'year' and 'month' query parameters.
    e.g., /api/maintenance_plan?year=2024&month=7
    """
    try:
        year = request.args.get('year', type=int)
        month = request.args.get('month', type=int)

        if not year or not month:
            abort(400, description="Missing required query parameters: 'year' and 'month'.")
        if not (1 <= month <= 12):
            abort(400, description="Invalid 'month' parameter. Must be between 1 and 12.")
        current_year = date.today().year
        if not (current_year - 10 <= year <= current_year + 10):
            abort(400, description="Invalid 'year' parameter. Seems out of reasonable range.")

    except ValueError:
        abort(400, description="Invalid 'year' or 'month' parameter. Must be integers.")

    try:
        plan_entries = MaintenancePlanEntry.query.filter_by(
            plan_year=year, plan_month=month
        ).options(
            # Eager load related data using the backrefs defined in the models
            db.joinedload(MaintenancePlanEntry.equipment),
            db.joinedload(MaintenancePlanEntry.task) # Eager load task details if needed
        ).order_by(
            MaintenancePlanEntry.planned_date, # Order by date first
            MaintenancePlanEntry.equipment_id # Then by equipment
        ).all()

        # Use to_dict, include equipment details
        results = [entry.to_dict(include_equipment=True) for entry in plan_entries]
        return jsonify(results)

    except Exception as e:
        logging.error(f"Error retrieving maintenance plan entries: {e}", exc_info=True)
        abort(500, description="Error retrieving maintenance plan entries.")


# --- NEW Endpoint for ALL Maintenance Plan Entries ---
@api_bp.route('/maintenance_plan/all', methods=['GET'])
def get_all_maintenance_plan_entries():
    """
    Returns ALL generated maintenance plan entries across all time periods.
    Warning: This could return a large amount of data. Consider pagination for production use.
    """
    try:
        # Query all entries, consider adding ordering
        all_entries = MaintenancePlanEntry.query.options(
            db.joinedload(MaintenancePlanEntry.equipment), # Eager load equipment
            # db.joinedload(MaintenancePlanEntry.task) # Optional: load task details
        ).order_by(
            MaintenancePlanEntry.plan_year.desc(), # Most recent year first
            MaintenancePlanEntry.plan_month.desc(), # Most recent month first
            MaintenancePlanEntry.planned_date, # Then by planned date
            MaintenancePlanEntry.equipment_id # Then by equipment
        ).all() # Use .all() for now, but add pagination later if needed

        # Add pagination logic here in a real application:
        # page = request.args.get('page', 1, type=int)
        # per_page = request.args.get('per_page', 20, type=int)
        # paginated_entries = query.paginate(page=page, per_page=per_page, error_out=False)
        # results = [entry.to_dict(...) for entry in paginated_entries.items]
        # return jsonify({
        #     'items': results,
        #     'total_pages': paginated_entries.pages,
        #     'current_page': page,
        #     'total_items': paginated_entries.total
        # })

        # For now, return all using to_dict
        results = [entry.to_dict(include_equipment=True) for entry in all_entries]
        return jsonify(results)

    except Exception as e:
        logging.error(f"Error retrieving all maintenance plan entries: {e}", exc_info=True)
        abort(500, description="Error retrieving all maintenance plan entries.")
# --- End NEW Endpoint ---


# ==============================================================================
# === Usage Log API Routes ===
# ==============================================================================
# ... (Keep existing Usage Log routes: GET list, GET id, POST) ...
# Example using model's to_dict:
@api_bp.route('/usage_logs', methods=['GET'])
def get_usage_logs():
    equipment_id_filter = request.args.get('equipment_id', type=int)
    query = UsageLog.query.options(db.joinedload(UsageLog.equipment_ref))
    if equipment_id_filter:
        if not Equipment.query.get(equipment_id_filter):
            abort(404, f"Equipment with ID {equipment_id_filter} not found.")
        query = query.filter(UsageLog.equipment_id == equipment_id_filter)
    try:
        # Add pagination if needed
        logs = query.order_by(desc(UsageLog.log_date)).all()
        results = [log.to_dict(include_equipment=True) for log in logs]
        return jsonify(results)
    except Exception as e:
         logging.error(f"Error retrieving usage logs: {e}", exc_info=True)
         abort(500, "Error retrieving usage logs.")

@api_bp.route('/usage_logs/<int:id>', methods=['GET'])
def get_usage_log(id):
    log = UsageLog.query.options(
             db.joinedload(UsageLog.equipment_ref)
         ).get_or_404(id, description=f"Usage Log with ID {id} not found.")
    return jsonify(log.to_dict(include_equipment=True))

@api_bp.route('/usage_logs', methods=['POST'])
def create_usage_log():
    if not request.json: abort(400, "Request must be JSON.")
    data = request.get_json()
    required_fields = ['equipment_id', 'usage_value']
    if not all(field in data for field in required_fields):
        abort(400, f"Missing required fields: {', '.join(required_fields)}")
    if not Equipment.query.get(data['equipment_id']):
         abort(404, f"Equipment with ID {data['equipment_id']} not found.")
    try:
        usage_value = float(data['usage_value'])
    except (ValueError, TypeError): abort(400, "Invalid usage_value. Must be a number.")
    log_date = datetime.now(timezone.utc)
    if data.get('log_date'):
        try:
            log_date = parse_datetime(data['log_date'])
            if log_date.tzinfo: log_date = log_date.astimezone(timezone.utc)
            # Handle naive storage if needed: log_date = log_date.replace(tzinfo=None)
        except (ValueError, TypeError): abort(400, "Invalid log_date format. Use ISO 8601.")
    try:
        new_log = UsageLog(
            equipment_id=data['equipment_id'], usage_value=usage_value, log_date=log_date
        )
        db.session.add(new_log)
        db.session.commit()
        logging.info(f"API: Created Usage Log {new_log.id} for equipment {new_log.equipment_id}")
        db.session.refresh(new_log) # Refresh to get relationships
        return jsonify(new_log.to_dict(include_equipment=True)), 201
    except Exception as e:
        db.session.rollback()
        logging.error(f"Database error creating usage log: {e}", exc_info=True)
        abort(500, "Database error creating usage log.")

# ==============================================================================
# === Checklist API Routes ===
# ==============================================================================
# ... (Keep existing Checklist routes: GET list, GET id, POST) ...
# Example using model's to_dict:
@api_bp.route('/checklists', methods=['GET'])
def get_checklists():
    equipment_id_filter = request.args.get('equipment_id', type=int)
    query = Checklist.query.options(db.joinedload(Checklist.equipment_ref))
    if equipment_id_filter:
        if not Equipment.query.get(equipment_id_filter):
            abort(404, f"Equipment with ID {equipment_id_filter} not found.")
        query = query.filter(Checklist.equipment_id == equipment_id_filter)
    try:
        # Add pagination if needed
        checklists = query.order_by(desc(Checklist.check_date)).all()
        results = [cl.to_dict(include_equipment=True) for cl in checklists]
        return jsonify(results)
    except Exception as e:
        logging.error(f"Error retrieving checklists: {e}", exc_info=True)
        abort(500, "Error retrieving checklists.")

@api_bp.route('/checklists/<int:id>', methods=['GET'])
def get_checklist(id):
    checklist = Checklist.query.options(
            db.joinedload(Checklist.equipment_ref)
        ).get_or_404(id, description=f"Checklist with ID {id} not found.")
    return jsonify(checklist.to_dict(include_equipment=True))

@api_bp.route('/checklists', methods=['POST'])
def create_checklist():
    if not request.json: abort(400, "Request must be JSON.")
    data = request.get_json()
    required_fields = ['equipment_id', 'status']
    if not all(field in data for field in required_fields):
        abort(400, f"Missing required fields: {', '.join(required_fields)}")
    if not Equipment.query.get(data['equipment_id']):
         abort(404, f"Equipment with ID {data['equipment_id']} not found.")
    valid_statuses = ["Go", "Go But", "No Go"]
    if data['status'] not in valid_statuses:
        abort(400, f"Invalid status '{data['status']}'. Must be one of: {', '.join(valid_statuses)}")
    check_date = datetime.now(timezone.utc)
    if data.get('check_date'):
        try:
            check_date = parse_datetime(data['check_date'])
            if check_date.tzinfo: check_date = check_date.astimezone(timezone.utc)
            # Handle naive storage if needed: check_date = check_date.replace(tzinfo=None)
        except (ValueError, TypeError): abort(400, "Invalid check_date format. Use ISO 8601.")
    try:
        new_checklist = Checklist(
            equipment_id=data['equipment_id'], status=data['status'],
            issues=data.get('issues'), check_date=check_date
        )
        db.session.add(new_checklist)
        db.session.commit()
        logging.info(f"API: Created Checklist {new_checklist.id} for equipment {new_checklist.equipment_id}")
        db.session.refresh(new_checklist) # Refresh to get relationships
        return jsonify(new_checklist.to_dict(include_equipment=True)), 201
    except Exception as e:
        db.session.rollback()
        logging.error(f"Database error creating checklist: {e}", exc_info=True)
        abort(500, "Database error creating checklist.")

# --- (Optional) Add other API endpoints for Supplier, Part, MaintenanceTask etc. if needed ---