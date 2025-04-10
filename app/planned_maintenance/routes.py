# tkr_system/app/planned_maintenance/routes.py
import logging # Import logging module
from flask import render_template, request, redirect, url_for, flash
from urllib.parse import urlencode # For encoding WhatsApp messages
from datetime import datetime, timedelta, timezone, UTC
from dateutil.parser import parse as parse_datetime  # Added import for datetime parsing
from app.planned_maintenance import bp  # Import the blueprint instance
from app import db                      # Import the database instance
from app.models import Equipment, JobCard, Checklist, StockTransaction, JobCardPart, Part, MaintenanceTask, UsageLog  # Added Part to imports
from itertools import zip_longest
from sqlalchemy import desc, func
import traceback # Optional: for better error logging

# --- Configure Logging (add this near the top) ---
# Basic configuration - logs to console. Adjust level and format as needed.
# Use DEBUG level to see all our messages.
logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s %(levelname)-8s %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')

# --- Planned Maintenance Routes ---
DUE_SOON_ESTIMATED_DAYS_THRESHOLD = 7
# ==============================================================================
# === Dashboard Route ===
# ==============================================================================
@bp.route('/')
def dashboard():
    logging.debug("--- Entering dashboard route ---")
    try:
        # 1. Fetch Equipment Data (for status and modals)
        equipment_list = Equipment.query.order_by(Equipment.type, Equipment.name).all()
        today_str = datetime.utcnow().strftime('%Y-%m-%d') # Naive UTC date string

        for eq in equipment_list:
            # Get latest usage and checklist for each equipment
            latest_usage = UsageLog.query.filter_by(equipment_id=eq.id).order_by(desc(UsageLog.log_date)).first()
            latest_checklist = Checklist.query.filter_by(equipment_id=eq.id).order_by(desc(Checklist.check_date)).first()
            # Store directly on the object for easy access in the template
            eq.latest_usage = latest_usage
            eq.latest_checklist = latest_checklist
            # Optionally pre-calculate checked_today flag here if preferred
            last_check_date_obj = eq.latest_checklist.check_date if eq.latest_checklist else None
            eq.checked_today = last_check_date_obj and last_check_date_obj.strftime('%Y-%m-%d') == today_str

        # 2. Fetch Open Job Cards
        open_job_cards = JobCard.query.filter(
            JobCard.status != 'Done'
        ).order_by(
            JobCard.due_date.asc().nullslast(), # Sort by due date (earliest first, None last)
            JobCard.id.desc() # Secondary sort by ID
        ).limit(10).all() # Limit for dashboard view

        # 3. Process Maintenance Tasks
        current_time = datetime.utcnow() # Use consistent naive UTC time
        logging.debug(f"Dashboard processing tasks using current_time (naive UTC): {current_time}")

        all_tasks = MaintenanceTask.query.join(Equipment).order_by(Equipment.name, MaintenanceTask.id).all()
        tasks_with_status_filtered = [] # List to hold only tasks relevant for dashboard

        logging.debug("Calculating task statuses for dashboard...")
        for task in all_tasks:
            # Calculate detailed status using the helper function
            status, due_info, due_date, last_performed_info, next_due_info, estimated_days_info = \
                calculate_task_due_status(task, current_time) # Ensure this function uses naive UTC

            # Assign calculated attributes to the task object
            task.due_status = status
            task.due_info = due_info
            task.due_date = due_date # Note: Primarily for days-based tasks
            task.last_performed_info = last_performed_info
            task.next_due_info = next_due_info
            task.estimated_days_info = estimated_days_info

            # --- Filter tasks for the dashboard view ---
            # Show Overdue, Due Soon, or tasks with specific warnings
            include_task = False
            # Check status string variations
            if isinstance(status, str):
                if "Overdue" in status or "Due Soon" in status or "Warning" in status:
                   include_task = True
            elif status in ["Overdue", "Due Soon"]: # Direct comparison if status is simple
                include_task = True

            if include_task:
                tasks_with_status_filtered.append(task)
        logging.debug(f"Finished calculating task statuses. {len(tasks_with_status_filtered)} tasks included for dashboard.")

        # --- Sort the filtered tasks in Python ---
        sort_order_map = {
            'Overdue': 1,
            'Due Soon': 2,
            'Warning': 3,
            'OK': 4, # Should generally not be in filtered list, but include for robustness
            'Error': 5,
            'Never Performed': 6, # Might appear if conditions met
            'Unknown': 7
        }

        def get_sort_key(task_item):
            # Gracefully handle potential None or non-string status
            status_str = getattr(task_item, 'due_status', 'Unknown')
            if not isinstance(status_str, str):
                status_str = 'Unknown'
            # Use the first word (e.g., "Overdue" from "Overdue (First)") for sorting key
            first_word = status_str.split(' ')[0]
            return sort_order_map.get(first_word, 99) # Default to 99 if word not in map

        try:
            tasks_with_status_filtered.sort(key=get_sort_key)
            logging.debug("Sorted filtered tasks for dashboard successfully.")
        except Exception as task_sort_exc:
            logging.error(f"Error sorting filtered tasks: {task_sort_exc}", exc_info=True)
            # Proceed with unsorted list in case of error

        # 4. Prepare Recent Activities
        recent_activities = []
        limit_count = 10 # How many recent items to fetch/show

        # Fetch recent items from different models
        latest_checklists = Checklist.query.order_by(Checklist.check_date.desc()).limit(limit_count).all()
        recent_usage_logs = UsageLog.query.order_by(UsageLog.log_date.desc()).limit(limit_count).all()
        # Fetch recent job cards (both created and completed might be relevant)
        recent_job_cards_created = JobCard.query.order_by(JobCard.start_datetime.desc().nullslast(), JobCard.id.desc()).limit(limit_count).all()
        recent_job_cards_completed = JobCard.query.filter(JobCard.status == 'Done').order_by(JobCard.end_datetime.desc().nullslast(), JobCard.id.desc()).limit(limit_count).all()

        logging.debug("Aggregating recent activities...")
        # Combine and format activities, ensuring naive UTC timestamps
        activity_map = {} # Use a dictionary to potentially avoid duplicates if needed, keyed by type+id

        for cl in latest_checklists:
            ts = cl.check_date
            if ts:
                 # Ensure naive UTC
                 if ts.tzinfo is not None: ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
                 key = f"checklist-{cl.id}"
                 if key not in activity_map:
                    activity_map[key] = {'type': 'checklist', 'timestamp': ts, 'description': f"Checklist logged for {cl.equipment_ref.code}: {cl.status}", 'details': cl}

        for ul in recent_usage_logs:
            ts = ul.log_date
            if ts:
                 if ts.tzinfo is not None: ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
                 key = f"usage-{ul.id}"
                 if key not in activity_map:
                    activity_map[key] = {'type': 'usage', 'timestamp': ts, 'description': f"Usage logged for {ul.equipment_ref.code}: {ul.usage_value}", 'details': ul}

        for jc in recent_job_cards_created:
             # Use start_datetime as the primary timestamp for creation
             create_time = jc.start_datetime
             if create_time:
                 if create_time.tzinfo is not None: create_time = create_time.astimezone(timezone.utc).replace(tzinfo=None)
                 key = f"jc_created-{jc.id}"
                 if key not in activity_map:
                     activity_map[key] = {'type': 'job_card_created', 'timestamp': create_time, 'description': f"Job Card {jc.job_number} created for {jc.equipment_ref.code}", 'details': jc}
             # else: Add fallback if start_datetime might be missing, using ID or another field?

        for jc in recent_job_cards_completed:
             completion_time = jc.end_datetime
             if completion_time:
                 if completion_time.tzinfo is not None: completion_time = completion_time.astimezone(timezone.utc).replace(tzinfo=None)
                 key = f"jc_completed-{jc.id}"
                 # Check if creation event is already there, maybe update description or add separate event
                 if key not in activity_map: # Add completion event if not present
                     activity_map[key] = {'type': 'job_card_completed', 'timestamp': completion_time, 'description': f"Job Card {jc.job_number} completed for {jc.equipment_ref.code}", 'details': jc}
                 # Optional: If creation event exists, maybe update its description? Or keep separate events.

        # Convert map values back to a list
        recent_activities = list(activity_map.values())

        # Sort the combined list by timestamp (descending)
        try:
            logging.debug("Attempting to sort combined recent_activities...")
            # Filter out any items that might have ended up without a timestamp
            valid_activities = [act for act in recent_activities if act.get('timestamp') is not None]

            if len(valid_activities) != len(recent_activities):
                logging.warning("Some aggregated activities were missing timestamps and excluded from sorting.")

            if valid_activities:
                valid_activities.sort(key=lambda x: x['timestamp'], reverse=True)
                recent_activities_sorted = valid_activities[:limit_count] # Limit the final list
                logging.debug(f"Sorting recent activities successful. Showing {len(recent_activities_sorted)} items.")
            else:
                 recent_activities_sorted = []
                 logging.debug("No valid activities with timestamps found for sorting.")

        except TypeError as sort_error:
            logging.error(f"TypeError during sorting recent_activities: {sort_error}", exc_info=True)
            recent_activities_sorted = [] # Clear list on error
            flash("Error sorting recent activity data due to inconsistent timezones.", "warning")
        except Exception as general_sort_error:
            logging.error(f"Unexpected error during sorting recent_activities: {general_sort_error}", exc_info=True)
            recent_activities_sorted = [] # Clear list on error
            flash("Unexpected error sorting recent activity data.", "warning")


        # 5. Render Template
        logging.debug("--- Rendering pm_dashboard.html ---")
        return render_template(
            'pm_dashboard.html',
            title='PM Dashboard',
            equipment=equipment_list, # Pass equipment list for status table and modals
            job_cards=open_job_cards, # Pass open job cards
            tasks=tasks_with_status_filtered, # Pass the pre-sorted list of relevant tasks
            recent_activities=recent_activities_sorted, # Pass the sorted recent activities
            today=today_str # Pass today's date string
            # Optional: Pass csrf_token() if forms on dashboard need it (modals already have it)
        )

    # --- Global Error Handling for the Route ---
    except Exception as e:
        # Log the full traceback for ANY exception in the dashboard route
        logging.error(f"--- Unhandled exception in dashboard route: {e} ---", exc_info=True)
        # Optionally use traceback.format_exc() for detailed logging
        # logging.error(traceback.format_exc())
        flash(f"An critical error occurred loading the dashboard: {e}. Please contact support.", "danger")
        # Return the template in a safe 'error' state
        # Ensure the error state template doesn't rely on data that might be missing
        return render_template('pm_dashboard.html',
                               title='PM Dashboard - Error',
                               error=True, # Flag for the template to show an error message
                               equipment=[], # Provide empty lists to prevent template errors
                               job_cards=[],
                               tasks=[],
                               recent_activities=[],
                               today=datetime.utcnow().strftime('%Y-%m-%d')) # Provide default today string

