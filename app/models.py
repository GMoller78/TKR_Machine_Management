# tkr_system/app/models.py
from app import db
from datetime import datetime, timezone, date # Added date
from sqlalchemy import Index # Import Index

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

    # --- REMOVED explicit ForeignKeyConstraint here as it's now inline ---
    # __table_args__ = (
    #     db.ForeignKeyConstraint(['equipment_id'], ['equipment.id'], name='fk_maintenance_task_equipment_id'),
    # )
    # --- END REMOVAL ---

    def __repr__(self):
        return f'<MaintenanceTask {self.description}>'

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

    # --- REMOVED explicit ForeignKeyConstraint here as it's now inline ---
    # __table_args__ = (
    #     db.ForeignKeyConstraint(['equipment_id'], ['equipment.id'], name='fk_job_card_equipment_id'),
    # )
    # --- END REMOVAL ---

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

    # --- REMOVED explicit ForeignKeyConstraint here as it's now inline ---
    # __table_args__ = (
    #     db.ForeignKeyConstraint(['equipment_id'], ['equipment.id'], name='fk_checklist_equipment_id'),
    # )
    # --- END REMOVAL ---

    def __repr__(self):
        return f'<Checklist {self.id} for Equipment ID:{self.equipment_id}>'

    def to_dict(self, include_equipment=False):
        """Returns a dictionary representation for API usage."""
        data = {
            'id': self.id,
            'equipment_id': self.equipment_id,
            'status': self.status,
            'issues': self.issues,
            'check_date': format_datetime_iso(self.check_date),
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
        if include_task_details and self.task:
             data['task_details'] = {
                 'id': self.task.id,
                 'description': self.task.description,
             }
        return data

    __tablename__ = 'maintenance_plan_entry'
    id = db.Column(db.Integer, primary_key=True)
    equipment_id = db.Column(db.Integer, nullable=False)
    task_description = db.Column(db.String(255), nullable=False)
    planned_date = db.Column(db.Date, nullable=False) # Store only the date
    interval_type = db.Column(db.String(50)) # Store type at generation time
    is_estimate = db.Column(db.Boolean, default=False) # Flag if it was usage-based
    generated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc)) # Changed default
    plan_year = db.Column(db.Integer, nullable=False) # Year of the plan
    plan_month = db.Column(db.Integer, nullable=False) # Month of the plan (1-12)
    task_id = db.Column(db.Integer, nullable=True) # Link back to the original MaintenanceTask.id

    # Removed explicit equipment/task relationships here as backrefs are defined in Equipment/MaintenanceTask
    # equipment = db.relationship('Equipment') # This is handled by backref='plan_entries' in Equipment
    # task = db.relationship('MaintenanceTask') # This is handled by backref='plan_entries' in MaintenanceTask

    __table_args__ = (
        db.ForeignKeyConstraint(['equipment_id'], ['equipment.id'], name='fk_plan_entry_equipment_id'),
        # Ensure task_id foreign key constraint matches definition in MaintenanceTask
        db.ForeignKeyConstraint(['task_id'], ['maintenance_task.id'], name='fk_plan_entry_task_id', use_alter=True, ondelete='SET NULL'),
        Index('ix_maintenance_plan_entry_year_month', 'plan_year', 'plan_month'), # Index
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
            'planned_date': format_datetime_iso(self.planned_date), # Will format date as YYYY-MM-DD
            'interval_type': self.interval_type,
            'is_estimate': self.is_estimate,
            'generated_at': format_datetime_iso(self.generated_at),
            'plan_year': self.plan_year,
            'plan_month': self.plan_month,
        }
        # Use the backrefs to access related objects if needed
        if include_equipment and self.equipment:
             data['equipment'] = self.equipment.to_dict()
        if include_task_details and self.task:
             # Avoid infinite recursion if task.to_dict includes plan entries
             data['task_details'] = {
                 'id': self.task.id,
                 'description': self.task.description,
                 # Add other relevant task fields if needed
             }
             # Or call task.to_dict() if safe:
             # data['task_details'] = self.task.to_dict()
        return data