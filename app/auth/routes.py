# app/auth/routes.py
import logging
from flask import render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, current_user, login_required
# from werkzeug.utils import url_parse # <<< REMOVE THIS LINE or THE ONE from werkzeug.urls
from urllib.parse import urlparse as _urlparse # <<< ADD THIS LINE (using an alias)
from app import db
from app.auth import bp
from app.auth.forms import LoginForm, RegistrationForm, UserEditForm
from app.models import User # Ensure User model can be imported
from functools import wraps
from datetime import datetime, timezone


# --- Role-based access decorator ---
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            logging.warning(f"Admin access denied for user {current_user.username if current_user.is_authenticated else 'Anonymous'} to {request.path}")
            flash('You do not have permission to access this page.', 'danger')
            return redirect(url_for('planned_maintenance.dashboard' if current_user.is_authenticated else 'auth.login'))
        return f(*args, **kwargs)
    return decorated_function

@bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('planned_maintenance.dashboard'))
    form = LoginForm()
    if form.validate_on_submit():
        user_query = User.query.filter(
            (User.username == form.username_or_email.data) |
            (User.email == form.username_or_email.data.lower())
        )
        user = user_query.first()

        if user is None or not user.check_password(form.password.data):
            flash('Invalid username/email or password.', 'danger')
            logging.warning(f"Failed login attempt for user/email: {form.username_or_email.data}") # Added logging
            return redirect(url_for('auth.login'))
        
        if not user.is_active:
            flash('This account is inactive. Please contact an administrator.', 'warning')
            logging.warning(f"Inactive user login attempt: {user.username}") # Added logging
            return redirect(url_for('auth.login'))

        login_user(user, remember=form.remember_me.data)
        user.last_login_at = datetime.now(timezone.utc)
        db.session.commit()

        next_page = request.args.get('next')
        # --- MODIFIED HERE to use _urlparse ---
        if not next_page or _urlparse(next_page).netloc != '':
            next_page = url_for('planned_maintenance.dashboard')
        # --- END MODIFICATION ---
        flash(f'Welcome back, {user.username}!', 'success') # Added welcome flash
        logging.info(f"User {user.username} logged in successfully.") # Added logging
        return redirect(next_page)
        
    return render_template('login.html', title='Sign In', form=form)

@bp.route('/logout')
@login_required
def logout():
    username = current_user.username
    logout_user()
    flash('You have been logged out.', 'info')
    logging.info(f"User {username} logged out.")
    return redirect(url_for('auth.login'))

@bp.route('/register', methods=['GET', 'POST'])
@login_required
@admin_required
def register():
    form = RegistrationForm()
    if form.validate_on_submit():
        try:
            email_data = form.email.data.lower() if form.email.data else None
            user = User(username=form.username.data, email=email_data, role=form.role.data)
            user.set_password(form.password.data)
            db.session.add(user)
            db.session.commit()
            flash(f'User {form.username.data} has been registered successfully!', 'success')
            logging.info(f"Admin {current_user.username} registered new user: {form.username.data}") # Added logging
            return redirect(url_for('auth.user_list'))
        except Exception as e:
            db.session.rollback() # Added rollback
            logging.error(f"Error during user registration by {current_user.username}: {e}", exc_info=True) # Added logging
            flash(f"An error occurred during registration: {str(e)}", "danger") # Added flash for general error
            # No redirect here, let it re-render the form with errors if any WTForms validation failed (though validate_on_submit should catch those)
            # or if a DB error occurred.
    return render_template('register.html', title='Register New User', form=form)

@bp.route('/users')
@login_required
@admin_required
def user_list():
    page = request.args.get('page', 1, type=int)
    per_page = 15
    users_pagination = User.query.order_by(User.username).paginate(page=page, per_page=per_page, error_out=False)
    return render_template('user_list.html', title='Manage Users', users_pagination=users_pagination)

@bp.route('/users/edit/<int:user_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_user(user_id):
    user_to_edit = db.session.get(User, user_id)
    if not user_to_edit:
        flash(f"User with ID {user_id} not found.", "danger") # Added flash
        return redirect(url_for('auth.user_list')) # Added redirect
        
    form = UserEditForm(original_username=user_to_edit.username, obj=user_to_edit) # Assuming UserEditForm handles original_email if still present
    
    if form.validate_on_submit():
        try:
            user_to_edit.username = form.username.data
            user_to_edit.email = form.email.data.lower() if form.email.data else None
            user_to_edit.role = form.role.data
            user_to_edit.is_active = form.is_active.data
            db.session.commit()
            flash(f'User {user_to_edit.username} updated successfully.', 'success')
            logging.info(f"Admin {current_user.username} edited user: {user_to_edit.username}") # Added logging
            return redirect(url_for('auth.user_list'))
        except Exception as e:
            db.session.rollback() # Added rollback
            logging.error(f"Error editing user {user_to_edit.username} by {current_user.username}: {e}", exc_info=True) # Added logging
            flash(f"An error occurred while updating the user: {str(e)}", "danger") # Added flash for general error
            # Re-render form with current data on error
            
    return render_template('user_edit.html', title=f'Edit User: {user_to_edit.username}', form=form, user_to_edit=user_to_edit)

@bp.route('/users/delete/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def delete_user(user_id):
    user_to_delete = db.session.get(User, user_id)
    if not user_to_delete:
        flash(f"User with ID {user_id} not found.", "danger")
        return redirect(url_for('auth.user_list'))

    if user_to_delete.id == current_user.id:
        flash("You cannot delete your own account.", "danger")
        return redirect(url_for('auth.user_list'))
    
    try:
        username_deleted = user_to_delete.username
        db.session.delete(user_to_delete)
        db.session.commit()
        flash(f"User {username_deleted} has been deleted.", "success")
        logging.info(f"Admin {current_user.username} deleted user: {username_deleted}")
    except Exception as e:
        db.session.rollback()
        logging.error(f"Error deleting user {user_to_delete.username} by {current_user.username}: {e}", exc_info=True)
        flash(f"An error occurred while deleting the user: {str(e)}", "danger")
    return redirect(url_for('auth.user_list'))