@bp.route('/equipment')
def equipment_list():
    """Displays a list of all equipment."""
    try:
        all_equipment = Equipment.query.order_by(Equipment.type, Equipment.name).all()
        return render_template('pm_equipment.html', equipment=all_equipment, title='Equipment List')
    except Exception as e:
        flash(f"Error loading equipment list: {e}", "danger")
        return render_template('pm_equipment.html', title='Equipment List', error=True)

@bp.route('/equipment/new', methods=['GET', 'POST'])
def add_equipment():
    if request.method == 'POST':
        code = request.form.get('code')  # New field
        name = request.form.get('name')
        type = request.form.get('type')
        checklist_required = 'checklist_required' in request.form

        if not code or not name or not type:
            flash('Equipment Code, Name, and Type are required.', 'warning')
            return render_template('pm_add_equipment.html', title='Add Equipment')

        existing_equipment = Equipment.query.filter_by(code=code).first()
        if existing_equipment:
            flash(f'Equipment with code "{code}" already exists.', 'warning')
            return render_template('pm_add_equipment.html', title='Add Equipment', 
                                 current_code=code, current_name=name, current_type=type, 
                                 current_checklist=checklist_required)

        new_equipment = Equipment(code=code, name=name, type=type, checklist_required=checklist_required)
        db.session.add(new_equipment)
        db.session.commit()
        flash(f'Equipment "{code} - {name}" added successfully!', 'success')
        return redirect(url_for('planned_maintenance.equipment_list'))
    return render_template('pm_add_equipment.html', title='Add Equipment')

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
                     part_id = int(part_id_s)
                     quantity = int(qty_s)
                     if quantity > 0: parts_to_process.append({'id': part_id, 'qty': quantity})
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
                task.last_performed = checkin_datetime # Assign naive checkin time
                logging.debug(f"Updating task {task.id} last_performed to {checkin_datetime} ({checkin_datetime.tzinfo})")

                # --- NEW: Update last performed usage value for hours/km tasks ---
                if task.interval_type in ['hours', 'km']:
                    # Find the latest usage log AT OR BEFORE the check-in time
                    usage_at_completion = UsageLog.query.filter(
                        UsageLog.equipment_id == task.equipment_id,
                        UsageLog.log_date <= checkin_datetime # Crucial filter condition
                    ).order_by(desc(UsageLog.log_date)).first()

                    if usage_at_completion:
                        task.last_performed_usage_value = usage_at_completion.usage_value
                        logging.debug(f"Updating task {task.id} last_performed_usage_value to {task.last_performed_usage_value}")
                    else:
                        # Handle case where no usage log exists before or at check-in
                        task.last_performed_usage_value = None # Set to None if unknown
                        logging.warning(f"Could not find usage log at or before {checkin_datetime} for task {task.id} completion. last_performed_usage_value set to None.")
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
                flash('Job card completed. Opening WhatsApp to share.', 'success')
                return redirect(whatsapp_url)
            else:
                flash(f'Job Card {job_card.job_number} completed successfully!', 'success')
                return redirect(url_for('planned_maintenance.dashboard'))

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

