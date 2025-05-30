# manage.py
import os
import click
from flask.cli import FlaskGroup

# Ensure app can be imported. If your structure is different, adjust path.
from app import create_app, db
from app.models import User # Import User model

def create_flask_app(info):
    return create_app()

@click.group(cls=FlaskGroup, create_app=create_flask_app)
def cli():
    """Management script for the TKR application."""

@cli.command("create-admin")
@click.option('--username', prompt=True, help='The username for the admin.')
@click.option('--email', prompt=True, help='The email for the admin.')
@click.option('--password', prompt=True, hide_input=True, confirmation_prompt=True, help='The password for the admin.')
def create_admin_user(username, email, password):
    """Creates an initial administrator user."""
    if User.query.filter((User.username == username) | (User.email == email.lower())).first():
        click.echo(click.style(f"Error: User with username '{username}' or email '{email}' already exists.", fg='red'))
        return
    try:
        admin = User(username=username, email=email.lower(), role='admin', is_active=True)
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()
        click.echo(click.style(f"Admin user '{username}' created successfully with role 'admin'.", fg='green'))
    except Exception as e:
        db.session.rollback()
        click.echo(click.style(f"Error creating admin user: {e}", fg='red'))

# You might have other commands here, e.g., for db migrations if you use Flask-Migrate
# Example for Flask-Migrate (if you set it up):
# from flask_migrate import Migrate
# migrate = Migrate()
# def create_flask_app_for_migrate(info):
#     app_instance = create_app()
#     migrate.init_app(app_instance, db)
#     return app_instance
# ... then adjust FlaskGroup create_app for migrate context if needed.

if __name__ == '__main__':
    cli()