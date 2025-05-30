# app/auth/forms.py
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, BooleanField, SubmitField, SelectField
from wtforms.validators import DataRequired, EqualTo, ValidationError, Length # Removed Email validator
from app.models import User

class LoginForm(FlaskForm):
    username_or_email = StringField('Username or Email', validators=[DataRequired(), Length(max=120)]) # User can still login with email
    password = PasswordField('Password', validators=[DataRequired()])
    remember_me = BooleanField('Remember Me')
    submit = SubmitField('Sign In')

class RegistrationForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired(), Length(min=3, max=64)])
    # Email field is kept, but without Email() validator or uniqueness check
    email = StringField('Email (Optional)', validators=[Length(max=120)])
    password = PasswordField('Password', validators=[DataRequired(), Length(min=8)])
    password2 = PasswordField(
        'Repeat Password', validators=[DataRequired(), EqualTo('password', message='Passwords must match.')])
    role = SelectField('Role', choices=[('user', 'User'), ('admin', 'Administrator')],
                       validators=[DataRequired()])
    submit = SubmitField('Register User')

    def validate_username(self, username):
        user = User.query.filter_by(username=username.data).first()
        if user is not None:
            raise ValidationError('This username is already taken. Please choose a different one.')

    # Removed validate_email method

class UserEditForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired(), Length(min=3, max=64)])
    # Email field is kept, but without Email() validator or uniqueness check
    email = StringField('Email (Optional)', validators=[Length(max=120)])
    role = SelectField('Role', choices=[('user', 'User'), ('admin', 'Administrator')],
                       validators=[DataRequired()])
    is_active = BooleanField('Active User')
    submit = SubmitField('Update User')

    def __init__(self, original_username, *args, **kwargs): # Removed original_email
        super(UserEditForm, self).__init__(*args, **kwargs)
        self.original_username = original_username
        # self.original_email = original_email # Removed

    def validate_username(self, username):
        if username.data != self.original_username:
            user = User.query.filter_by(username=username.data).first()
            if user:
                raise ValidationError('This username is already taken. Please choose a different one.')

    # Removed validate_email method