@bp.route('/job_card/new', methods=['POST'])
def new_job_card():
    try:
        equipment_id = request.form.get('equipment_id')
        description = request.form.get('description')
        technician = request.form.get('technician')
        oem_required = 'oem_required' in request.form
        kit_required = 'kit_required' in request.form
        send_whatsapp = 'send_whatsapp' in request.form
        due_date_str = request.form.get('due_date')  # New field

        if not equipment_id or not description:
            flash('Equipment and Description are required for a Job Card.', 'warning')
            return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

        due_date = None
        if due_date_str:
            try:
                due_date = parse_datetime(due_date_str)
                logging.debug(f"New JC due_date parsed: {due_date} ({due_date.tzinfo})")
            except ValueError:
                flash('Invalid due date format.', 'warning')
                return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

        year = datetime.now(UTC).year
        count = JobCard.query.count() + 1
        job_number = f"JC-{year}-{count:04d}"

        job_card = JobCard(
            job_number=job_number,
            equipment_id=int(equipment_id),
            description=description,
            technician=technician,
            status='To Do',
            oem_required=oem_required,
            kit_required=kit_required,
            due_date=due_date  # Save the due date
        )
        db.session.add(job_card)
        db.session.commit()

        equipment_name = Equipment.query.get(int(equipment_id)).name or 'Unknown Equipment'
        whatsapp_msg = (
            f"Job Card #{job_number} created:\n"
            f"Equipment: {equipment_name}\n"
            f"Task: {description}\n"
            f"Assigned: {technician or 'Unassigned'}\n"
            f"OEM Required: {'Yes' if oem_required else 'No'}\n"
            f"Kit Required: {'Yes' if kit_required else 'No'}\n"
            f"Due Date: {due_date.strftime('%Y-%m-%d') if due_date else 'Not Set'}"
        )
        
        flash(f'Job Card {job_number} created!', 'success')
        
        if send_whatsapp:
            encoded_msg = urlencode({'text': whatsapp_msg})
            whatsapp_url = f"https://wa.me/?{encoded_msg}"
            flash('Click the WhatsApp link to share the job card.', 'info')
            return redirect(whatsapp_url)
        
        return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

    except Exception as e:
        db.session.rollback()
        flash(f"Error creating job card: {e}", "danger")
        return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

