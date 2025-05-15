
import logging
# Corrected Flask imports
from flask import render_template, request, redirect, url_for, flash, make_response, session, jsonify
from urllib.parse import urlencode
# Corrected datetime imports
from datetime import datetime, timedelta, timezone, date, time
# <<< ADDED: dateutil.parser for flexible datetime string parsing >>>
from dateutil.parser import parse as parse_datetime
from dateutil.relativedelta import relativedelta
from calendar import monthrange
from io import BytesIO
from app.planned_maintenance import bp
from app import db
from sqlalchemy import cast, Date # Add Date cast
from app.forms import ChecklistEditForm, UsageLogEditForm 

from app.models import (
    Equipment, JobCard, Checklist, StockTransaction,
    JobCardPart, Part, MaintenanceTask, UsageLog,
    MaintenancePlanEntry # <-- Added import
)
from itertools import zip_longest
# Corrected SQLAlchemy imports (added extract)
from sqlalchemy import desc, func, extract, and_, or_
import traceback
from collections import defaultdict

# --- PDF Generation (Ensure this is near the top, after imports) ---
try:
    from weasyprint import HTML # <-- Define HTML here
    WEASYPRINT_AVAILABLE = True
except ImportError:
    WEASYPRINT_AVAILABLE = False
    HTML = None # Define HTML as None if import fails
    logging.warning("WeasyPrint not found. PDF generation will be disabled.")
# --- End PDF Generation ---

# --- Configure Logging (add this near the top) ---
# Basic configuration - logs to console. Adjust level and format as needed.
# Use DEBUG level to see all our messages.
logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s %(levelname)-8s %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')

# --- Planned Maintenance Routes ---
DUE_SOON_ESTIMATED_DAYS_THRESHOLD = 7
EQUIPMENT_STATUSES = ['Operational', 'At OEM', 'Sold', 'Broken Down', 'Under Repair', 'Awaiting Spares']
JOB_CARD_STATUSES = ['To Do', 'In Progress', 'Done', 'Deleted']
MAX_REASONABLE_DAILY_USAGE_INCREASE = 500

def generate_whatsapp_share_url(job_card):
    """Generates a WhatsApp share URL for a given job card."""
    if not job_card:
        return None
    try:
        if job_card.equipment_ref:
            equipment_name = f"{job_card.equipment_ref.code} - {job_card.equipment_ref.name}"
        else:
            equipment_name = "Unknown Equipment"
        due_date_str = job_card.due_date.strftime('%Y-%m-%d') if job_card.due_date else 'Not Set'
        technician_str = job_card.technician or 'Unassigned'
        status_str = job_card.status or 'N/A'
        start_str = job_card.start_datetime.strftime('%Y-%m-%d %H:%M') if job_card.start_datetime else 'N/A'
        end_str = job_card.end_datetime.strftime('%Y-%m-%d %H:%M') if job_card.end_datetime else 'N/A'

        # Decide which details to include based on status maybe?
        whatsapp_msg = (
            f"*Job Card Details:*\n"
            f"*Number:* {job_card.job_number}\n"
            f"*Equipment:* {equipment_name}\n"
            f"*Task:* {job_card.description}\n"
            f"*Status:* {status_str}\n"
            f"*Assigned:* {technician_str}\n"
            f"*Due:* {due_date_str}\n"
        )
        # Add completion details if done
        if job_card.status == 'Done':
             whatsapp_msg += f"*Started:* {start_str}\n"
             whatsapp_msg += f"*Completed:* {end_str}\n"
             whatsapp_msg += f"*Comments:* {job_card.comments or 'None'}\n"
             # Consider adding parts used if desired

        encoded_msg = urlencode({'text': whatsapp_msg})
        return f"https://wa.me/?{encoded_msg}"
    except Exception as e:
        logging.error(f"Error generating WhatsApp URL for JC {getattr(job_card, 'id', 'N/A')}: {e}", exc_info=True)
        return None

def predict_task_due_dates_in_range(task, start_date, end_date):
    """
    Predicts specific dates a task might be due within a given date range.

    Args:
        task (MaintenanceTask): The task object.
        start_date (date): The start date of the planning period (inclusive).
        end_date (date): The end date of the planning period (inclusive).

    Returns:
        list: A list of date objects when the task is predicted to be due
              within the range. Returns an empty list if prediction isn't
              possible or no due dates fall within the range.
    """
    due_dates = []
    current_time = datetime.utcnow() # Use consistent naive UTC

    # --- Ensure dates are naive for comparison ---
    if isinstance(start_date, datetime): start_date = start_date.date()
    if isinstance(end_date, datetime): end_date = end_date.date()

    # --- Timezone handling for last_performed ---
    last_performed_dt = task.last_performed
    if last_performed_dt and last_performed_dt.tzinfo is not None:
        last_performed_dt = last_performed_dt.astimezone(timezone.utc).replace(tzinfo=None)

    # --- DAYS BASED TASKS ---
    if task.interval_type == 'days':
        if not last_performed_dt:
            # Cannot predict accurately if never performed based on days
            logging.debug(f"Task {task.id} ('days') never performed, cannot predict for plan.")
            return []

        next_due = last_performed_dt # Start from last performed
        while True:
            # Calculate the next potential due date
            next_due = next_due + timedelta(days=task.interval_value)
            next_due_date_only = next_due.date() # Compare dates only

            # Stop if we've gone past the planning period
            if next_due_date_only > end_date:
                break

            # Add if it falls within the planning period
            if next_due_date_only >= start_date:
                due_dates.append(next_due_date_only)

        logging.debug(f"Task {task.id} ('days'): Predicted due dates in range: {due_dates}")
        return due_dates

    # --- HOURS/KM BASED TASKS (Estimation) ---
    elif task.interval_type in ['hours', 'km']:
        # We use the *estimated* days until due from calculate_task_due_status
        # This is an approximation for planning purposes.
        try:
            status, due_info, calc_due_date, lp_info, nd_info, est_days_info = \
                calculate_task_due_status(task, current_time) # Use naive UTC

            # We only plot if the task is NOT overdue and has an estimated days calculation
            if status != "Overdue" and est_days_info and "~" in est_days_info and "days" in est_days_info:
                 # Extract the numeric part of the estimate
                 try:
                      # Find number like "~5.2 days" or "~10 days"
                      numeric_part = ''.join(filter(lambda x: x.isdigit() or x == '.', est_days_info.split('~')[-1].split(' ')[0]))
                      estimated_days_until = float(numeric_part)

                      # Calculate the estimated due date based on *today*
                      estimated_due_date = (current_time + timedelta(days=estimated_days_until)).date()

                      # If this single estimated date falls within the range, add it
                      if start_date <= estimated_due_date <= end_date:
                           logging.debug(f"Task {task.id} ('{task.interval_type}'): Estimated due date {estimated_due_date} falls in range.")
                           # NOTE: We only add ONE estimated date for usage-based tasks in this simplified model
                           return [estimated_due_date]
                      else:
                           logging.debug(f"Task {task.id} ('{task.interval_type}'): Estimated due date {estimated_due_date} OUTSIDE range.")
                           return []
                 except (ValueError, IndexError) as parse_err:
                      logging.warning(f"Task {task.id} ('{task.interval_type}'): Could not parse estimated days from '{est_days_info}': {parse_err}")
                      return []
            else:
                # Cannot reliably estimate or it's already overdue (handled elsewhere)
                 logging.debug(f"Task {task.id} ('{task.interval_type}'): Cannot estimate days or is overdue/error state ('{status}', '{est_days_info}'). Skipping for plan.")
                 return []

        except Exception as calc_err:
            logging.error(f"Error calculating status for Task {task.id} during planning: {calc_err}", exc_info=True)
            return []

    else: # Unknown interval type
        logging.warning(f"Task {task.id}: Unknown interval type '{task.interval_type}' for planning.")
        return []

# ==============================================================================
# === Maintenance Plan Generation Route ===
# ==============================================================================
@bp.route('/maintenance_plan/generate', methods=['POST'])
def generate_maintenance_plan():
    logging.debug("--- Received request to generate maintenance plan ---")
    try:
        year_str = request.form.get('year')
        month_str = request.form.get('month')

        if not year_str or not month_str:
            flash("Year and Month are required to generate the plan.", "warning")
            return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

        try:
            year = int(year_str)
            month = int(month_str)
            if not (1 <= month <= 12):
                raise ValueError("Month must be between 1 and 12.")
            # Basic check for sensible year range
            current_year = datetime.utcnow().year
            if not (current_year - 5 <= year <= current_year + 5):
                 raise ValueError("Year seems unreasonable.")

        except ValueError as ve:
            flash(f"Invalid year or month selected: {ve}", "warning")
            return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

        # Calculate Date Range for the selected month
        try:
            plan_start_date = date(year, month, 1)
            days_in_month = monthrange(year, month)[1]
            plan_end_date = date(year, month, days_in_month)
            month_name_full = plan_start_date.strftime("%B %Y")
        except ValueError:
             flash(f"Invalid date combination for {month}/{year}.", "danger")
             return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

        logging.info(f"Generating plan for {month_name_full} ({plan_start_date} to {plan_end_date})")

        # 1. Clear existing entries for this month/year
        logging.debug(f"Deleting existing plan entries for {year}-{month}...")
        deleted_count = MaintenancePlanEntry.query.filter_by(plan_year=year, plan_month=month).delete()
        db.session.commit() # Commit deletion before adding new ones
        logging.debug(f"Deleted {deleted_count} existing entries.")

        # 2. Fetch Tasks
        all_tasks = MaintenanceTask.query.all()
        generation_time = datetime.utcnow()
        new_entries_count = 0

        # 3. Predict and Save New Entries
        for task in all_tasks:
            predicted_dates = predict_task_due_dates_in_range(task, plan_start_date, plan_end_date)

            if predicted_dates:
                 is_estimate_flag = task.interval_type in ['hours', 'km'] # Mark estimates
                 for due_date_val in predicted_dates: # Renamed variable to avoid clash
                     entry = MaintenancePlanEntry(
                         equipment_id=task.equipment_id,
                         task_description=task.description, # Store description
                         planned_date=due_date_val,             # Store the specific date
                         interval_type=task.interval_type,  # Store type at generation
                         is_estimate=is_estimate_flag,
                         generated_at=generation_time,
                         plan_year=year,
                         plan_month=month,
                         task_id=task.id # Link back to original task
                     )
                     db.session.add(entry)
                     new_entries_count += 1

        # 4. Commit new entries
        db.session.commit()
        logging.info(f"Successfully generated plan for {month_name_full}. Added {new_entries_count} entries.")
        flash(f"Maintenance plan for {month_name_full} generated/updated successfully ({new_entries_count} entries).", "success")

    except Exception as e:
        db.session.rollback()
        logging.error(f"--- Error generating maintenance plan: {e} ---", exc_info=True)
        flash(f"An error occurred while generating the plan: {e}", "danger")

    return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

# ==============================================================================
# === Maintenance Plan PDF Route (Modified) ===
# ==============================================================================
@bp.route('/maintenance_plan/pdf') # Keep GET method
def maintenance_plan_pdf():
    if not WEASYPRINT_AVAILABLE:
        flash("PDF generation library (WeasyPrint) is not installed or configured correctly.", "danger")
        # Decide: redirect to dashboard or maybe a dedicated plan view page?
        return redirect(url_for('planned_maintenance.dashboard'))

    logging.debug("--- Request for Maintenance Plan PDF ---")
    try:
        # 1. Get Target Month/Year from Query Parameters
        try:
            # Default to current month if not specified
            default_date_val = date.today() # Renamed variable
            year = request.args.get('year', default=default_date_val.year, type=int)
            month = request.args.get('month', default=default_date_val.month, type=int)

            if not (1 <= month <= 12): raise ValueError("Month out of range")
            current_year = datetime.utcnow().year
            if not (current_year - 5 <= year <= current_year + 5): raise ValueError("Year out of range")

        except (ValueError, TypeError):
            flash("Invalid or missing year/month for PDF plan. Defaulting to current month.", "warning")
            default_date_val = date.today() # Renamed variable
            year = default_date_val.year
            month = default_date_val.month

        # Calculate Date Range and Month Name
        try:
            plan_start_date = date(year, month, 1)
            days_in_month = monthrange(year, month)[1]
            plan_end_date = date(year, month, days_in_month)
            month_name = plan_start_date.strftime("%B %Y")
        except ValueError:
             flash(f"Cannot generate PDF for invalid date {month}/{year}.", "danger")
             return redirect(url_for('planned_maintenance.dashboard'))

        logging.info(f"Generating PDF for plan: {month_name}")

        # 2. Fetch Equipment List (for rows)
        # Eager load equipment to avoid N+1 queries in the template if accessing eq details
        equipment_list = Equipment.query.order_by(Equipment.code).all()


        # 3. Fetch Stored Plan Entries for the selected month/year
        logging.debug(f"Querying stored plan entries for {year}-{month}...")
        plan_entries = MaintenancePlanEntry.query.filter(
            MaintenancePlanEntry.plan_year == year,
            MaintenancePlanEntry.plan_month == month
        ).options(
            db.joinedload(MaintenancePlanEntry.equipment) # Eager load equipment data if needed
        ).order_by(
            MaintenancePlanEntry.equipment_id,
            MaintenancePlanEntry.planned_date
        ).all()
        logging.debug(f"Found {len(plan_entries)} stored entries.")

        # 4. Structure Data for the Template
        plan_data = {} # Structure: {eq_id: {date_obj: [task_label1, task_label2]}}
        generation_info = "Plan not generated for this period." # Default message
        if plan_entries:
            # Find the latest generation time for this batch
            latest_generation_time = max(entry.generated_at for entry in plan_entries if entry.generated_at)
            generation_info = f"Plan generated on: {latest_generation_time.strftime('%Y-%m-%d %H:%M:%S UTC') if latest_generation_time else 'N/A'}"

            for entry in plan_entries:
                eq_id = entry.equipment_id
                due_date_val = entry.planned_date # Already a date object

                if eq_id not in plan_data:
                    plan_data[eq_id] = {}
                if due_date_val not in plan_data[eq_id]:
                    plan_data[eq_id][due_date_val] = []

                task_label = entry.task_description
                if entry.is_estimate:
                    task_label += " (Est.)"
                plan_data[eq_id][due_date_val].append(task_label)

        # Generate list of dates for the header
        dates_in_month = [plan_start_date + timedelta(days=d) for d in range(days_in_month)]

        # 5. Render HTML Template
        logging.debug("Rendering maintenance_plan_template.html for PDF")
        html_string = render_template(
            'maintenance_plan_template.html', # Use the same template
            title=f"Maintenance Plan - {month_name}",
            month_name=month_name,
            equipment_list=equipment_list,
            dates_in_month=dates_in_month,
            plan_data=plan_data, # Pass the structured data from the DB
            date_today=date.today(),
            generation_info=generation_info # Pass generation info
        )

        # 6. Generate PDF using WeasyPrint
        logging.debug("Generating PDF with WeasyPrint...")
        pdf_file = HTML(string=html_string).write_pdf()
        logging.debug("PDF generation complete.")

        # 7. Create Response
        response = make_response(pdf_file)
        response.headers['Content-Type'] = 'application/pdf'
        response.headers['Content-Disposition'] = f'attachment; filename="maintenance_plan_{year}_{month:02d}.pdf"'
        return response

    except Exception as e:
        logging.error(f"--- Error generating maintenance plan PDF: {e} ---", exc_info=True)
        flash(f"An error occurred while generating the PDF plan: {e}", "danger")
        return redirect(url_for('planned_maintenance.dashboard'))

# ==============================================================================
# === NEW Route: List Generated Maintenance Plans ===
# ==============================================================================

