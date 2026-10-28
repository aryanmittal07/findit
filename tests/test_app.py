from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

from app import create_app, get_db, match_score


@pytest.fixture()
def app(tmp_path):
    return create_app({
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "DATABASE": Path(tmp_path) / "test.sqlite3",
    })


@pytest.fixture()
def client(app):
    return app.test_client()


def csrf_token(client, path="/admin/login"):
    client.get(path)
    with client.session_transaction() as session:
        return session["csrf_token"]


def create_user(app, email="student@example.test", password="correct-password", role="user"):
    with app.app_context():
        get_db().execute("INSERT INTO users (email, password_hash, role) VALUES (?, ?, ?)", (email, generate_password_hash(password), role))
        get_db().commit()


def login(client, email="student@example.test", password="correct-password"):
    return client.post("/login", data={"email": email, "password": password, "csrf_token": csrf_token(client, "/login")})


def test_home_and_health_are_available(client):
    assert client.get("/").status_code == 200
    assert client.get("/health").get_json() == {"status": "ok"}


def test_database_is_initialized(app):
    import sqlite3
    connection = sqlite3.connect(app.config["DATABASE"])
    tables = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    connection.close()
    assert ("app_settings",) in tables
    assert ("users",) in tables


def test_admin_dashboard_requires_login(client):
    response = client.get("/admin")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin/login")


def test_admin_login_logout_and_csrf_protection(client, app):
    create_user(app, "admin@example.test", role="admin")
    missing_token = client.post("/admin/login", data={"password": "correct-password"})
    assert missing_token.status_code == 400

    response = client.post("/admin/login", data={
        "email": "admin@example.test", "password": "correct-password", "csrf_token": csrf_token(client)
    })
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin")
    assert client.get("/admin").status_code == 200

    response = client.post("/admin/logout", data={"csrf_token": csrf_token(client, "/admin")})
    assert response.status_code == 302
    assert client.get("/admin").status_code == 302


def test_invalid_password_does_not_sign_in(client, app):
    create_user(app, "admin@example.test", role="admin")
    response = client.post("/admin/login", data={
        "email": "admin@example.test", "password": "wrong-password", "csrf_token": csrf_token(client)
    })
    assert response.status_code == 200
    assert client.get("/admin").status_code == 302


def test_report_submission_is_pending_and_not_public(client, app):
    create_user(app)
    login(client)
    response = client.post("/report", data={
        "csrf_token": csrf_token(client, "/report"), "report_type": "lost", "title": "Blue water bottle",
        "category": "Accessories", "description": "Steel bottle with stickers", "location": "Library",
        "event_date": "2026-10-20", "contact_name": "Ari", "contact_detail": "ari@example.test",
    })
    assert response.status_code == 302
    assert client.get("/items").status_code == 200
    assert b"Blue water bottle" not in client.get("/items").data

    import sqlite3
    db = sqlite3.connect(app.config["DATABASE"])
    status = db.execute("SELECT status FROM item_reports").fetchone()[0]
    db.close()
    assert status == "pending"


def test_public_search_only_shows_published_reports(client, app):
    import sqlite3
    db = sqlite3.connect(app.config["DATABASE"])
    db.execute("INSERT INTO item_reports (report_type, title, category, description, location, event_date, contact_name, contact_detail, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
               ("found", "Campus ID card", "ID & documents", "Name begins with A", "Cafeteria", "2026-10-22", "Sam", "sam@example.test", "published"))
    db.commit(); db.close()
    response = client.get("/items?q=Campus")
    assert response.status_code == 200
    assert b"Campus ID card" in response.data
    assert client.get("/items/1").status_code == 200


def test_signup_and_claim_require_signed_in_user(client, app):
    assert client.get("/report").status_code == 302
    signup = client.post("/signup", data={"email": "new@example.test", "password": "new-password", "csrf_token": csrf_token(client, "/signup")})
    assert signup.status_code == 302
    assert client.get("/report").status_code == 200
    assert client.get("/my-activity").status_code == 200

    import sqlite3
    db = sqlite3.connect(app.config["DATABASE"])
    db.execute("INSERT INTO item_reports (report_type, title, category, description, location, event_date, contact_name, contact_detail, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
               ("found", "Green bottle", "Accessories", "A bottle", "Gym", "2026-10-22", "Sam", "sam@example.test", "published"))
    db.commit(); db.close()
    response = client.post("/items/1/claim", data={"message": "It has my name", "csrf_token": csrf_token(client, "/items/1")})
    assert response.status_code == 302


def test_my_activity_requires_sign_in(client):
    assert client.get("/my-activity").status_code == 302