@bp.route('/checklist/new', methods=['POST'])
def new_checklist():
    """Logs a new equipment checklist."""
    # This route expects the form to be included elsewhere (e.g., dashboard)
    if request.method == 'POST':
        try:
            equipment_id = request.form.get('equipment_id', type=int)
            status = request.form.get('status')
            issues = request.form.get('issues') # Optional

            # Basic Validation
            if not equipment_id or not status:
                flash('Equipment and Status are required for checklist.', 'warning')
                return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

            # Validate status value
            valid_statuses = ["Go", "Go But", "No Go"]
            if status not in valid_statuses:
                flash(f'Invalid status "{status}" selected.', 'warning')
                return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

            equipment = Equipment.query.get(equipment_id)
            if not equipment:
                 flash('Selected equipment not found.', 'danger')
                 return redirect(request.referrer or url_for('planned_maintenance.dashboard'))

            # Create checklist entry
            new_log = Checklist(
                equipment_id=equipment_id,
                status=status,
                issues=issues,
                check_date=datetime.now(UTC) # Log time automatically
            )
            logging.debug(f"New checklist check_date: {new_log.check_date} ({new_log.check_date.tzinfo})")
            db.session.add(new_log)
            db.session.commit()
            flash(f'Checklist logged successfully for {equipment.name} with status "{status}".', 'success')

        except Exception as e:
            db.session.rollback()
            flash(f"Error logging checklist: {e}", "danger")

        return redirect(url_for('planned_maintenance.dashboard'))
    
    # If accessed via GET, just redirect away
    return redirect(url_for('planned_maintenance.dashboard'))

