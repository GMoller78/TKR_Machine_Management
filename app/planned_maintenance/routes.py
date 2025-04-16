
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
# Corrected model imports (added MaintenancePlanEntry)
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
@bp.route('/maintenance_plan') # Changed root path to list
def maintenance_plan_list_view():
    logging.debug("--- Request for Maintenance Plan List View ---")
    try:
        # Query distinct year/month pairs for which plan entries exist
        existing_plans_query = db.session.query(
            MaintenancePlanEntry.plan_year,
            MaintenancePlanEntry.plan_month,
            func.max(MaintenancePlanEntry.generated_at).label('last_generated') # Get latest generation time for the period
        ).group_by(
            MaintenancePlanEntry.plan_year,
            MaintenancePlanEntry.plan_month
        ).order_by(
            desc(MaintenancePlanEntry.plan_year),
            desc(MaintenancePlanEntry.plan_month)
        ).all()

        existing_plans = []
        for year, month, generated_at in existing_plans_query:
            # Convert naive UTC stored time to display format
            generated_str = "Unknown"
            if generated_at:
                 if generated_at.tzinfo is not None:
                     generated_at_naive = generated_at.astimezone(timezone.utc).replace(tzinfo=None)
                 else:
                     generated_at_naive = generated_at # Assume already naive UTC
                 generated_str = generated_at_naive.strftime('%Y-%m-%d %H:%M UTC')

            existing_plans.append({
                'year': year,
                'month': month,
                'month_name': date(year, month, 1).strftime('%B'), # Get month name
                'generated_at': generated_str
            })

        today_val = date.today() # Renamed variable
        current_year=today_val.year
        current_month=today_val.month

        logging.debug(f"Found {len(existing_plans)} existing plan periods.")

        return render_template(
            'maintenance_plan_list.html', # NEW Template name
            title="Maintenance Plans",
            existing_plans=existing_plans,
            current_year=current_year,
            current_month=current_month,
            date_today=today_val, # Pass today's date for form defaults
            WEASYPRINT_AVAILABLE = WEASYPRINT_AVAILABLE # Pass PDF availability flag
        )

    except Exception as e:
        logging.error(f"--- Error loading maintenance plan list view: {e} ---", exc_info=True)
        flash(f"An error occurred while loading the plan list: {e}", "danger")
        return render_template('maintenance_plan_list.html', # Still render list template on error
                               title="Maintenance Plans - Error",
                               error=f"An unexpected error occurred: {e}",
                               existing_plans=[],
                               current_year=date.today().year,
                               current_month=date.today().month,
                               date_today=date.today(),
                               WEASYPRINT_AVAILABLE = WEASYPRINT_AVAILABLE
                              )


