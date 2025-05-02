# app/forms.py
from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, SubmitField, SelectField, FloatField, DateTimeLocalField
from wtforms.validators import DataRequired, Optional, Length, NumberRange

# Define choices for Checklist status
CHECKLIST_STATUS_CHOICES = [('Go', 'Go'), ('Go But', 'Go But'), ('No Go', 'No Go')]

class ChecklistEditForm(FlaskForm):
    status = SelectField('Status', choices=CHECKLIST_STATUS_CHOICES, validators=[DataRequired()])
    issues = TextAreaField('Issues/Notes', validators=[Optional(), Length(max=500)])
    operator = StringField('Operator', validators=[Optional(), Length(max=100)])
    # Use DateTimeLocalField for easier input, but requires specific format handling
    check_date = DateTimeLocalField('Check Date/Time (UTC)', format='%Y-%m-%dT%H:%M', validators=[DataRequired()])
    submit = SubmitField('Update Checklist Log')

class UsageLogEditForm(FlaskForm):
    usage_value = FloatField('Usage Value (Hours/KM)', validators=[DataRequired(), NumberRange(min=0)])
    log_date = DateTimeLocalField('Log Date/Time (UTC)', format='%Y-%m-%dT%H:%M', validators=[DataRequired()])
    submit = SubmitField('Update Usage Log')