def calculate_task_due_status(task, current_time): # Accept current_time (naive UTC)
    # ... (initial setup: status, due_info, etc. remains the same) ...
    logging.debug(f"    Calculating status for Task {task.id} ({task.description}) for Eq {task.equipment_id}. current_time = {current_time}")
    status = "Unknown"
    due_info = "N/A"
    due_date = None # Primarily for 'days' type
    last_performed_info = "Never"
    next_due_info = "N/A" # Will store 'Next Due at X hours/km'
    estimated_days_info = "N/A" # Will store '~Y days'
    numeric_estimated_days = None # Store the raw number for comparison

    # --- Timezone handling (remains the same) ---
    if current_time.tzinfo is not None:
        current_time = current_time.astimezone(timezone.utc).replace(tzinfo=None)
    last_performed_dt = task.last_performed
    if last_performed_dt and last_performed_dt.tzinfo is not None:
        last_performed_dt = last_performed_dt.astimezone(timezone.utc).replace(tzinfo=None)


    # ================== HOURS / KM LOGIC ==================
    if task.interval_type == 'hours' or task.interval_type == 'km':
        # ... (Steps 1-3: get latest log, check if performed, get last_performed_usage - remains the same) ...
        interval_unit = task.interval_type
        latest_log = UsageLog.query.filter_by(equipment_id=task.equipment_id).order_by(desc(UsageLog.log_date)).first()
        if not latest_log:
            status = f"Unknown (No Usage Data)"
            return status, due_info, due_date, last_performed_info, next_due_info, estimated_days_info
        current_usage = latest_log.usage_value
        current_usage_date = latest_log.log_date
        if current_usage_date and current_usage_date.tzinfo is not None:
             current_usage_date = current_usage_date.astimezone(timezone.utc).replace(tzinfo=None)
        next_due_info = f"Current: {current_usage:.1f} {interval_unit}"

        if not last_performed_dt: # Never performed logic
            status = "Never Performed"
            next_due_info = f"Due at {task.interval_value} {interval_unit} (First)"
            hours_until_due = task.interval_value - current_usage
            if hours_until_due <= 0: status = "Overdue (First)"; due_info = f"Over by {abs(hours_until_due):.1f} {interval_unit}"
            elif hours_until_due <= task.interval_value * 0.1: status = "Due Soon (First)"; due_info = f"{hours_until_due:.1f} {interval_unit} remaining"
            else: status = "OK (First)"; due_info = f"{hours_until_due:.1f} {interval_unit} remaining"
            estimated_days_info = "N/A (First)"
            return status, due_info, due_date, last_performed_info, next_due_info, estimated_days_info

        last_performed_usage = task.last_performed_usage_value
        if last_performed_usage is None: # Usage unknown logic
             status = f"Warning (Usage at Last Done Unknown)"
             last_performed_info = f"{last_performed_dt.strftime('%Y-%m-%d')} (Usage Unknown)"
             next_due_info = "Cannot Calculate"; estimated_days_info = "Cannot Calculate"
             return status, due_info, due_date, last_performed_info, next_due_info, estimated_days_info

        last_performed_info = f"{last_performed_dt.strftime('%Y-%m-%d')} at {last_performed_usage:.1f} {interval_unit}"

        # 4. Calculate key metrics (remains the same)
        next_due_at_usage = last_performed_usage + task.interval_value
        hours_until_next = next_due_at_usage - current_usage
        next_due_info = f"Next due at {next_due_at_usage:.1f} {interval_unit}"
        due_info = f"{hours_until_next:.1f} {interval_unit} remaining"

        # 5. Determine PRIMARY Status based on hours_until_next (remains the same)
        if hours_until_next <= 0:
            status = "Overdue"
            due_info = f"Overdue by {abs(hours_until_next):.1f} {interval_unit}"
        elif hours_until_next <= task.interval_value * 0.10: # 10% buffer
            status = "Due Soon"
        else:
            status = "OK"

        # 6. Estimate days until due (Calculate numeric_estimated_days)
        previous_log = UsageLog.query.filter(
            UsageLog.equipment_id == task.equipment_id,
            UsageLog.log_date < current_usage_date
        ).order_by(desc(UsageLog.log_date)).first()

        if previous_log and current_usage_date > previous_log.log_date:
             prev_log_date = previous_log.log_date
             if prev_log_date and prev_log_date.tzinfo is not None:
                 prev_log_date = prev_log_date.astimezone(timezone.utc).replace(tzinfo=None)
             usage_diff = current_usage - previous_log.usage_value
             time_diff = current_usage_date - prev_log_date
             time_diff_days = time_diff.total_seconds() / (24 * 3600)

             if time_diff_days > 0 and usage_diff >= 0:
                 avg_daily_usage = usage_diff / time_diff_days
                 logging.debug(f"    Task {task.id}: Avg daily usage = {avg_daily_usage:.2f} {interval_unit}/day")
                 if avg_daily_usage > 0.01: # Check for minimal usage rate
                     # --- Calculate numeric value ---
                     numeric_estimated_days = hours_until_next / avg_daily_usage
                     # --- Format the string ---
                     if status == "Overdue": estimated_days_info = f"~{abs(numeric_estimated_days):.1f} days Overdue"
                     else: estimated_days_info = f"~{numeric_estimated_days:.1f} days"
                 else: estimated_days_info = "N/A (Low Usage Rate)"
             else: estimated_days_info = "N/A (Rate Calc Error)"
        else: estimated_days_info = "N/A (Insufficient Data)"

        # --- **** NEW LOGIC **** ---
        # 7. Refine status based on estimated days, if currently "OK"
        if status == "OK" and numeric_estimated_days is not None and numeric_estimated_days <= DUE_SOON_ESTIMATED_DAYS_THRESHOLD:
            logging.debug(f"    Task {task.id}: Status was OK, but estimated days ({numeric_estimated_days:.1f}) <= threshold ({DUE_SOON_ESTIMATED_DAYS_THRESHOLD}). Changing status to Due Soon.")
            status = "Due Soon"
        # --- **** END NEW LOGIC **** ---

        return status, due_info, due_date, last_performed_info, next_due_info, estimated_days_info

    # ================== DAYS LOGIC (remains the same) ==================
    elif task.interval_type == 'days':
        if last_performed_dt:
            due_date = last_performed_dt + timedelta(days=task.interval_value)
            logging.debug(f"    Task {task.id} ('days'): Calculated due_date = {due_date} (naive)")
            try:
                time_difference = due_date - current_time
                days_until_due = time_difference.days
                total_seconds_until_due = time_difference.total_seconds()

                # Determine status based on actual days remaining
                if total_seconds_until_due < 0:
                    status = "Overdue"
                    due_info = f"Due on {due_date.strftime('%Y-%m-%d')} ({abs(days_until_due)} days ago)"
                # Use the SAME threshold constant for consistency in "Due Soon" definition
                elif days_until_due <= DUE_SOON_ESTIMATED_DAYS_THRESHOLD:
                    status = "Due Soon"
                    due_info = f"Due on {due_date.strftime('%Y-%m-%d')} (in {days_until_due} days)"
                else:
                    status = "OK"
                    due_info = f"Due on {due_date.strftime('%Y-%m-%d')} (in {days_until_due} days)"

                last_performed_info = f"{last_performed_dt.strftime('%Y-%m-%d')}"
                next_due_info = f"Due on {due_date.strftime('%Y-%m-%d')}"
                estimated_days_info = due_info # For days tasks, due_info is the estimate

            except TypeError as calc_err:
                 logging.error(f"    Task {task.id}: TYPE ERROR during 'days_until_due' calculation!", exc_info=True)
                 status, due_info, due_date, last_performed_info, next_due_info = "Error Calculating Due", "Calculation Error", None, "Error", "Error"
                 estimated_days_info = "Error" # Ensure estimate also shows error
        else:
            status, last_performed_info, next_due_info, due_info = "Never Performed", "Never", "N/A (First)", "N/A (First)"
            estimated_days_info = "N/A (First)" # No estimate if never performed

        return status, due_info, due_date, last_performed_info, next_due_info, estimated_days_info
    # ===================================================================
    else: # Unknown interval type
        status = "Unknown Interval Type"
        due_info = task.interval_type
        last_performed_info = last_performed_dt.strftime('%Y-%m-%d') if last_performed_dt else "Never"
        return status, due_info, due_date, last_performed_info, next_due_info, estimated_days_info