def test_admin_can_publish_report(client, app):
    create_user(app, "admin@example.test", role="admin")
    import sqlite3
    db = sqlite3.connect(app.config["DATABASE"])
    db.execute("INSERT INTO item_reports (report_type, title, category, description, location, event_date, contact_name, contact_detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               ("lost", "Silver key", "Keys", "Small key", "Lab", "2026-10-22", "Sam", "sam@example.test"))
    db.commit(); db.close()
    client.post("/admin/login", data={"email": "admin@example.test", "password": "correct-password", "csrf_token": csrf_token(client)})
    response = client.post("/admin/reports/1/status", data={"status": "published", "csrf_token": csrf_token(client, "/admin")})
    assert response.status_code == 302
    assert b"Silver key" in client.get("/items").data


def test_admin_can_remove_a_report_and_its_claims(client, app):
    create_user(app, "admin@example.test", role="admin")
    import sqlite3
    db = sqlite3.connect(app.config["DATABASE"])
    db.execute("INSERT INTO item_reports (report_type, title, category, description, location, event_date, contact_name, contact_detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               ("lost", "Spam listing", "Other", "Remove this", "Lab", "2026-10-22", "Sam", "sam@example.test"))
    db.commit(); db.close()
    client.post("/admin/login", data={"email": "admin@example.test", "password": "correct-password", "csrf_token": csrf_token(client)})
    response = client.post("/admin/reports/1/delete", data={"csrf_token": csrf_token(client, "/admin")})
    assert response.status_code == 302
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM item_reports").fetchone()[0] == 0


def test_public_detail_hides_contact_details_and_shows_approved_photo(client, app):
    import sqlite3
    db = sqlite3.connect(app.config["DATABASE"])
    db.execute(
        "INSERT INTO item_reports (report_type, title, category, description, location, event_date, contact_name, contact_detail, photo_filename, photo_status, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("found", "Photo backpack", "Accessories", "Green bag", "Library", "2026-10-22", "Sam", "sam@example.test", "backpack.png", "approved", "published"),
    )
    db.commit(); db.close()

    response = client.get("/items/1")
    assert response.status_code == 200
    assert b"sam@example.test" not in response.data
    assert b"/items/1/photo" in response.data


def test_claim_cannot_be_duplicate_or_made_by_reporter(client, app):
    create_user(app, "reporter@example.test")
    create_user(app, "claimant@example.test")
    with app.app_context():
        reporter_id = get_db().execute("SELECT id FROM users WHERE email = ?", ("reporter@example.test",)).fetchone()[0]
        get_db().execute(
            "INSERT INTO item_reports (report_type, title, category, description, location, event_date, contact_name, contact_detail, reporter_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("found", "Blue umbrella", "Accessories", "Blue umbrella", "Hall", "2026-10-22", "Sam", "sam@example.test", reporter_id, "published"),
        )
        get_db().commit()

    login(client, "reporter@example.test")
    response = client.post("/items/1/claim", data={"message": "Mine", "csrf_token": csrf_token(client, "/items/1")})
    assert b"cannot make a claim" in client.get(response.headers["Location"]).data

    client.post("/logout", data={"csrf_token": csrf_token(client, "/")})
    login(client, "claimant@example.test")
    claim = {"message": "The handle has a scratch", "csrf_token": csrf_token(client, "/items/1")}
    assert client.post("/items/1/claim", data=claim).status_code == 302
    duplicate = client.post("/items/1/claim", data={"message": "Still mine", "csrf_token": csrf_token(client, "/items/1")})
    assert b"already have a claim" in client.get(duplicate.headers["Location"]).data


def test_accepting_claim_resolves_report_and_closes_other_pending_claims(client, app):
    create_user(app, "admin@example.test", role="admin")
    create_user(app, "first@example.test")
    create_user(app, "second@example.test")
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO item_reports (report_type, title, category, description, location, event_date, contact_name, contact_detail, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   ("found", "Keys", "Keys", "Silver keys", "Hall", "2026-10-22", "Sam", "sam@example.test", "published"))
        db.execute("INSERT INTO item_claims (item_id, claimant_id, message) VALUES (1, (SELECT id FROM users WHERE email = ?), ?)", ("first@example.test", "First claim"))
        db.execute("INSERT INTO item_claims (item_id, claimant_id, message) VALUES (1, (SELECT id FROM users WHERE email = ?), ?)", ("second@example.test", "Second claim"))
        db.commit()

    client.post("/admin/login", data={"email": "admin@example.test", "password": "correct-password", "csrf_token": csrf_token(client)})
    response = client.post("/admin/claims/1/status", data={"status": "accepted", "csrf_token": csrf_token(client, "/admin")})
    assert response.status_code == 302
    with app.app_context():
        assert get_db().execute("SELECT status FROM item_reports WHERE id = 1").fetchone()[0] == "resolved"
        assert get_db().execute("SELECT status FROM item_claims WHERE id = 2").fetchone()[0] == "declined"


def test_seed_demo_adds_public_listings_once(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["seed-demo"])
    assert result.exit_code == 0
    assert "Added 4 demo listings." in result.output
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM item_reports WHERE status = 'published'").fetchone()[0] == 4

    repeat = runner.invoke(args=["seed-demo"])
    assert repeat.exit_code == 0
    assert "Added 0 demo listings." in repeat.output


def test_matching_rewards_opposite_reports_with_shared_details():
    lost = {"category": "Accessories", "title": "Silver bottle", "description": "Slim upper body with Wilson written on it", "location": "BTech building second floor", "event_date": "2026-10-28"}
    found = {"category": "Accessories", "title": "Silver water bottle", "description": "Wilson bottle, narrow near the top", "location": "Second floor BTech building", "event_date": "2026-10-27"}
    score, reasons = match_score(lost, found)
    assert score >= 70
    assert "same category" in reasons
    assert "similar item name" in reasons


def test_admin_can_confirm_a_lost_found_match(client, app):
    create_user(app, "admin@example.test", role="admin")
    with app.app_context():
        db = get_db()
        for report_type in ("lost", "found"):
            db.execute(
                "INSERT INTO item_reports (report_type, title, category, description, location, event_date, contact_name, contact_detail, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (report_type, "Silver bottle", "Accessories", "Wilson bottle", "Library", "2026-10-28", "Sam", "sam@example.test", "published"),
            )
        db.commit()
    client.post("/admin/login", data={"email": "admin@example.test", "password": "correct-password", "csrf_token": csrf_token(client)})
    response = client.post("/admin/reports/1/confirm-match/2", data={"csrf_token": csrf_token(client, "/admin")})
    assert response.status_code == 302
    with app.app_context():
        statuses = get_db().execute("SELECT status FROM item_reports ORDER BY id").fetchall()
        assert [row[0] for row in statuses] == ["resolved", "resolved"]
