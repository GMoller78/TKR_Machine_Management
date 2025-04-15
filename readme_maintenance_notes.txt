# TKR Machine Management - Deployment and Maintenance Instructions

This file outlines the steps to deploy local code changes to the production server and manage database migrations for the TKR Machine Management application.

## Server Environment

*   **Provider:** Linode
*   **VM IP Address:** 172.236.3.134
*   **Operating System:** Ubuntu (assumed)
*   **SSH User:** appuser
*   **Application Directory:** /home/appuser/TKR_Machine_Management
*   **GitHub Repository:** https://github.com/GMoller78/TKR_Production_Tracking.git
*   **Systemd Service Name:** tkr-machine-app

## Application Stack

*   **Framework:** Flask
*   **Database:** SQLite (located at `instance/tkr.db`)
*   **WSGI Server:** Gunicorn
*   **Reverse Proxy:** Nginx
*   **Version Control:** Git / GitHub
*   **Python Version:** 3.10 (via virtual environment)
*   **Virtual Environment Path:** /home/appuser/TKR_Machine_Management/venv (assumed)

## Deployment Steps: Pushing Local Changes to Server

These steps assume your local repository is configured with a remote named `origin` pointing to your GitHub repository.

1.  **On your Local Machine:**
    *   Make your code changes.
    *   Stage the changes:
        ```bash
        git add .
        # Or git add <specific_file> ...
        ```
    *   Commit the changes:
        ```bash
        git commit -m "Your descriptive commit message about the changes"
        ```
    *   Push the changes to GitHub (replace `<branch_name>` with your working branch, e.g., `main` or `master`):
        ```bash
        git push origin <branch_name>
        ```

2.  **On the Linode Server (172.236.3.134):**
    *   Connect to the server via SSH:
        ```bash
        ssh appuser@172.236.3.134
        ```
    *   Navigate to the application directory:
        ```bash
        cd /home/appuser/TKR_Machine_Management
        ```
    *   Pull the latest changes from GitHub (replace `<branch_name>` with the same branch you pushed to):
        ```bash
        git pull origin <branch_name>
        ```
        *(Note: If you haven't set up SSH keys or a credential helper, Git might prompt for authentication.)*

    *   Activate the Python virtual environment:
        ```bash
        source venv/bin/activate
        ```
    *   **(If applicable) Install or update dependencies:** If you modified `requirements.txt`, run:
        ```bash
        pip install -r requirements.txt
        ```
    *   **(If applicable) Apply Database Migrations:** See the section below.

    *   Restart the application service to apply changes:
        ```bash
        sudo systemctl restart tkr-machine-app
        ```
        *(You might be prompted for the `appuser`'s password if `sudo` requires it).*

    *   Deactivate the virtual environment (optional):
        ```bash
        deactivate
        ```
    *   Exit the SSH session:
        ```bash
        exit
        ```

## Database Migrations (Flask-Migrate)

Database migrations should be applied *after* pulling the new code containing the migration scripts and *before* restarting the application service.

**Note:** Typically, new migration scripts (`flask db migrate ...`) are generated on your *local* development machine when you change your SQLAlchemy models, committed, and pushed along with your code. The steps below assume the migration script already exists in the code pulled from GitHub.

1.  **On the Linode Server (after connecting via SSH and navigating to the app directory):**
    *   Ensure you are in the application directory:
        ```bash
        cd /home/appuser/TKR_Machine_Management
        ```
    *   Activate the virtual environment:
        ```bash
        source venv/bin/activate
        ```
    *   Apply any pending database migrations:
        ```bash
        flask db upgrade
        ```
    *   After migrations are complete, you can proceed to restart the application service (`sudo systemctl restart tkr-machine-app`) as shown in the deployment steps.
    *   Deactivate the virtual environment (optional):
        ```bash
        deactivate
        ```

## File Structure Overview
TKR_Machine_Management/
├── app/ # Main application package
├── migrations/ # Alembic/Flask-Migrate migration files
├── instance/ # Instance folder (contains tkr.db)
│ └── tkr.db # SQLite Database File
├── venv/ # Python virtual environment (usually not committed)
├── config.py # Configuration settings
├── requirements.txt # Python dependencies
└── run.py # Application entry point (for development/Flask commands)