@bp.route('/maintenance_plan/detail', methods=['GET'])
def maintenance_plan_detail_view():
    """
    Displays the detailed maintenance plan for a given month and year,
    including planned tasks, completed job cards, and 'To Do' job cards due
    within that month. Allows filtering by equipment type.
    """
    logging.debug("--- Request for Maintenance Plan Detail View ---")
    try:
        # 1. Get Target Month/Year and Filters from Query Parameters
        try:
            year = request.args.get('year', type=int)
            month = request.args.get('month', type=int)
            # Get Equipment Type Filter
            equipment_type_filter = request.args.get('equipment_type', None) # Get filter value, default to None

            if not year or not month:
                flash("Year and Month are required to view plan details.", "warning")
                return redirect(url_for('planned_maintenance.maintenance_plan_list_view'))

            # Basic date validation
            if not (1 <= month <= 12): raise ValueError("Month out of range")
            current_year_check = date.today().year
            # Allow viewing plans for a reasonable range around the current year
            if not (current_year_check - 10 <= year <= current_year_check + 10): raise ValueError("Year out of reasonable range")

        except (ValueError, TypeError):
            flash("Invalid year or month specified.", "warning")
            return redirect(url_for('planned_maintenance.maintenance_plan_list_view'))

        # 2. Calculate Date Range and Month Name
        try:
            plan_start_date = date(year, month, 1)
            days_in_month = monthrange(year, month)[1]
            plan_end_date = date(year, month, days_in_month)
            month_name_str = plan_start_date.strftime("%B %Y")

            # Define datetime ranges for filtering Job Cards based on DB column types
            # For Completed JCs (filter by end_datetime which is DateTime)
            month_start_dt_completion = datetime.combine(plan_start_date, time.min)
            month_end_dt_completion = datetime.combine(plan_end_date, time.max)
            # For ToDo JCs (filter by due_date which is DateTime)
            month_start_dt_due_date = datetime.combine(plan_start_date, time.min)
            month_end_dt_due_date = datetime.combine(plan_end_date, time.max)

        except ValueError:
             flash(f"Cannot display plan details for invalid date {month}/{year}.", "danger")
             return redirect(url_for('planned_maintenance.maintenance_plan_list_view'))

        logging.info(f"Displaying plan detail view for: {month_name_str}, Type Filter: '{equipment_type_filter}'")

        # 3. Fetch Equipment Types for Filter Dropdown
        equipment_types_query = db.session.query(Equipment.type).distinct().order_by(Equipment.type).all()
        equipment_types = [t[0] for t in equipment_types_query if t[0]] # Get non-empty types

        # 4. Fetch Data: Planned Entries, Completed JCs, ToDo JCs

        # Fetch Planned Entries for the period (with filtering)
        logging.debug(f"Querying stored plan entries for {year}-{month}...")
        plan_entries_query = MaintenancePlanEntry.query.filter(
            MaintenancePlanEntry.plan_year == year,
            MaintenancePlanEntry.plan_month == month
        ).options(
             db.joinedload(MaintenancePlanEntry.equipment), # Eager load equipment
             db.joinedload(MaintenancePlanEntry.original_task) # Eager load original task
        )
        # Apply Equipment Type Filter to Planned Entries
        if equipment_type_filter and equipment_type_filter != 'All':
            plan_entries_query = plan_entries_query.join(Equipment, MaintenancePlanEntry.equipment_id == Equipment.id)\
                                                 .filter(Equipment.type == equipment_type_filter)
        plan_entries = plan_entries_query.order_by(MaintenancePlanEntry.planned_date).all()
        logging.debug(f"Found {len(plan_entries)} planned entries matching filters.")

        # Fetch Completed Job Cards for the period (with filtering)
        logging.debug(f"Querying completed job cards ended between {month_start_dt_completion} and {month_end_dt_completion}...")
        completed_job_cards_query = JobCard.query.filter(
            JobCard.status == 'Done',
            JobCard.end_datetime.isnot(None), # Ensure completion time exists
            JobCard.end_datetime >= month_start_dt_completion, # Completion time within the month
            JobCard.end_datetime <= month_end_dt_completion
        ).options(
             db.joinedload(JobCard.equipment_ref) # Eager load equipment
        )
        # Apply Equipment Type Filter to Completed Job Cards
        if equipment_type_filter and equipment_type_filter != 'All':
            completed_job_cards_query = completed_job_cards_query.join(Equipment, JobCard.equipment_id == Equipment.id)\
                                                               .filter(Equipment.type == equipment_type_filter)
        completed_job_cards = completed_job_cards_query.order_by(JobCard.end_datetime).all()
        logging.debug(f"Found {len(completed_job_cards)} completed job cards matching filters.")

        # Fetch "To Do" Job Cards Due This Month (with filtering)
        logging.debug(f"Querying 'To Do' job cards due between {month_start_dt_due_date} and {month_end_dt_due_date}...")
        todo_job_cards_query = JobCard.query.filter(
            JobCard.status == 'To Do',
            JobCard.due_date.isnot(None), # Ensure due date exists
            JobCard.due_date >= month_start_dt_due_date, # Due date within the month
            JobCard.due_date <= month_end_dt_due_date
        ).options(
             db.joinedload(JobCard.equipment_ref) # Eager load equipment
        )
        # Apply Equipment Type Filter to "To Do" Job Cards
        if equipment_type_filter and equipment_type_filter != 'All':
            todo_job_cards_query = todo_job_cards_query.join(Equipment, JobCard.equipment_id == Equipment.id)\
                                                               .filter(Equipment.type == equipment_type_filter)
        todo_job_cards = todo_job_cards_query.order_by(JobCard.due_date).all()
        logging.debug(f"Found {len(todo_job_cards)} 'To Do' job cards matching filters.")

        # 5. Structure Data by Day
        # Initialize structure to hold planned, completed, and todo items for each day
        daily_plan_data = defaultdict(lambda: {'planned': [], 'completed': [], 'todo': []})
        generation_info = "Plan not generated or no tasks found for this period/filter."

        # Process planned entries
        if plan_entries:
            # Determine generation info from the *first* found entry
            first_entry_gen_time = plan_entries[0].generated_at
            if first_entry_gen_time:
                 # Ensure naive UTC for display consistency
                 if first_entry_gen_time.tzinfo is not None:
                     first_entry_gen_time = first_entry_gen_time.astimezone(timezone.utc).replace(tzinfo=None)
                 generation_info = f"Plan generated on: {first_entry_gen_time.strftime('%Y-%m-%d %H:%M UTC')}"
            else:
                 generation_info = "Plan generated (time unknown)"

            for entry in plan_entries:
                day_date = entry.planned_date # Key is the date object
                # Determine if legal from original task
                is_legal = entry.original_task.is_legal_compliance if entry.original_task else False
                daily_plan_data[day_date]['planned'].append({
                    'entry': entry,
                    'equipment': entry.equipment, # Pass equipment object
                    'is_legal': is_legal,
                    'is_estimate': entry.is_estimate,
                    'description': entry.task_description
                })

        # Process completed job cards
        for jc in completed_job_cards:
            # Group by the DATE part of the end_datetime
            if jc.end_datetime:
                 day_date = jc.end_datetime.date() # Key is the date object
                 # Determine if legal from job number prefix
                 is_legal = jc.job_number and jc.job_number.startswith('LC-')
                 daily_plan_data[day_date]['completed'].append({
                    'job_card': jc,
                    'equipment': jc.equipment_ref, # Pass equipment object
                    'is_legal': is_legal,
                    'description': jc.description
                 })
            else:
                logging.warning(f"Completed Job Card {jc.job_number} has no end_datetime, cannot place in daily view.")

        # Process "To Do" job cards
        for jc in todo_job_cards:
            # Group by the DATE part of the due_date
            if jc.due_date:
                 day_date = jc.due_date.date() # Key is the date object
                 is_legal = jc.job_number and jc.job_number.startswith('LC-')
                 daily_plan_data[day_date]['todo'].append({
                    'job_card': jc,
                    'equipment': jc.equipment_ref, # Pass equipment object
                    'is_legal': is_legal,
                    'description': jc.description
                 })
            else:
                 # This case is excluded by the query filter (due_date.isnot(None))
                 logging.warning(f"'To Do' Job Card {jc.job_number} has no due_date despite query filter, cannot place in daily view.")

        # 6. Sort the daily data by date for display order
        sorted_daily_plan_data = dict(sorted(daily_plan_data.items()))

        # 7. Render Detail Template with the structured data
        logging.debug("Rendering maintenance_plan_detail.html (daily view)")
        return render_template(
            'maintenance_plan_detail.html', # Use the updated template
            title=f"Plan Details - {month_name_str}",
            month_name=month_name_str,
            current_year=year,
            current_month=month,
            daily_plan_data=sorted_daily_plan_data, # Pass the data structure containing planned, completed, and todo
            generation_info=generation_info,
            equipment_types=equipment_types, # Pass types for filter dropdown
            current_equipment_type_filter=equipment_type_filter, # Pass current filter value
            WEASYPRINT_AVAILABLE=WEASYPRINT_AVAILABLE # Pass PDF availability flag
        )

    # Global Error Handling for the route
    except Exception as e:
        logging.error(f"--- Error loading maintenance plan detail view: {e} ---", exc_info=True)
        flash(f"An error occurred while loading the plan details: {e}", "danger")
        # Redirect back to the list view on error
        return redirect(url_for('planned_maintenance.maintenance_plan_list_view'))

# ==============================================================================
# === NEW Route: Print Plan Details (Daily Layout) ===
# ==============================================================================

# tkr_system/app/routes.py

# ... (Keep all other imports at the top of the file) ...
from flask import render_template, request, redirect, url_for, flash, make_response, session, jsonify
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone, date, time
from dateutil.parser import parse as parse_datetime
from dateutil.relativedelta import relativedelta
from calendar import monthrange
from io import BytesIO
from app.planned_maintenance import bp
from app import db
from app.models import (
    Equipment, JobCard, Checklist, StockTransaction,
    JobCardPart, Part, MaintenanceTask, UsageLog,
    MaintenancePlanEntry
)
from itertools import zip_longest
from sqlalchemy import desc, func, extract, and_, or_
import traceback
from collections import defaultdict
import logging # Make sure logging is imported

# ... (Keep Weasyprint setup, helper functions like generate_whatsapp_share_url, etc.) ...
# ... (Keep the maintenance_plan_detail_view function as rewritten previously) ...


# ==============================================================================
# === Print Plan Details (Daily Layout) (Rewritten with ToDo JCs) ===
# ==============================================================================
@bp.route('/maintenance_plan/print_detail', methods=['GET'])
def print_maintenance_plan_detail():
    """
    Generates an HTML page suitable for printing, displaying the maintenance
    plan details for a given month and year (planned tasks, completed jobs,
    and 'To Do' jobs due in the month). Allows filtering by equipment type.
    """
    logging.debug("--- Request for Maintenance Plan Detail Print View ---")
    try:
        # 1. Get Target Month/Year and Filters from Query Parameters
        try:
            year = request.args.get('year', type=int)
            month = request.args.get('month', type=int)
            equipment_type_filter = request.args.get('equipment_type', None)

            if not year or not month:
                flash("Year and Month are required to print plan details.", "warning")
                # Redirect back to the detail view which can handle missing params better
                return redirect(url_for('planned_maintenance.maintenance_plan_detail_view'))

            # Basic date validation
            if not (1 <= month <= 12): raise ValueError("Month out of range")
            current_year_check = date.today().year
            if not (current_year_check - 10 <= year <= current_year_check + 10): raise ValueError("Year out of reasonable range")

        except (ValueError, TypeError):
            flash("Invalid year or month specified for printing.", "warning")
            # Redirect back to the detail view
            detail_url = url_for('planned_maintenance.maintenance_plan_detail_view', year=request.args.get('year'), month=request.args.get('month'), equipment_type=request.args.get('equipment_type'))
            return redirect(detail_url)

        # 2. Calculate Date Range and Month Name
        try:
            plan_start_date = date(year, month, 1)
            days_in_month = monthrange(year, month)[1]
            plan_end_date = date(year, month, days_in_month)
            month_name_str = plan_start_date.strftime("%B %Y")

            # Define datetime ranges for filtering Job Cards
            month_start_dt_completion = datetime.combine(plan_start_date, time.min)
            month_end_dt_completion = datetime.combine(plan_end_date, time.max)
            month_start_dt_due_date = datetime.combine(plan_start_date, time.min)
            month_end_dt_due_date = datetime.combine(plan_end_date, time.max)

        except ValueError:
             flash(f"Cannot generate print view for invalid date {month}/{year}.", "danger")
             # Redirect back to the detail view
             detail_url = url_for('planned_maintenance.maintenance_plan_detail_view', year=year, month=month, equipment_type=equipment_type_filter)
             return redirect(detail_url)

        logging.info(f"Generating Print view for: {month_name_str}, Type Filter: '{equipment_type_filter}'")

        # 3. Fetch Data (mirroring the detail view logic)

        # Fetch Planned Entries (with filtering)
        logging.debug(f"Print: Querying stored plan entries for {year}-{month}...")
        plan_entries_query = MaintenancePlanEntry.query.filter(
            MaintenancePlanEntry.plan_year == year,
            MaintenancePlanEntry.plan_month == month
        ).options(
            db.joinedload(MaintenancePlanEntry.equipment),
            db.joinedload(MaintenancePlanEntry.original_task)
        )
        if equipment_type_filter and equipment_type_filter != 'All':
            plan_entries_query = plan_entries_query.join(Equipment, MaintenancePlanEntry.equipment_id == Equipment.id)\
                                                 .filter(Equipment.type == equipment_type_filter)
        plan_entries = plan_entries_query.order_by(MaintenancePlanEntry.planned_date).all()
        logging.debug(f"Print: Found {len(plan_entries)} planned entries.")

        # Fetch Completed Job Cards (with filtering)
        logging.debug(f"Print: Querying completed job cards ended between {month_start_dt_completion} and {month_end_dt_completion}...")
        completed_job_cards_query = JobCard.query.filter(
            JobCard.status == 'Done',
            JobCard.end_datetime.isnot(None),
            JobCard.end_datetime >= month_start_dt_completion,
            JobCard.end_datetime <= month_end_dt_completion
        ).options(db.joinedload(JobCard.equipment_ref))
        if equipment_type_filter and equipment_type_filter != 'All':
            completed_job_cards_query = completed_job_cards_query.join(Equipment, JobCard.equipment_id == Equipment.id)\
                                                               .filter(Equipment.type == equipment_type_filter)
        completed_job_cards = completed_job_cards_query.order_by(JobCard.end_datetime).all()
        logging.debug(f"Print: Found {len(completed_job_cards)} completed job cards.")

        # Fetch "To Do" Job Cards Due This Month (with filtering)
        logging.debug(f"Print: Querying 'To Do' job cards due between {month_start_dt_due_date} and {month_end_dt_due_date}...")
        todo_job_cards_query = JobCard.query.filter(
            JobCard.status == 'To Do',
            JobCard.due_date.isnot(None),
            JobCard.due_date >= month_start_dt_due_date,
            JobCard.due_date <= month_end_dt_due_date
        ).options(db.joinedload(JobCard.equipment_ref))
        if equipment_type_filter and equipment_type_filter != 'All':
            todo_job_cards_query = todo_job_cards_query.join(Equipment, JobCard.equipment_id == Equipment.id)\
                                                               .filter(Equipment.type == equipment_type_filter)
        todo_job_cards = todo_job_cards_query.order_by(JobCard.due_date).all()
        logging.debug(f"Print: Found {len(todo_job_cards)} 'To Do' job cards.")

        # 4. Structure Data by Day (mirroring the detail view logic)
        daily_plan_data = defaultdict(lambda: {'planned': [], 'completed': [], 'todo': []})

        # Process planned entries
        for entry in plan_entries:
            day_date = entry.planned_date
            is_legal = entry.original_task.is_legal_compliance if entry.original_task else False
            daily_plan_data[day_date]['planned'].append({
                'entry': entry, 'equipment': entry.equipment, 'is_legal': is_legal,
                'is_estimate': entry.is_estimate, 'description': entry.task_description
            })

        # Process completed job cards
        for jc in completed_job_cards:
            if jc.end_datetime:
                 day_date = jc.end_datetime.date()
                 is_legal = jc.job_number and jc.job_number.startswith('LC-')
                 daily_plan_data[day_date]['completed'].append({
                    'job_card': jc, 'equipment': jc.equipment_ref, 'is_legal': is_legal,
                    'description': jc.description
                 })

        # Process "To Do" job cards
        for jc in todo_job_cards:
            if jc.due_date:
                 day_date = jc.due_date.date()
                 is_legal = jc.job_number and jc.job_number.startswith('LC-')
                 daily_plan_data[day_date]['todo'].append({
                    'job_card': jc,
                    'equipment': jc.equipment_ref,
                    'is_legal': is_legal,
                    'description': jc.description
                 })

        # Sort the daily data by date
        sorted_daily_plan_data = dict(sorted(daily_plan_data.items()))

        # 5. Render Print Template
        logging.debug("Rendering maintenance_plan_print_detail.html")
        return render_template(
            'maintenance_plan_print_detail.html', # The print-specific template
            title=f"Print Plan - {month_name_str}",
            month_name=month_name_str,
            daily_plan_data=sorted_daily_plan_data, # Pass the structured data
            equipment_type_filter=equipment_type_filter # Pass filter info to display on printout
        )

    # Global Error Handling for the route
    except Exception as e:
        logging.error(f"--- Error generating maintenance plan print view: {e} ---", exc_info=True)
        flash(f"An error occurred while generating the printable plan: {e}", "danger")
        # Redirect back to the detail view on error, preserving filters
        detail_url = url_for('planned_maintenance.maintenance_plan_detail_view',
                             year=request.args.get('year'),
                             month=request.args.get('month'),
                             equipment_type=request.args.get('equipment_type'))
        return redirect(detail_url)

# ... (Keep the rest of your routes.py file) ...

# ==============================================================================
# === Dashboard Route ===
# ==============================================================================

@bp.route('/maintenance_plan', methods=['GET']) # Route for the list view
def maintenance_plan_list_view():
    """Displays a list of generated maintenance plans and the form to generate new ones."""
    logging.debug("--- Request for Maintenance Plan List View ---")
    try:
        # Query distinct year/month pairs for which plan entries exist
        # Also get the latest generation timestamp for each distinct period
        existing_plans_query = db.session.query(
            MaintenancePlanEntry.plan_year,
            MaintenancePlanEntry.plan_month,
            func.max(MaintenancePlanEntry.generated_at).label('last_generated')
        ).group_by(
            MaintenancePlanEntry.plan_year,
            MaintenancePlanEntry.plan_month
        ).order_by(
            desc(MaintenancePlanEntry.plan_year),
            desc(MaintenancePlanEntry.plan_month)
        ).all()

        existing_plans = []
        for year, month, generated_at in existing_plans_query:
            # Format the generation timestamp (handle potential None and timezones)
            generated_str = "Unknown"
            if generated_at:
                # Ensure it's naive UTC before formatting
                generated_at_naive = generated_at
                if generated_at.tzinfo is not None:
                    generated_at_naive = generated_at.astimezone(timezone.utc).replace(tzinfo=None)
                generated_str = generated_at_naive.strftime('%Y-%m-%d %H:%M UTC')

            # Get the month name
            try:
                 month_name = date(year, month, 1).strftime('%B')
            except ValueError:
                 month_name = "Invalid Month" # Should not happen if data is valid

            existing_plans.append({
                'year': year,
                'month': month,
                'month_name': month_name,
                'generated_at': generated_str
            })

        # Get current year/month for the form defaults
        today_val = date.today()
        current_year = today_val.year
        current_month = today_val.month

        logging.debug(f"Found {len(existing_plans)} existing plan periods.")

        # Render the list template
        return render_template(
            'maintenance_plan_list.html', # The template for this view
            title="Maintenance Plans",
            existing_plans=existing_plans,
            current_year=current_year,
            current_month=current_month,
            date_today=today_val, # Pass today's date for form defaults if needed
            WEASYPRINT_AVAILABLE = WEASYPRINT_AVAILABLE # Pass PDF flag for button enable/disable
        )

    except Exception as e:
        logging.error(f"--- Error loading maintenance plan list view: {e} ---", exc_info=True)
        flash(f"An error occurred while loading the plan list: {e}", "danger")
        # Render the template even on error, but show an error message
        return render_template('maintenance_plan_list.html',
                               title="Maintenance Plans - Error",
                               error=f"An unexpected error occurred: {e}",
                               existing_plans=[],
                               current_year=date.today().year,
                               current_month=date.today().month,
                               date_today=date.today(),
                               WEASYPRINT_AVAILABLE = WEASYPRINT_AVAILABLE
                              )