@bp.route('/tasks', methods=['GET'])
def tasks_list():
    logging.debug("--- Entering tasks_list route ---")
    try:
        # ... (existing code: type_filter, query setup) ...
        type_filter = request.args.get('type')
        query = MaintenanceTask.query.join(Equipment)
        if type_filter:
            query = query.filter(Equipment.type == type_filter)
        all_tasks_query = query.order_by(Equipment.code, Equipment.name, MaintenanceTask.id).all()

        current_time_for_list = datetime.utcnow() # Naive UTC
        logging.debug(f"Tasks List using current_time: {current_time_for_list}")

        tasks_by_equipment = {}
        for task in all_tasks_query: # Use a different variable name
            eq_key = (task.equipment_ref.code, task.equipment_ref.name, task.equipment_ref.type)
            if eq_key not in tasks_by_equipment:
                tasks_by_equipment[eq_key] = []

            # Calculate status and add attributes
            status, due_info, due_date, last_performed_info, next_due_info, estimated_days_info = \
                calculate_task_due_status(task, current_time_for_list)
            task.due_status = status
            task.due_info = due_info
            task.due_date = due_date
            task.last_performed_info = last_performed_info
            task.next_due_info = next_due_info
            task.estimated_days_info = estimated_days_info
            tasks_by_equipment[eq_key].append(task) # Add task object with attributes

        # --- Sort tasks within each equipment group ---
        sort_order_tasks = { # Define sort order again or make it global/helper
            'Overdue': 1, 'Due Soon': 2, 'Warning': 3, 'OK': 4,
            'Error': 5, 'Never Performed': 6, 'Unknown': 7
        }
        def get_tasks_sort_key(task_item):
            status_str = getattr(task_item, 'due_status', 'Unknown')
            if not isinstance(status_str, str): status_str = 'Unknown'
            first_word = status_str.split(' ')[0]
            return sort_order_tasks.get(first_word, 99)

        sorted_tasks_by_equipment = {}
        # Sort equipment keys alphabetically for consistent display order
        sorted_equipment_keys = sorted(tasks_by_equipment.keys())

        for eq_key in sorted_equipment_keys:
            tasks_list_for_eq = tasks_by_equipment[eq_key]
            try:
                 tasks_list_for_eq.sort(key=get_tasks_sort_key)
                 sorted_tasks_by_equipment[eq_key] = tasks_list_for_eq
            except Exception as eq_sort_exc:
                logging.error(f"Error sorting tasks for equipment {eq_key}: {eq_sort_exc}", exc_info=True)
                sorted_tasks_by_equipment[eq_key] = tasks_by_equipment[eq_key] # Use unsorted list on error


        equipment_types = db.session.query(Equipment.type).distinct().order_by(Equipment.type).all()
        equipment_types = [t[0] for t in equipment_types]

        logging.debug("--- Rendering pm_tasks.html ---")
        return render_template(
            'pm_tasks.html',
            tasks_by_equipment=sorted_tasks_by_equipment, # Pass the sorted dictionary
            equipment_types=equipment_types,
            type_filter=type_filter,
            title='Maintenance Tasks'
        )
    except Exception as e:
        logging.error(f"--- Error loading tasks_list page: {e} ---", exc_info=True)
        flash(f"Error loading maintenance tasks: {e}", "danger")
        return render_template('pm_tasks.html',
                               tasks_by_equipment={},
                               equipment_types=[],
                               type_filter=request.args.get('type'),
                               title='Maintenance Tasks',
                               error=True)

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

