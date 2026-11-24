import os
import secrets
import hashlib
import csv
import io
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

from flask import Flask, render_template, request, redirect, url_for, flash, abort, Response, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO'))
logger = logging.getLogger('village_people_management')

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config.update(
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS={'pool_pre_ping': True, 'pool_recycle': 280},
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', 'true').lower() == 'true',
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)


def database_url():
    """Read the database URL passed to THIS web service by Railway."""
    keys = ('DATABASE_URL', 'RAILWAY_DATABASE_URL', 'POSTGRES_URL', 'POSTGRESQL_URL')
    for key in keys:
        value = (os.environ.get(key) or '').strip()
        if not value:
            continue
        if value.startswith('postgres://'):
            value = 'postgresql://' + value[len('postgres://'):]
        if value.startswith('postgresql://'):
            value = 'postgresql+psycopg://' + value[len('postgresql://'):]
        # Validate syntax before SQLAlchemy is initialized; never expose credentials.
        try:
            make_url(value)
        except Exception:
            logger.exception('Invalid database URL syntax in environment variable %s', key)
            return None
        return value

    # Split PG variables are supported only when all required values exist.
    host = (os.environ.get('PGHOST') or '').strip()
    user = (os.environ.get('PGUSER') or '').strip()
    password = os.environ.get('PGPASSWORD') or ''
    dbname = (os.environ.get('PGDATABASE') or '').strip()
    if host and user and password and dbname:
        port = (os.environ.get('PGPORT') or '5432').strip()
        return f'postgresql+psycopg://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{dbname}'
    return None


DB_URL = database_url()
DATABASE_CONFIGURED = bool(DB_URL)
# Diagnostic-only SQLite keeps the service able to show a useful setup page.
# It is NEVER treated as the production database and is NEVER used to save records.
app.config['SQLALCHEMY_DATABASE_URI'] = DB_URL or 'sqlite:////tmp/vpm-diagnostic-only.db'
db = SQLAlchemy(app)


def now():
    return datetime.now(timezone.utc)


def token_hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class Village(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    president_name = db.Column(db.String(120), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)


class Person(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    village_id = db.Column(db.Integer, db.ForeignKey('village.id'), nullable=False, index=True)
    serial_no = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String(160), nullable=False)
    father_name = db.Column(db.String(160), nullable=False)
    address = db.Column(db.String(300), nullable=False)
    gender = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)
    village = db.relationship('Village')
    __table_args__ = (db.UniqueConstraint('village_id', 'serial_no', name='uq_village_serial'),)