@bp.route('/')
def dashboard():
    logging.debug("--- Entering dashboard route ---")
    try:
        # 1. Fetch Equipment Data (for status and modals)
        all_equipment_list = Equipment.query.order_by(Equipment.type, Equipment.code).all()
        equipment_for_display = [eq for eq in all_equipment_list if eq.status != 'Sold']
        # Get today's date object and string representation
        today_date_obj = date.today() # Use date.today() for date object
        yesterday_date_obj = today_date_obj - timedelta(days=1)
        today_str = today_date_obj.strftime('%Y-%m-%d') # String for comparisons if needed

        logging.debug("Fetching latest usage/checklist for equipment...")
        for eq in equipment_for_display:
            # Get latest usage and checklist for each equipment
            latest_usage = UsageLog.query.filter_by(equipment_id=eq.id).order_by(desc(UsageLog.log_date)).first()
            latest_checklist = Checklist.query.filter_by(equipment_id=eq.id).order_by(desc(Checklist.check_date)).first()
            # Store directly on the object for easy access in the template
            eq.latest_usage = latest_usage
            eq.latest_checklist = latest_checklist
            # Pre-calculate checked_today flag
            last_check_date_obj = eq.latest_checklist.check_date if eq.latest_checklist else None
            # Ensure comparison is date-only if check_date is datetime
            last_check_date_str = last_check_date_obj.strftime('%Y-%m-%d') if last_check_date_obj else None
            eq.checked_today = last_check_date_str == today_str

        # 2. Fetch Open Job Cards
        logging.debug("Fetching open job cards...")
        open_job_cards = JobCard.query.filter(
            JobCard.status == 'To Do'
        ).options(
            db.joinedload(JobCard.equipment_ref) # Eager load equipment details
        ).order_by(
            JobCard.due_date.asc().nullslast(), # Sort by due date (earliest first, None last)
            JobCard.id.desc() # Secondary sort by ID
        ).limit(20).all() # Limit for dashboard view, adjust as needed

        # --- Generate WhatsApp URLs for Job Cards ---
        logging.debug(f"Generating WhatsApp URLs for {len(open_job_cards)} open job cards...")
        for jc in open_job_cards:
            jc.whatsapp_share_url = generate_whatsapp_share_url(jc)

        # 3. Process Maintenance Tasks
        logging.debug("Processing maintenance tasks for status...")
        current_time = datetime.utcnow() # Use consistent naive UTC time
        logging.debug(f"Dashboard processing tasks using current_time (naive UTC): {current_time}")

        # 3.1 Process regular maintenance tasks (is_legal_compliance=False or NULL)
        all_maintenance_tasks = MaintenanceTask.query.filter(
            MaintenanceTask.is_legal_compliance.is_(False)
        ).options(
            db.joinedload(MaintenanceTask.equipment_ref)
        ).order_by(
             MaintenanceTask.equipment_id,
             MaintenanceTask.id
        ).all()

        # 3.2 Process legal compliance tasks (is_legal_compliance=True)
        all_legal_tasks = MaintenanceTask.query.join(Equipment).filter(
            MaintenanceTask.is_legal_compliance.is_(True),
            Equipment.status == 'Operational'  # Only consider operational equipment for legal tasks
        ).options(
            db.joinedload(MaintenanceTask.equipment_ref)
        ).order_by(
             MaintenanceTask.equipment_id,
             MaintenanceTask.id
        ).all()

        tasks_with_status_filtered = [] # For maintenance tasks
        legal_tasks_with_status_filtered = [] # For legal compliance tasks

        logging.debug("Calculating task statuses for dashboard...")
        for task in all_maintenance_tasks:
            if not task.equipment_ref or task.equipment_ref.status == 'Sold':
                logging.debug(f"Skipping task {task.id} for sold equipment {getattr(task.equipment_ref, 'code', 'N/A')}")
                continue # Skip tasks for sold equipment

            # Calculate detailed status using the helper function
            status, due_info, due_date_val, last_performed_info, next_due_info, estimated_days_info = \
                calculate_task_due_status(task, current_time) # Assumes this helper exists and works

            # Assign calculated attributes to the task object for template use
            task.due_status = status
            task.due_info = due_info
            task.due_date = due_date_val # This is the datetime object or None
            task.last_performed_info = last_performed_info
            task.next_due_info = next_due_info
            task.estimated_days_info = estimated_days_info

            # Filter tasks for the dashboard view
            include_task = False
            if isinstance(status, str):
                # Check for keywords indicating urgency or warning
                if "Overdue" in status or "Due Soon" in status or "Warning" in status:
                   include_task = True
            # Add elif for non-string status types if calculate_task_due_status can return them

            if include_task:
                tasks_with_status_filtered.append(task)

        logging.debug(f"Filtered tasks list has {len(tasks_with_status_filtered)} tasks")
        for task in tasks_with_status_filtered:
            logging.debug(f"Filtered task included: ID {task.id} - Status: {task.due_status}")

        # Process legal compliance tasks
        logging.debug("Calculating legal compliance task statuses for dashboard...")
        for task in all_legal_tasks:
            if not task.equipment_ref or task.equipment_ref.status == 'Sold':
                logging.debug(f"Skipping legal task {task.id} for sold equipment {getattr(task.equipment_ref, 'code', 'N/A')}")
                continue # Skip tasks for sold equipment

            # Calculate detailed status using the helper function
            status, due_info, due_date_val, last_performed_info, next_due_info, estimated_days_info = \
                calculate_task_due_status(task, current_time)

            # Assign calculated attributes to the task object for template use
            task.due_status = status
            task.due_info = due_info
            task.due_date = due_date_val
            task.last_performed_info = last_performed_info
            task.next_due_info = next_due_info
            task.estimated_days_info = estimated_days_info

            # Filter tasks for the dashboard view
            include_task = False
            if isinstance(status, str):
                # Check for keywords indicating urgency or warning
                if "Overdue" in status or "Due Soon" in status or "Warning" in status:
                   include_task = True

            if include_task:
                legal_tasks_with_status_filtered.append(task)
                
        logging.debug("=== FILTERED TASKS DEBUG ===")
        logging.debug(f"tasks_with_status_filtered count: {len(tasks_with_status_filtered)}")
        for idx, task in enumerate(tasks_with_status_filtered):
            logging.debug(f"Task #{idx+1} - ID: {task.id}, Status: {task.due_status}, Description: {task.description}")

        logging.debug("=== LEGAL TASKS DEBUG ===")
        logging.debug(f"legal_tasks_with_status_filtered count: {len(legal_tasks_with_status_filtered)}")
        for idx, task in enumerate(legal_tasks_with_status_filtered):
            logging.debug(f"Legal Task #{idx+1} - ID: {task.id}, Status: {task.due_status}, Description: {task.description}")
# Sort both task lists based on severity
        sort_order_map = {
            'Overdue': 1, 'Due Soon': 2, 'Warning': 3,
            'Error': 4, 'Unknown': 99
        }
        default_sort_prio = 100

        def get_sort_key(task_item):
            status_str = getattr(task_item, 'due_status', 'Unknown')
            if not isinstance(status_str, str): status_str = 'Unknown'
            # Extract the primary status keyword for sorting
            prio = default_sort_prio
            for key, sort_prio in sort_order_map.items():
                if key in status_str: # Check if keyword is present
                    prio = sort_prio
                    break # Use first match
            # Optional secondary sort key (e.g., by due date if available)
            secondary_key = task_item.due_date if task_item.due_date else datetime.max.replace(tzinfo=None)
            return (prio, secondary_key)

        try:
            tasks_with_status_filtered.sort(key=get_sort_key)
            legal_tasks_with_status_filtered.sort(key=get_sort_key)
            logging.debug("Sorted filtered tasks for dashboard successfully.")
        except Exception as task_sort_exc:
            logging.error(f"Error sorting filtered tasks: {task_sort_exc}", exc_info=True)
        
        logging.debug(f"Finished calculating task statuses. {len(tasks_with_status_filtered)} tasks included for dashboard display.")

        # 4. Prepare Recent Activities
        logging.debug("Aggregating recent activities...")
        recent_activities = []
        limit_count = 25 # Show more activities in the dedicated tab

        # Fetch recent items 
        try:
            latest_checklists = Checklist.query.options(db.joinedload(Checklist.equipment_ref)).order_by(Checklist.check_date.desc()).limit(limit_count).all()
            recent_usage_logs = UsageLog.query.options(db.joinedload(UsageLog.equipment_ref)).order_by(UsageLog.log_date.desc()).limit(limit_count).all()
            recent_job_cards = JobCard.query.options(db.joinedload(JobCard.equipment_ref)).order_by(JobCard.id.desc()).limit(limit_count * 2).all()

            activity_list = []

            for cl in latest_checklists:
                ts = cl.check_date
                if ts:
                    if ts.tzinfo is not None: ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
                    activity_list.append({
                        'type': 'checklist', 'timestamp': ts,
                        'description': f"Checklist for {cl.equipment_ref.code}: {cl.status}",
                        'details': cl
                    })

            for ul in recent_usage_logs:
                ts = ul.log_date
                if ts:
                    if ts.tzinfo is not None: ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
                    activity_list.append({
                        'type': 'usage', 'timestamp': ts,
                        'description': f"Usage for {ul.equipment_ref.code}: {ul.usage_value}",
                        'details': ul
                    })

            for jc in recent_job_cards:
                # Creation Activity
                create_ts = jc.start_datetime
                if create_ts:
                     if create_ts.tzinfo is not None: create_ts = create_ts.astimezone(timezone.utc).replace(tzinfo=None)
                     activity_list.append({
                         'type': 'job_card_created', 'timestamp': create_ts,
                         'description': f"Job Card {jc.job_number} created for {jc.equipment_ref.code} - {jc.equipment_ref.name}",
                         'details': jc
                     })

                # Completion Activity
                if jc.status == 'Done' and jc.end_datetime:
                    complete_ts = jc.end_datetime
                    if complete_ts.tzinfo is not None: complete_ts = complete_ts.astimezone(timezone.utc).replace(tzinfo=None)
                    activity_list.append({
                         'type': 'job_card_completed', 'timestamp': complete_ts,
                         'description': f"Job Card {jc.job_number} completed for {jc.equipment_ref.code} - {jc.equipment_ref.name}",
                         'details': jc
                     })

            # Sort the combined list by timestamp (descending)
            valid_activities = [act for act in activity_list if act.get('timestamp') is not None]
            if len(valid_activities) != len(activity_list):
                logging.warning("Some aggregated activities were missing timestamps.")

            valid_activities.sort(key=lambda x: x['timestamp'], reverse=True)
            recent_activities = valid_activities[:limit_count] # Limit the final list
            logging.debug(f"Sorting recent activities successful. Showing {len(recent_activities)} items.")

        except Exception as activity_err:
             logging.error(f"Error aggregating recent activities: {activity_err}", exc_info=True)
             flash("Could not load recent activity.", "warning")
             recent_activities = [] # Ensure empty list on error

        # 5. Render Template
        logging.debug("--- Rendering pm_dashboard.html with tab based interface ---")
        return render_template(
            'pm_dashboard.html',
            title='PM Dashboard',
            equipment=equipment_for_display,
            all_equipment=all_equipment_list,
            job_cards=open_job_cards,           # For open job cards table (with whatsapp_share_url)
            tasks=tasks_with_status_filtered,   # For upcoming tasks table (with status/due info)
            legal_tasks=legal_tasks_with_status_filtered,
            recent_activities=recent_activities, # For activity feed
            today=today_date_obj,                # Pass the actual date object for template logic
            yesterday=yesterday_date_obj
        )

    # --- Global Error Handling for the Route ---
    except Exception as e:
        # Log the full traceback for ANY exception in the dashboard route
        logging.error(f"--- Unhandled exception in dashboard route: {e} ---", exc_info=True)
        flash(f"An critical error occurred loading the dashboard: {e}. Please check logs and contact support.", "danger")
        # Return the template in a safe 'error' state
        return render_template('pm_dashboard.html',
                               title='PM Dashboard - Error',
                               error=True, # Flag for the template to show an error message
                               equipment=[], # Provide empty lists to prevent template errors
                               all_equipment=[],
                               job_cards=[],
                               tasks=[],
                               legal_tasks=[],
                               recent_activities=[],
                               today=date.today(),
                               yesterday=date.today() - timedelta(days=1)) # Provide default date objects on error

@bp.route('/equipment')
def equipment_list():
    """Displays a list of all equipment, grouped by type."""
    try:
        # Fetch all equipment, ordered primarily by type, then code
        all_equipment_query = Equipment.query.order_by(Equipment.type, Equipment.code).all()

        # Group equipment by type and calculate summaries
        grouped_equipment = defaultdict(lambda: {'items': [], 'total': 0, 'operational': 0})

        for item in all_equipment_query:
            group_key = item.type if item.type else "Uncategorized" # Handle potential None type
            grouped_equipment[group_key]['items'].append(item)
            grouped_equipment[group_key]['total'] += 1
            # Count how many are 'Operational' (case-insensitive check might be safer)
            if item.status and item.status.lower() == 'operational':
                grouped_equipment[group_key]['operational'] += 1

        # Sort the groups by type name for consistent order
        sorted_grouped_equipment = dict(sorted(grouped_equipment.items()))

        # Fetch form values from query parameters if redirected on validation error
        current_data = {
            'code': request.args.get('code', ''),
            'name': request.args.get('name', ''),
            'type': request.args.get('type', ''),
            'checklist_required': request.args.get('checklist_required') == 'true', # Convert string back to bool
            'status': request.args.get('status', 'Operational')
        }

        return render_template(
            'pm_equipment.html',
            grouped_equipment=sorted_grouped_equipment, # Pass grouped data
            equipment_statuses=EQUIPMENT_STATUSES, # Pass statuses for forms
            current_data=current_data, # Pass potentially repopulated data
            title='Manage Equipment'
        )
    except Exception as e:
        logging.error(f"Error loading equipment list: {e}", exc_info=True)
        flash(f"Error loading equipment list: {e}", "danger")
        # Provide empty dict and statuses even on error to prevent template crashes
        return render_template('pm_equipment.html',
                                title='Manage Equipment',
                                error=True,
                                grouped_equipment={},
                                equipment_statuses=EQUIPMENT_STATUSES,
                                current_data={})

@bp.route('/equipment/add', methods=['POST'])
def add_equipment():
    """Processes the form submission for adding new equipment."""
    code = request.form.get('code', '').strip()
    name = request.form.get('name', '').strip()
    type_val = request.form.get('type') # Renamed variable
    checklist_required = 'checklist_required' in request.form
    status = request.form.get('status', 'Operational') # Default if not sent

    # Store current values for potential repopulation on error
    current_data_for_redirect = {
        'code': code,
        'name': name,
        'type': type_val,
        'checklist_required': 'true' if checklist_required else 'false', # Pass as string
        'status': status
    }
    # Build query parameters string from the dict
    query_params = urlencode(current_data_for_redirect)


    # --- Validation ---
    errors = False
    if not code:
        flash('Equipment Code is required.', 'warning')
        errors = True
    if not name:
        flash('Equipment Name is required.', 'warning')
        errors = True
    if not type_val:
        flash('Equipment Type is required.', 'warning')
        errors = True
    if status not in EQUIPMENT_STATUSES:
        flash(f'Invalid Equipment Status "{status}".', 'warning')
        errors = True # Or handle differently, maybe just default

    # Check for existing code
    existing_equipment = Equipment.query.filter(func.lower(Equipment.code) == func.lower(code)).first()
    if existing_equipment:
        flash(f'Equipment with code "{code}" already exists.', 'warning')
        errors = True

    if errors:
         # Redirect back to the equipment list page with query params to repopulate form
         return redirect(url_for('planned_maintenance.equipment_list') + '?' + query_params + '#addEquipmentForm')


    # --- Add to DB ---
    try:
        new_equipment = Equipment(
            code=code,
            name=name,
            type=type_val,
            checklist_required=checklist_required,
            status=status # Add the status
        )
        db.session.add(new_equipment)
        db.session.commit()
        flash(f'Equipment "{code} - {name}" added successfully!', 'success')
        # Redirect to the main list, clearing any previous error params
        return redirect(url_for('planned_maintenance.equipment_list'))

    except Exception as e:
        db.session.rollback()
        logging.error(f"Error adding equipment: {e}", exc_info=True)
        flash(f"Database error adding equipment: {e}", "danger")
        # Redirect back with data for repopulation
        return redirect(url_for('planned_maintenance.equipment_list') + '?' + query_params + '#addEquipmentForm')

@bp.route('/equipment/edit/<int:id>', methods=['GET', 'POST'])
def edit_equipment(id):
    """Displays the edit form (GET) or processes the update (POST) for equipment."""
    equipment_to_edit = Equipment.query.get_or_404(id)

    if request.method == 'POST':
        try:
            original_code = equipment_to_edit.code
            new_code = request.form.get('code', '').strip()
            new_name = request.form.get('name', '').strip()
            new_type = request.form.get('type')
            new_checklist_required = 'checklist_required' in request.form
            new_status = request.form.get('status')

            # --- Validation ---
            errors = False
            if not new_code:
                flash('Equipment Code is required.', 'warning')
                errors = True
            if not new_name:
                flash('Equipment Name is required.', 'warning')
                errors = True
            if not new_type:
                flash('Equipment Type is required.', 'warning')
                errors = True
            if not new_status or new_status not in EQUIPMENT_STATUSES:
                flash('A valid Equipment Status is required.', 'warning')
                errors = True

            # Check if code changed and if the new code already exists
            if new_code.lower() != original_code.lower():
                existing = Equipment.query.filter(func.lower(Equipment.code) == func.lower(new_code)).first()
                if existing:
                    flash(f'Equipment code "{new_code}" already exists.', 'warning')
                    errors = True

            if errors:
                 # Re-render the edit form with current (potentially invalid) data
                 # We need to pass the data back to the template
                 # Create a temporary object or dict with the submitted data
                 submitted_data = {
                     'id': id, # Keep the ID
                     'code': new_code,
                     'name': new_name,
                     'type': new_type,
                     'checklist_required': new_checklist_required,
                     'status': new_status
                 }
                 # Render the edit template again
                 return render_template('pm_edit_equipment.html',
                                        title=f'Edit {original_code}',
                                        equipment=submitted_data, # Pass the submitted data
                                        equipment_statuses=EQUIPMENT_STATUSES)


            # --- Update DB ---
            equipment_to_edit.code = new_code
            equipment_to_edit.name = new_name
            equipment_to_edit.type = new_type
            equipment_to_edit.checklist_required = new_checklist_required
            equipment_to_edit.status = new_status

            db.session.commit()
            flash(f'Equipment "{equipment_to_edit.code}" updated successfully!', 'success')
            return redirect(url_for('planned_maintenance.equipment_list'))

        except Exception as e:
            db.session.rollback()
            logging.error(f"Error updating equipment (id: {id}): {e}", exc_info=True)
            flash(f"Error updating equipment: {e}", "danger")
            # Re-render edit form, pass original object back after rollback
            return render_template('pm_edit_equipment.html',
                                   title=f'Edit {equipment_to_edit.code}', # Use original code in title
                                   equipment=equipment_to_edit, # Pass original object
                                   equipment_statuses=EQUIPMENT_STATUSES)

    # --- Handle GET Request ---
    # Pass the actual equipment object fetched by get_or_404
    return render_template('pm_edit_equipment.html',
                           title=f'Edit {equipment_to_edit.code}',
                           equipment=equipment_to_edit, # Pass the DB object
                           equipment_statuses=EQUIPMENT_STATUSES)

# ==============================================================================
# === Job cards            ===
# ==============================================================================
@bp.route('/job_card_reports/by_technician', methods=['GET'])
def report_jobs_by_technician():
    """Displays job cards grouped by technician, based on filters."""
    logging.debug("--- Request for Jobs by Technician Report ---")
    try:
        # 1. Get Filter Parameters from query args
        equipment_type_filter = request.args.get('equipment_type', 'All')
        job_type_filter = request.args.get('job_type', 'All')
        technician_filter = request.args.get('technician_filter', 'All')
        status_filter = request.args.get('status_filter', 'All') # <<< NEW: Get status filter
        
        start_date_str = request.args.get('start_date', '')
        end_date_str = request.args.get('end_date', '')

        filter_start_date = None
        filter_end_date = None
        date_filter_active = False

        if start_date_str:
            try:
                filter_start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
                date_filter_active = True
            except ValueError:
                flash(f"Invalid start date for report: {start_date_str}.", "warning")
        if end_date_str:
            try:
                filter_end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
                date_filter_active = True
            except ValueError:
                flash(f"Invalid end date for report: {end_date_str}.", "warning")
        
        # 2. Build Query
        query = JobCard.query.options(db.joinedload(JobCard.equipment_ref))

        if equipment_type_filter and equipment_type_filter != 'All':
            query = query.join(Equipment, JobCard.equipment_id == Equipment.id)\
                         .filter(Equipment.type == equipment_type_filter)
        
        if job_type_filter == 'Maintenance':
            query = query.filter(JobCard.job_number.like('JC-%'))
        elif job_type_filter == 'Legal':
            query = query.filter(JobCard.job_number.like('LC-%'))

        # Apply technician filter if it's not 'All'
        if technician_filter and technician_filter != 'All':
            if technician_filter == 'Unassigned':
                query = query.filter(or_(JobCard.technician == None, JobCard.technician == ''))
            else:
                query = query.filter(JobCard.technician == technician_filter)
        
        # <<< NEW: Apply Status Filter to the report query >>>
        if status_filter and status_filter != 'All' and status_filter in JOB_CARD_STATUSES:
            query = query.filter(JobCard.status == status_filter)

        # Apply date filters (e.g., to filter JCs by due date)
        if filter_start_date:
            query = query.filter(JobCard.due_date >= datetime.combine(filter_start_date, time.min))
        if filter_end_date:
            query = query.filter(JobCard.due_date <= datetime.combine(filter_end_date, time.max))

        all_job_cards = query.order_by(JobCard.technician, JobCard.due_date.asc().nullslast()).all()

        # 3. Group by Technician
        jobs_by_technician = defaultdict(list)
        for jc in all_job_cards:
            tech_name = jc.technician if jc.technician and jc.technician.strip() else "Unassigned"
            jc.is_overdue = jc.due_date and jc.due_date < datetime.combine(date.today(), time.min) and jc.status not in ['Done', 'Deleted']
            jobs_by_technician[tech_name].append(jc)
        
        sorted_technicians = sorted(jobs_by_technician.keys(), key=lambda t: (t == "Unassigned", t.lower()))

        current_report_filters = {
            'equipment_type': equipment_type_filter,
            'job_type': job_type_filter,
            'technician_filter': technician_filter,
            'status_filter': status_filter, # <<< NEW: Pass status filter back
            'start_date': start_date_str,
            'end_date': end_date_str,
            'date_filter_active': date_filter_active,
            'filter_start_date_obj': filter_start_date,
            'filter_end_date_obj': filter_end_date,
        }
        
        filter_summary_parts = []
        if equipment_type_filter != 'All': filter_summary_parts.append(f"Equip. Type: {equipment_type_filter}")
        if job_type_filter != 'All': filter_summary_parts.append(f"Job Type: {job_type_filter}")
        if technician_filter != 'All': filter_summary_parts.append(f"Technician: {technician_filter}")
        if status_filter != 'All': filter_summary_parts.append(f"Status: {status_filter}") # <<< NEW: Add to summary
        if filter_start_date: filter_summary_parts.append(f"Due From: {filter_start_date.strftime('%Y-%m-%d')}")
        if filter_end_date: filter_summary_parts.append(f"Due To: {filter_end_date.strftime('%Y-%m-%d')}")
        
        filter_summary = ", ".join(filter_summary_parts) if filter_summary_parts else "None"

        return render_template(
            'pm_report_jobs_by_technician.html',
            title=f"Job Cards by Technician ({status_filter if status_filter != 'All' else 'All Statuses'})", # Update title
            jobs_by_technician=jobs_by_technician,
            sorted_technicians=sorted_technicians,
            current_filters=current_report_filters,
            filter_summary=filter_summary,
            today=date.today()
        )

    except Exception as e:
        logging.error(f"--- Error generating jobs by technician report: {e} ---", exc_info=True)
        flash(f"An error occurred: {e}", "danger")
        # Pass back attempted filters on error
        error_filters = {
            'equipment_type': request.args.get('equipment_type', 'All'),
            'job_type': request.args.get('job_type', 'All'),
            'technician_filter': request.args.get('technician_filter', 'All'),
            'status_filter': request.args.get('status_filter', 'All'),
            'start_date': request.args.get('start_date', ''),
            'end_date': request.args.get('end_date', '')
        }
        return redirect(url_for('planned_maintenance.job_card_reports_dashboard', **error_filters))

