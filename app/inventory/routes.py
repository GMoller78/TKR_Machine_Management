# tkr_system/app/inventory/routes.py
from flask import render_template, request, redirect, url_for, flash, abort
from urllib.parse import urlencode  # For encoding WhatsApp messages
from datetime import datetime
from sqlalchemy.exc import IntegrityError  # To catch unique constraint violations
from app.inventory import bp  # Import the blueprint instance
from app import db            # Import the database instance
from app.models import Part, StockTransaction, Supplier  # Import necessary models
from itertools import zip_longest

# --- Inventory Routes ---

@bp.route('/')
def dashboard():
    """Displays the Inventory dashboard."""
    try:
        # Query all parts, ordered by store then name for grouping in template
        all_parts = Part.query.order_by(Part.store, Part.name).all()
        # Query parts below minimum stock level
        low_stock_parts = Part.query.filter(Part.current_stock < Part.min_stock).order_by(Part.store, Part.name).all()
        # Query the 10 most recent stock transactions
        recent_transactions = StockTransaction.query.order_by(StockTransaction.transaction_date.desc()).limit(10).all()
        all_parts_for_dropdown = Part.query.order_by(Part.store, Part.name).all()

        return render_template(
            'inv_dashboard.html',
            title='Inventory Dashboard',
            parts=all_parts,
            low_stock=low_stock_parts,
            transactions=recent_transactions,
            all_parts=all_parts, # The parts list for display
            parts_for_dropdown=all_parts_for_dropdown # Pass specifically for the form
        )
    except Exception as e:
        flash(f"Error loading inventory dashboard: {e}", "danger")
        # Render a minimal template or redirect to an error page if preferred
        return render_template('inv_dashboard.html', title='Inventory Dashboard', error=True)

@bp.route('/parts')
def parts_list():
    """Displays a list of all parts and a form to add new parts."""
    try:
        all_parts = Part.query.order_by(Part.store, Part.name).all()
        suppliers = Supplier.query.order_by(Supplier.name).all() # Needed for Add Part form dropdown
        # Prepare parts data grouped by store for easier rendering
        parts_by_store = {}
        for part in all_parts:
            if part.store not in parts_by_store:
                parts_by_store[part.store] = []
            parts_by_store[part.store].append(part)
            
        return render_template(
            'inv_parts.html', 
            parts_by_store=parts_by_store, 
            suppliers=suppliers, 
            title='Manage Parts'
        )
    except Exception as e:
        flash(f"Error loading parts list: {e}", "danger")
        return render_template('inv_parts.html', title='Manage Parts', error=True, suppliers=[]) # Pass empty suppliers on error

@bp.route('/parts/add', methods=['POST'])
def add_part():
    """Handles adding a new part."""
    # This route should only accept POST requests
    if request.method != 'POST':
        abort(405) # Method Not Allowed

    try:
        name = request.form.get('name')
        supplier_id = request.form.get('supplier_id', type=int)
        store = request.form.get('store')
        # Get stock levels, default to 0 if empty or invalid, ensure non-negative
        current_stock = max(0, request.form.get('current_stock', 0, type=int))
        min_stock = max(0, request.form.get('min_stock', 0, type=int))
        is_get = 'is_get' in request.form # Checkbox value

        # --- Basic Input Validation ---
        errors = []
        if not name: errors.append("Part Name is required.")
        if not supplier_id: errors.append("Supplier is required.")
        if not store: errors.append("Store location is required.")
        
        # Check if supplier exists only if supplier_id is provided
        supplier = None
        if supplier_id:
            supplier = Supplier.query.get(supplier_id)
            if not supplier:
                errors.append(f'Invalid Supplier ID selected.')
        else:
            # This case is already caught by 'if not supplier_id'
            pass 

        if errors:
            for error in errors:
                flash(error, 'warning')
            # Redirect back to the parts list page where the form likely resides
            return redirect(url_for('inventory.parts_list')) 
             
        # --- Create and save the new part ---
        new_part = Part(
            name=name,
            supplier_id=supplier_id,
            store=store,
            current_stock=current_stock,
            min_stock=min_stock,
            is_get=is_get
        )
        db.session.add(new_part)
        db.session.commit()
        flash(f'Part "{name}" added successfully to {store} store!', 'success')

    except IntegrityError: # Catch potential unique constraint violation on Part.name
         db.session.rollback()
         flash(f'Error: Part name "{name}" already exists. Please use a unique name.', 'danger')
    except Exception as e:
        db.session.rollback() # Rollback in case of other errors
        flash(f"Error adding part: {e}", "danger")

    # Redirect back to the parts list page after POST attempt (success or fail)
    return redirect(url_for('inventory.parts_list'))