class AccessLink(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    village_id = db.Column(db.Integer, db.ForeignKey('village.id'), nullable=False, index=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    revoked = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)
    village = db.relationship('Village')


_tables_ready = False
_tables_error = None


def ensure_schema():
    """Create missing tables only. No DROP, TRUNCATE, or automatic column alteration."""
    global _tables_ready, _tables_error
    if not DATABASE_CONFIGURED:
        return False
    if _tables_ready:
        return True
    try:
        with app.app_context():
            db.create_all()
            db.session.execute(text('SELECT 1'))
            db.session.commit()
        _tables_ready = True
        _tables_error = None
        logger.info('Database connection and table check succeeded')
        return True
    except Exception as exc:
        db.session.rollback()
        _tables_error = type(exc).__name__
        logger.exception('Database initialization/table check failed')
        return False


def db_ready():
    if not DATABASE_CONFIGURED:
        return False
    if not ensure_schema():
        return False
    try:
        db.session.execute(text('SELECT 1'))
        return True
    except Exception:
        db.session.rollback()
        logger.exception('Database ping failed during request')
        return False


@app.get('/health')
def health():
    return jsonify(status='ok', database_configured=DATABASE_CONFIGURED)


@app.get('/health/config')
def health_config():
    keys = ('DATABASE_URL', 'RAILWAY_DATABASE_URL', 'POSTGRES_URL', 'POSTGRESQL_URL', 'PGHOST', 'PGUSER', 'PGPASSWORD', 'PGDATABASE')
    present = [key for key in keys if (os.environ.get(key) or '').strip()]
    return jsonify(database_configured=DATABASE_CONFIGURED, recognized_variable_names_present=present,
                   url_scheme=('postgresql' if DB_URL and DB_URL.startswith('postgresql') else ('other' if DB_URL else None)),
                   note='Secret values are never returned. A configured variable does not guarantee a successful connection.')


@app.get('/health/db')
def health_db():
    if not DATABASE_CONFIGURED:
        return jsonify(connected=False, error='database_configuration_missing',
                       message='This Web service process has no non-empty DATABASE_URL or complete PG* variables.'), 503
    if not ensure_schema():
        return jsonify(connected=False, error='database_initialization_failed', detail=_tables_error), 503
    try:
        db.session.execute(text('SELECT 1'))
        return jsonify(connected=True, database='postgresql', tables_created_or_present=True)
    except Exception as exc:
        db.session.rollback()
        logger.exception('Health database query failed')
        return jsonify(connected=False, error=type(exc).__name__, detail=str(exc)[:250]), 503


@app.before_request
def prepare_database():
    # Admin routes are protected by a password stored only in Railway Variables.
    if request.path.startswith('/admin') and request.endpoint not in ('admin_login', 'admin_logout'):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login', next=request.path))

    if request.endpoint in ('health', 'health_config', 'health_db', 'static', 'admin_login', 'admin_logout'):
        return None
    if not db_ready():
        return render_template('setup_error.html'), 503
    return None


@app.get('/')
def home():
    return redirect(url_for('admin'))


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    admin_password = os.environ.get('ADMIN_PASSWORD', '')
    if not admin_password:
        return render_template('login.html', setup_missing=True), 503
    if request.method == 'POST':
        supplied = request.form.get('password', '')
        if secrets.compare_digest(supplied, admin_password):
            session.clear()
            session['admin_logged_in'] = True
            session.permanent = True
            destination = request.args.get('next', '')
            if not destination.startswith('/admin') or destination.startswith('//'):
                destination = url_for('admin')
            return redirect(destination)
        flash('Incorrect admin password. Please try again.', 'error')
    return render_template('login.html', setup_missing=False)


@app.post('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))


@app.get('/admin')
def admin():
    villages = Village.query.order_by(Village.name).all()
    q = request.args.get('q', '').strip()
    village_id = request.args.get('village', type=int)
    gender = request.args.get('gender', '')
    people_q = Person.query.join(Village)
    if q:
        pattern = f'%{q}%'
        people_q = people_q.filter(db.or_(Person.name.ilike(pattern), Person.father_name.ilike(pattern), Person.address.ilike(pattern)))
    if village_id:
        people_q = people_q.filter(Person.village_id == village_id)
    if gender in ('Male', 'Female', 'Other'):
        people_q = people_q.filter(Person.gender == gender)
    filtered_total = people_q.order_by(None).count()
    people = people_q.order_by(Village.name, Person.serial_no).limit(2000).all()
    total = db.session.query(func.count(Person.id)).scalar() or 0
    grouped = db.session.query(Person.village_id, Person.gender, func.count(Person.id)).group_by(Person.village_id, Person.gender).all()
    counts = {}
    for vid, gen, count in grouped:
        counts.setdefault(vid, {'total': 0, 'male': 0, 'female': 0, 'other': 0})
        counts[vid]['total'] += count
        counts[vid][{'Male': 'male', 'Female': 'female', 'Other': 'other'}.get(gen, 'other')] += count
    summaries = [{'village': v, **counts.get(v.id, {'total': 0, 'male': 0, 'female': 0, 'other': 0})} for v in villages]
    stats = {g: sum(c for _, gen, c in grouped if gen == g) for g in ('Male', 'Female', 'Other')}
    links = AccessLink.query.order_by(AccessLink.created_at.desc()).all()
    return render_template('admin.html', villages=villages, people=people, total=total, stats=stats,
                           summaries=summaries, links=links, q=q, selected=village_id, gender=gender,
                           filtered_total=filtered_total)


@app.post('/admin/villages')
def add_village():
    name = request.form.get('name', '').strip()
    president = request.form.get('president_name', '').strip()
    if not name or not president:
        flash('Village aur President name required hai.', 'error')
    elif Village.query.filter(func.lower(Village.name) == name.lower()).first():
        flash('Village already exists.', 'error')
    else:
        db.session.add(Village(name=name, president_name=president))
        db.session.commit()
        flash('Village added.', 'success')
    return redirect(url_for('admin') + '#villages')


@app.post('/admin/villages/<int:vid>/edit')
def edit_village(vid):
    village = db.get_or_404(Village, vid)
    name = request.form.get('name', '').strip()
    president = request.form.get('president_name', '').strip()
    duplicate = Village.query.filter(
        func.lower(Village.name) == name.lower(), Village.id != village.id
    ).first() if name else None
    if not name or not president:
        flash('Village name and president name are required.', 'error')
    elif duplicate:
        flash('Another village already uses this name.', 'error')
    else:
        try:
            village.name = name
            village.president_name = president
            db.session.commit()
            flash('Village updated successfully.', 'success')
        except Exception:
            db.session.rollback()
            logger.exception('Could not update village')
            flash('Village update nahi hua. Please retry.', 'error')
    return redirect(url_for('admin') + '#villages')


@app.post('/admin/people')
def add_person_admin():
    return save_person()


@app.post('/p/<token>/people')
def add_person_president(token):
    link = get_link(token)
    if not link:
        abort(404)
    return save_person(locked_village_id=link.village_id, token=token)


def save_person(locked_village_id=None, token=None):
    vid = locked_village_id if locked_village_id is not None else request.form.get('village_id', type=int)
    if not vid:
        flash('Please choose a village.', 'error')
        return redirect(url_for('president', token=token) if token else url_for('admin') + '#people')
    v = db.get_or_404(Village, vid)
    name = request.form.get('name', '').strip()
    father = request.form.get('father_name', '').strip()
    address = request.form.get('address', '').strip()
    gender = request.form.get('gender', '')
    if not all((name, father, address)) or gender not in ('Male', 'Female', 'Other'):
        flash('Please complete all required fields.', 'error')
        return redirect(url_for('president', token=token) if token else url_for('admin') + '#people')
    try:
        # PostgreSQL row lock serializes serial-number assignment per village.
        db.session.execute(text('SELECT id FROM village WHERE id=:id FOR UPDATE'), {'id': vid})
        last = db.session.query(func.max(Person.serial_no)).filter_by(village_id=vid).scalar() or 0
        person = Person(village_id=vid, serial_no=last + 1, name=name, father_name=father, address=address, gender=gender)
        db.session.add(person)
        db.session.commit()
        flash(f'Record saved. Serial No. {person.serial_no}', 'success')
    except Exception:
        db.session.rollback()
        logger.exception('Could not save person record')
        flash('Record save nahi hua. Please retry.', 'error')
    return redirect(url_for('president', token=token) if token else url_for('admin') + '#people')


@app.post('/admin/people/<int:pid>/edit')
def edit_person(pid):
    person = db.get_or_404(Person, pid)
    name = request.form.get('name', '').strip()
    father = request.form.get('father_name', '').strip()
    address = request.form.get('address', '').strip()
    gender = request.form.get('gender', '')
    if not all((name, father, address)) or gender not in ('Male', 'Female', 'Other'):
        flash('Please enter name, father name, address and a valid gender.', 'error')
        return redirect(url_for('admin') + '#people')
    try:
        person.name = name
        person.father_name = father
        person.address = address
        person.gender = gender
        db.session.commit()
        flash('Record updated successfully.', 'success')
    except Exception:
        db.session.rollback()
        logger.exception('Could not update person record')
        flash('Record update nahi hua. Please retry.', 'error')
    return redirect(url_for('admin') + '#people')


@app.post('/admin/people/<int:pid>/delete')
def delete_person(pid):
    person = db.get_or_404(Person, pid)
    db.session.delete(person)
    db.session.commit()
    flash('Record deleted.', 'success')
    return redirect(url_for('admin') + '#people')


@app.post('/admin/links/<int:vid>')
def create_link(vid):
    village = db.get_or_404(Village, vid)
    raw = secrets.token_urlsafe(32)
    days = request.form.get('days', 7, type=int) or 7
    days = max(1, min(days, 30))
    link = AccessLink(village_id=village.id, token_hash=token_hash(raw), expires_at=now() + timedelta(days=days))
    db.session.add(link)
    db.session.commit()
    flash('President link (copy now): ' + url_for('president', token=raw, _external=True), 'success')
    return redirect(url_for('admin') + '#links')


@app.post('/admin/links/<int:lid>/revoke')
def revoke_link(lid):
    link = db.get_or_404(AccessLink, lid)
    link.revoked = True
    db.session.commit()
    flash('President link revoked.', 'success')
    return redirect(url_for('admin') + '#links')


def get_link(token):
    link = AccessLink.query.filter_by(token_hash=token_hash(token)).first()
    if not link or link.revoked:
        return None
    expiry = link.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry <= now():
        return None
    return link


@app.get('/p/<token>')
def president(token):
    link = get_link(token)
    if not link:
        abort(404)
    village = link.village
    q = request.args.get('q', '').strip()
    gender = request.args.get('gender', '')
    query = Person.query.filter_by(village_id=village.id)
    if q:
        pattern = f'%{q}%'
        query = query.filter(db.or_(Person.name.ilike(pattern), Person.father_name.ilike(pattern), Person.address.ilike(pattern)))
    if gender in ('Male', 'Female', 'Other'):
        query = query.filter_by(gender=gender)
    filtered_total = query.count()
    people = query.order_by(Person.serial_no).all()
    total_people = Person.query.filter_by(village_id=village.id).count()
    stats = {g: Person.query.filter_by(village_id=village.id, gender=g).count() for g in ('Male', 'Female', 'Other')}
    return render_template('president_connected.html', village=village, people=people, stats=stats,
                           link=link, q=q, gender=gender, total_people=total_people,
                           filtered_total=filtered_total)


def csv_response(rows, filename):
    output = io.StringIO()
    writer = csv.writer(output)
    for row in rows:
        writer.writerow(row)
    return Response('\ufeff' + output.getvalue(), mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename={filename}'})


@app.get('/admin/export.csv')
def export_admin():
    rows = [['Village', 'Serial No', 'Name', 'Father Name', 'Address', 'Gender']]
    for person in Person.query.join(Village).order_by(Village.name, Person.serial_no).all():
        rows.append([person.village.name, person.serial_no, person.name, person.father_name, person.address, person.gender])
    return csv_response(rows, 'all_village_people.csv')


@app.get('/p/<token>/export.csv')
def export_president(token):
    link = get_link(token)
    if not link:
        abort(404)
    rows = [['Serial No', 'Name', 'Father Name', 'Address', 'Gender']]
    for person in Person.query.filter_by(village_id=link.village_id).order_by(Person.serial_no).all():
        rows.append([person.serial_no, person.name, person.father_name, person.address, person.gender])
    return csv_response(rows, 'village_people.csv')


@app.errorhandler(500)
def internal_error(error):
    db.session.rollback()
    logger.exception('Unhandled application error: %s', error)
    return ('Application error. Check Railway Deploy Logs for the detailed traceback.', 500)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