@bp.route('/job_card_reports', methods=['GET'])
def job_card_reports_dashboard():
    """Displays job card metrics and links to detailed reports."""
    logging.debug("--- Request for Job Card Reports Dashboard ---")
    try:
        # 1. Get Filter Parameters
        equipment_type_filter = request.args.get('equipment_type', 'All')
        job_type_filter = request.args.get('job_type', 'All')
        technician_filter = request.args.get('technician_filter', 'All')
        start_date_str = request.args.get('start_date', '')
        end_date_str = request.args.get('end_date', '')
        status_filter_for_reports = request.args.get('status_filter', 'All')

        # 2. Prepare Date Range for "Completed" metric
        # If date range is provided in filters, use it. Otherwise, default to current month.
        filter_start_date = None
        filter_end_date = None
        
        if start_date_str:
            try:
                filter_start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            except ValueError:
                flash(f"Invalid start date for report: {start_date_str}. Using no start date.", "warning")
        
        if end_date_str:
            try:
                filter_end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            except ValueError:
                flash(f"Invalid end date for report: {end_date_str}. Using no end date.", "warning")

        # For "Completed" metric, define the period
        today_obj = date.today()
        if filter_start_date and filter_end_date:
            completed_period_start_dt = datetime.combine(filter_start_date, time.min)
            completed_period_end_dt = datetime.combine(filter_end_date, time.max)
            completed_period_label = f"{filter_start_date.strftime('%b %d, %Y')} to {filter_end_date.strftime('%b %d, %Y')}"
        elif filter_start_date: # Only start date given
            completed_period_start_dt = datetime.combine(filter_start_date, time.min)
            completed_period_end_dt = datetime.combine(today_obj, time.max) # up to today
            completed_period_label = f"From {filter_start_date.strftime('%b %d, %Y')} to Today"
        else: # Default to current month if no specific range given
            current_month_start_date = today_obj.replace(day=1)
            days_in_month = monthrange(today_obj.year, today_obj.month)[1]
            current_month_end_date = today_obj.replace(day=days_in_month)
            completed_period_start_dt = datetime.combine(current_month_start_date, time.min)
            completed_period_end_dt = datetime.combine(current_month_end_date, time.max)
            completed_period_label = f"Current Month ({today_obj.strftime('%B %Y')})"


        # 3. Build Base Query for Metrics
        base_query = JobCard.query

        # Apply common filters
        if equipment_type_filter and equipment_type_filter != 'All':
            base_query = base_query.join(Equipment, JobCard.equipment_id == Equipment.id)\
                                   .filter(Equipment.type == equipment_type_filter)
        
        if job_type_filter == 'Maintenance':
            base_query = base_query.filter(JobCard.job_number.like('JC-%'))
        elif job_type_filter == 'Legal':
            base_query = base_query.filter(JobCard.job_number.like('LC-%'))

        if technician_filter and technician_filter != 'All':
            if technician_filter == 'Unassigned':
                base_query = base_query.filter(or_(JobCard.technician == None, JobCard.technician == ''))
            else:
                base_query = base_query.filter(JobCard.technician == technician_filter)

        # 4. Calculate Metrics
        count_todo = base_query.filter(JobCard.status == 'To Do').count()
        
        # Overdue: due_date < today (midnight) AND status is not Done or Deleted
        overdue_datetime_threshold = datetime.combine(today_obj, time.min)
        count_overdue = base_query.filter(
            JobCard.due_date < overdue_datetime_threshold,
            JobCard.status.notin_(['Done', 'Deleted'])
        ).count()

        count_completed_in_period = base_query.filter(
            JobCard.status == 'Done',
            JobCard.end_datetime >= completed_period_start_dt,
            JobCard.end_datetime <= completed_period_end_dt
        ).count()

        # 5. Data for Filter Dropdowns
        equipment_types_query = db.session.query(Equipment.type).distinct().order_by(Equipment.type).all()
        report_equipment_types = ['All'] + [t[0] for t in equipment_types_query if t[0]]

        technicians_query = db.session.query(JobCard.technician).distinct().order_by(JobCard.technician).all()
        report_technicians = ['All', 'Unassigned'] + [t[0] for t in technicians_query if t[0] and t[0].strip()]
        
        report_job_types = ['All', 'Maintenance', 'Legal']

        current_report_filters = {
            'equipment_type': equipment_type_filter,
            'job_type': job_type_filter,
            'technician_filter': technician_filter,
            'status_filter': status_filter_for_reports,
            'start_date': start_date_str,
            'end_date': end_date_str
        }
        
        logging.debug(f"Report Metrics: ToDo={count_todo}, Overdue={count_overdue}, Completed ({completed_period_label})={count_completed_in_period}")

        return render_template(
            'pm_job_card_reports_dashboard.html',
            title="Job Card Reports & Metrics",
            count_todo=count_todo,
            count_overdue=count_overdue,
            count_completed_in_period=count_completed_in_period,
            completed_period_label=completed_period_label,
            report_equipment_types=report_equipment_types,
            report_job_types=report_job_types,
            report_technicians=report_technicians,
            JOB_CARD_STATUSES=JOB_CARD_STATUSES,
            current_filters=current_report_filters
        )

    except Exception as e:
        logging.error(f"--- Error loading job card reports dashboard: {e} ---", exc_info=True)
        flash(f"An error occurred: {e}", "danger")
        return render_template(
            'pm_job_card_reports_dashboard.html',
            title="Job Card Reports & Metrics - Error",
            error=str(e),
            report_equipment_types=['All'], report_job_types=['All'], report_technicians=['All'],
            JOB_CARD_STATUSES=JOB_CARD_STATUSES,
            current_filters={'equipment_type': 'All', 'job_type': 'All', 'technician_filter': 'All', 'start_date': '', 'end_date': ''}
        )


@bp.route('/job_card/new_from_task/<int:task_id>', methods=['POST'])
def new_job_card_from_task(task_id):
    try:
        task = MaintenanceTask.query.get_or_404(task_id)
        technician = request.form.get('technician')
        due_date_str = request.form.get('due_date')
        logging.debug(f"Received due_date string from form for task {task_id}: '{due_date_str}'")

        # --- <<< CHECK FOR EXISTING OPEN JOB CARD >>> ---
        existing_open_jc = JobCard.query.filter(
            JobCard.equipment_id == task.equipment_id,
            JobCard.description == task.description,
            JobCard.status.notin_(['Done', 'Deleted'])
        ).first()

        if existing_open_jc:
            flash(f"Cannot create new job card. An open job card (#{existing_open_jc.job_number}) "
                  f"already exists for task '{task.description}' on equipment {task.equipment_ref.code}.",
                  "warning")
            return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

        # --- Parse the received due date string ---
        due_date_dt = None
        if due_date_str:
            try:
                parsed_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
                due_date_dt = datetime.combine(parsed_date, time.min)
                logging.debug(f"Parsed due_date_str '{due_date_str}' to datetime: {due_date_dt}")
            except ValueError:
                flash(f"Invalid due date format '{due_date_str}' received. Job card created without due date.", "warning")

        # --- Generate Job Number with type distinction ---
        # Determine if this is a legal compliance task
        job_type = 'LEGAL' if task.is_legal_compliance else 'MAINT'
        job_number = generate_next_job_number(job_type)
        logging.debug(f"Generated Job Number: {job_number} (Type: {job_type})")

        # --- Create New Job Card ---
        job_card = JobCard(
            job_number=job_number,
            equipment_id=task.equipment_id,
            description=task.description,
            technician=technician if technician else None,
            status='To Do',
            oem_required=task.oem_required,
            kit_required=task.kit_required,
            due_date=due_date_dt
        )
        db.session.add(job_card)
        db.session.commit()
        logging.info(f"Job Card {job_number} created from task {task_id} with due date {due_date_dt}.")

        # --- Format WhatsApp message ---
        equipment_name = task.equipment_ref.name or 'Unknown Equipment'
        due_date_display_str = due_date_dt.strftime('%Y-%m-%d') if due_date_dt else 'Not Set'
        # Add job type information to the WhatsApp message
        job_type_display = "Legal Compliance Task" if task.is_legal_compliance else "Maintenance Task"
        whatsapp_msg = (
            f"Job Card #{job_number} created from {job_type_display}:\n"
            f"Equipment: {equipment_name}\n"
            f"Task: {task.description}\n"
            f"Assigned: {technician or 'Unassigned'}\n"
            f"OEM Required: {'Yes' if task.oem_required else 'No'}\n"
            f"Kit Required: {'Yes' if task.kit_required else 'No'}\n"
            f"Due Date: {due_date_display_str}"
        )

        flash(f'Job Card {job_number} created from task!', 'success')
        encoded_msg = urlencode({'text': whatsapp_msg})
        whatsapp_url = f"https://wa.me/?{encoded_msg}"
        return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

    except Exception as e:
        db.session.rollback()
        logging.error(f"Error creating job card from task {task_id}: {e}", exc_info=True)
        flash(f"Error creating job card from task: {e}", "danger")
        return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

@bp.route('/job_cards', methods=['GET'])
def job_card_list():
    """Displays a list of job cards with filtering options."""
    logging.debug("--- Request for Job Card List View ---")
    try:
        page = request.args.get('page', 1, type=int)
        per_page = 25
        overdue_threshold_dt = datetime.combine(date.today(), time.min)

        # ... (all your filter string retrievals: start_date_str, equipment_search_term, etc.) ...
        start_date_str = request.args.get('start_date', '')
        end_date_str = request.args.get('end_date', '')
        equipment_search_term = request.args.get('equipment_search', '').strip()
        status_filter = request.args.get('status', 'All')
        job_type_filter = request.args.get('job_type', 'All')
        equipment_type_filter = request.args.get('equipment_type', 'All')
        technician_filter = request.args.get('technician_filter', 'All')


        # ... (start_date_val, end_date_val parsing) ...
        start_date_val = None
        end_date_val = None
        if start_date_str:
            try:
                start_date_val = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            except ValueError:
                flash(f"Invalid start date format: {start_date_str}. Please use YYYY-MM-DD.", "warning")
        if end_date_str:
            try:
                end_date_val = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            except ValueError:
                flash(f"Invalid end date format: {end_date_str}. Please use YYYY-MM-DD.", "warning")

        logging.debug(f"Filters - Start: {start_date_val}, End: {end_date_val}, Equip Search: '{equipment_search_term}', Status: {status_filter}, Job Type: {job_type_filter}, Equip Type: {equipment_type_filter}, Technician: {technician_filter}, Page: {page}")

        query = JobCard.query.options(
            db.joinedload(JobCard.equipment_ref)
        )

        # ... (all your query filter applications) ...
        if start_date_val:
            query = query.filter(JobCard.due_date >= datetime.combine(start_date_val, time.min))
        if end_date_val:
            query = query.filter(JobCard.due_date <= datetime.combine(end_date_val, time.max))
        if equipment_search_term: 
            search_pattern = f"%{equipment_search_term}%"
            if Equipment not in [c.entity for c in query._join_entities]:
                 query = query.join(JobCard.equipment_ref)
            query = query.filter(
                or_(
                    Equipment.code.ilike(search_pattern),
                    Equipment.name.ilike(search_pattern)
                )
            )
        if equipment_type_filter and equipment_type_filter != 'All':
            if Equipment not in [c.entity for c in query._join_entities]:
                 query = query.join(JobCard.equipment_ref)
            query = query.filter(Equipment.type == equipment_type_filter)
        if status_filter and status_filter != 'All' and status_filter in JOB_CARD_STATUSES:
            query = query.filter(JobCard.status == status_filter)
        if job_type_filter == 'Maintenance':
            query = query.filter(JobCard.job_number.like('JC-%'))
        elif job_type_filter == 'Legal':
            query = query.filter(JobCard.job_number.like('LC-%'))
        if technician_filter and technician_filter != 'All':
            if technician_filter == 'Unassigned':
                query = query.filter(or_(JobCard.technician == None, JobCard.technician == ''))
            else:
                query = query.filter(JobCard.technician == technician_filter)

        query = query.order_by(JobCard.id.desc())
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)
        job_cards_page = pagination.items

        for jc in job_cards_page:
            jc.whatsapp_share_url = generate_whatsapp_share_url(jc)

        equipment_types_query = db.session.query(Equipment.type).distinct().order_by(Equipment.type).all()
        equipment_types_for_filter = ['All'] + [t[0] for t in equipment_types_query if t[0]]

        technicians_query = db.session.query(JobCard.technician).distinct().order_by(JobCard.technician).all()
        technicians_for_filter = ['All', 'Unassigned'] + [t[0] for t in technicians_query if t[0] and t[0].strip()]
        
        job_type_options = ['All', 'Maintenance', 'Legal']

        current_filters = {
            'start_date': start_date_str,
            'end_date': end_date_str,
            'equipment_search': equipment_search_term,
            'status': status_filter,
            'job_type': job_type_filter,
            'equipment_type': equipment_type_filter,
            'technician_filter': technician_filter
        }

        # <<< --- ENSURE THIS LINE IS PRESENT --- >>>
        all_equipment_for_modal = Equipment.query.filter(Equipment.status != 'Sold').order_by(Equipment.code).all()

        logging.debug(f"Found {pagination.total} job cards matching filters. Displaying page {page} ({len(job_cards_page)} items).")

        return render_template(
            'pm_job_card_list.html',
            title="Job Card List",
            pagination=pagination,
            job_cards=job_cards_page,
            job_card_statuses=['All'] + JOB_CARD_STATUSES,
            job_type_options=job_type_options,
            equipment_types_for_filter=equipment_types_for_filter,
            technicians_for_filter=technicians_for_filter,
            current_filters=current_filters,
            # today=date.today(), # Not strictly needed if overdue_threshold_dt is used for all date comparisons
            overdue_threshold_dt=overdue_threshold_dt,
            all_equipment=all_equipment_for_modal  # <<< --- PASS IT TO THE TEMPLATE --- >>>
        )

    except Exception as e:
        logging.error(f"--- Error loading job card list view: {e} ---", exc_info=True)
        flash(f"An error occurred while loading the job card list: {e}", "danger")
        return render_template(
            'pm_job_card_list.html',
            title="Job Card List - Error",
            error=f"Could not load job cards: {e}",
            pagination=None, job_cards=[], 
            job_card_statuses=['All'] + JOB_CARD_STATUSES,
            job_type_options=['All', 'Maintenance', 'Legal'],
            equipment_types_for_filter=['All'],
            technicians_for_filter=['All'],
            current_filters={},
            # today=date.today(),
            overdue_threshold_dt=datetime.combine(date.today(), time.min),
            all_equipment=[] # <<< --- Pass empty list on error --- >>>
        )
    
@bp.route('/job_card/<int:id>', methods=['GET']) # Use GET to view details
def job_card_detail(id):
    """Displays the details of a single Job Card."""
    logging.debug(f"--- Request to view details for Job Card ID: {id} ---")
    try:
        # Fetch the job card with eager loading of related data
        job_card = JobCard.query.options(
            db.joinedload(JobCard.equipment_ref), # Load equipment details

        ).get_or_404(id) # Use get_or_404 to handle not found errors

        logging.debug(f"Found Job Card: {job_card.job_number}")

        whatsapp_url = generate_whatsapp_share_url(job_card)
        # Accessing parts in the template (e.g., job_card.parts_used.all())
        # will now use the pre-loaded data instead of hitting the DB again.
        # parts_list = job_card.parts_used.all() # Just for logging confirmation if needed
        # if parts_list:
        #     logging.debug(f"Found {len(parts_list)} parts associated (pre-loaded).")

        # Render the template to display this data
        return render_template(
            'pm_job_card_detail.html', # Your detail template
            title=f"Job Card {job_card.job_number}",
            job_card=job_card
        )

    except Exception as e:
        logging.error(f"--- Error loading job card detail view for ID {id}: {e} ---", exc_info=True)
        flash(f"An error occurred while loading the job card details: {e}", "danger")
        # Redirect to dashboard or job card list on error
        return redirect(url_for('planned_maintenance.dashboard'))

