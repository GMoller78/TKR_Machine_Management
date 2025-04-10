# run_data_migration.py
import os
from app import create_app, db # Assuming your Flask app factory and db instance
from app.models import JobCard, StockTransaction # Your models
from datetime import datetime, timezone
import pytz # For robust timezone handling

# --- Configuration ---
# !!! IMPORTANT: Set the timezone your *original* naive datetimes represented !!!
# Examples: 'UTC', 'America/New_York', 'Europe/London'
ORIGINAL_TZ_NAME = 'UTC' # <--- CHANGE THIS TO YOUR ACTUAL ORIGINAL TIMEZONE

# --- Script Logic ---
app = create_app(os.getenv('FLASK_CONFIG') or 'default') # Create your Flask app instance

def convert_naive_string_to_aware_utc(naive_dt_str, original_timezone):
    """Parses a naive datetime string, localizes it, converts to UTC."""
    if not naive_dt_str:
        return None
    try:
        # Step 1: Parse the string into a naive datetime object
        # Adjust format if your strings are different. This handles common formats.
        try:
             naive_dt = datetime.strptime(naive_dt_str, '%Y-%m-%d %H:%M:%S.%f')
        except ValueError:
             try:
                 naive_dt = datetime.strptime(naive_dt_str, '%Y-%m-%d %H:%M:%S')
             except ValueError:
                 print(f"  WARN: Could not parse date string: {naive_dt_str}. Skipping.")
                 return None # Or raise error

        # Step 2: Make the naive datetime aware in its *original* timezone
        aware_original = original_timezone.localize(naive_dt)

        # Step 3: Convert to UTC
        aware_utc = aware_original.astimezone(pytz.utc)
        return aware_utc

    except Exception as e:
        print(f"  ERROR converting '{naive_dt_str}': {e}")
        return None

with app.app_context():
    print(f"Starting data migration. Assuming original timezone: {ORIGINAL_TZ_NAME}")
    original_tz = pytz.timezone(ORIGINAL_TZ_NAME)
    commit_count = 0

    try:
        # --- Update JobCard.due_date ---
        print("\nProcessing Job Cards...")
        job_cards_to_update = db.session.query(JobCard).filter(JobCard.due_date != None).all()
        print(f"Found {len(job_cards_to_update)} Job Cards with due dates to potentially update.")
        for jc in job_cards_to_update:
            # Read current value (SQLAlchemy might return naive str or datetime)
            current_value = jc.due_date
            if isinstance(current_value, str): # Check if it's still a string
                 naive_date_string = current_value
                 print(f"  Updating JC {jc.job_number} due_date from string '{naive_date_string}'...")
                 aware_utc_dt = convert_naive_string_to_aware_utc(naive_date_string, original_tz)
                 if aware_utc_dt:
                     jc.due_date = aware_utc_dt # Assign the aware object
                     commit_count += 1
            elif isinstance(current_value, datetime) and current_value.tzinfo is None:
                 # If SQLAlchemy loaded as naive datetime somehow
                 print(f"  Updating JC {jc.job_number} due_date from naive datetime {current_value}...")
                 aware_original = original_tz.localize(current_value)
                 aware_utc = aware_original.astimezone(pytz.utc)
                 jc.due_date = aware_utc
                 commit_count +=1
            # Else: Assume it's already aware (less likely on first run)

        # --- Update StockTransaction.transaction_date ---
        print("\nProcessing Stock Transactions...")
        transactions_to_update = db.session.query(StockTransaction).all()
        print(f"Found {len(transactions_to_update)} Transactions to potentially update.")
        for tx in transactions_to_update:
            current_value = tx.transaction_date
            if isinstance(current_value, str):
                 naive_date_string = current_value
                 print(f"  Updating Tx {tx.id} transaction_date from string '{naive_date_string}'...")
                 aware_utc_dt = convert_naive_string_to_aware_utc(naive_date_string, original_tz)
                 if aware_utc_dt:
                     tx.transaction_date = aware_utc_dt
                     commit_count += 1
            elif isinstance(current_value, datetime) and current_value.tzinfo is None:
                 print(f"  Updating Tx {tx.id} transaction_date from naive datetime {current_value}...")
                 aware_original = original_tz.localize(current_value)
                 aware_utc = aware_original.astimezone(pytz.utc)
                 tx.transaction_date = aware_utc
                 commit_count += 1

        if commit_count > 0:
            print(f"\nCommitting {commit_count} changes...")
            db.session.commit()
            print("Commit successful.")
        else:
            print("\nNo changes needed or made.")

    except Exception as e:
        db.session.rollback()
        print(f"\nERROR during data migration: {e}")
        print("Rolled back any changes.")
    finally:
        print("Data migration script finished.")