# ==============================================================================
# === MODIFIED Route: View Plan Details (Planned vs Actual) ===
# ==============================================================================
@bp.route('/maintenance_plan/detail', methods=['GET']) # Changed path
def maintenance_plan_detail_view():
    logging.debug("--- Request for Maintenance Plan Detail View ---")
    try:
        # 1. Get Target Month/Year from Query Parameters
        try:
            year = request.args.get('year', type=int)
            month = request.args.get('month', type=int)

            if not year or not month:
                flash("Year and Month are required to view plan details.", "warning")
                return redirect(url_for('planned_maintenance.maintenance_plan_list_view'))

            if not (1 <= month <= 12): raise ValueError("Month out of range")
            current_year_check = date.today().year
            if not (current_year_check - 10 <= year <= current_year_check + 10): raise ValueError("Year out of reasonable range")

        except (ValueError, TypeError):
            flash("Invalid year or month specified.", "warning")
            return redirect(url_for('planned_maintenance.maintenance_plan_list_view'))

        # Calculate Date Range and Month Name
        try:
            plan_start_date = date(year, month, 1)
            days_in_month = monthrange(year, month)[1]
            plan_end_date = date(year, month, days_in_month)
            month_name_str = plan_start_date.strftime("%B %Y")
            # Define start and end datetime for job card filtering (naive)
            month_start_dt = datetime.combine(plan_start_date, time.min)
            month_end_dt = datetime.combine(plan_end_date, time.max)
        except ValueError:
             flash(f"Cannot display plan details for invalid date {month}/{year}.", "danger")
             return redirect(url_for('planned_maintenance.maintenance_plan_list_view'))

        logging.info(f"Displaying plan detail view for: {month_name_str}")

        # 2. Fetch Planned Entries for the period
        logging.debug(f"Querying stored plan entries for {year}-{month}...")
        plan_entries = MaintenancePlanEntry.query.filter(
            MaintenancePlanEntry.plan_year == year,
            MaintenancePlanEntry.plan_month == month
        ).options(
             db.joinedload(MaintenancePlanEntry.equipment) # Eager load equipment
        ).order_by(
            MaintenancePlanEntry.equipment_id,
            MaintenancePlanEntry.planned_date
        ).all()
        logging.debug(f"Found {len(plan_entries)} planned entries.")

        # 3. Fetch Completed Job Cards for the period
        logging.debug(f"Querying completed job cards between {month_start_dt} and {month_end_dt}...")
        completed_job_cards = JobCard.query.filter(
            JobCard.status == 'Done',
            JobCard.end_datetime.isnot(None), # Ensure completion time exists
            JobCard.end_datetime >= month_start_dt, # Completion time within the month
            JobCard.end_datetime <= month_end_dt
        ).options(
             db.joinedload(JobCard.equipment_ref) # Eager load equipment
        ).order_by(
             JobCard.equipment_id,
             JobCard.end_datetime # Order by completion time
        ).all()
        logging.debug(f"Found {len(completed_job_cards)} completed job cards.")

        # 4. Structure Data for Comparison View
        # Group by equipment involved in either planned or completed tasks this month
        comparison_data = defaultdict(lambda: {'equipment': None, 'planned': [], 'completed': []})
        all_equipment_ids = set()

        # Process planned entries
        generation_info = "Plan not generated or no tasks planned for this period."
        if plan_entries:
            latest_generation_time = max((entry.generated_at for entry in plan_entries if entry.generated_at), default=None)
            if latest_generation_time:
                if latest_generation_time.tzinfo is not None:
                    latest_generation_time = latest_generation_time.astimezone(timezone.utc).replace(tzinfo=None)
                generation_info = f"Plan generated on: {latest_generation_time.strftime('%Y-%m-%d %H:%M UTC')}"
            else:
                 generation_info = "Plan generated (time unknown)"

            for entry in plan_entries:
                eq_id = entry.equipment_id
                all_equipment_ids.add(eq_id)
                if not comparison_data[eq_id]['equipment'] and entry.equipment:
                    comparison_data[eq_id]['equipment'] = entry.equipment # Store equipment object
                comparison_data[eq_id]['planned'].append(entry)

        # Process completed job cards
        for jc in completed_job_cards:
            eq_id = jc.equipment_id
            all_equipment_ids.add(eq_id)
            if not comparison_data[eq_id]['equipment'] and jc.equipment_ref:
                 comparison_data[eq_id]['equipment'] = jc.equipment_ref # Store equipment object
            comparison_data[eq_id]['completed'].append(jc)

        # Fetch equipment details if not already loaded (e.g., if only completed JCs existed for an equipment)
        missing_eq_ids = all_equipment_ids - set(k for k, v in comparison_data.items() if v['equipment'])
        if missing_eq_ids:
            missing_equipment = Equipment.query.filter(Equipment.id.in_(missing_eq_ids)).all()
            for eq in missing_equipment:
                 if not comparison_data[eq.id]['equipment']:
                     comparison_data[eq.id]['equipment'] = eq

        # Sort the final data by equipment code/name for display
        # Convert defaultdict to dict and sort
        sorted_comparison_data = dict(sorted(comparison_data.items(), key=lambda item: getattr(item[1]['equipment'], 'code', 'ZZZ') or 'ZZZ')) # Sort by code, handle None


        # 5. Render Detail Template
        logging.debug("Rendering maintenance_plan_detail.html for web view")
        return render_template(
            'maintenance_plan_detail.html', # NEW Template Name
            title=f"Plan Details - {month_name_str}",
            month_name=month_name_str,
            current_year=year,
            current_month=month,
            comparison_data=sorted_comparison_data, # Pass the structured data
            generation_info=generation_info,
            WEASYPRINT_AVAILABLE = WEASYPRINT_AVAILABLE # Pass PDF availability flag
        )

    except Exception as e:
        logging.error(f"--- Error loading maintenance plan detail view: {e} ---", exc_info=True)
        flash(f"An error occurred while loading the plan details: {e}", "danger")
        # Redirect back to the list view on error
        return redirect(url_for('planned_maintenance.maintenance_plan_list_view'))