@bp.route('/job_card/complete/<int:id>', methods=['GET', 'POST'])
def complete_job_card(id):
    job_card = JobCard.query.get_or_404(id)
    if job_card.status == 'Done':
        flash(f'Job Card {job_card.job_number} is already marked as Done.', 'info')
        return redirect(url_for('planned_maintenance.dashboard'))

    if request.method == 'POST':
        checkout_datetime = None
        checkin_datetime = None
        parts_to_process = []
        parts_summary = []

        try:
            comments = request.form.get('comments')
            checkout_datetime_str = request.form.get('checkout_datetime')
            checkin_datetime_str = request.form.get('checkin_datetime')

            # --- Validation for datetime presence ---
            if not checkout_datetime_str or not checkin_datetime_str:
                 flash('Machine Check-out and Check-in times are required.', 'warning')
                 parts_for_dropdown = Part.query.order_by(Part.store, Part.name).all()
                 return render_template('pm_job_card_complete.html', job_card=job_card,
                                      parts=parts_for_dropdown,
                                      title=f'Complete JC {job_card.job_number}')

            logging.debug(f"Parsing checkout_datetime: '{checkout_datetime_str}'")
            checkout_datetime = parse_datetime(checkout_datetime_str) # Naive
            logging.debug(f"Parsing checkin_datetime: '{checkin_datetime_str}'")
            checkin_datetime = parse_datetime(checkin_datetime_str)   # Naive

            logging.debug(f"Completing JC {id}: Parsed checkout={checkout_datetime}({checkout_datetime.tzinfo}), checkin={checkin_datetime}({checkin_datetime.tzinfo})")

            # --- Validation for datetime order ---
            if checkin_datetime <= checkout_datetime:
                flash('Machine Check-in Time must be after Check-out Time.', 'warning')
                parts_for_dropdown = Part.query.order_by(Part.store, Part.name).all()
                return render_template('pm_job_card_complete.html', job_card=job_card,
                                     parts=parts_for_dropdown,
                                     title=f'Complete JC {job_card.job_number}')

            # --- Parts Processing (remains the same) ---
            part_ids_str = request.form.getlist('part_id')
            quantities_str = request.form.getlist('quantity')
            parts_to_process = []
            for part_id_s, qty_s in zip_longest(part_ids_str, quantities_str):
                if not part_id_s or not qty_s: continue
                try:
                     part_id_val = int(part_id_s) # Renamed variable
                     quantity = int(qty_s)
                     if quantity > 0: parts_to_process.append({'id': part_id_val, 'qty': quantity})
                     elif quantity <=0: logging.warning(f"Skipping part {part_id_s} with non-positive quantity {qty_s}")
                except ValueError:
                     logging.warning(f"Invalid part_id '{part_id_s}' or quantity '{qty_s}'. Skipping row.")
                     continue

            logging.debug(f"Parts to process: {parts_to_process}")

            # --- Update Job Card Status and Times ---
            job_card.status = 'Done'
            job_card.comments = comments
            job_card.start_datetime = checkout_datetime # Use parsed naive datetime
            job_card.end_datetime = checkin_datetime   # Use parsed naive datetime
            db.session.add(job_card)

            # --- Process Parts Used (remains the same) ---
            parts_summary = []
            for item in parts_to_process:
                part = Part.query.get(item['id'])
                if not part: raise ValueError(f"Invalid part ID {item['id']} selected.")
                if part.current_stock < item['qty']: raise ValueError(f"Insufficient stock for {part.name} (Needed: {item['qty']}, Available: {part.current_stock}).")
                part.current_stock -= item['qty']
                db.session.add(part)
                jc_part = JobCardPart(job_card_id=job_card.id, part_id=part.id, quantity=item['qty'])
                db.session.add(jc_part)
                stock_tx = StockTransaction(part_id=part.id, quantity=-item['qty'], description=f"Used in Job Card {job_card.job_number}")
                db.session.add(stock_tx)
                parts_summary.append(f"- {item['qty']} x {part.name}")

            # ================== TASK UPDATE LOGIC ==================
            # Find the related maintenance task
            task = MaintenanceTask.query.filter_by(
                equipment_id=job_card.equipment_id,
                description=job_card.description # Assuming description uniquely identifies the task for the equipment
            ).first()

            if task:
                # Update last performed timestamp
                # <<< MAKE UTC AWARE BEFORE STORING >>>
                checkin_datetime_utc = checkin_datetime # Start with naive
                if checkin_datetime_utc.tzinfo is None:
                    # Assume the naive time represents UTC, make it aware
                    checkin_datetime_utc = checkin_datetime_utc.replace(tzinfo=timezone.utc)
                else:
                     # If already aware (e.g. from parse_datetime heuristic), convert to UTC
                    checkin_datetime_utc = checkin_datetime_utc.astimezone(timezone.utc)

                task.last_performed = checkin_datetime_utc # Assign UTC-aware checkin time
                logging.debug(f"Updating task {task.id} last_performed to {checkin_datetime_utc} ({checkin_datetime_utc.tzinfo})")

                # --- NEW: Update last performed usage value for hours/km tasks ---
                if task.interval_type in ['hours', 'km']:
                    # Find the latest usage log AT OR BEFORE the check-in time (use UTC aware time for comparison)
                    usage_at_completion = UsageLog.query.filter(
                        UsageLog.equipment_id == task.equipment_id,
                        UsageLog.log_date <= checkin_datetime_utc # Crucial filter condition (compare aware with aware/naive correctly)
                    ).order_by(desc(UsageLog.log_date)).first()

                    if usage_at_completion:
                        task.last_performed_usage_value = usage_at_completion.usage_value
                        logging.debug(f"Updating task {task.id} last_performed_usage_value to {task.last_performed_usage_value}")
                    else:
                        # Handle case where no usage log exists before or at check-in
                        task.last_performed_usage_value = None # Set to None if unknown
                        logging.warning(f"Could not find usage log at or before {checkin_datetime_utc} for task {task.id} completion. last_performed_usage_value set to None.")
                else:
                    # For 'days' tasks, the usage value isn't relevant
                    task.last_performed_usage_value = None

                db.session.add(task)
            else:
                 logging.warning(f"Could not find matching MaintenanceTask for Job Card {job_card.job_number} (Eq: {job_card.equipment_id}, Desc: {job_card.description})")
            # ========================================================

            db.session.commit()
            logging.info(f"Job Card {job_card.job_number} completed successfully.")

            # --- WhatsApp Message Generation (remains the same) ---
            equipment_name = job_card.equipment_ref.name or 'Unknown Equipment'
            parts_str = "\n".join(parts_summary) if parts_summary else "No parts used."
            # Display naive time in WhatsApp for simplicity/local context?
            checkout_str = checkout_datetime.strftime('%Y-%m-%d %H:%M') if checkout_datetime else "N/A"
            checkin_str = checkin_datetime.strftime('%Y-%m-%d %H:%M') if checkin_datetime else "N/A"
            whatsapp_msg = (
                f"Job Card Completed: #{job_card.job_number}\n"
                f"Equipment: {equipment_name}\n"
                f"Task: {job_card.description}\n"
                f"Technician: {job_card.technician or 'N/A'}\n"
                f"Machine Checked Out: {checkout_str}\n"
                f"Machine Checked In: {checkin_str}\n"
                f"Parts Used:\n{parts_str}"
            )
            whatsapp_msg += f"\nComments: {comments or 'None'}"

            # --- Redirect/Flash (remains the same) ---
            send_whatsapp = 'send_whatsapp' in request.form
            if send_whatsapp:
                encoded_msg = urlencode({'text': whatsapp_msg})
                whatsapp_url = f"https://wa.me/?{encoded_msg}"
                # Redirecting to detail view first makes more sense
                flash(f'Job Card {job_card.job_number} completed. Click <a href="{whatsapp_url}" target="_blank">here</a> to share via WhatsApp.', 'success')
                return redirect(url_for('planned_maintenance.job_card_detail', id=job_card.id))
            else:
                flash(f'Job Card {job_card.job_number} completed successfully!', 'success')
                return redirect(url_for('planned_maintenance.job_card_detail', id=job_card.id))

        except ValueError as ve:
            db.session.rollback()
            logging.error(f"ValueError processing job card {id} completion: {ve}", exc_info=True)
            flash(f"Error processing job card: {ve}", "danger")
            parts_for_dropdown = Part.query.order_by(Part.store, Part.name).all()
            return render_template('pm_job_card_complete.html', job_card=job_card,
                                 parts=parts_for_dropdown,
                                 title=f'Complete JC {job_card.job_number}')

        except Exception as e:
            db.session.rollback()
            logging.error(f"Unexpected error processing job card {id} completion: {e}", exc_info=True)
            flash(f"An unexpected error occurred: {e}", "danger")
            parts_for_dropdown = Part.query.order_by(Part.store, Part.name).all()
            return render_template('pm_job_card_complete.html', job_card=job_card,
                                 parts=parts_for_dropdown,
                                 title=f'Complete JC {job_card.job_number}')

    # --- Handle GET Request (remains the same) ---
    parts_for_dropdown = Part.query.order_by(Part.store, Part.name).all()
    return render_template('pm_job_card_complete.html', job_card=job_card,
                         parts=parts_for_dropdown,
                         title=f'Complete JC {job_card.job_number}')

# Route deprecated - use create_job_card instead
#@bp.route('/job_card/new', methods=['POST'])
#def new_job_card():
#    # ... (keep logic if needed for specific reason, otherwise remove)
#    pass

@bp.route('/job_card/create', methods=['POST'])
def create_job_card():
    """Processes the form submission for creating a new job card."""
    logging.info("--- Processing POST request to create new Job Card ---")
    try:
        # 1. Get Data from Form
        equipment_id_str = request.form.get('equipment_id')
        description = request.form.get('description', '').strip()
        technician = request.form.get('technician', '').strip() or None
        due_date_str = request.form.get('due_date')
        oem_required = 'oem_required' in request.form
        kit_required = 'kit_required' in request.form
        send_whatsapp = 'send_whatsapp' in request.form
        # New field: is_legal_compliance
        is_legal = 'is_legal_compliance' in request.form

        logging.debug(f"Received form data: equipment_id='{equipment_id_str}', description='{description}', due_date='{due_date_str}', is_legal='{is_legal}'...")

        # 2. Validation
        errors = False
        equipment_id = None
        if not equipment_id_str or not equipment_id_str.isdigit():
            flash('Valid Equipment selection is required.', 'danger')
            errors = True
        else:
            equipment_id = int(equipment_id_str)
            equipment = Equipment.query.get(equipment_id)
            if not equipment:
                flash('Selected equipment not found in database.', 'danger')
                errors = True

        if not description:
            flash('Task Description is required.', 'danger')
            errors = True

        due_date_dt = None
        if due_date_str:
            try:
                due_date_only = datetime.strptime(due_date_str, '%Y-%m-%d').date()
                due_date_dt = datetime.combine(due_date_only, time.min)
                logging.debug(f"Parsed due_date: {due_date_dt}")
            except ValueError:
                flash('Invalid Due Date format. Please use YYYY-MM-DD.', 'warning')
                errors = True

        if errors:
            logging.warning("Validation errors encountered during Job Card creation.")
            return redirect(request.referrer or url_for('planned_maintenance.job_card_list'))

        # 3. Generate Job Number with type distinction
        job_type = 'LEGAL' if is_legal else 'MAINT'
        job_number = generate_next_job_number(job_type)
        logging.debug(f"Generated Job Number: {job_number} (Type: {job_type})")

        # 4. Create JobCard Object
        new_job_card = JobCard(
            job_number=job_number,
            equipment_id=equipment_id,
            description=description,
            technician=technician,
            status='To Do',
            oem_required=oem_required,
            kit_required=kit_required,
            due_date=due_date_dt,
        )

        # 5. Add to Database
        db.session.add(new_job_card)
        db.session.commit()
        logging.info(f"Successfully created Job Card {job_number} (ID: {new_job_card.id})")

        # 6. Post-Creation Actions (Flash, WhatsApp)
        flash(f'Job Card {job_number} created successfully!', 'success')

        if send_whatsapp:
            whatsapp_url = generate_whatsapp_share_url(new_job_card)
            if whatsapp_url:
                flash(f'Click <a href="{whatsapp_url}" target="_blank">here</a> to share Job Card {job_number} via WhatsApp.', 'info')
            else:
                flash('Could not generate WhatsApp link.', 'warning')

        # 7. Redirect to the Job Card List view (or referrer)
        return redirect(url_for('planned_maintenance.job_card_list'))

    except Exception as e:
        db.session.rollback()
        logging.error(f"Error creating new job card: {e}", exc_info=True)
        flash(f"An unexpected error occurred while creating the job card: {e}", "danger")
        return redirect(request.referrer or url_for('planned_maintenance.job_card_list'))

@bp.route('/job_card/edit/<int:id>', methods=['GET', 'POST'])
def edit_job_card(id):
    """Displays the edit form (GET) or processes the update (POST) for a job card."""
    job_card = JobCard.query.get_or_404(id)
    
    if request.method == 'POST':
        try:
            # Get form data
            equipment_id = request.form.get('equipment_id', type=int)
            description = request.form.get('description', '').strip()
            technician = request.form.get('technician', '').strip() or None
            status = request.form.get('status')
            due_date_str = request.form.get('due_date')
            oem_required = 'oem_required' in request.form
            kit_required = 'kit_required' in request.form
            comments = request.form.get('comments', '')
            
            # Validate required fields
            if not equipment_id:
                flash('Equipment is required.', 'danger')
                raise ValueError("Equipment selection is required")
                
            if not description:
                flash('Description is required.', 'danger')
                raise ValueError("Description is required")
                
            if status not in JOB_CARD_STATUSES:
                flash('Valid status is required.', 'danger')
                raise ValueError("Invalid status value")
            
            # Parse due date
            due_date_dt = None
            if due_date_str:
                try:
                    # Parse date only, combine with min time for naive datetime
                    due_date_only = datetime.strptime(due_date_str, '%Y-%m-%d').date()
                    due_date_dt = datetime.combine(due_date_only, time.min)
                except ValueError:
                    flash('Invalid due date format. Please use YYYY-MM-DD.', 'warning')
                    # Keep the existing due date if we couldn't parse the new one
                    due_date_dt = job_card.due_date
            
            # Update the job card
            job_card.equipment_id = equipment_id
            job_card.description = description
            job_card.technician = technician
            job_card.status = status
            job_card.due_date = due_date_dt
            job_card.oem_required = oem_required
            job_card.kit_required = kit_required
            
            # Only update comments if provided and not empty
            if comments:
                # Preserve existing comments if any
                if job_card.comments:
                    job_card.comments = f"{job_card.comments}\n\n[Edit on {datetime.now().strftime('%Y-%m-%d %H:%M')}]:\n{comments}"
                else:
                    job_card.comments = comments
            
            db.session.commit()
            flash(f'Job Card {job_card.job_number} updated successfully!', 'success')
            return redirect(url_for('planned_maintenance.job_card_detail', id=job_card.id))
            
        except ValueError as ve:
            # Form validation errors are handled here
            pass  # Flash messages already set
        except Exception as e:
            db.session.rollback()
            logging.error(f"Error updating Job Card {id}: {e}", exc_info=True)
            flash(f"An error occurred while updating the job card: {e}", "danger")
        
        # On error, re-fetch data and re-render the form
        all_equipment = Equipment.query.order_by(Equipment.code).all()
        return render_template('pm_job_card_edit_form.html', 
                               job_card=job_card,
                               all_equipment=all_equipment,
                               job_card_statuses=JOB_CARD_STATUSES,
                               title=f'Edit JC {job_card.job_number}')
    
    # GET Request - Display the edit form
    all_equipment = Equipment.query.order_by(Equipment.code).all()
    return render_template('pm_job_card_edit_form.html', 
                           job_card=job_card,
                           all_equipment=all_equipment,
                           job_card_statuses=JOB_CARD_STATUSES,
                           title=f'Edit JC {job_card.job_number}')

@bp.route('/job_card/print/<int:id>')
def print_job_card(id):
    """Displays a printable version of a job card."""
    logging.debug(f"--- Request to print Job Card ID: {id} ---")
    try:
        # Fetch the job card with eager loading of related data
        # Modified to correctly handle the parts relationship
        job_card = JobCard.query.options(
            db.joinedload(JobCard.equipment_ref)
        ).get_or_404(id)

        logging.debug(f"Preparing print view for Job Card: {job_card.job_number}")

        # Render the print template
        return render_template(
            'pm_job_card_print.html',
            job_card=job_card,
            now=datetime.now(),
            title=f"Print Job Card {job_card.job_number}"
        )

    except Exception as e:
        logging.error(f"--- Error loading print view for Job Card ID {id}: {e} ---", exc_info=True)
        flash(f"An error occurred while preparing the print view: {e}", "danger")
        # Redirect to job card detail view on error
        return redirect(url_for('planned_maintenance.job_card_detail', id=id))

@bp.route('/job_card/delete/<int:id>', methods=['POST'])
def delete_job_card(id):
    """Soft-deletes a job card by changing its status to 'Deleted' and logging the reason."""
    job_card = JobCard.query.get_or_404(id)
    
    # Only allow deletion of job cards with 'To Do' status
    if job_card.status != 'To Do':
        flash(f"Cannot delete job card {job_card.job_number}. Only job cards with 'To Do' status can be deleted.", "warning")
        return redirect(url_for('planned_maintenance.job_card_detail', id=id))
        
    if job_card.status == 'Deleted':
        flash(f"Job card {job_card.job_number} is already deleted.", "warning")
        return redirect(url_for('planned_maintenance.job_card_list'))
    
    # Get the deletion reason from the form
    delete_reason = request.form.get('delete_reason', '').strip()
    if not delete_reason:
        flash("Please provide a reason for deletion.", "warning")
        return redirect(url_for('planned_maintenance.job_card_detail', id=id))
    
    try:
        # Update the job card status and add the deletion reason to comments
        original_status = job_card.status
        
        # Prepare deletion comment
        deletion_comment = f"[DELETED on {datetime.now().strftime('%Y-%m-%d %H:%M')}]\nPrevious status: {original_status}\nReason: {delete_reason}"
        
        # Update job card
        job_card.status = 'Deleted'
        
        # Append to existing comments or create new
        if job_card.comments:
            job_card.comments = f"{job_card.comments}\n\n{deletion_comment}"
        else:
            job_card.comments = deletion_comment
        
        db.session.commit()
        logging.info(f"Job Card {job_card.job_number} (ID: {id}) has been soft-deleted. Previous status: {original_status}")
        
        flash(f"Job Card {job_card.job_number} has been successfully deleted.", "success")
        return redirect(url_for('planned_maintenance.job_card_list'))
        
    except Exception as e:
        db.session.rollback()
        logging.error(f"Error deleting Job Card {id}: {e}", exc_info=True)
        flash(f"Error deleting job card: {e}", "danger")
        return redirect(url_for('planned_maintenance.job_card_detail', id=id))

# ==============================================================================
# === Check Lists ===
# ==============================================================================

@bp.route('/checklist/new', methods=['POST'])
def new_checklist():
    """Logs a new equipment checklist."""
    # This route expects the form to be included elsewhere (e.g., dashboard modal)
    if request.method == 'POST':
        try:
            equipment_id = request.form.get('equipment_id', type=int)
            status = request.form.get('status')
            issues = request.form.get('issues') # Optional
            check_date_str = request.form.get('check_date')
            # *** Get the new operator name ***
            operator_name = request.form.get('operator', '').strip() # Get and strip whitespace

            # *** Update validation ***
            if not equipment_id or not status or not check_date_str or not operator_name:
                # Updated error message
                flash('Equipment, Status, Check Date/Time, and Operator Name are required for checklist.', 'warning')
                return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

            # Validate status value
            valid_statuses = ["Go", "Go But", "No Go"]
            if status not in valid_statuses:
                flash(f'Invalid status "{status}" selected.', 'warning')
                return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

            # *** Parse and Validate Date/Time *** (Existing code)
            try:
                parsed_dt = parse_datetime(check_date_str)
                check_date_utc = parsed_dt.replace(tzinfo=timezone.utc)
                logging.debug(f"Parsed check date string '{check_date_str}' to naive {parsed_dt}, assumed UTC: {check_date_utc} ({check_date_utc.tzinfo})")
            except ValueError:
                flash("Invalid Check Date/Time format submitted.", "warning")
                return redirect(request.referrer or url_for('planned_maintenance.dashboard'))
            except Exception as parse_err:
                logging.error(f"Error parsing check_date '{check_date_str}': {parse_err}", exc_info=True)
                flash(f"Error parsing check date: {parse_err}", "danger")
                return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

            equipment = Equipment.query.get(equipment_id)
            if not equipment:
                 flash('Selected equipment not found.', 'danger')
                 return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

            # *** Update Checklist creation to include operator ***
            new_log = Checklist(
                equipment_id=equipment_id,
                status=status,
                issues=issues,
                check_date=check_date_utc,
                operator=operator_name # Add the operator name here
            )
            db.session.add(new_log)
            db.session.commit()
            # Updated flash message
            flash(f'Checklist logged successfully by {operator_name} for {equipment.name} with status "{status}" at {check_date_utc.strftime("%Y-%m-%d %H:%M UTC")}.', 'success')

        except Exception as e:
            db.session.rollback()
            logging.error(f"Error logging checklist: {e}", exc_info=True)
            flash(f"Error logging checklist: {e}", "danger")

        return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

    # If accessed via GET, just redirect away
    return redirect(url_for('planned_maintenance.dashboard'))

