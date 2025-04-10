# tkr_system/app/models.py
from app import db
from datetime import datetime, timezone

class Equipment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), nullable=False, unique=True)
    name = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(50), nullable=False)
    checklist_required = db.Column(db.Boolean, default=False)
    maintenance_tasks = db.relationship('MaintenanceTask', backref='equipment_ref', lazy='dynamic')
    job_cards = db.relationship('JobCard', backref='equipment_ref', lazy='dynamic')
    checklists = db.relationship('Checklist', backref='equipment_ref', lazy='dynamic')
    usage_logs = db.relationship('UsageLog', backref='equipment_ref', lazy='dynamic')

    def __repr__(self):
        return f'<Equipment {self.name}>'

class MaintenanceTask(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    equipment_id = db.Column(db.Integer, nullable=False)
    description = db.Column(db.String(255), nullable=False)
    interval_type = db.Column(db.String(50), nullable=False)
    interval_value = db.Column(db.Integer, nullable=False)
    oem_required = db.Column(db.Boolean, default=False)
    kit_required = db.Column(db.Boolean, default=False)
    last_performed = db.Column(db.DateTime, nullable=True)
    last_performed_usage_value = db.Column(db.Float, nullable=True)
    __table_args__ = (
        db.ForeignKeyConstraint(['equipment_id'], ['equipment.id'], name='fk_maintenance_task_equipment_id'),
    )

    def __repr__(self):
        return f'<MaintenanceTask {self.description}>'

class JobCard(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    job_number = db.Column(db.String(20), nullable=False, unique=True)
    equipment_id = db.Column(db.Integer, nullable=False)
    description = db.Column(db.Text, nullable=False)
    technician = db.Column(db.String(100))
    status = db.Column(db.String(20), default='To Do')
    oem_required = db.Column(db.Boolean, default=False)
    kit_required = db.Column(db.Boolean, default=False)
    due_date = db.Column(db.DateTime, nullable=True)  # New field for due date
    start_datetime = db.Column(db.DateTime)
    end_datetime = db.Column(db.DateTime)
    comments = db.Column(db.Text, nullable=True)  # New field
    parts = db.relationship('JobCardPart', backref='job_card', lazy='dynamic')
    __table_args__ = (
        db.ForeignKeyConstraint(['equipment_id'], ['equipment.id'], name='fk_job_card_equipment_id'),
    )

    def __repr__(self):
        return f'<JobCard {self.job_number}>'

class Checklist(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    equipment_id = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), nullable=False)
    issues = db.Column(db.Text)
    check_date = db.Column(db.DateTime, nullable=False)
    __table_args__ = (
        db.ForeignKeyConstraint(['equipment_id'], ['equipment.id'], name='fk_checklist_equipment_id'),
    )

    def __repr__(self):
        return f'<Checklist {self.id} for Equipment ID:{self.equipment_id}>'

class Supplier(db.Model):
    __tablename__ = 'supplier'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, unique=True)
    contact_info = db.Column(db.String(100), nullable=True)
    parts = db.relationship('Part', backref='supplier', lazy=True)

    def __repr__(self):
        return f'<Supplier {self.name}>'

class Part(db.Model):
    __tablename__ = 'part'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, unique=True)
    is_get = db.Column(db.Boolean, default=False)
    supplier_id = db.Column(db.Integer, nullable=True)  # Made nullable for flexibility
    store = db.Column(db.String(20), nullable=False)
    current_stock = db.Column(db.Integer, nullable=False, default=0)
    min_stock = db.Column(db.Integer, nullable=False, default=0)
    stock_transactions = db.relationship('StockTransaction', backref='part', lazy=True, cascade='all, delete-orphan')
    job_cards_association = db.relationship('JobCardPart', back_populates='part', cascade='all, delete-orphan')
    __table_args__ = (
        db.ForeignKeyConstraint(['supplier_id'], ['supplier.id'], name='fk_part_supplier_id'),
    )

    def __repr__(self):
        return f'<Part {self.name} (Stock: {self.current_stock})>'

class StockTransaction(db.Model):
    __tablename__ = 'stock_transaction'
    id = db.Column(db.Integer, primary_key=True)
    part_id = db.Column(db.Integer, nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    transaction_date = db.Column(db.DateTime, nullable=False,default=datetime.utcnow) 
    description = db.Column(db.String(100), nullable=True)
    __table_args__ = (
        db.ForeignKeyConstraint(['part_id'], ['part.id'], name='fk_stock_transaction_part_id'),
    )

    def __repr__(self):
        return f'<StockTransaction {self.id} for Part ID:{self.part_id} Qty:{self.quantity} on {self.transaction_date.date()}>'

class JobCardPart(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    job_card_id = db.Column(db.Integer, nullable=False)
    part_id = db.Column(db.Integer, nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    part = db.relationship('Part', back_populates='job_cards_association')
    __table_args__ = (
        db.ForeignKeyConstraint(['job_card_id'], ['job_card.id'], name='fk_job_card_part_job_card_id'),
        db.ForeignKeyConstraint(['part_id'], ['part.id'], name='fk_job_card_part_part_id'),
    )

    def __repr__(self):
        return f'<JobCardPart JobCard:{self.job_card_id} Part:{self.part_id} Qty:{self.quantity}>'

class UsageLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    equipment_id = db.Column(db.Integer, nullable=False)
    usage_value = db.Column(db.Float, nullable=False)
    log_date = db.Column(db.DateTime, nullable=False)
    __table_args__ = (
        db.ForeignKeyConstraint(['equipment_id'], ['equipment.id'], name='fk_usage_log_equipment_id'),
    )

    def __repr__(self):
        return f'<UsageLog {self.id} for Equipment ID:{self.equipment_id}>'