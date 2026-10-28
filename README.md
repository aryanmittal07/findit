# FindIt

Phase 1 foundation for the college lost-and-found portal.

## Local setup

1. Create a virtual environment and install `requirements.txt`.
2. Copy `.env.example` into your preferred local environment configuration.
3. Set `SECRET_KEY` to a unique random value; never commit it. For example: `export SECRET_KEY='your-random-value'`.
4. Run `python app.py` from this folder.

The current routes include public browsing/search, email sign-up/sign-in, item reporting, private item claims, and separate admin review tools.

## First administrator

From the project folder, run `flask --app app create-admin` and follow the prompts. Admin accounts cannot be created through public sign-up.

## Demo listings

For a populated local preview, run `flask --app app seed-demo`. It adds four published campus-style listings and can be run repeatedly without creating duplicates.

## Verification

Run `python -m pytest -q` from an environment with the requirements installed.

## Deploy to Render

This project includes `render.yaml` for a production deployment. Create a new Render Blueprint from the GitHub repository and choose the included configuration. It creates a paid web service with a 1 GB persistent disk at `/var/data`; both the SQLite database and uploaded photos are stored there, so they survive service restarts and deployments.

Render creates a secure `SECRET_KEY` automatically. Do not use the local `instance/` folder or local administrator accounts as production data; create the first live administrator after the service is deployed with Render's shell.