# ==============================================================================
# === Task Status Calculation ===
# ==============================================================================
def calculate_task_due_status(task, current_time): # Accept current_time (naive UTC)
    logging.debug(f"    Calculating status for Task {task.id} ({task.description}) for Eq {task.equipment_id}. current_time = {current_time}")
    # Initialize return values with defaults
    status = "Unknown"
    due_info = "N/A"
    due_date = None # Initialize to None. Will be set to a datetime object if calculable.
    last_performed_info = "Never"
    next_due_info = "N/A" # Will store 'Next Due at X hours/km' or 'Due on YYYY-MM-DD'
    estimated_days_info = "N/A" # Will store '~Y days' or date info for 'days' tasks
    numeric_estimated_days = None # Store the raw number for comparison
    # --- Timezone handling ---
    # Ensure current_time is naive UTC
    if current_time.tzinfo is not None:
        current_time = current_time.astimezone(timezone.utc).replace(tzinfo=None)

    # Ensure last_performed_dt is naive UTC or None
    last_performed_dt = task.last_performed
    if last_performed_dt and last_performed_dt.tzinfo is not None:
        last_performed_dt = last_performed_dt.astimezone(timezone.utc).replace(tzinfo=None)
    # Update last_performed_info based on processed last_performed_dt
    last_performed_info = last_performed_dt.strftime('%Y-%m-%d %H:%M') if last_performed_dt else "Never"


    # ================== HOURS / KM LOGIC ==================
    if task.interval_type == 'hours' or task.interval_type == 'km':
        # --- Initialize variables specific to this block ---
        interval_unit = task.interval_type
        current_usage = None
        current_usage_date = None
        hours_until_next = None
        # --- End Initialization ---

        # 1. Get latest usage log
        latest_log = UsageLog.query.filter_by(equipment_id=task.equipment_id).order_by(desc(UsageLog.log_date)).first()
        if not latest_log:
            status = f"Unknown (No Usage Data)"
            # Return default values, ensure due_date is None
            return status, due_info, None, last_performed_info, next_due_info, estimated_days_info

        # We know latest_log exists
        current_usage = latest_log.usage_value
        current_usage_date = latest_log.log_date
        if current_usage_date and current_usage_date.tzinfo is not None:
             current_usage_date = current_usage_date.astimezone(timezone.utc).replace(tzinfo=None)

        # Temp storage for current state before calculating next due
        current_state_info = f"Current: {current_usage:.1f} {interval_unit}"

        # 2. Check if task was ever performed
        if not last_performed_dt: # Never performed logic
            status = "Never Performed"
            next_due_info = f"Due at {task.interval_value} {interval_unit} (First)"
            # Use local var name to avoid confusion with later hours_until_next
            local_hours_until_due = task.interval_value - current_usage
            if local_hours_until_due <= 0:
                status = "Overdue (First)"
                due_info = f"Over by {abs(local_hours_until_due):.1f} {interval_unit}"
            elif local_hours_until_due <= task.interval_value * 0.1: # 10% buffer
                status = "Due Soon (First)"
                due_info = f"{local_hours_until_due:.1f} {interval_unit} remaining"
            else:
                status = "OK (First)"
                due_info = f"{local_hours_until_due:.1f} {interval_unit} remaining"
            estimated_days_info = "N/A (First)" # Cannot estimate days accurately for first run yet
            # Return, ensuring due_date is None
            return status, due_info, None, last_performed_info, next_due_info, estimated_days_info

        # We know last_performed_dt exists
        # 3. Get usage value at the time of the last performance
        last_performed_usage = task.last_performed_usage_value
        if last_performed_usage is None: # Usage unknown at last performance
             status = f"Warning (Usage at Last Done Unknown)"
             # Update last_performed_info specifically for this case
             last_performed_info = f"{last_performed_dt.strftime('%Y-%m-%d %H:%M')} (Usage Unknown)"
             next_due_info = "Cannot Calculate"; estimated_days_info = "Cannot Calculate"
             # Return, ensuring due_date is None
             return status, due_info, None, last_performed_info, next_due_info, estimated_days_info

        # We know last_performed_usage exists
        # Update last_performed_info with known usage
        last_performed_info = f"{last_performed_dt.strftime('%Y-%m-%d %H:%M')} at {last_performed_usage:.1f} {interval_unit}"

        # 4. Calculate key metrics
        next_due_at_usage = last_performed_usage + task.interval_value
        hours_until_next = next_due_at_usage - current_usage # Assign hours_until_next
        next_due_info = f"Next due at {next_due_at_usage:.1f} {interval_unit}" # Define next due target
        due_info = f"{hours_until_next:.1f} {interval_unit} remaining" # Define remaining buffer

        # 5. Determine PRIMARY Status based on hours_until_next
        if hours_until_next <= 0:
            status = "Overdue"
            due_info = f"Overdue by {abs(hours_until_next):.1f} {interval_unit}"
        elif hours_until_next <= task.interval_value * 0.10: # 10% buffer
            status = "Due Soon"
        else:
            status = "OK"

        # 6. Estimate days until due
        # Find the log entry *before* the latest one to calculate rate
        previous_log = UsageLog.query.filter(
            UsageLog.equipment_id == task.equipment_id,
            UsageLog.log_date < current_usage_date # Find one strictly before latest
        ).order_by(desc(UsageLog.log_date)).first()

        if previous_log and current_usage_date > previous_log.log_date:
             prev_log_date = previous_log.log_date
             if prev_log_date and prev_log_date.tzinfo is not None:
                 prev_log_date = prev_log_date.astimezone(timezone.utc).replace(tzinfo=None)

             usage_diff = current_usage - previous_log.usage_value
             time_diff = current_usage_date - prev_log_date
             time_diff_days = time_diff.total_seconds() / (24 * 3600)

             if time_diff_days > 0 and usage_diff >= 0: # Avoid division by zero and negative time/usage diffs
                 avg_daily_usage = usage_diff / time_diff_days
                 logging.debug(f"    Task {task.id}: Avg daily usage = {avg_daily_usage:.2f} {interval_unit}/day")

                 if avg_daily_usage > 0.01: # Check for minimal usage rate to avoid huge day estimates
                     # --- Calculate numeric estimated days ---
                     numeric_estimated_days = hours_until_next / avg_daily_usage

                     # --- Calculate estimated due_date object ---
                     try:
                         # Calculate the estimated date based on naive current_time
                         # Combine date with time.min for a full datetime object
                         estimated_date_only = (current_time + timedelta(days=numeric_estimated_days)).date()
                         due_date = datetime.combine(estimated_date_only, time.min) # Assign to due_date
                         logging.debug(f"    Task {task.id}: Estimated due_date object set to: {due_date}")
                     except OverflowError:
                         logging.warning(f"    Task {task.id}: OverflowError calculating estimated due date (numeric_estimated_days={numeric_estimated_days}). due_date remains None.")
                         due_date = None # Ensure it's None on overflow
                     except Exception as date_calc_err:
                         logging.error(f"    Task {task.id}: Error calculating estimated due_date object: {date_calc_err}", exc_info=True)
                         due_date = None # Ensure it's None on other calculation errors

                     # --- Format the estimated_days_info string ---
                     if status == "Overdue": # Although estimated_days might be positive if usage decreased
                         estimated_days_info = f"~{abs(numeric_estimated_days):.1f} days Overdue (est.)"
                     elif numeric_estimated_days >= 0 :
                         estimated_days_info = f"~{numeric_estimated_days:.1f} days (est.)"
                     else: # Should not happen if status isn't Overdue, but handle anyway
                         estimated_days_info = f"~{abs(numeric_estimated_days):.1f} days ago (est.)"

                 else:
                     estimated_days_info = "N/A (Low Usage Rate)"
                     due_date = None # Cannot estimate date if rate is too low
             else:
                 estimated_days_info = "N/A (Rate Calc Error)" # e.g. time diff is zero or negative
                 due_date = None
        else:
            estimated_days_info = "N/A (Insufficient Data)" # Need at least two logs
            due_date = None

        # 7. Refine status based on estimated days, if currently "OK"
        if status == "OK" and numeric_estimated_days is not None and numeric_estimated_days <= DUE_SOON_ESTIMATED_DAYS_THRESHOLD:
            logging.debug(f"    Task {task.id}: Status was OK, but estimated days ({numeric_estimated_days:.1f}) <= threshold ({DUE_SOON_ESTIMATED_DAYS_THRESHOLD}). Changing status to Due Soon.")
            status = "Due Soon"

        # Return potentially calculated due_date object
        return status, due_info, due_date, last_performed_info, next_due_info, estimated_days_info

    # ================== DAYS LOGIC ==================
    elif task.interval_type == 'days':
        if last_performed_dt:
            # Calculate the actual due date+time
            due_datetime = last_performed_dt + timedelta(days=task.interval_value)
            # Assign the calculated datetime object to due_date
            due_date = due_datetime
            logging.debug(f"    Task {task.id} ('days'): Calculated due_date = {due_date} (naive)")
            try:
                # Use due_date (the datetime obj) for calculations now
                time_difference = due_date - current_time
                days_until_due = time_difference.days
                total_seconds_until_due = time_difference.total_seconds()

                # Determine status based on actual days remaining
                if total_seconds_until_due < 0:
                    status = "Overdue"
                    due_info = f"Due on {due_date.strftime('%Y-%m-%d')} ({abs(days_until_due)} days ago)"
                # Use the SAME threshold constant for consistency
                elif days_until_due <= DUE_SOON_ESTIMATED_DAYS_THRESHOLD:
                    status = "Due Soon"
                    due_info = f"Due on {due_date.strftime('%Y-%m-%d')} (in {days_until_due} days)"
                else:
                    status = "OK"
                    due_info = f"Due on {due_date.strftime('%Y-%m-%d')} (in {days_until_due} days)"

                # Update last_performed_info (already set earlier based on processed dt)
                # Use due_date (the datetime obj) for formatting next due info
                next_due_info = f"Due on {due_date.strftime('%Y-%m-%d')}"
                estimated_days_info = due_info # For days tasks, due_info *is* the estimate

            except TypeError as calc_err:
                 logging.error(f"    Task {task.id}: TYPE ERROR during 'days_until_due' calculation!", exc_info=True)
                 status, due_info, due_date, last_performed_info, next_due_info = "Error Calculating Due", "Calculation Error", None, "Error", "Error"
                 estimated_days_info = "Error" # Ensure estimate also shows error
        else:
            # Never performed logic (due_date remains None)
            status = "Never Performed"
            # last_performed_info is already "Never"
            next_due_info = "N/A (First)"
            due_info = "N/A (First)"
            estimated_days_info = "N/A (First)" # No estimate if never performed

        # Return due_date (which is either calculated datetime or None)
        return status, due_info, due_date, last_performed_info, next_due_info, estimated_days_info
    # ===================================================================

    # ================== UNKNOWN TYPE ==================
    else: # Unknown interval type
        status = "Unknown Interval Type"
        due_info = task.interval_type
        # last_performed_info is already set based on processed dt
        # Return, due_date remains None
        return status, due_info, None, last_performed_info, next_due_info, estimated_days_info

# ==============================================================================
# === Tasks List ===
# ==============================================================================
@bp.route('/tasks', methods=['GET'])
def tasks_list():
    logging.debug("--- Entering tasks_list route ---")
    try:
        type_filter = request.args.get('type')
        query = MaintenanceTask.query.join(Equipment).filter(
            or_(
                MaintenanceTask.is_legal_compliance.is_(False),
                MaintenanceTask.is_legal_compliance.is_(None)  # Handle NULL values too
            )
        )
        if type_filter:
            query = query.filter(Equipment.type == type_filter)
        # Fetch tasks WITH equipment eagerly loaded to avoid N+1 in loops
        all_tasks_query = query.options(db.joinedload(MaintenanceTask.equipment_ref)).order_by(Equipment.code, Equipment.name, MaintenanceTask.id).all()

        current_time_for_list = datetime.utcnow() # Naive UTC
        logging.debug(f"Tasks List using current_time: {current_time_for_list}")

        tasks_by_equipment = defaultdict(list) # Use defaultdict for easier appending
        for task in all_tasks_query:
            # Ensure equipment_ref is loaded (should be by joinedload)
            if not task.equipment_ref:
                logging.error(f"Task {task.id} is missing equipment reference. Skipping.")
                continue
            eq_key = (task.equipment_ref.code, task.equipment_ref.name, task.equipment_ref.type)
            # Calculate status and add attributes directly to the task object
            status, due_info, due_date_val, last_performed_info, next_due_info, estimated_days_info = \
                calculate_task_due_status(task, current_time_for_list)
            task.due_status = status # Store the full status string
            task.due_info = due_info
            task.due_date = due_date_val
            task.last_performed_info = last_performed_info
            task.next_due_info = next_due_info
            task.estimated_days_info = estimated_days_info
            tasks_by_equipment[eq_key].append(task)

        # --- Define Status Priority AND Header Label Mapping ---
        # Priority: Lower number = higher priority
        # Label: The text to display in the header badge
        status_config = {
            # Base Status Key: (Priority, Header Label)
            'Overdue':          (1, 'Overdue'),
            'Due Soon':         (2, 'Due Soon'),
            'Warning':          (3, 'Warning'),
            'Error':            (4, 'Error'), # Separate Error from Warning?
            'Never Performed':  (5, 'Never Done'),
            'OK':               (6, 'OK'),
            'Unknown':          (7, 'Unknown')
            # Add other base statuses returned by calculate_task_due_status if needed
        }
        default_priority = 99
        default_label = 'Unknown'
        ok_priority = status_config.get('OK', (default_priority, default_label))[0]
        ok_label = status_config.get('OK', (default_priority, default_label))[1]


        # --- Process tasks for sorting and header status ---
        processed_tasks_data = {}
        # Sort equipment keys alphabetically first
        sorted_equipment_keys = sorted(tasks_by_equipment.keys())

        for eq_key in sorted_equipment_keys:
            tasks_list_for_eq = tasks_by_equipment[eq_key]
            highest_priority_level_found = ok_priority # Start assuming OK priority

            # --- First pass: Find the highest priority level in the group ---
            for task in tasks_list_for_eq:
                # Ensure task has a status string before processing
                if not hasattr(task, 'due_status') or not isinstance(task.due_status, str):
                    logging.warning(f"Task {getattr(task, 'id', 'N/A')} missing valid due_status attribute. Assigning default priority.")
                    current_prio = default_priority
                else:
                    # --- CORRECTED: Extract base status reliably ---
                    # Handle variations like 'Overdue (First)', 'Due Soon (First)', 'Unknown (No Usage)' etc.
                    # We want the core status word recognized by status_config keys
                    current_status_str = task.due_status # e.g., "Due Soon (First)"
                    current_prio = default_priority # Default if no match found below
                    matched_base = None

                    # Check for specific keywords IN ORDER OF PRIORITY (most specific first)
                    # This ensures "Due Soon" is caught before just "Due" if that were a key
                    if 'Overdue' in current_status_str: matched_base = 'Overdue'
                    elif 'Due Soon' in current_status_str: matched_base = 'Due Soon'
                    elif 'Warning' in current_status_str: matched_base = 'Warning'
                    elif 'Error' in current_status_str: matched_base = 'Error'
                    elif 'Never Performed' in current_status_str: matched_base = 'Never Performed'
                    elif 'OK' in current_status_str: matched_base = 'OK' # Must be checked after more specific ones
                    elif 'Unknown' in current_status_str: matched_base = 'Unknown'
                    # Add elif for other specific statuses if needed

                    # Get priority from config if a base status was matched
                    if matched_base and matched_base in status_config:
                        current_prio = status_config[matched_base][0]
                    else:
                        # Log if status wasn't mapped - indicates potential need to update status_config
                         logging.warning(f"Status '{current_status_str}' for task {task.id} did not map to a known base status in status_config. Using default priority.")
                         # current_prio remains default_priority

                # Update the highest priority level found so far
                if current_prio < highest_priority_level_found:
                    highest_priority_level_found = current_prio
                    logging.debug(f"  Eq {eq_key}: New highest prio level found: {highest_priority_level_found} from task {task.id} (status: {task.due_status})")


            # --- Determine the final header label based on the highest priority level found ---
            final_header_label = default_label # Default
            # Find the label corresponding to the highest priority level achieved
            for base_status, (prio, label) in status_config.items():
                if prio == highest_priority_level_found:
                    final_header_label = label
                    break # Found the matching label
            logging.debug(f"Eq {eq_key}: Final highest prio level: {highest_priority_level_found}, Assigned Header Label: '{final_header_label}'")

            # --- Second pass (or combined): Sort the tasks list itself ---
            # Use a stable sort key function referencing the config
            def get_tasks_sort_key(task_item):
                sort_prio = default_priority # Default
                sort_matched_base = None
                if hasattr(task_item, 'due_status') and isinstance(task_item.due_status, str):
                    status_str = task_item.due_status
                    # Use same matching logic as priority finding for consistency
                    if 'Overdue' in status_str: sort_matched_base = 'Overdue'
                    elif 'Due Soon' in status_str: sort_matched_base = 'Due Soon'
                    elif 'Warning' in status_str: sort_matched_base = 'Warning'
                    elif 'Error' in status_str: sort_matched_base = 'Error'
                    elif 'Never Performed' in status_str: sort_matched_base = 'Never Performed'
                    elif 'OK' in status_str: sort_matched_base = 'OK'
                    elif 'Unknown' in status_str: sort_matched_base = 'Unknown'

                    if sort_matched_base and sort_matched_base in status_config:
                        sort_prio = status_config[sort_matched_base][0]

                # Add secondary sort criteria if needed (e.g., by due date/remaining value within the same status)
                # return (sort_prio, task_item.due_date or date.max) # Example secondary sort
                return sort_prio

            try:
                 tasks_list_for_eq.sort(key=get_tasks_sort_key)
            except Exception as eq_sort_exc:
                logging.error(f"Error sorting tasks for equipment {eq_key}: {eq_sort_exc}", exc_info=True)
                # Use unsorted list on error

            # Store the sorted list and the final header status label
            processed_tasks_data[eq_key] = {
                'tasks': tasks_list_for_eq,
                'header_status': final_header_label # Use the label derived from the highest priority
            }

        equipment_types = db.session.query(Equipment.type).distinct().order_by(Equipment.type).all()
        equipment_types = [t[0] for t in equipment_types]

        logging.debug("--- Rendering pm_tasks.html ---")
        return render_template(
            'pm_tasks.html',
            tasks_data=processed_tasks_data, # Pass the new structure
            equipment_types=equipment_types,
            type_filter=type_filter,
            title='Maintenance Tasks'
        )
    except Exception as e:
        logging.error(f"--- Error loading tasks_list page: {e} ---", exc_info=True)
        flash(f"Error loading maintenance tasks: {e}", "danger")
        # Pass empty data structure to avoid template errors
        return render_template('pm_tasks.html',
                               tasks_data={},
                               equipment_types=[],
                               type_filter=request.args.get('type'),
                               title='Maintenance Tasks',
                               error=True)

# ==============================================================================
# === Tasks ===
# ==============================================================================
@bp.route('/task/add', methods=['GET', 'POST'])
def add_task():
    """Displays form to add a new task (GET) or processes addition (POST)."""
    if request.method == 'POST':
        try:
            equipment_id = request.form.get('equipment_id', type=int)
            description = request.form.get('description')
            interval_type = request.form.get('interval_type')
            interval_value_str = request.form.get('interval_value')
            oem_required = 'oem_required' in request.form
            kit_required = 'kit_required' in request.form

            # Validation
            errors = []
            if not equipment_id: errors.append("Equipment is required.")
            if not description: errors.append("Description is required.")
            if not interval_type: errors.append("Interval Type is required.")
            if not interval_value_str: errors.append("Interval Value is required.")

            interval_value = 0
            if interval_value_str:
                try:
                    interval_value = int(interval_value_str)
                    if interval_value <= 0:
                        errors.append("Interval Value must be positive.")
                except ValueError:
                    errors.append("Interval Value must be a whole number.")

            if errors:
                for error in errors: flash(error, 'warning')
                # Re-render form with errors (need equipment list again)
                equipment_list = Equipment.query.order_by(Equipment.name).all()
                return render_template('pm_task_form.html', equipment=equipment_list, title="Add New Task")


            new_task = MaintenanceTask(
                equipment_id=equipment_id,
                description=description,
                interval_type=interval_type,
                interval_value=interval_value,
                oem_required=oem_required,
                kit_required=kit_required
            )
            db.session.add(new_task)
            db.session.commit()
            flash(f'Maintenance task "{description}" added successfully!', 'success')
            return redirect(url_for('planned_maintenance.tasks_list'))

        except Exception as e:
            db.session.rollback()
            flash(f"Error adding task: {e}", "danger")
            # Re-render form on error (need equipment list again)
            equipment_list = Equipment.query.order_by(Equipment.name).all()
            return render_template('pm_task_form.html', equipment=equipment_list, title="Add New Task")

    # --- Handle GET Request ---
    try:
        equipment_list = Equipment.query.order_by(Equipment.name).all()
    except Exception as e:
        flash(f"Error loading equipment list for form: {e}", "danger")
        equipment_list = []

    return render_template('pm_task_form.html', equipment=equipment_list, title="Add New Task")

