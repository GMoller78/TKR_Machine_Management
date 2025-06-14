# tkr_system/app/models.py
from app import db, login_manager 
from datetime import datetime, timezone, date # Added date
from sqlalchemy import Index # Import Index
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash



# --- Helper Function for Date/Time Formatting ---
def format_datetime_iso(dt):
    """Helper to format date or datetime to ISO string, handling None and timezones."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        # If timezone-aware, convert to UTC before formatting
        if dt.tzinfo is not None and dt.tzinfo.utcoffset(dt) is not None:
            dt = dt.astimezone(timezone.utc)
        # If naive, assume UTC or format directly (depending on app consistency needs)
        # Formatting naive as is, common practice if app uses naive UTC internally
    elif isinstance(dt, date):
        # Dates are formatted directly
        pass
    else: # Should not happen with Date/DateTime columns
        return str(dt)

    return dt.isoformat()

class Equipment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), nullable=False, unique=True)
    name = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(50), nullable=False, index=True) # Added index
    checklist_required = db.Column(db.Boolean, default=False)
    # ---- NEW FIELD ----
    status = db.Column(db.String(50), nullable=False, default='Operational', index=True)
    # ---- END NEW FIELD ----
    maintenance_tasks = db.relationship('MaintenanceTask', backref='equipment_ref', lazy='dynamic')
    job_cards = db.relationship('JobCard', backref='equipment_ref', lazy='dynamic')
    checklists = db.relationship('Checklist', backref='equipment_ref', lazy='dynamic')
    usage_logs = db.relationship('UsageLog', backref='equipment_ref', lazy='dynamic')
    plan_entries = db.relationship('MaintenancePlanEntry', backref='equipment', lazy='dynamic') # Added backref

    def __repr__(self):
        return f'<Equipment {self.name} ({self.status})>' # Added status to repr

    def to_dict(self):
        """Returns a dictionary representation for API usage."""
        return {
            'id': self.id,
            'code': self.code,
            'name': self.name,
            'type': self.type,
            'checklist_required': self.checklist_required,
            'status': self.status, # Added status
        }

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), index=True, unique=True, nullable=False)
    email = db.Column(db.String(120), index=True, unique=True, nullable=False)
    password_hash = db.Column(db.String(256))
    role = db.Column(db.String(20), default='user', nullable=False) # 'user' or 'admin'
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_login_at = db.Column(db.DateTime, nullable=True)


    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        if self.password_hash is None: # Handle users created without a password initially
            return False
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f'<User {self.username} ({self.role})>'

    # UserMixin provides: is_authenticated, is_active (uses our column), is_anonymous, get_id

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id)) # Use db.session.get for SQLAlchemy 2.0+

class MaintenanceTask(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # --- IMPORTANT: Changed equipment_id foreign key ---
    equipment_id = db.Column(db.Integer, db.ForeignKey('equipment.id', name='fk_maintenance_task_equipment_id'), nullable=False)
    # --- END CHANGE ---
    description = db.Column(db.String(255), nullable=False)
    interval_type = db.Column(db.String(50), nullable=False)
    interval_value = db.Column(db.Integer, nullable=False)
    oem_required = db.Column(db.Boolean, default=False)
    kit_required = db.Column(db.Boolean, default=False)
    last_performed = db.Column(db.DateTime, nullable=True) # Naive or Aware depends on app logic
    last_performed_usage_value = db.Column(db.Float, nullable=True)
    plan_entries = db.relationship('MaintenancePlanEntry', backref='task', lazy='dynamic') # Added relationship
    is_legal_compliance = db.Column(db.Boolean, default=False, nullable=False, index=True)

    # --- REMOVED explicit ForeignKeyConstraint here as it's now inline ---
    # __table_args__ = (
    #     db.ForeignKeyConstraint(['equipment_id'], ['equipment.id'], name='fk_maintenance_task_equipment_id'),
    # )
    # --- END REMOVAL ---

    def __repr__(self):
        task_type = "[Legal]" if self.is_legal_compliance else "[Maint]"
        return f'<MaintenanceTask {task_type} {self.description}>' # Added type indicator

    def to_dict(self, include_equipment=False):
        """Returns a dictionary representation for API usage."""
        data = {
            'id': self.id,
            'equipment_id': self.equipment_id,
            'description': self.description,
            'interval_type': self.interval_type,
            'interval_value': self.interval_value,
            'oem_required': self.oem_required,
            'kit_required': self.kit_required,
            'last_performed': format_datetime_iso(self.last_performed),
            'last_performed_usage_value': self.last_performed_usage_value,
            'is_legal_compliance': self.is_legal_compliance,
        }
        if include_equipment and self.equipment_ref:
            data['equipment'] = self.equipment_ref.to_dict()
        return data

class JobCard(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    job_number = db.Column(db.String(20), nullable=False, unique=True)
    # --- IMPORTANT: Changed equipment_id foreign key ---
    equipment_id = db.Column(db.Integer, db.ForeignKey('equipment.id', name='fk_job_card_equipment_id'), nullable=False)
    # --- END CHANGE ---
    description = db.Column(db.Text, nullable=False)
    technician = db.Column(db.String(100))
    status = db.Column(db.String(20), default='To Do')
    oem_required = db.Column(db.Boolean, default=False)
    kit_required = db.Column(db.Boolean, default=False)
    due_date = db.Column(db.DateTime, nullable=True)  # Can be Date or DateTime depending on need
    start_datetime = db.Column(db.DateTime)
    end_datetime = db.Column(db.DateTime)
    comments = db.Column(db.Text, nullable=True)
    parts_used = db.relationship('JobCardPart', back_populates='job_card', lazy='dynamic', cascade='all, delete-orphan')

    @property
    def is_legal_compliance(self):
        """Determine if this is a legal compliance job card based on job number prefix."""
        if self.job_number and self.job_number.startswith('LC-'):
            return True
        return False

    @property
    def job_type_display(self):
        """Return a display string for the job type."""
        return "Legal Compliance" if self.is_legal_compliance else "Maintenance"

    def __repr__(self):
        return f'<JobCard {self.job_number}>'

    def to_dict(self, include_equipment=False, include_parts=False):
        """Returns a dictionary representation for API usage."""
        data = {
            'id': self.id,
            'job_number': self.job_number,
            'equipment_id': self.equipment_id,
            'description': self.description,
            'technician': self.technician,
            'status': self.status,
            'oem_required': self.oem_required,
            'kit_required': self.kit_required,
            'due_date': format_datetime_iso(self.due_date),
            'start_datetime': format_datetime_iso(self.start_datetime),
            'end_datetime': format_datetime_iso(self.end_datetime),
            'comments': self.comments,
            'is_legal_compliance': self.is_legal_compliance,
            'job_type_display': self.job_type_display,
        }
        if include_equipment and self.equipment_ref:
            data['equipment'] = self.equipment_ref.to_dict()

        if include_parts:
             data['parts_used'] = [
                 {**p.part.to_dict(), 'quantity_used': p.quantity}
                 for p in self.parts_used
             ]
        return data

class Checklist(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # --- IMPORTANT: Changed equipment_id foreign key ---
    equipment_id = db.Column(db.Integer, db.ForeignKey('equipment.id', name='fk_checklist_equipment_id'), nullable=False)
    # --- END CHANGE ---
    status = db.Column(db.String(20), nullable=False)
    issues = db.Column(db.Text)
    check_date = db.Column(db.DateTime, nullable=False) # Storing as DateTime
    # *** START NEW FIELD ***
    operator = db.Column(db.String(100), nullable=False) # Mandatory field for operator name
    # *** END NEW FIELD ***

    # --- REMOVED explicit ForeignKeyConstraint here as it's now inline ---
    # __table_args__ = (
    #     db.ForeignKeyConstraint(['equipment_id'], ['equipment.id'], name='fk_checklist_equipment_id'),
    # )
    # --- END REMOVAL ---

    def __repr__(self):
        # Updated repr to include operator
        return f'<Checklist {self.id} for Eq ID:{self.equipment_id} by {self.operator}>'

    def to_dict(self, include_equipment=False):
        """Returns a dictionary representation for API usage."""
        data = {
            'id': self.id,
            'equipment_id': self.equipment_id,
            'status': self.status,
            'issues': self.issues,
            'check_date': format_datetime_iso(self.check_date),
            'operator': self.operator, # Added operator
        }
        if include_equipment and self.equipment_ref:
             data['equipment'] = self.equipment_ref.to_dict()
        return data

class Supplier(db.Model):
    __tablename__ = 'supplier'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, unique=True)
    contact_info = db.Column(db.String(100), nullable=True)
    parts = db.relationship('Part', backref='supplier_ref', lazy='dynamic') # Changed backref name slightly for clarity

    def __repr__(self):
        return f'<Supplier {self.name}>'

    def to_dict(self):
        """Returns a dictionary representation for API usage."""
        return {
            'id': self.id,
            'name': self.name,
            'contact_info': self.contact_info,
        }

class Part(db.Model):
    __tablename__ = 'part'
    id = db.Column(db.Integer, primary_key=True)
    part_number = db.Column(db.String(50), nullable=True, index=True)
    name = db.Column(db.String(100), nullable=False, index=True)
    is_get = db.Column(db.Boolean, default=False)
    # --- IMPORTANT: Changed supplier_id foreign key ---
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id', name='fk_part_supplier_id'), nullable=True)
    # --- END CHANGE ---
    store = db.Column(db.String(50), nullable=False, index=True)
    current_stock = db.Column(db.Integer, nullable=False, default=0)
    min_stock = db.Column(db.Integer, nullable=False, default=0)
    stock_transactions = db.relationship('StockTransaction', backref='part', lazy='dynamic', cascade='all, delete-orphan')
    job_cards_association = db.relationship('JobCardPart', back_populates='part', lazy='dynamic', cascade='all, delete-orphan')

    # --- REMOVED explicit ForeignKeyConstraint here as it's now inline ---
    # __table_args__ = (
    #     db.ForeignKeyConstraint(['supplier_id'], ['supplier.id'], name='fk_part_supplier_id'),
    # )
    # --- END REMOVAL ---

    def __repr__(self):
        return f'<Part {self.name} (Stock: {self.current_stock})>'

    def to_dict(self, include_supplier=False):
        """Returns a dictionary representation for API usage."""
        data = {
            'id': self.id,
            'part_number': self.part_number,
            'name': self.name,
            'is_get': self.is_get,
            'supplier_id': self.supplier_id,
            'store': self.store,
            'current_stock': self.current_stock,
            'min_stock': self.min_stock,
        }
        if include_supplier and self.supplier_ref:
            data['supplier'] = self.supplier_ref.to_dict()
        return data

class StockTransaction(db.Model):
    __tablename__ = 'stock_transaction'
    id = db.Column(db.Integer, primary_key=True)
    # --- IMPORTANT: Changed part_id foreign key ---
    part_id = db.Column(db.Integer, db.ForeignKey('part.id', name='fk_stock_transaction_part_id'), nullable=False)
    # --- END CHANGE ---
    quantity = db.Column(db.Integer, nullable=False)
    transaction_date = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    description = db.Column(db.String(255), nullable=True)

    # --- REMOVED explicit ForeignKeyConstraint here as it's now inline ---
    # __table_args__ = (
    #     db.ForeignKeyConstraint(['part_id'], ['part.id'], name='fk_stock_transaction_part_id'),
    # )
    # --- END REMOVAL ---

    def __repr__(self):
        return f'<StockTransaction {self.id} for Part ID:{self.part_id} Qty:{self.quantity} on {self.transaction_date}>'

    def to_dict(self, include_part=False):
        """Returns a dictionary representation for API usage."""
        data = {
            'id': self.id,
            'part_id': self.part_id,
            'quantity': self.quantity,
            'transaction_date': format_datetime_iso(self.transaction_date),
            'description': self.description,
        }
        if include_part and self.part:
            data['part'] = self.part.to_dict()
        return data

class JobCardPart(db.Model):
    __tablename__ = 'job_card_part'
    id = db.Column(db.Integer, primary_key=True)
    # --- IMPORTANT: Changed foreign keys ---
    job_card_id = db.Column(db.Integer, db.ForeignKey('job_card.id', name='fk_job_card_part_job_card_id'), nullable=False)
    part_id = db.Column(db.Integer, db.ForeignKey('part.id', name='fk_job_card_part_part_id'), nullable=False)
    # --- END CHANGE ---
    quantity = db.Column(db.Integer, nullable=False)

    job_card = db.relationship('JobCard', back_populates='parts_used')
    part = db.relationship('Part', back_populates='job_cards_association')

    __table_args__ = (
        # --- REMOVED explicit ForeignKeyConstraints here as they are now inline ---
        db.UniqueConstraint('job_card_id', 'part_id', name='uq_job_card_part'),
    )

    def __repr__(self):
        return f'<JobCardPart JobCard:{self.job_card_id} Part:{self.part_id} Qty:{self.quantity}>'

class UsageLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # --- IMPORTANT: Changed equipment_id foreign key ---
    equipment_id = db.Column(db.Integer, db.ForeignKey('equipment.id', name='fk_usage_log_equipment_id'), nullable=False)
    # --- END CHANGE ---
    usage_value = db.Column(db.Float, nullable=False)
    log_date = db.Column(db.DateTime, nullable=False, index=True) # Added index

    # --- REMOVED explicit ForeignKeyConstraint here as it's now inline ---
    # __table_args__ = (
    #     db.ForeignKeyConstraint(['equipment_id'], ['equipment.id'], name='fk_usage_log_equipment_id'),
    # )
    # --- END REMOVAL ---

    def __repr__(self):
        return f'<UsageLog {self.id} for Equipment ID:{self.equipment_id}>'

    def to_dict(self, include_equipment=False):
        """Returns a dictionary representation for API usage."""
        data = {
            'id': self.id,
            'equipment_id': self.equipment_id,
            'usage_value': self.usage_value,
            'log_date': format_datetime_iso(self.log_date),
        }
        if include_equipment and self.equipment_ref:
             data['equipment'] = self.equipment_ref.to_dict()
        return data

class MaintenancePlanEntry(db.Model):
    __tablename__ = 'maintenance_plan_entry'
    id = db.Column(db.Integer, primary_key=True)
    # --- IMPORTANT: Changed foreign keys ---
    equipment_id = db.Column(db.Integer, db.ForeignKey('equipment.id', name='fk_plan_entry_equipment_id'), nullable=False)
    task_id = db.Column(db.Integer, db.ForeignKey('maintenance_task.id', name='fk_plan_entry_task_id', use_alter=True, ondelete='SET NULL'), nullable=True)
    # --- END CHANGE ---
    task_description = db.Column(db.String(255), nullable=False)
    planned_date = db.Column(db.Date, nullable=False)
    interval_type = db.Column(db.String(50))
    is_estimate = db.Column(db.Boolean, default=False)
    generated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    plan_year = db.Column(db.Integer, nullable=False)
    plan_month = db.Column(db.Integer, nullable=False)

    original_task = db.relationship('MaintenanceTask') # Moved relationship here
    # equipment = db.relationship('Equipment') # Backref handles this
    # task = db.relationship('MaintenanceTask') # Backref handles this

    __table_args__ = (
        # --- REMOVED explicit ForeignKeyConstraints here as they are now inline ---
        Index('ix_maintenance_plan_entry_year_month', 'plan_year', 'plan_month'),
    )

    def __repr__(self):
        return f'<MaintenancePlanEntry Eq:{self.equipment_id} Task:"{self.task_description}" Date:{self.planned_date}>'

    def to_dict(self, include_equipment=False, include_task_details=False):
        """Returns a dictionary representation for API usage."""
        data = {
            'id': self.id,
            'equipment_id': self.equipment_id,
            'task_id': self.task_id,
            'task_description': self.task_description,
            'planned_date': format_datetime_iso(self.planned_date),
            'interval_type': self.interval_type,
            'is_estimate': self.is_estimate,
            'generated_at': format_datetime_iso(self.generated_at),
            'plan_year': self.plan_year,
            'plan_month': self.plan_month,
        }
        if include_equipment and self.equipment:
             data['equipment'] = self.equipment.to_dict()
        # Correctly use the relationship name 'original_task'
        if include_task_details and self.original_task:
             data['task_details'] = {
                 'id': self.original_task.id,
                 'description': self.original_task.description,
                 'is_legal_compliance': self.original_task.is_legal_compliance, # Add flag here
             }
        return data

# Note: Removed the duplicate MaintenancePlanEntry class definition