# --- Usage Log (Needed for Task Status - Add if not present) ---
# Simple route to add usage logs manually for testing task status
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

            log_date = datetime.now(UTC)
            if log_date_str:
                try:
                    log_date = parse_datetime(log_date_str)
                except ValueError:
                    flash("Invalid Log Date format, using current time.", "info")
            logging.debug(f"Adding usage log_date: {log_date} ({log_date.tzinfo})")
            usage = UsageLog(equipment_id=equipment_id, usage_value=usage_value, log_date=log_date)
            db.session.add(usage)
            db.session.commit()
            flash("Usage log added successfully.", "success")

        except Exception as e:
            db.session.rollback()
            flash(f"Error adding usage log: {e}", "danger")
            
        return redirect(request.referrer or url_for('planned_maintenance.dashboard'))
        
     # If accessed via GET, just redirect away
     return redirect(url_for('planned_maintenance.dashboard'))    

@bp.route('/job_card/new_from_task/<int:task_id>', methods=['POST'])
def new_job_card_from_task(task_id):
    try:
        task = MaintenanceTask.query.get_or_404(task_id)
        technician = request.form.get('technician')
        year = datetime.now().year
        count = JobCard.query.count() + 1
        job_number = f"JC-{year}-{count:04d}"
        current_time_for_calc = datetime.utcnow()
        status, due_info, due_date, last_performed_info, next_due_info, estimated_days_info = \
            calculate_task_due_status(task, current_time_for_calc)
        logging.debug(f"New JC from task {task.id} calculated due_date: {due_date} ({due_date.tzinfo if due_date else 'None'})")
        
        job_card = JobCard(
            job_number=job_number,
            equipment_id=task.equipment_id,
            description=task.description,
            technician=technician,
            status='To Do',
            oem_required=task.oem_required,
            kit_required=task.kit_required,
            due_date=due_date  # Set due date from task
        )
        db.session.add(job_card)
        db.session.commit()

        equipment_name = task.equipment_ref.name or 'Unknown Equipment'
        whatsapp_msg = (
            f"Job Card #{job_number} created from Task:\n"
            f"Equipment: {equipment_name}\n"
            f"Task: {task.description}\n"
            f"Assigned: {technician or 'Unassigned'}\n"
            f"OEM Required: {'Yes' if task.oem_required else 'No'}\n"
            f"Kit Required: {'Yes' if task.kit_required else 'No'}\n"
            f"Due Date: {due_date.strftime('%Y-%m-%d') if due_date else 'Not Set'}"
        )
        
        flash(f'Job Card {job_number} created from task!', 'success')
        encoded_msg = urlencode({'text': whatsapp_msg})
        whatsapp_url = f"https://wa.me/?{encoded_msg}"
        return redirect(whatsapp_url)

    except Exception as e:
        db.session.rollback()
        logging.error(f"Error creating job card from task {task_id}: {e}", exc_info=True) 
        flash(f"Error creating job card from task: {e}", "danger")
        return redirect(url_for('planned_maintenance.dashboard'))

@bp.route('/checklist_logs', methods=['GET'])
def checklist_logs():
    """Displays checklist logs, optionally filtered by equipment."""
    equipment_id = request.args.get('equipment_id', type=int)
    query = Checklist.query
    if equipment_id:
        query = query.filter(Checklist.equipment_id == equipment_id)
    logs = query.order_by(desc(Checklist.check_date)).all()
    equipment_list = Equipment.query.order_by(Equipment.name).all()
    return render_template('pm_checklist_logs.html', logs=logs, equipment=equipment_list, equipment_id=equipment_id, title='Checklist Logs')

@bp.route('/usage_logs', methods=['GET'])
def usage_logs():
    type_filter = request.args.get('type')
    name_filter = request.args.get('name')
    query = UsageLog.query.join(Equipment)
    if type_filter:
        query = query.filter(Equipment.type == type_filter)
    if name_filter:
        query = query.filter(Equipment.name.ilike(f"%{name_filter}%"))
    logs = query.order_by(desc(UsageLog.log_date)).all()
    types = Equipment.query.with_entities(Equipment.type).distinct().all()
    return render_template('pm_usage_logs.html', logs=logs, types=[t[0] for t in types], type_filter=type_filter, name_filter=name_filter)    