# ==============================================================================
# === Dashboard Route ===
# ==============================================================================
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
            try:
                # Format the message details (handle None values)
                if jc.equipment_ref:
                    equipment_name = f"{jc.equipment_ref.code} - {jc.equipment_ref.name}"
                else:
                     equipment_name = "Unknown Equipment" # Fallback if relation fails
                due_date_str = jc.due_date.strftime('%Y-%m-%d') if jc.due_date else 'Not Set'
                technician_str = jc.technician or 'Unassigned'

                # Construct the message string (using markdown for WhatsApp)
                whatsapp_msg = (
                    f"Job Card Details:\n"
                    f"*Number:* {jc.job_number}\n"
                    f"*Equipment:* {equipment_name}\n"
                    f"*Task:* {jc.description}\n"
                    f"*Status:* {jc.status}\n"
                    f"*Assigned:* {technician_str}\n"
                    f"*Due:* {due_date_str}"
                    # Add other details if desired
                )
                encoded_msg = urlencode({'text': whatsapp_msg})
                jc.whatsapp_share_url = f"https://wa.me/?{encoded_msg}" # Assign URL to temporary attribute
                logging.debug(f"  Generated URL for JC {jc.job_number}")
            except Exception as url_gen_err:
                logging.error(f"Error generating WhatsApp URL for JC {jc.id}: {url_gen_err}", exc_info=True)
                jc.whatsapp_share_url = None # Set to None on error


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
        all_legal_tasks = MaintenanceTask.query.filter(
            MaintenanceTask.is_legal_compliance.is_(True)
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

        # Sort the filtered tasks based on severity (Overdue > Due Soon > Warning)
        sort_order_map = {
            'Overdue': 1, 'Due Soon': 2, 'Warning': 3,
            # Add others if they can appear in filtered list and need specific order
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
                    break # Use first match (assuming Overdue/Due Soon/Warning are distinct enough)
            # Optional secondary sort key (e.g., by due date if available)
            secondary_key = task_item.due_date if task_item.due_date else datetime.max.replace(tzinfo=None) # Ensure comparable type
            return (prio, secondary_key)

        try:
            tasks_with_status_filtered.sort(key=get_sort_key)
            logging.debug("Sorted filtered tasks for dashboard successfully.")
        except Exception as task_sort_exc:
            logging.error(f"Error sorting filtered tasks: {task_sort_exc}", exc_info=True)
            # Proceed with unsorted list in case of error

        # 4. Prepare Recent Activities
        logging.debug("Aggregating recent activities...")
        recent_activities = []
        limit_count = 15 # How many recent items to fetch/show

        # Fetch recent items (adjust models and fields as needed)
        try: # Wrap aggregation in try/except
            latest_checklists = Checklist.query.options(db.joinedload(Checklist.equipment_ref)).order_by(Checklist.check_date.desc()).limit(limit_count).all()
            recent_usage_logs = UsageLog.query.options(db.joinedload(UsageLog.equipment_ref)).order_by(UsageLog.log_date.desc()).limit(limit_count).all()
            # Fetch recent job cards - use creation time (start_datetime or ID) and completion time (end_datetime)
            recent_job_cards = JobCard.query.options(db.joinedload(JobCard.equipment_ref)).order_by(JobCard.id.desc()).limit(limit_count * 2).all() # Fetch more initially

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
                # Creation Activity (Use start_datetime if available, else maybe creation from ID?)
                create_ts = jc.start_datetime # This might be set later, consider ID or another field?
                if create_ts: # Or always add based on ID?
                     if create_ts.tzinfo is not None: create_ts = create_ts.astimezone(timezone.utc).replace(tzinfo=None)
                     activity_list.append({
                         'type': 'job_card_created', 'timestamp': create_ts,
                         'description': f"Job Card {jc.job_number} created for {jc.equipment_ref.code}",
                         'details': jc
                     })

                # Completion Activity
                if jc.status == 'Done' and jc.end_datetime:
                    complete_ts = jc.end_datetime
                    if complete_ts.tzinfo is not None: complete_ts = complete_ts.astimezone(timezone.utc).replace(tzinfo=None)
                    activity_list.append({
                         'type': 'job_card_completed', 'timestamp': complete_ts,
                         'description': f"Job Card {jc.job_number} completed for {jc.equipment_ref.code}",
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
        logging.debug("--- Rendering pm_dashboard.html ---")
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
                               recent_activities=[],
                               today=date.today()) # Provide default date object on error

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
            JobCard.status != 'Done'
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
        # 1. Get Filter Parameters from Request Args
        page = request.args.get('page', 1, type=int)
        per_page = 25 # Or get from request.args or config

        # Dates - filter by 'due_date' for relevance? Or 'created_at'? Let's use due_date
        # Default to no date filter unless specified
        start_date_str = request.args.get('start_date', '')
        end_date_str = request.args.get('end_date', '')

        # Equipment search - Text-based search
        equipment_search_term = request.args.get('equipment_search', '').strip() # Get text and strip whitespace

        # Status - Allow 'All' or specific status
        status_filter = request.args.get('status', 'All') # Default to 'All'
        
        # New: Job Type filter - 'All', 'Maintenance', or 'Legal'
        job_type_filter = request.args.get('job_type', 'All') # Default to 'All'

        # Convert filter values
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

        logging.debug(f"Filters - Start: {start_date_val}, End: {end_date_val}, Equip Search: '{equipment_search_term}', Status: {status_filter}, Job Type: {job_type_filter}, Page: {page}")

        # 2. Build Base Query
        query = JobCard.query.options(
            db.joinedload(JobCard.equipment_ref) # Eager load equipment
        )

        # 3. Apply Filters
        if start_date_val:
            query = query.filter(JobCard.due_date >= start_date_val)
        if end_date_val:
            query = query.filter(JobCard.due_date <= end_date_val)

        # Apply equipment search filter *after* the join is guaranteed (by options/join)
        if equipment_search_term:
            search_pattern = f"%{equipment_search_term}%"
            query = query.join(JobCard.equipment_ref).filter(
                or_(
                    Equipment.code.ilike(search_pattern),
                    Equipment.name.ilike(search_pattern)
                )
            )

        if status_filter and status_filter != 'All' and status_filter in JOB_CARD_STATUSES:
            query = query.filter(JobCard.status == status_filter)
            
        # Apply job type filter
        if job_type_filter == 'Maintenance':
            query = query.filter(JobCard.job_number.like('JC-%'))
        elif job_type_filter == 'Legal':
            query = query.filter(JobCard.job_number.like('LC-%'))
        # 'All' doesn't need a filter

        # 4. Ordering (e.g., newest first or by due date)
        query = query.order_by(JobCard.id.desc()) # Or JobCard.due_date.desc().nullslast()

        # 5. Execute Query with Pagination
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)
        job_cards_page = pagination.items

        # 6. Generate WhatsApp URLs for the current page items
        for jc in job_cards_page:
            jc.whatsapp_share_url = generate_whatsapp_share_url(jc)

        # 7. Fetch Data for Filter Dropdowns
        all_equipment = Equipment.query.order_by(Equipment.code).all()
        
        # 8. Define job type options for filter dropdown
        job_type_options = ['All', 'Maintenance', 'Legal']

        # 9. Store filter values for repopulating the form
        current_filters = {
            'start_date': start_date_str,
            'end_date': end_date_str,
            'equipment_search': equipment_search_term,
            'status': status_filter,
            'job_type': job_type_filter
        }

        logging.debug(f"Found {pagination.total} job cards matching filters. Displaying page {page} ({len(job_cards_page)} items).")

        return render_template(
            'pm_job_card_list.html',
            title="Job Card List",
            pagination=pagination, # Pass pagination object
            job_cards=job_cards_page, # Pass items for the current page
            all_equipment=all_equipment,
            job_card_statuses=['All'] + JOB_CARD_STATUSES, # Add 'All' option
            job_type_options=job_type_options, # Pass job type options for filter
            current_filters=current_filters, # Pass current filters back
            today=date.today() # Pass today's date for potential defaults
        )

    except Exception as e:
        logging.error(f"--- Error loading job card list view: {e} ---", exc_info=True)
        flash(f"An error occurred while loading the job card list: {e}", "danger")
        # Render the template safely on error
        return render_template(
            'pm_job_card_list.html',
            title="Job Card List - Error",
            error=f"Could not load job cards: {e}",
            pagination=None, job_cards=[], all_equipment=[],
            job_card_statuses=['All'] + JOB_CARD_STATUSES,
            job_type_options=['All', 'Maintenance', 'Legal'],
            current_filters={}, today=date.today()
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
@bp.route('/checklist/new', methods=['POST'])
def new_checklist():
    """Logs a new equipment checklist."""
    # This route expects the form to be included elsewhere (e.g., dashboard modal)
    if request.method == 'POST':
        try:
            equipment_id = request.form.get('equipment_id', type=int)
            status = request.form.get('status')
            issues = request.form.get('issues') # Optional
            # *** Get the new date/time string ***
            check_date_str = request.form.get('check_date')

            if not equipment_id or not status or not check_date_str:
                flash('Equipment, Status, and Check Date/Time are required for checklist.', 'warning')
                return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

            # Validate status value
            valid_statuses = ["Go", "Go But", "No Go"]
            if status not in valid_statuses:
                flash(f'Invalid status "{status}" selected.', 'warning')
                return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

            # *** Parse and Validate Date/Time ***
            try:
                # Parse the string from datetime-local input (usually ISO format without Z)
                parsed_dt = parse_datetime(check_date_str)
                # Assume the naive datetime represents UTC time, make it timezone-aware
                check_date_utc = parsed_dt.replace(tzinfo=timezone.utc)
                logging.debug(f"Parsed check date string '{check_date_str}' to naive {parsed_dt}, assumed UTC: {check_date_utc} ({check_date_utc.tzinfo})")
            except ValueError:
                flash("Invalid Check Date/Time format submitted.", "warning")
                return redirect(request.referrer or url_for('planned_maintenance.dashboard'))
            except Exception as parse_err: # Catch other potential parsing errors
                logging.error(f"Error parsing check_date '{check_date_str}': {parse_err}", exc_info=True)
                flash(f"Error parsing check date: {parse_err}", "danger")
                return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

            equipment = Equipment.query.get(equipment_id)
            if not equipment:
                 flash('Selected equipment not found.', 'danger')
                 return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

            # Create checklist entry using the parsed date
            new_log = Checklist(
                equipment_id=equipment_id,
                status=status,
                issues=issues,
                check_date=check_date_utc # Use the parsed and UTC-aware datetime
            )
            db.session.add(new_log)
            db.session.commit()
            flash(f'Checklist logged successfully for {equipment.name} with status "{status}" at {check_date_utc.strftime("%Y-%m-%d %H:%M UTC")}.', 'success')

        except Exception as e:
            db.session.rollback()
            logging.error(f"Error logging checklist: {e}", exc_info=True) # Log the full error
            flash(f"Error logging checklist: {e}", "danger")

        return redirect(request.referrer or url_for('planned_maintenance.dashboard')) # Redirect back to where the form was

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
# === Add/Edit Task ===
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

# ==============================================================================
# === Usage Log ===
# ==============================================================================
@bp.route('/usage/add', methods=['POST'])
def add_usage():
    """Adds a usage log entry."""
    if request.method == 'POST':
        try:
            equipment_id = request.form.get('equipment_id', type=int)
            usage_value_str = request.form.get('usage_value')
            log_date_str = request.form.get('log_date') # Optional, defaults to now

            if not equipment_id or not usage_value_str:
                flash("Equipment and Usage Value are required.", "warning")
                return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

            try:
                usage_value = float(usage_value_str)
            except ValueError:
                 flash("Invalid Usage Value.", "warning")
                 return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

            log_date_dt = datetime.now(timezone.utc) # Default to now (UTC)
            if log_date_str:
                try:
                    # Parse naive datetime from input
                    parsed_naive = parse_datetime(log_date_str)
                    # Assume it represents UTC and make it aware
                    log_date_dt = parsed_naive.replace(tzinfo=timezone.utc)
                    logging.debug(f"Parsed usage log_date string '{log_date_str}' to naive {parsed_naive}, assumed UTC: {log_date_dt} ({log_date_dt.tzinfo})")
                except ValueError:
                    flash("Invalid Log Date format, using current UTC time.", "info")
                except Exception as parse_err:
                    logging.error(f"Error parsing usage log_date '{log_date_str}': {parse_err}", exc_info=True)
                    flash(f"Error parsing log date: {parse_err}. Using current UTC time.", "warning")

            usage = UsageLog(equipment_id=equipment_id, usage_value=usage_value, log_date=log_date_dt)
            db.session.add(usage)
            db.session.commit()
            flash("Usage log added successfully.", "success")

        except Exception as e:
            db.session.rollback()
            logging.error(f"Error adding usage log: {e}", exc_info=True) # Log error details
            flash(f"Error adding usage log: {e}", "danger")

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
    """Displays checklist logs, optionally filtered by equipment."""
    equipment_id = request.args.get('equipment_id', type=int)
    query = Checklist.query.options(db.joinedload(Checklist.equipment_ref)) # Eager load
    if equipment_id:
        query = query.filter(Checklist.equipment_id == equipment_id)
    # Order by check date descending
    logs = query.order_by(desc(Checklist.check_date)).all()
    equipment_list = Equipment.query.order_by(Equipment.name).all()
    # Get selected equipment name if filtered
    selected_equipment = Equipment.query.get(equipment_id) if equipment_id else None
    return render_template('pm_checklist_logs.html',
                           logs=logs,
                           equipment_list=equipment_list, # Renamed for clarity
                           selected_equipment_id=equipment_id, # Renamed for clarity
                           selected_equipment=selected_equipment, # Pass the object too
                           title='Checklist Logs')

@bp.route('/usage_logs', methods=['GET'])
def usage_logs():
    """Displays usage logs with filters."""
    equipment_id_filter = request.args.get('equipment_id', type=int) # Filter by specific equipment ID
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')

    query = UsageLog.query.join(Equipment).options(db.joinedload(UsageLog.equipment_ref)) # Eager load

    # Apply equipment filter
    if equipment_id_filter:
        query = query.filter(UsageLog.equipment_id == equipment_id_filter)

    # Apply date filters
    start_date_dt = None
    if start_date_str:
        try:
            start_date_dt = datetime.strptime(start_date_str, '%Y-%m-%d')
            # Make timezone aware (assuming UTC start of day) if log_date is aware
            start_date_dt = start_date_dt.replace(tzinfo=timezone.utc)
            query = query.filter(UsageLog.log_date >= start_date_dt)
        except ValueError:
            flash("Invalid start date format. Use YYYY-MM-DD.", "warning")

    end_date_dt = None
    if end_date_str:
        try:
            end_date_dt = datetime.strptime(end_date_str, '%Y-%m-%d')
            # Make inclusive by going to end of day and making aware (assuming UTC)
            end_date_dt = datetime.combine(end_date_dt.date(), time.max).replace(tzinfo=timezone.utc)
            query = query.filter(UsageLog.log_date <= end_date_dt)
        except ValueError:
            flash("Invalid end date format. Use YYYY-MM-DD.", "warning")


    # Fetch all equipment for the dropdown filter
    all_equipment_list = Equipment.query.order_by(Equipment.code).all()

    # Fetch the filtered logs
    logs = query.order_by(desc(UsageLog.log_date)).all()

    # Get selected equipment name if filtered
    selected_equipment = Equipment.query.get(equipment_id_filter) if equipment_id_filter else None

    return render_template('pm_usage_logs.html',
                           logs=logs,
                           all_equipment=all_equipment_list, # Pass full list for dropdown
                           selected_equipment_id=equipment_id_filter,
                           selected_equipment=selected_equipment,
                           start_date=start_date_str, # Pass back filter values
                           end_date=end_date_str,
                           title='Usage Logs')

# Add these routes to routes.py

# ==============================================================================
# === Legal Compliance Tasks List ===
# ==============================================================================
@bp.route('/legal_tasks', methods=['GET'])
def legal_tasks_list():
    logging.debug("--- Entering legal_tasks_list route ---")
    try:
        type_filter = request.args.get('type')
        query = MaintenanceTask.query.join(Equipment).filter(MaintenanceTask.is_legal_compliance == True)
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

        equipment_types = db.session.query(Equipment.type).distinct().order_by(Equipment.type).all()
        equipment_types = [t[0] for t in equipment_types]

        logging.debug("--- Rendering pm_legal_tasks.html ---")
        return render_template(
            'pm_legal_tasks.html',
            tasks_data=processed_tasks_data, # Pass the new structure
            equipment_types=equipment_types,
            type_filter=type_filter,
            title='Legal Compliance Tasks'
        )
    except Exception as e:
        logging.error(f"--- Error loading legal_tasks_list page: {e} ---", exc_info=True)
        flash(f"Error loading legal compliance tasks: {e}", "danger")
        # Pass empty data structure to avoid template errors
        return render_template('pm_legal_tasks.html',
                               tasks_data={},
                               equipment_types=[],
                               type_filter=request.args.get('type'),
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