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