@bp.route('/task/edit/<int:id>', methods=['GET', 'POST'])
def edit_task(id):
    """Displays form to edit a task (GET) or processes update (POST)."""
    task_to_edit = MaintenanceTask.query.get_or_404(id)

    if request.method == 'POST':
        try:
            # Get data from form
            equipment_id = request.form.get('equipment_id', type=int)
            description = request.form.get('description')
            interval_type = request.form.get('interval_type')
            interval_value_str = request.form.get('interval_value')
            oem_required = 'oem_required' in request.form
            kit_required = 'kit_required' in request.form
            # Allow editing the legal compliance flag
            is_legal_compliance = 'is_legal_compliance' in request.form

            # Validation (same as add_task)
            errors = []
            if not equipment_id: errors.append("Equipment is required.")
            if not description: errors.append("Description is required.")
            if not interval_type: errors.append("Interval Type is required.")
            if not interval_value_str: errors.append("Interval Value is required.")

            interval_value = 0
            if interval_value_str:
                try:
                    interval_value = int(interval_value_str)
                    if interval_value <= 0:
                        errors.append("Interval Value must be positive.")
                except ValueError:
                    errors.append("Interval Value must be a whole number.")

            if errors:
                for error in errors: flash(error, 'warning')
                # Re-render edit form with validation errors
                equipment_list = Equipment.query.order_by(Equipment.name).all()
                # <<< PASS BACK SUBMITTED DATA TO REPOPULATE >>>
                submitted_data = request.form.to_dict() # Get submitted form data
                submitted_data['id'] = id # Keep the ID
                submitted_data['oem_required'] = oem_required # Reflect submitted checkbox state
                submitted_data['kit_required'] = kit_required
                submitted_data['is_legal_compliance'] = is_legal_compliance # Reflect submitted checkbox state
                return render_template('pm_task_edit_form.html',
                                       equipment=equipment_list,
                                       task=submitted_data, # Pass submitted data
                                       title=f"Edit Task: {task_to_edit.description}")

            # --- Update the task object in the database ---
            task_to_edit.equipment_id = equipment_id
            task_to_edit.description = description
            task_to_edit.interval_type = interval_type
            task_to_edit.interval_value = interval_value
            task_to_edit.oem_required = oem_required
            task_to_edit.kit_required = kit_required
            task_to_edit.is_legal_compliance = is_legal_compliance # Update the flag

            db.session.commit()
            flash(f'Task "{task_to_edit.description}" updated successfully!', 'success')

            # Redirect to the appropriate task list based on the (potentially updated) type
            if task_to_edit.is_legal_compliance:
                 return redirect(url_for('planned_maintenance.legal_tasks_list'))
            else:
                 return redirect(url_for('planned_maintenance.tasks_list'))


        except Exception as e:
            db.session.rollback()
            logging.error(f"Error updating task (id: {id}): {e}", exc_info=True)
            flash(f"Error updating task: {e}", "danger")
            # Re-render edit form on unexpected error
            equipment_list = Equipment.query.order_by(Equipment.name).all()
            # Pass the original task object back after rollback
            return render_template('pm_task_edit_form.html',
                                   equipment=equipment_list,
                                   task=task_to_edit, # Pass original object
                                   title=f"Edit Task: {task_to_edit.description}")

    # --- Handle GET Request ---
    # Fetch equipment list for the dropdown
    try:
        equipment_list = Equipment.query.order_by(Equipment.name).all()
    except Exception as e:
        flash(f"Error loading equipment list for form: {e}", "danger")
        equipment_list = []
        # Redirect if equipment can't be loaded? Or show form without dropdown?
        # Let's show the form but the dropdown will be empty.
        # Alternatively, redirect:
        # return redirect(url_for('planned_maintenance.tasks_list'))

    # Render the edit form, passing the existing task object
    return render_template('pm_task_edit_form.html',
                           equipment=equipment_list,
                           task=task_to_edit, # Pass the DB object for pre-filling
                           title=f"Edit Task: {task_to_edit.description}")

# ==============================================================================
# === Usage Log ===
# ==============================================================================
@bp.route('/usage/add', methods=['POST'])
def add_usage():
    """Adds a usage log entry with enhanced validation."""
    if request.method == 'POST':
        try:
            equipment_id = request.form.get('equipment_id', type=int)
            usage_value_str = request.form.get('usage_value')
            log_date_str = request.form.get('log_date') # Optional, defaults to now

            # Basic form data validation
            if not equipment_id or not usage_value_str:
                flash("Equipment and Usage Value are required.", "warning")
                return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

            try:
                usage_value = float(usage_value_str)
                if usage_value < 0:
                    flash("Usage Value cannot be negative.", "warning")
                    return redirect(request.referrer or url_for('planned_maintenance.dashboard'))
            except ValueError:
                 flash("Invalid Usage Value. Please enter a number.", "warning")
                 return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

            # Parse log_date_str to a timezone-aware UTC datetime object
            log_date_dt = datetime.now(timezone.utc) # Default to now (UTC)
            if log_date_str:
                try:
                    parsed_naive = parse_datetime(log_date_str)
                    log_date_dt = parsed_naive.replace(tzinfo=timezone.utc) # Assume naive input is UTC
                    logging.debug(f"Parsed usage log_date string '{log_date_str}' to UTC: {log_date_dt}")
                except ValueError:
                    flash("Invalid Log Date format. Using current UTC time.", "info")
                except Exception as parse_err:
                    logging.error(f"Error parsing usage log_date '{log_date_str}': {parse_err}", exc_info=True)
                    flash(f"Error parsing log date: {parse_err}. Using current UTC time.", "warning")
            
            # Ensure equipment exists
            equipment = Equipment.query.get(equipment_id)
            if not equipment:
                flash(f"Equipment with ID {equipment_id} not found.", "danger")
                return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

            # --- Validation 1: Prevent duplicate records (same machine, same datetime) ---
            existing_log_at_same_time = UsageLog.query.filter_by(
                equipment_id=equipment_id, 
                log_date=log_date_dt
            ).first()
            if existing_log_at_same_time:
                flash(f"A usage log for {equipment.code} already exists at {log_date_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}.", "warning")
                return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

            # --- Fetch previous and next logs for chronological validation ---
            # Previous log: latest log strictly before the new log's datetime
            previous_log = UsageLog.query.filter(
                UsageLog.equipment_id == equipment_id,
                UsageLog.log_date < log_date_dt
            ).order_by(UsageLog.log_date.desc()).first()

            # Next log: earliest log strictly after the new log's datetime
            next_log = UsageLog.query.filter(
                UsageLog.equipment_id == equipment_id,
                UsageLog.log_date > log_date_dt
            ).order_by(UsageLog.log_date.asc()).first()

            # --- Validation 2a: Usage cannot be less than previously recorded ---
            if previous_log and usage_value < previous_log.usage_value:
                prev_log_time_str = previous_log.log_date.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
                flash(f"Usage value ({usage_value:.2f}) cannot be less than the previous log's value "
                      f"({previous_log.usage_value:.2f} recorded on {prev_log_time_str}).", "warning")
                return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

            # --- Validation 2b: Usage cannot be more than a subsequent existing log ---
            if next_log and usage_value > next_log.usage_value:
                next_log_time_str = next_log.log_date.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
                flash(f"Usage value ({usage_value:.2f}) cannot be greater than the next log's value "
                      f"({next_log.usage_value:.2f} recorded on {next_log_time_str}). "
                      f"You might be trying to insert a log out of sequence.", "warning")
                return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

            # --- Validation 3: Limit unreasonable usage increase ---
            if previous_log:
                usage_difference = usage_value - previous_log.usage_value
                # Ensure previous_log.log_date is timezone-aware UTC for accurate calculations
                prev_log_date_utc = previous_log.log_date
                if prev_log_date_utc.tzinfo is None: # Should not happen if model default is used
                    prev_log_date_utc = prev_log_date_utc.replace(tzinfo=timezone.utc)
                else:
                    prev_log_date_utc = prev_log_date_utc.astimezone(timezone.utc)

                # Calculate calendar days spanned
                calendar_days_spanned = (log_date_dt.date() - prev_log_date_utc.date()).days
                
                current_max_increase_threshold = MAX_REASONABLE_DAILY_USAGE_INCREASE
                # Future enhancement: current_max_increase_threshold could be set based on equipment.usage_unit
                
                if calendar_days_spanned == 0: # Same calendar day
                    if usage_difference > current_max_increase_threshold:
                        flash(f"Usage increase of {usage_difference:.2f} on the same day ({log_date_dt.strftime('%Y-%m-%d')}) "
                              f"is too high. Max allowed daily increase is {current_max_increase_threshold:.2f}.", "warning")
                        return redirect(request.referrer or url_for('planned_maintenance.dashboard'))
                elif calendar_days_spanned > 0: # Spans one or more full calendar days
                    max_allowed_increase_for_period = current_max_increase_threshold * calendar_days_spanned
                    if usage_difference > max_allowed_increase_for_period:
                        flash(f"Usage increase of {usage_difference:.2f} over {calendar_days_spanned} day(s) "
                              f"(from {prev_log_date_utc.strftime('%Y-%m-%d')} to {log_date_dt.strftime('%Y-%m-%d')}) "
                              f"is too high. Max allowed for this period is {max_allowed_increase_for_period:.2f} "
                              f"(based on {current_max_increase_threshold:.2f}/day).", "warning")
                        return redirect(request.referrer or url_for('planned_maintenance.dashboard'))
                # No 'else' needed for calendar_days_spanned < 0, as previous_log query ensures it's earlier.

            # All validations passed, create and save the log
            new_usage_log = UsageLog(equipment_id=equipment_id, usage_value=usage_value, log_date=log_date_dt)
            db.session.add(new_usage_log)
            db.session.commit()
            flash("Usage log added successfully.", "success")

        except Exception as e:
            db.session.rollback()
            logging.error(f"Error adding usage log: {e}", exc_info=True)
            flash(f"An unexpected error occurred while adding usage log: {e}", "danger")

        return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

    # If accessed via GET, just redirect away
    return redirect(url_for('planned_maintenance.dashboard'))
# ==============================================================================
# === Create Job Card From Task ===
# ==============================================================================

def generate_next_job_number(job_type='MAINT'):
    """
    Generates the next sequential job number with type distinction.
    
    Args:
        job_type (str): Type of job - 'MAINT' for maintenance or 'LEGAL' for legal compliance
                       This will add a prefix to the job number
    
    Returns:
        str: Formatted job number with type distinction
    """
    # Validate and standardize job_type
    if job_type.upper() not in ['MAINT', 'LEGAL']:
        logging.warning(f"Invalid job_type '{job_type}'. Defaulting to 'MAINT'.")
        job_type = 'MAINT'
    
    job_type = job_type.upper()  # Ensure uppercase
    
    # Define prefix for the specific job type
    if job_type == 'LEGAL':
        type_prefix = 'LC'  # Legal Compliance
    else:
        type_prefix = 'JC'  # Job Card (maintenance)
    
    # Create date portion of the prefix
    date_prefix = f"{datetime.now(timezone.utc).strftime('%y')}"
    
    # Complete prefix including type and date
    prefix = f"{type_prefix}-{date_prefix}-"
    
    # Find the last job card with this prefix
    last_jc = JobCard.query.filter(JobCard.job_number.like(f"{prefix}%")).order_by(JobCard.id.desc()).first()
    
    next_num = 1
    
    if last_jc:
        try:
            last_num_part = last_jc.job_number[len(prefix):]
            next_num = int(last_num_part) + 1
        except (ValueError, IndexError):
            logging.warning(f"Could not parse sequence number from last job card '{last_jc.job_number}'. Resetting sequence for prefix {prefix}.")
            next_num = 1  # Reset if parsing fails
    
    return f"{prefix}{next_num:04d}"  # e.g., "JC-24-0001" or "LC-24-0001"
# ==============================================================================
# === Log Views ===
# ==============================================================================

@bp.route('/checklist_logs', methods=['GET'])
def checklist_logs():
    """Displays checklist logs in a matrix: equipment vs. last 10 days (grouped by date)."""
    logging.debug("--- Request for Checklist Log 10-Day Matrix View ---")
    try:
        # 1. Determine Date Range (Last 10 days including today)
        today = date.today()
        start_date = today - timedelta(days=9)
        dates_in_range = [start_date + timedelta(days=i) for i in range(10)] # List of date objects
        logging.debug(f"Date range for checklist matrix: {start_date} to {today}")

        # 2. Fetch all Equipment
        all_equipment = Equipment.query.order_by(Equipment.code).all()
        if not all_equipment:
            flash("No equipment found.", "info")
            return render_template('pm_checklist_logs_matrix.html',
                                   all_equipment=[],
                                   dates_in_range=[],
                                   processed_data={},
                                   title='Checklist Log Matrix (Last 10 Days)')

        equipment_ids = [eq.id for eq in all_equipment]

        # 3. Fetch Relevant Logs within the date range
        range_start_dt_utc = datetime.combine(start_date, time.min).replace(tzinfo=timezone.utc)
        range_end_dt_utc = datetime.combine(today, time.max).replace(tzinfo=timezone.utc)

        relevant_logs_query = Checklist.query.filter(
            Checklist.equipment_id.in_(equipment_ids),
            Checklist.check_date >= range_start_dt_utc,
            Checklist.check_date <= range_end_dt_utc
        ).options(
            db.joinedload(Checklist.equipment_ref)
        ).order_by(
            Checklist.equipment_id,
            Checklist.check_date # Order chronologically
        )

        relevant_logs = relevant_logs_query.all()
        logging.debug(f"Found {len(relevant_logs)} checklist logs within the date range.")

        # 4. Process logs into the desired structure: {eq_id: {date: {'count': N, 'latest_status': S, ..., 'logs': [...]}}}
        processed_data = defaultdict(lambda: defaultdict(lambda: {'count': 0, 'logs': [], 'latest_log': None}))

        for log in relevant_logs:
            eq_id = log.equipment_id

            # Ensure log_date is treated as UTC before getting the date part
            log_date_utc = log.check_date
            if log_date_utc.tzinfo is None:
                log_date_utc = log_date_utc.replace(tzinfo=timezone.utc)
            else:
                log_date_utc = log_date_utc.astimezone(timezone.utc)

            log_date_only = log_date_utc.date()

            if start_date <= log_date_only <= today:
                cell_data = processed_data[eq_id][log_date_only]
                cell_data['count'] += 1
                # Store essential info for modal list
                cell_data['logs'].append({
                    'id': log.id,
                    'status': log.status,
                    'issues': log.issues or '',
                    'operator': log.operator or 'N/A',
                    'timestamp': log_date_utc.strftime('%Y-%m-%d %H:%M UTC')
                })
                # Keep track of the latest log for display in the cell
                # Since logs are ordered by date ascending, the last one processed for the day is the latest
                cell_data['latest_log'] = {
                    'status': log.status,
                    'operator': log.operator or 'N/A',
                    'comments': log.issues or "",
                    'full_timestamp_str': log_date_utc.strftime('%Y-%m-%d %H:%M UTC')
                 }


        logging.debug(f"Processed checklist data structure contains entries for {len(processed_data)} equipment.")
        view_title = 'Checklist Log Matrix (Last 10 Days)'
        base_url = url_for('planned_maintenance.dashboard', _external=False).rstrip('/')
        # 5. Render the template
        return render_template('pm_checklist_logs_matrix.html', 
                          base_url=base_url, 
                          title="Checklist Logs Matrix", 
                          all_equipment=all_equipment, 
                          dates_in_range=dates_in_range, 
                          processed_data=processed_data)

    except Exception as e:
        logging.error(f"--- Error loading checklist log matrix view: {e} ---", exc_info=True)
        flash(f"An error occurred while loading the checklist logs: {e}", "danger")
        return render_template('pm_checklist_logs_matrix.html',
                               all_equipment=[],
                               dates_in_range=[],
                               processed_data={},
                               error=f"Could not load logs: {e}",
                               title='Checklist Log Matrix - Error')

@bp.route('/usage_logs', methods=['GET'])
def usage_logs():
    """Displays usage logs in a matrix: equipment vs. last 10 days."""
    logging.debug("--- Request for Usage Log 10-Day Matrix View ---")
    try:
        # 1. Determine Date Range (Last 10 days including today)
        today = date.today()
        start_date = today - timedelta(days=9)
        dates_in_range = [start_date + timedelta(days=i) for i in range(10)] # List of date objects
        logging.debug(f"Date range for usage matrix: {start_date} to {today}")

        # 2. Fetch all Equipment (Consider adding filters back later if needed)
        all_equipment = Equipment.query.order_by(Equipment.code).all()
        if not all_equipment:
            flash("No equipment found.", "info")
            return render_template('pm_usage_logs.html',
                                   all_equipment=[],
                                   dates_in_range=[],
                                   processed_data={},
                                   title='Usage Log Matrix (Last 10 Days)')

        equipment_ids = [eq.id for eq in all_equipment]

        # 3. Fetch Relevant Logs within the date range (Ensure timezone handling)
        # Convert date range to timezone-aware datetimes for comparison
        range_start_dt_utc = datetime.combine(start_date, time.min).replace(tzinfo=timezone.utc)
        # Go to the very end of the 'today' date for inclusive range
        range_end_dt_utc = datetime.combine(today, time.max).replace(tzinfo=timezone.utc)

        # Assume UsageLog.log_date is stored as UTC DateTime
        relevant_logs_query = UsageLog.query.filter(
            UsageLog.equipment_id.in_(equipment_ids),
            UsageLog.log_date >= range_start_dt_utc,
            UsageLog.log_date <= range_end_dt_utc
        ).options(
            db.joinedload(UsageLog.equipment_ref) # Still useful if accessing eq details
        ).order_by(
            UsageLog.equipment_id,
            UsageLog.log_date # Order chronologically for processing
        )

        relevant_logs = relevant_logs_query.all()
        logging.debug(f"Found {len(relevant_logs)} usage logs within the date range.")

        # 4. Process logs into the desired structure: {eq_id: {date: {'count': N, 'total': X, 'logs': [...]}}}
        processed_data = defaultdict(lambda: defaultdict(lambda: {'count': 0, 'total': 0.0, 'logs': []}))

        for log in relevant_logs:
            eq_id = log.equipment_id
            # Ensure log_date is treated as UTC before getting the date part
            log_date_utc = log.log_date
            if log_date_utc.tzinfo is None:
                # If stored naive, assume it's UTC (adjust if your DB stores local time)
                log_date_utc = log_date_utc.replace(tzinfo=timezone.utc)
            else:
                # Convert to UTC if it has another timezone
                log_date_utc = log_date_utc.astimezone(timezone.utc)

            log_date_only = log_date_utc.date() # Get the date part after ensuring UTC

            # Check if the date falls within our display range (it should due to query, but belts & braces)
            if start_date <= log_date_only <= today:
                cell_data = processed_data[eq_id][log_date_only]
                cell_data['count'] += 1
                cell_data['total'] += log.usage_value
                # Append essential log info for potential modal display
                cell_data['logs'].append({
                    'id': log.id,
                    'value': log.usage_value,
                    'timestamp': log_date_utc.strftime('%Y-%m-%d %H:%M UTC') # Store formatted string
                })

        logging.debug(f"Processed usage data structure contains entries for {len(processed_data)} equipment.")
        view_title = 'Usage Log Matrix (Last 10 Days)'

        # 5. Render the template
        base_url = url_for('planned_maintenance.dashboard', _external=False).rstrip('/')
        return render_template('pm_usage_logs.html',      # *** CORRECTED TEMPLATE NAME ***
                          base_url=base_url,               # You might not need base_url anymore if you removed it earlier
                          title="Usage Logs Matrix",
                          all_equipment=all_equipment,
                          dates_in_range=dates_in_range,
                          processed_data=processed_data)
    except Exception as e:
        logging.error(f"--- Error loading usage log matrix view: {e} ---", exc_info=True)
        flash(f"An error occurred while loading the usage logs: {e}", "danger")
        return render_template('pm_usage_logs.html',
                               all_equipment=[],
                               dates_in_range=[],
                               processed_data={},
                               error=f"Could not load logs: {e}",
                               title='Usage Log Matrix - Error')
