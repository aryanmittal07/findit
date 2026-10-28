import os
import re
import secrets
import sqlite3
import uuid
from datetime import date
from pathlib import Path

import click
from flask import Flask, abort, current_app, g, redirect, flash, render_template, request, send_from_directory, session, url_for
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY"),
        DATABASE=BASE_DIR / "instance" / "findit.sqlite3",
        UPLOAD_FOLDER=BASE_DIR / "instance" / "uploads",
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,
    )

    if test_config:
        app.config.update(test_config)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)

    with app.app_context():
        init_db()

    @app.before_request
    def ensure_csrf_token():
        session.setdefault("csrf_token", secrets.token_urlsafe(32))

    @app.teardown_appcontext
    def close_db(_error=None):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.get("/")
    def home():
        latest_items = get_db().execute(
            "SELECT id, report_type, title, category, location, event_date, photo_filename, photo_status FROM item_reports "
            "WHERE status = 'published' ORDER BY created_at DESC LIMIT 3"
        ).fetchall()
        return render_template("public/home.html", latest_items=latest_items)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.route("/signup", methods=["GET", "POST"])
    def signup():
        if session.get("user_id"):
            return redirect(url_for("home"))
        if request.method == "POST":
            if not valid_csrf_token(request.form.get("csrf_token")):
                abort(400)
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            if "@" not in email or len(email) > 254 or len(password) < 10:
                flash("Use a valid email address and a password of at least 10 characters.", "error")
                return render_template("public/signup.html"), 400
            try:
                get_db().execute("INSERT INTO users (email, password_hash) VALUES (?, ?)", (email, generate_password_hash(password)))
                get_db().commit()
            except sqlite3.IntegrityError:
                flash("An account already exists for that email. Please sign in.", "error")
                return render_template("public/signup.html"), 409
            user = get_db().execute("SELECT id, role FROM users WHERE email = ?", (email,)).fetchone()
            sign_in_user(user)
            flash("Welcome to FindIt. You can now submit reports and claims.", "success")
            return redirect(url_for("home"))
        return render_template("public/signup.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if session.get("user_id"):
            return redirect(url_for("home"))
        if request.method == "POST":
            if not valid_csrf_token(request.form.get("csrf_token")):
                abort(400)
            user = get_db().execute("SELECT id, password_hash, role FROM users WHERE email = ?", (request.form.get("email", "").strip().lower(),)).fetchone()
            if user is None or not check_password_hash(user["password_hash"], request.form.get("password", "")):
                flash("Email or password is incorrect.", "error")
                return render_template("public/login.html"), 401
            sign_in_user(user)
            return redirect(url_for("home"))
        return render_template("public/login.html")

    @app.post("/logout")
    def logout():
        if not valid_csrf_token(request.form.get("csrf_token")):
            abort(400)
        session.clear()
        return redirect(url_for("home"))

    @app.get("/my-activity")
    def my_activity():
        if not session.get("user_id"):
            flash("Please sign in to view your activity.", "error")
            return redirect(url_for("login"))
        reports = get_db().execute(
            "SELECT id, title, report_type, category, description, location, event_date, status, created_at "
            "FROM item_reports WHERE reporter_id = ? ORDER BY created_at DESC", (session["user_id"],)
        ).fetchall()
        claims = get_db().execute(
            "SELECT c.id, c.message, c.status, c.created_at, r.id AS item_id, r.title, r.report_type "
            "FROM item_claims c JOIN item_reports r ON r.id = c.item_id WHERE c.claimant_id = ? ORDER BY c.created_at DESC",
            (session["user_id"],),
        ).fetchall()
        return render_template(
            "public/my_activity.html",
            reports=reports,
            claims=claims,
            match_candidates=potential_matches_for(reports),
        )

    @app.route("/report", methods=["GET", "POST"])
    def report_item():
        if not session.get("user_id"):
            flash("Please sign in to submit a report.", "error")
            return redirect(url_for("login"))
        if request.method == "POST":
            if not valid_csrf_token(request.form.get("csrf_token")):
                abort(400)
            fields = ("report_type", "title", "category", "description", "location", "event_date", "contact_name", "contact_detail")
            data = {field: request.form.get(field, "").strip() for field in fields}
            if not all(data.values()) or data["report_type"] not in {"lost", "found"}:
                flash("Please complete every field before submitting.", "error")
                return render_template("public/report.html", form=data), 400
            photo, photo_error = save_photo(request.files.get("photo"))
            if photo_error:
                flash(photo_error, "error")
                return render_template("public/report.html", form=data), 400
            get_db().execute(
                "INSERT INTO item_reports (report_type, title, category, description, location, event_date, contact_name, contact_detail, reporter_id, photo_filename) "
                "VALUES (:report_type, :title, :category, :description, :location, :event_date, :contact_name, :contact_detail, :reporter_id, :photo_filename)",
                {**data, "reporter_id": session["user_id"], "photo_filename": photo},
            )
            get_db().commit()
            flash("Your report was received and will appear after admin review.", "success")
            return redirect(url_for("report_item"))
        return render_template("public/report.html", form={})

    @app.get("/items")
    def item_list():
        query = request.args.get("q", "").strip()
        report_type = request.args.get("type", "").strip()
        sql = "SELECT id, report_type, title, category, description, location, event_date, photo_filename, photo_status FROM item_reports WHERE status = 'published'"
        params = []
        if query:
            sql += " AND (title LIKE ? OR category LIKE ? OR description LIKE ? OR location LIKE ?)"
            wildcard = f"%{query}%"
            params.extend([wildcard] * 4)
        if report_type in {"lost", "found"}:
            sql += " AND report_type = ?"
            params.append(report_type)
        sql += " ORDER BY created_at DESC"
        items = get_db().execute(sql, params).fetchall()
        return render_template("public/items.html", items=items, query=query, selected_type=report_type)

    @app.get("/items/<int:item_id>")
    def item_detail(item_id):
        item = get_db().execute(
            "SELECT id, report_type, title, category, description, location, event_date, reporter_id, photo_filename, photo_status "
            "FROM item_reports WHERE id = ? AND status = 'published'", (item_id,)
        ).fetchone()
        if item is None:
            abort(404)
        return render_template("public/item_detail.html", item=item)

    @app.get("/items/<int:item_id>/photo")
    def item_photo(item_id):
        item = get_db().execute("SELECT photo_filename, photo_status, status FROM item_reports WHERE id = ?", (item_id,)).fetchone()
        if not item or not item["photo_filename"] or (not session.get("is_admin") and (item["status"] != "published" or item["photo_status"] != "approved")):
            abort(404)
        return send_from_directory(app.config["UPLOAD_FOLDER"], item["photo_filename"])

    @app.post("/items/<int:item_id>/claim")
    def create_claim(item_id):
        if not session.get("user_id"):
            flash("Please sign in to make a claim.", "error")
            return redirect(url_for("login"))
        if not valid_csrf_token(request.form.get("csrf_token")):
            abort(400)
        item = get_db().execute(
            "SELECT id, reporter_id FROM item_reports WHERE id = ? AND status = 'published'", (item_id,)
        ).fetchone()
        message = request.form.get("message", "").strip()
        if item is None:
            abort(404)
        if item["reporter_id"] == session["user_id"]:
            flash("You cannot make a claim on your own report.", "error")
            return redirect(url_for("item_detail", item_id=item_id))
        if not message:
            flash("Please add a short message that helps verify your claim.", "error")
            return redirect(url_for("item_detail", item_id=item_id))
        existing_claim = get_db().execute(
            "SELECT id FROM item_claims WHERE item_id = ? AND claimant_id = ? AND status = 'pending'",
            (item_id, session["user_id"]),
        ).fetchone()
        if existing_claim:
            flash("You already have a claim awaiting review for this item.", "error")
            return redirect(url_for("item_detail", item_id=item_id))
        get_db().execute("INSERT INTO item_claims (item_id, claimant_id, message) VALUES (?, ?, ?)", (item_id, session["user_id"], message))
        get_db().commit()
        flash("Your claim was sent for review.", "success")
        return redirect(url_for("item_detail", item_id=item_id))

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if session.get("is_admin"):
            return redirect(url_for("admin_dashboard"))

        error = None
        if request.method == "POST":
            if not valid_csrf_token(request.form.get("csrf_token")):
                abort(400)
            email = request.form.get("email", "").strip().lower()
            user = get_db().execute("SELECT id, password_hash, role FROM users WHERE email = ?", (email,)).fetchone()
            if user is None or user["role"] != "admin" or not check_password_hash(user["password_hash"], request.form.get("password", "")):
                error = "The password is incorrect."
            else:
                sign_in_user(user)
                return redirect(url_for("admin_dashboard"))
        return render_template("admin/login.html", error=error)

    @app.get("/admin")
    def admin_dashboard():
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        reports = get_db().execute("SELECT id, title, report_type, category, description, location, event_date, contact_name, contact_detail, status, photo_filename, photo_status FROM item_reports ORDER BY created_at DESC").fetchall()
        claims = get_db().execute("SELECT c.id, c.message, c.status, r.title, u.email FROM item_claims c JOIN item_reports r ON r.id = c.item_id JOIN users u ON u.id = c.claimant_id WHERE c.status = 'pending' ORDER BY c.created_at DESC").fetchall()
        return render_template(
            "admin/dashboard.html",
            reports=reports,
            claims=claims,
            match_candidates=potential_matches_for(reports, include_pending=True),
        )

    @app.post("/admin/reports/<int:item_id>/status")
    def admin_update_report(item_id):
        require_admin()
        if not valid_csrf_token(request.form.get("csrf_token")):
            abort(400)
        status = request.form.get("status")
        if status not in {"published", "resolved", "pending"}:
            abort(400)
        get_db().execute("UPDATE item_reports SET status = ? WHERE id = ?", (status, item_id))
        get_db().commit()
        flash("Report status updated.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.post("/admin/reports/<int:item_id>/photo-status")
    def admin_update_photo(item_id):
        require_admin()
        if not valid_csrf_token(request.form.get("csrf_token")): abort(400)
        status = request.form.get("photo_status")
        if status not in {"approved", "hidden", "pending"}: abort(400)
        get_db().execute("UPDATE item_reports SET photo_status = ? WHERE id = ?", (status, item_id)); get_db().commit()
        return redirect(url_for("admin_dashboard"))

    @app.post("/admin/reports/<int:item_id>/confirm-match/<int:matched_item_id>")
    def admin_confirm_match(item_id, matched_item_id):
        """Resolve an admin-reviewed lost/found pair together."""
        require_admin()
        if not valid_csrf_token(request.form.get("csrf_token")):
            abort(400)
        reports = get_db().execute(
            "SELECT id, report_type, status FROM item_reports WHERE id IN (?, ?)", (item_id, matched_item_id)
        ).fetchall()
        if len(reports) != 2 or reports[0]["report_type"] == reports[1]["report_type"]:
            abort(400)
        get_db().execute("UPDATE item_reports SET status = 'resolved' WHERE id IN (?, ?)", (item_id, matched_item_id))
        get_db().execute(
            "UPDATE item_claims SET status = 'declined' WHERE item_id IN (?, ?) AND status = 'pending'",
            (item_id, matched_item_id),
        )
        get_db().commit()
        flash("The lost and found reports were confirmed as a match and resolved together.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.post("/admin/reports/<int:item_id>/delete")
    def admin_delete_report(item_id):
        require_admin()
        if not valid_csrf_token(request.form.get("csrf_token")):
            abort(400)
        report = get_db().execute("SELECT photo_filename FROM item_reports WHERE id = ?", (item_id,)).fetchone()
        if report is None:
            abort(404)
        get_db().execute("DELETE FROM item_claims WHERE item_id = ?", (item_id,))
        get_db().execute("DELETE FROM item_reports WHERE id = ?", (item_id,))
        get_db().commit()
        if report["photo_filename"]:
            photo_path = Path(app.config["UPLOAD_FOLDER"]) / report["photo_filename"]
            if photo_path.exists():
                photo_path.unlink()
        flash("The report and any related claims were removed.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.post("/admin/claims/<int:claim_id>/status")
    def admin_update_claim(claim_id):
        require_admin()
        if not valid_csrf_token(request.form.get("csrf_token")):
            abort(400)
        status = request.form.get("status")
        if status not in {"accepted", "declined"}:
            abort(400)
        claim = get_db().execute("SELECT item_id FROM item_claims WHERE id = ?", (claim_id,)).fetchone()
        if claim is None:
            abort(404)
        get_db().execute("UPDATE item_claims SET status = ? WHERE id = ?", (status, claim_id))
        if status == "accepted":
            get_db().execute("UPDATE item_reports SET status = 'resolved' WHERE id = ?", (claim["item_id"],))
            get_db().execute(
                "UPDATE item_claims SET status = 'declined' WHERE item_id = ? AND id != ? AND status = 'pending'",
                (claim["item_id"], claim_id),
            )
        get_db().commit()
        flash("Claim updated.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.post("/admin/logout")
    def admin_logout():
        if not valid_csrf_token(request.form.get("csrf_token")):
            abort(400)
        session.clear()
        return redirect(url_for("home"))

    @app.cli.command("create-admin")
    @click.option("--email", prompt=True)
    @click.password_option()
    def create_admin(email, password):
        """Create a separate FindIt administrator account."""
        try:
            get_db().execute("INSERT INTO users (email, password_hash, role) VALUES (?, ?, 'admin')", (email.strip().lower(), generate_password_hash(password)))
            get_db().commit()
        except sqlite3.IntegrityError:
            raise click.ClickException("That email already has an account. Choose a different admin email.")
        click.echo("Admin account created.")

    @app.cli.command("seed-demo")
    def seed_demo():
        """Add a small set of public demo listings without duplicating them."""
        listings = (
            ("found", "Navy water bottle", "Accessories", "Matte bottle with a small mountain sticker near the base.", "Library, second floor", "2026-10-26", "FindIt desk", "findit@example.test"),
            ("lost", "Wireless earbuds case", "Electronics", "Small white charging case; left after an afternoon study session.", "Student centre lounge", "2026-10-25", "FindIt desk", "findit@example.test"),
            ("found", "Set of silver keys", "Keys", "Three keys on a green fabric loop with a round metal charm.", "Science building entrance", "2026-10-24", "FindIt desk", "findit@example.test"),
            ("lost", "Black notebook", "Books & stationery", "A5 notebook with handwritten economics notes and a cream elastic band.", "Cafeteria", "2026-10-23", "FindIt desk", "findit@example.test"),
        )
        db = get_db()
        added = 0
        for listing in listings:
            exists = db.execute("SELECT id FROM item_reports WHERE title = ? AND location = ?", (listing[1], listing[4])).fetchone()
            if exists is None:
                db.execute(
                    "INSERT INTO item_reports (report_type, title, category, description, location, event_date, contact_name, contact_detail, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'published')",
                    listing,
                )
                added += 1
        db.commit()
        click.echo(f"Added {added} demo listing{'s' if added != 1 else ''}.")

    return app


def valid_csrf_token(value):
    expected = session.get("csrf_token")
    return bool(expected and value and secrets.compare_digest(expected, value))


def sign_in_user(user):
    session.clear()
    session["user_id"] = user["id"]
    session["is_admin"] = user["role"] == "admin"
    session["csrf_token"] = secrets.token_urlsafe(32)


def require_admin():
    if not session.get("is_admin"):
        abort(403)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


def init_db():
    schema = (BASE_DIR / "schema.sql").read_text()
    get_db().executescript(schema)
    columns = {row[1] for row in get_db().execute("PRAGMA table_info(item_reports)").fetchall()}
    if "photo_filename" not in columns: get_db().execute("ALTER TABLE item_reports ADD COLUMN photo_filename TEXT")
    if "photo_status" not in columns: get_db().execute("ALTER TABLE item_reports ADD COLUMN photo_status TEXT NOT NULL DEFAULT 'pending'")
    get_db().commit()


def save_photo(upload):
    if upload is None or not upload.filename:
        return None, None
    name = secure_filename(upload.filename)
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if extension not in {"jpg", "jpeg", "png", "gif", "webp"}:
        return None, "Please upload a PNG, JPEG, GIF, or WebP photo."
    filename = f"{uuid.uuid4().hex}.{extension}"
    upload.save(Path(current_app.config["UPLOAD_FOLDER"]) / filename)
    return filename, None


def potential_matches_for(reports, include_pending=False):
    """Return useful opposite-type report pairs for a human to review."""
    available_statuses = ("published", "pending") if include_pending else ("published",)
    placeholders = ", ".join("?" for _ in available_statuses)
    candidates = get_db().execute(
        f"SELECT id, report_type, title, category, description, location, event_date, status "
        f"FROM item_reports WHERE status IN ({placeholders})",
        available_statuses,
    ).fetchall()
    matches = {}
    for report in reports:
        if report["status"] == "resolved":
            continue
        suggestions = []
        for candidate in candidates:
            if candidate["id"] == report["id"] or candidate["report_type"] == report["report_type"]:
                continue
            score, reasons = match_score(report, candidate)
            if score >= 40:
                suggestions.append({"item": candidate, "score": score, "reasons": reasons})
        matches[report["id"]] = sorted(suggestions, key=lambda suggestion: suggestion["score"], reverse=True)[:3]
    return matches


def match_score(first, second):
    score = 0
    reasons = []
    if first["category"].casefold() == second["category"].casefold():
        score += 25
        reasons.append("same category")

    title_overlap = word_overlap(first["title"], second["title"])
    if title_overlap:
        score += round(30 * title_overlap)
        reasons.append("similar item name")

    detail_overlap = word_overlap(first["description"], second["description"])
    if detail_overlap >= 0.15:
        score += round(15 * detail_overlap)
        reasons.append("shared details")

    location_overlap = word_overlap(first["location"], second["location"])
    if location_overlap:
        score += round(15 * location_overlap)
        reasons.append("similar location")

    try:
        days_apart = abs((date.fromisoformat(first["event_date"]) - date.fromisoformat(second["event_date"])).days)
        if days_apart <= 3:
            score += 15
            reasons.append("nearby dates")
        elif days_apart <= 7:
            score += 10
            reasons.append("close dates")
        elif days_apart <= 14:
            score += 5
    except ValueError:
        pass
    return min(score, 100), reasons


def word_overlap(first, second):
    ignored = {"and", "the", "with", "from", "this", "that", "for", "was", "has", "have", "item"}
    first_words = {word for word in re.findall(r"[a-z0-9]+", first.casefold()) if len(word) > 2 and word not in ignored}
    second_words = {word for word in re.findall(r"[a-z0-9]+", second.casefold()) if len(word) > 2 and word not in ignored}
    if not first_words or not second_words:
        return 0
    return len(first_words & second_words) / min(len(first_words), len(second_words))


if __name__ == "__main__":
    create_app().run(debug=True)