@bp.route('/suppliers', methods=['GET', 'POST'])
def manage_suppliers():
    """Displays a list of suppliers and handles adding new ones."""
    
    if request.method == 'POST':
        # Handle the form submission for adding a new supplier
        try:
            name = request.form.get('name')
            contact_info = request.form.get('contact_info') # Optional

            if not name:
                flash('Supplier Name is required.', 'warning')
                # Redirect back to the GET view of this same page
                return redirect(url_for('inventory.manage_suppliers'))

            # Create new supplier instance
            new_supplier = Supplier(name=name, contact_info=contact_info)
            db.session.add(new_supplier)
            db.session.commit()
            flash(f'Supplier "{name}" added successfully!', 'success')

        except IntegrityError: # Catch unique constraint violation (assuming name is unique)
             db.session.rollback()
             flash(f'Error: Supplier name "{name}" already exists. Please use a unique name.', 'danger')
        except Exception as e:
            db.session.rollback()
            flash(f"Error adding supplier: {e}", "danger")
            
        # Redirect back to the GET view after POST processing
        return redirect(url_for('inventory.manage_suppliers'))

    # --- Handle GET request ---
    # Fetch all suppliers to display in the list
    try:
        all_suppliers = Supplier.query.order_by(Supplier.name).all()
    except Exception as e:
        flash(f"Error loading suppliers: {e}", "danger")
        all_suppliers = [] # Ensure template receives a list even on error

    return render_template('inv_suppliers.html', suppliers=all_suppliers, title='Manage Suppliers')

@bp.route('/receive_stock', methods=['POST'])
def receive_stock():
    """Handles receiving stock for a specific part."""
    # This route should only accept POST requests
    if request.method != 'POST':
        abort(405) # Method Not Allowed
        
    part_id_str = request.form.get('part_id')
    quantity_str = request.form.get('quantity')
    
    # --- Input Validation ---
    errors = []
    part = None
    quantity = 0

    if not part_id_str:
        errors.append('Part selection is required.')
    else:
        try:
            part_id = int(part_id_str)
            # Retrieve the part or return 404 if not found (get_or_404 handles this)
            part = Part.query.get_or_404(part_id) 
        except ValueError:
             errors.append('Invalid Part ID.')
        except Exception: # Catches the 404 from get_or_404 if needed, though it usually aborts
             errors.append('Selected Part not found.') # Should ideally not be reached if get_or_404 works

    if not quantity_str:
        errors.append('Quantity is required.')
    else:
        try:
            quantity = int(quantity_str)
            if quantity <= 0:
                 errors.append("Quantity must be a positive whole number.")
        except ValueError:
            errors.append('Invalid quantity entered. Please enter a positive whole number.')

    if errors:
        for error in errors:
            flash(error, 'warning')
        # Redirect back to where the form was likely submitted (e.g., dashboard or parts list)
        return redirect(request.referrer or url_for('inventory.dashboard')) 
        
    # --- Process Stock Update and Transaction ---
    try:
        # Update stock level (part object is already fetched and validated)
        part.current_stock += quantity

        # Create stock transaction record
        description = f"Received {quantity} unit(s)"
        transaction = StockTransaction(
            part_id=part.id,
            quantity=quantity, # Positive for received
            description=description,
            transaction_date=datetime.utcnow() # Explicitly set, though model has default
        )

        db.session.add(part) # Add modified part to session (SQLAlchemy tracks changes)
        db.session.add(transaction)
        db.session.commit()

        # Prepare WhatsApp message
        whatsapp_msg = (
            f"Stock Received:\n"
            f"Part: {part.name}\n"
            f"Quantity: +{quantity}\n"
            f"New Stock: {part.current_stock}\n"
            f"Store: {part.store}\n"
            f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}" # Use local time for user readability
        )

        # URL Encode the message text
        encoded_msg = urlencode({'text': whatsapp_msg})
        whatsapp_url = f"https://wa.me/?{encoded_msg}" # Standard WhatsApp link format

        flash(f'Successfully received {quantity} unit(s) of {part.name}. New stock: {part.current_stock}.', 'success')
        # Redirect user's browser to the WhatsApp URL
        return redirect(whatsapp_url)

    except Exception as e:
        db.session.rollback()
        flash(f"Error processing stock reception: {e}", "danger")
        # Redirect back to the referring page or dashboard on error
        return redirect(request.referrer or url_for('inventory.dashboard'))
    
    # Add these imports if not already present