# Add these routes to routes.py

# ==============================================================================
# === Legal Compliance Tasks List ===
# ==============================================================================

@bp.route('/legal_tasks', methods=['GET'])
def legal_tasks_list():
    logging.debug("--- Entering legal_tasks_list route ---")
    try:
        type_filter = request.args.get('type')
        status_filter = request.args.get('status')
        
        # First join Equipment to get access to its properties
        query = MaintenanceTask.query.join(Equipment).filter(MaintenanceTask.is_legal_compliance == True)
        
        # Add filter for operational equipment only (unless overridden by status filter)
        if status_filter:
            # If a specific status is requested, filter by that
            query = query.filter(Equipment.status == status_filter)
        else:
            # By default, only show tasks for operational equipment
            query = query.filter(Equipment.status == 'Operational')
            
        # Apply equipment type filter if provided
        if type_filter:
            query = query.filter(Equipment.type == type_filter)
            
        # Fetch tasks WITH equipment eagerly loaded to avoid N+1 in loops
        all_tasks_query = query.options(db.joinedload(MaintenanceTask.equipment_ref)).order_by(Equipment.code, Equipment.name, MaintenanceTask.id).all()

        current_time_for_list = datetime.utcnow() # Naive UTC
        logging.debug(f"Legal Tasks List using current_time: {current_time_for_list}")

        tasks_by_equipment = defaultdict(list) # Use defaultdict for easier appending
        for task in all_tasks_query:
            # Ensure equipment_ref is loaded (should be by joinedload)
            if not task.equipment_ref:
                logging.error(f"Task {task.id} is missing equipment reference. Skipping.")
                continue
            eq_key = (task.equipment_ref.code, task.equipment_ref.name, task.equipment_ref.type)
            # Calculate status and add attributes directly to the task object
            status, due_info, due_date_val, last_performed_info, next_due_info, estimated_days_info = \
                calculate_task_due_status(task, current_time_for_list)
            task.due_status = status # Store the full status string
            task.due_info = due_info
            task.due_date = due_date_val
            task.last_performed_info = last_performed_info
            task.next_due_info = next_due_info
            task.estimated_days_info = estimated_days_info
            tasks_by_equipment[eq_key].append(task)

        # --- Define Status Priority AND Header Label Mapping ---
        # Priority: Lower number = higher priority
        # Label: The text to display in the header badge
        status_config = {
            # Base Status Key: (Priority, Header Label)
            'Overdue':          (1, 'Overdue'),
            'Due Soon':         (2, 'Due Soon'),
            'Warning':          (3, 'Warning'),
            'Error':            (4, 'Error'), # Separate Error from Warning?
            'Never Performed':  (5, 'Never Done'),
            'OK':               (6, 'OK'),
            'Unknown':          (7, 'Unknown')
            # Add other base statuses returned by calculate_task_due_status if needed
        }
        default_priority = 99
        default_label = 'Unknown'
        ok_priority = status_config.get('OK', (default_priority, default_label))[0]
        ok_label = status_config.get('OK', (default_priority, default_label))[1]


        # --- Process tasks for sorting and header status ---
        processed_tasks_data = {}
        # Sort equipment keys alphabetically first
        sorted_equipment_keys = sorted(tasks_by_equipment.keys())

        for eq_key in sorted_equipment_keys:
            tasks_list_for_eq = tasks_by_equipment[eq_key]
            highest_priority_level_found = ok_priority # Start assuming OK priority

            # --- First pass: Find the highest priority level in the group ---
            for task in tasks_list_for_eq:
                # Ensure task has a status string before processing
                if not hasattr(task, 'due_status') or not isinstance(task.due_status, str):
                    logging.warning(f"Task {getattr(task, 'id', 'N/A')} missing valid due_status attribute. Assigning default priority.")
                    current_prio = default_priority
                else:
                    # --- CORRECTED: Extract base status reliably ---
                    # Handle variations like 'Overdue (First)', 'Due Soon (First)', 'Unknown (No Usage)' etc.
                    # We want the core status word recognized by status_config keys
                    current_status_str = task.due_status # e.g., "Due Soon (First)"
                    current_prio = default_priority # Default if no match found below
                    matched_base = None

                    # Check for specific keywords IN ORDER OF PRIORITY (most specific first)
                    # This ensures "Due Soon" is caught before just "Due" if that were a key
                    if 'Overdue' in current_status_str: matched_base = 'Overdue'
                    elif 'Due Soon' in current_status_str: matched_base = 'Due Soon'
                    elif 'Warning' in current_status_str: matched_base = 'Warning'
                    elif 'Error' in current_status_str: matched_base = 'Error'
                    elif 'Never Performed' in current_status_str: matched_base = 'Never Performed'
                    elif 'OK' in current_status_str: matched_base = 'OK' # Must be checked after more specific ones
                    elif 'Unknown' in current_status_str: matched_base = 'Unknown'
                    # Add elif for other specific statuses if needed

                    # Get priority from config if a base status was matched
                    if matched_base and matched_base in status_config:
                        current_prio = status_config[matched_base][0]
                    else:
                        # Log if status wasn't mapped - indicates potential need to update status_config
                         logging.warning(f"Status '{current_status_str}' for task {task.id} did not map to a known base status in status_config. Using default priority.")
                         # current_prio remains default_priority

                # Update the highest priority level found so far
                if current_prio < highest_priority_level_found:
                    highest_priority_level_found = current_prio
                    logging.debug(f"  Eq {eq_key}: New highest prio level found: {highest_priority_level_found} from task {task.id} (status: {task.due_status})")


            # --- Determine the final header label based on the highest priority level found ---
            final_header_label = default_label # Default
            # Find the label corresponding to the highest priority level achieved
            for base_status, (prio, label) in status_config.items():
                if prio == highest_priority_level_found:
                    final_header_label = label
                    break # Found the matching label
            logging.debug(f"Eq {eq_key}: Final highest prio level: {highest_priority_level_found}, Assigned Header Label: '{final_header_label}'")

            # --- Second pass (or combined): Sort the tasks list itself ---
            # Use a stable sort key function referencing the config
            def get_tasks_sort_key(task_item):
                sort_prio = default_priority # Default
                sort_matched_base = None
                if hasattr(task_item, 'due_status') and isinstance(task_item.due_status, str):
                    status_str = task_item.due_status
                    # Use same matching logic as priority finding for consistency
                    if 'Overdue' in status_str: sort_matched_base = 'Overdue'
                    elif 'Due Soon' in status_str: sort_matched_base = 'Due Soon'
                    elif 'Warning' in status_str: sort_matched_base = 'Warning'
                    elif 'Error' in status_str: sort_matched_base = 'Error'
                    elif 'Never Performed' in status_str: sort_matched_base = 'Never Performed'
                    elif 'OK' in status_str: sort_matched_base = 'OK'
                    elif 'Unknown' in status_str: sort_matched_base = 'Unknown'

                    if sort_matched_base and sort_matched_base in status_config:
                        sort_prio = status_config[sort_matched_base][0]

                # Add secondary sort criteria if needed (e.g., by due date/remaining value within the same status)
                # return (sort_prio, task_item.due_date or date.max) # Example secondary sort
                return sort_prio

            try:
                 tasks_list_for_eq.sort(key=get_tasks_sort_key)
            except Exception as eq_sort_exc:
                logging.error(f"Error sorting tasks for equipment {eq_key}: {eq_sort_exc}", exc_info=True)
                # Use unsorted list on error

            # Store the sorted list and the final header status label
            processed_tasks_data[eq_key] = {
                'tasks': tasks_list_for_eq,
                'header_status': final_header_label # Use the label derived from the highest priority
            }

        # Get list of equipment types for filter dropdown
        equipment_types = db.session.query(Equipment.type).distinct().order_by(Equipment.type).all()
        equipment_types = [t[0] for t in equipment_types]
        
        # Get list of equipment statuses for filter dropdown (adding status filter)
        equipment_statuses = ['Operational'] + [status for status in EQUIPMENT_STATUSES if status != 'Operational']

        logging.debug("--- Rendering pm_legal_tasks.html ---")
        return render_template(
            'pm_legal_tasks.html',
            tasks_data=processed_tasks_data, # Pass the new structure
            equipment_types=equipment_types,
            equipment_statuses=equipment_statuses,  # Add statuses for dropdown
            type_filter=type_filter,
            status_filter=status_filter,  # Pass the status filter to template
            title='Legal Compliance Tasks'
        )
    except Exception as e:
        logging.error(f"--- Error loading legal_tasks_list page: {e} ---", exc_info=True)
        flash(f"Error loading legal compliance tasks: {e}", "danger")
        # Pass empty data structure to avoid template errors
        return render_template('pm_legal_tasks.html',
                               tasks_data={},
                               equipment_types=[],
                               equipment_statuses=EQUIPMENT_STATUSES,  # Add statuses for dropdown
                               type_filter=request.args.get('type'),
                               status_filter=request.args.get('status'),  # Pass the status filter
                               title='Legal Compliance Tasks',
                               error=True)

# ==============================================================================
# === Add/Edit Legal Compliance Task ===
# ==============================================================================
@bp.route('/legal_task/add', methods=['GET', 'POST'])
def add_legal_task():
    """Displays form to add a new legal compliance task (GET) or processes addition (POST)."""
    if request.method == 'POST':
        try:
            equipment_id = request.form.get('equipment_id', type=int)
            description = request.form.get('description')
            interval_type = request.form.get('interval_type')
            interval_value_str = request.form.get('interval_value')
            oem_required = 'oem_required' in request.form
            kit_required = 'kit_required' in request.form

            # Validation
            errors = []
            if not equipment_id: errors.append("Equipment is required.")
            if not description: errors.append("Description is required.")
            if not interval_type: errors.append("Interval Type is required.")
            if not interval_value_str: errors.append("Interval Value is required.")

            interval_value = 0
            if interval_value_str:
                try:
                    interval_value = int(interval_value_str)
                    if interval_value <= 0:
                        errors.append("Interval Value must be positive.")
                except ValueError:
                    errors.append("Interval Value must be a whole number.")

            if errors:
                for error in errors: flash(error, 'warning')
                # Re-render form with errors (need equipment list again)
                equipment_list = Equipment.query.order_by(Equipment.name).all()
                return render_template('pm_legal_task_form.html', equipment=equipment_list, title="Add New Legal Compliance Task")

            # Create new task with is_legal_compliance=True
            new_task = MaintenanceTask(
                equipment_id=equipment_id,
                description=description,
                interval_type=interval_type,
                interval_value=interval_value,
                oem_required=oem_required,
                kit_required=kit_required,
                is_legal_compliance=True  # Set legal compliance flag to True
            )
            db.session.add(new_task)
            db.session.commit()
            flash(f'Legal compliance task "{description}" added successfully!', 'success')
            return redirect(url_for('planned_maintenance.legal_tasks_list'))

        except Exception as e:
            db.session.rollback()
            flash(f"Error adding legal compliance task: {e}", "danger")
            # Re-render form on error (need equipment list again)
            equipment_list = Equipment.query.order_by(Equipment.name).all()
            return render_template('pm_legal_task_form.html', equipment=equipment_list, title="Add New Legal Compliance Task")

    # --- Handle GET Request ---
    try:
        equipment_list = Equipment.query.order_by(Equipment.name).all()
    except Exception as e:
        flash(f"Error loading equipment list for form: {e}", "danger")
        equipment_list = []

    return render_template('pm_legal_task_form.html', equipment=equipment_list, title="Add New Legal Compliance Task")

# === AJAX Endpoint to get logs for modal ===
@bp.route('/logs/get_for_cell', methods=['GET'])
def get_logs_for_cell():
    log_type = request.args.get('log_type')
    equipment_id = request.args.get('equipment_id', type=int)
    log_date_str = request.args.get('log_date') # Expecting YYYY-MM-DD

    if not all([log_type, equipment_id, log_date_str]):
        return jsonify({'error': 'Missing parameters'}), 400

    try:
        target_date = date.fromisoformat(log_date_str)
        # Define the date range for the specific day (UTC)
        start_dt = datetime.combine(target_date, time.min).replace(tzinfo=timezone.utc)
        end_dt = datetime.combine(target_date, time.max).replace(tzinfo=timezone.utc)

        logs_data = []
        if log_type == 'checklist':
            logs = Checklist.query.filter(
                Checklist.equipment_id == equipment_id,
                Checklist.check_date >= start_dt,
                Checklist.check_date <= end_dt
            ).order_by(Checklist.check_date.desc()).all() # Latest first in modal
            for log in logs:
                log_date_utc = log.check_date.astimezone(timezone.utc) if log.check_date.tzinfo else log.check_date.replace(tzinfo=timezone.utc)
                logs_data.append({
                    'id': log.id,
                    'timestamp': log_date_utc.strftime('%H:%M:%S UTC'), # Only time part needed?
                    'status': log.status,
                    'issues': log.issues or '',
                    'operator': log.operator or 'N/A',
                    'edit_url': url_for('planned_maintenance.edit_checklist_log_form', log_id=log.id) # URL for fetching form
                })
        elif log_type == 'usage':
            logs = UsageLog.query.filter(
                UsageLog.equipment_id == equipment_id,
                UsageLog.log_date >= start_dt,
                UsageLog.log_date <= end_dt
            ).order_by(UsageLog.log_date.desc()).all() # Latest first in modal
            for log in logs:
                 log_date_utc = log.log_date.astimezone(timezone.utc) if log.log_date.tzinfo else log.log_date.replace(tzinfo=timezone.utc)
                 logs_data.append({
                    'id': log.id,
                    'timestamp': log_date_utc.strftime('%H:%M:%S UTC'),
                    'value': log.usage_value,
                    'edit_url': url_for('planned_maintenance.edit_usage_log_form', log_id=log.id) # URL for fetching form
                })
        else:
            return jsonify({'error': 'Invalid log type'}), 400

        return jsonify({'logs': logs_data})

    except ValueError:
        return jsonify({'error': 'Invalid date format'}), 400
    except Exception as e:
        logging.error(f"Error fetching logs for cell: {e}", exc_info=True)
        return jsonify({'error': 'Server error fetching log details'}), 500


# === Routes to GET Edit Form Content (for Modal) ===

@bp.route('/checklist/edit_form/<int:log_id>', methods=['GET'])
def edit_checklist_log_form(log_id):
    log = Checklist.query.get_or_404(log_id)
    form = ChecklistEditForm(obj=log) # Populate form from log object
    # Format date for datetime-local input (needs YYYY-MM-DDTHH:MM)
    # Ensure datetime is naive for the input field, representing local time if applicable
    # For simplicity, let's use the stored UTC time directly in the input for now.
    # User might need to be aware it's UTC. Convert to local if needed.
    if log.check_date:
         if log.check_date.tzinfo:
              check_date_naive = log.check_date.astimezone(timezone.utc).replace(tzinfo=None)
         else:
             check_date_naive = log.check_date # Assume stored naive is UTC intended
         form.check_date.data = check_date_naive


    # Render *only* the form part using a partial template
    return render_template('_checklist_edit_form_partial.html', form=form, log_id=log_id)

@bp.route('/usage/edit_form/<int:log_id>', methods=['GET'])
def edit_usage_log_form(log_id):
    log = UsageLog.query.get_or_404(log_id)
    form = UsageLogEditForm(obj=log)
    # Format date for datetime-local input
    if log.log_date:
        if log.log_date.tzinfo:
            log_date_naive = log.log_date.astimezone(timezone.utc).replace(tzinfo=None)
        else:
            log_date_naive = log.log_date
        form.log_date.data = log_date_naive
    form.usage_value.data = log.usage_value # Ensure value is set

    return render_template('_usage_log_edit_form_partial.html', form=form, log_id=log_id)


# === Routes to PROCESS Edit Form Submission ===
@bp.route('/checklist/edit/<int:log_id>', methods=['POST'])
def edit_checklist_log(log_id):
    log = Checklist.query.get_or_404(log_id)
    form = ChecklistEditForm(request.form)
    if form.validate_on_submit():
        try:
            log.status = form.status.data
            log.issues = form.issues.data
            log.operator = form.operator.data
            naive_dt = form.check_date.data
            if naive_dt:
                log.check_date = naive_dt.replace(tzinfo=timezone.utc)
            db.session.commit()
            return jsonify({'success': True, 'message': f'Checklist log ID {log.id} updated successfully.'})
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
    else:
        errors = {field: errors for field, errors in form.errors.items()}
        return jsonify({'success': False, 'error': 'Validation failed', 'errors': errors}), 400

@bp.route('/usage/edit/<int:log_id>', methods=['POST'])
def edit_usage_log(log_id):
    log = UsageLog.query.get_or_404(log_id)
    # Use request.form directly with the form for AJAX posts
    form = UsageLogEditForm(request.form)

    # Use WTForms validation
    if form.validate(): # Use validate() instead of validate_on_submit for direct form data
        try:
            log.usage_value = form.usage_value.data

            naive_dt = form.log_date.data
            if naive_dt:
                 # Assume naive datetime from form represents UTC
                 log.log_date = naive_dt.replace(tzinfo=timezone.utc)
            else:
                 # Handle case where date might be cleared
                 log.log_date = None # Or set to a default if required

            db.session.commit()
            logging.info(f"Usage log ID {log.id} updated successfully via AJAX.")
            # Return JSON success response
            return jsonify({'success': True, 'message': f'Usage log ID {log.id} updated successfully.'})

        except Exception as e:
            db.session.rollback()
            logging.error(f"Error updating usage log {log_id} via AJAX: {e}", exc_info=True)
            # Return JSON error response
            return jsonify({'success': False, 'error': f'Database error: {str(e)}'}), 500
    else:
        # Validation failed, return JSON error response with validation errors
        errors = {field: errors for field, errors in form.errors.items()}
        logging.warning(f"Validation failed for usage log edit {log_id}: {errors}")
        return jsonify({'success': False, 'error': 'Validation failed', 'errors': errors}), 400

    if form.validate_on_submit():
        try:
            log.usage_value = form.usage_value.data

            naive_dt = form.log_date.data
            if naive_dt:
                 log.log_date = naive_dt.replace(tzinfo=timezone.utc) # Assume UTC
            else:
                 log.log_date = None

            db.session.commit()
            flash(f'Usage log ID {log.id} updated successfully.', 'success')
            return redirect(url_for('planned_maintenance.usage_logs'))
        except Exception as e:
            db.session.rollback()
            logging.error(f"Error updating usage log {log_id}: {e}", exc_info=True)
            flash(f'Error updating usage log: {e}', 'danger')
            return redirect(url_for('planned_maintenance.usage_logs'))
    else:
        for field, errors in form.errors.items():
            for error in errors:
                flash(f"Error in {getattr(form, field).label.text}: {error}", 'warning')
        return redirect(url_for('planned_maintenance.usage_logs'))