from itertools import zip_longest # For stock take processing

# --- Stock Take ---
@bp.route('/stock_take', methods=['GET', 'POST'])
def stock_take():
    """Displays form for stock take (GET) or processes results (POST)."""
    
    if request.method == 'POST':
        part_ids_str = request.form.getlist('part_id')
        actual_stocks_str = request.form.getlist('actual_stock')
        
        discrepancies_found = []
        processed_count = 0
        error_count = 0
        
        try:
            # Iterate through submitted parts and actual stock counts
            for part_id_s, actual_s in zip_longest(part_ids_str, actual_stocks_str):
                # Only process if both part_id and actual_stock are provided for a row
                if not part_id_s or actual_s is None or actual_s == '': 
                    continue

                try:
                    part_id = int(part_id_s)
                    actual_stock = int(actual_s)

                    if actual_stock < 0:
                         flash(f"Invalid negative stock '{actual_s}' for Part ID {part_id_s}. Skipped.", "warning")
                         error_count += 1
                         continue

                    part = Part.query.get(part_id)
                    if not part:
                        flash(f"Part ID {part_id_s} not found. Skipped.", "warning")
                        error_count += 1
                        continue

                    # Calculate discrepancy
                    discrepancy = actual_stock - part.current_stock

                    if discrepancy != 0:
                        # Record the change
                        original_stock = part.current_stock
                        part.current_stock = actual_stock # Update part's stock level
                        
                        # Log the transaction representing the adjustment
                        transaction = StockTransaction(
                            part_id=part.id,
                            quantity=discrepancy, # Positive if stock increased, negative if decreased
                            description=f"Stock Take Adjustment (From {original_stock} to {actual_stock})",
                            transaction_date=datetime.utcnow()
                        )
                        db.session.add(part) # Add modified part to session
                        db.session.add(transaction)
                        
                        discrepancies_found.append(
                            f"{part.name}: {original_stock} -> {actual_stock} ({'+' if discrepancy > 0 else ''}{discrepancy})"
                        )
                    
                    processed_count += 1 # Count successfully processed items

                except ValueError:
                    flash(f"Invalid number format for Part ID '{part_id_s}' or Stock '{actual_s}'. Skipped.", "warning")
                    error_count += 1
                    continue
            
            # Commit all changes made during the loop
            if processed_count > 0 or discrepancies_found: # Only commit if something changed
                 db.session.commit()
                 if discrepancies_found:
                     flash("Stock take processed. Discrepancies recorded: " + "; ".join(discrepancies_found), "info")
                 else:
                     flash(f"Stock take processed. {processed_count} items checked, no discrepancies found.", "success")
            else:
                 if error_count == 0:
                     flash("Stock take submitted, but no changes were detected or processed.", "info")
                 # If only errors occurred, previous flashes cover it.

        except Exception as e:
            db.session.rollback()
            flash(f"Error processing stock take: {e}", "danger")

        return redirect(url_for('inventory.dashboard'))

    # --- Handle GET Request ---
    try:
        # Fetch all parts ordered for the form
        all_parts = Part.query.order_by(Part.store, Part.name).all()
    except Exception as e:
        flash(f"Error loading parts for stock take: {e}", "danger")
        all_parts = []

    return render_template('inv_stock_take.html', parts=all_parts, title='Perform Stock Take')