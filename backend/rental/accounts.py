import re
import secrets
from time import time

from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from rental.store import digest


ROLES = {
    'owner': 'Chủ trọ',
    'manager': 'Quản lý dãy',
    'technician': 'Kỹ thuật viên',
    'tenant': 'Người thuê',
}
USERNAME = re.compile(r'[a-z][a-z0-9_.-]{2,31}')
INVITE_CODE = re.compile(r'[A-Za-z0-9_-]{24}')
INVITE_REVISION = re.compile(r'[a-f0-9]{16}')
INVITE_SECONDS = 24 * 60 * 60
MAX_RECORD_ID = 2**63 - 1
ACCOUNT_SCHEMA = """
CREATE TABLE IF NOT EXISTS rental_accounts (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('manager','technician','tenant')),
    house_id INTEGER REFERENCES houses(id),
    stay_id INTEGER REFERENCES stays(id),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    version INTEGER NOT NULL DEFAULT 1,
    CHECK ((role IN ('manager','technician') AND house_id IS NOT NULL AND stay_id IS NULL)
        OR (role='tenant' AND house_id IS NULL AND stay_id IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS rental_active_resident_account
    ON rental_accounts(stay_id) WHERE role='tenant' AND enabled=1;
CREATE TABLE IF NOT EXISTS rental_account_invites (
    stay_id INTEGER PRIMARY KEY REFERENCES stays(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL UNIQUE,
    expires_at INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS rental_invite_checkout
AFTER UPDATE OF ended_at ON stays
WHEN NEW.ended_at IS NOT NULL
BEGIN
    DELETE FROM rental_account_invites WHERE stay_id=NEW.id;
END;
"""
ACCOUNT_SCOPE = """FROM rental_accounts a LEFT JOIN stays s ON s.id=a.stay_id
                   LEFT JOIN rooms r ON r.id=s.room_id
                   WHERE (? IS NULL OR COALESCE(a.house_id,r.house_id)=?)"""
INVITE_ASSIGNMENT = """FROM stays s JOIN rooms r ON r.id=s.room_id
                       WHERE s.ended_at IS NULL AND (? IS NULL OR r.house_id=?)
                         AND NOT EXISTS(SELECT 1 FROM rental_accounts a WHERE a.stay_id=s.id AND a.enabled=1)"""


def initialize_accounts(store):
    with store.connect() as db:
        db.executescript(ACCOUNT_SCHEMA)


def lookup_account(store, *, username=None, account_id=None, house_id=None, enabled=True):
    key, value = ('a.username', username) if username is not None else ('a.id', account_id)
    with store.connect() as db:
        row = db.execute(f'SELECT a.* {ACCOUNT_SCOPE} AND {key}=? AND a.enabled=?',
                         (house_id, house_id, value, enabled)).fetchone()
    return dict(row) if row else None


def require_roles(*roles):
    if not g.actor or g.actor['role'] not in roles:
        abort(403)
    return g.actor


def scoped_house(actor, house_id):
    scope = actor.get('house_id')
    return scope == house_id or (actor['role'] == 'owner' and scope is None)


def valid_password(value):
    return 12 <= len(value) <= 128 and value.strip() == value


def read_assignment():
    role = request.form.get('role', '')
    house = request.form.get('house_id', type=int) if role in {'manager','technician'} else None
    stay = request.form.get('stay_id', type=int) if role == 'tenant' else None
    if house is not None and not 1 <= house <= MAX_RECORD_ID:
        house = None
    if role in {'manager','technician'} and g.house_a_id is not None:
        house = g.house_a_id
    if stay is not None and not 1 <= stay <= MAX_RECORD_ID:
        stay = None
    return role, house, stay


def active_assignment(db, house, stay):
    if house:
        return db.execute('SELECT 1 FROM houses WHERE id=?', (house,)).fetchone() is not None
    return db.execute('''SELECT 1 FROM stays s JOIN rooms r ON r.id=s.room_id
                         WHERE s.id=? AND s.ended_at IS NULL AND (? IS NULL OR r.house_id=?)''',
                      (stay, g.house_a_id, g.house_a_id)).fetchone() is not None


def create_accounts_blueprint(store):
    bp = Blueprint('accounts', __name__)

    @bp.get('/accounts')
    def index():
        require_roles('owner')
        scope = g.house_a_id
        now = int(time())
        with store.connect() as db:
            accounts = db.execute("""SELECT a.id,a.username,a.display_name,a.role,a.enabled,a.house_id,a.stay_id,
                    COALESCE(h.name, th.name) AS property, r.code AS room, s.name AS resident, s.ended_at
                FROM rental_accounts a LEFT JOIN houses h ON h.id=a.house_id
                LEFT JOIN stays s ON s.id=a.stay_id LEFT JOIN rooms r ON r.id=s.room_id
                LEFT JOIN houses th ON th.id=r.house_id WHERE (? IS NULL OR COALESCE(a.house_id,r.house_id)=?)
                ORDER BY a.enabled DESC, a.username""", (scope, scope)).fetchall()
            houses = db.execute('SELECT id,name FROM houses WHERE (? IS NULL OR id=?) ORDER BY name',
                                (scope, scope)).fetchall()
            stays = db.execute("""SELECT s.id,s.name,r.code,h.name AS property, i.expires_at,
                       i.expires_at>? AS invite_active, substr(i.code_hash,1,16) AS invite_revision
                FROM stays s JOIN rooms r ON r.id=s.room_id JOIN houses h ON h.id=r.house_id
                LEFT JOIN rental_account_invites i ON i.stay_id=s.id
                WHERE s.ended_at IS NULL AND (? IS NULL OR r.house_id=?)
                ORDER BY h.name,r.code,s.name""", (now, scope, scope)).fetchall()
        assigned = {account['stay_id'] for account in accounts if account['enabled']}
        available_stays = [stay for stay in stays if stay['id'] not in assigned]
        return render_template('accounts.html', accounts=accounts, houses=houses, stays=stays,
                               available_stays=available_stays,
                               active_invites=[stay for stay in available_stays if stay['invite_active']])

    @bp.post('/accounts/invite')
    def invite():
        require_roles('owner')
        stay = request.form.get('stay_id', type=int)
        if stay is None or not 1 <= stay <= MAX_RECORD_ID:
            flash('Chọn người đang thuê để tạo mã mời.', 'error')
            return redirect(url_for('accounts.index'), code=303)
        code = secrets.token_urlsafe(18)
        with store.write() as db:
            resident = db.execute('SELECT s.name, r.code ' + INVITE_ASSIGNMENT + ' AND s.id=?',
                                  (g.house_a_id, g.house_a_id, stay)).fetchone()
            if resident is None:
                flash('Hồ sơ đã có tài khoản hoặc không còn được đăng ký.', 'error')
                return redirect(url_for('accounts.index'), code=303)
            db.execute('''INSERT INTO rental_account_invites(stay_id,code_hash,expires_at) VALUES(?,?,?)
                          ON CONFLICT(stay_id) DO UPDATE SET code_hash=excluded.code_hash,
                                                          expires_at=excluded.expires_at''',
                       (stay, digest(code.encode()), int(time()) + INVITE_SECONDS))
        return render_template('account_invite.html', code=code, resident=resident), 201

    @bp.post('/accounts/invite/<int:stay_id>/revoke')
    def revoke_invite(stay_id):
        require_roles('owner')
        revision = request.form.get('revision', '')
        if not 1 <= stay_id <= MAX_RECORD_ID or not INVITE_REVISION.fullmatch(revision):
            abort(400)
        with store.write() as db:
            changed = db.execute(
                '''DELETE FROM rental_account_invites
                   WHERE stay_id=? AND substr(code_hash,1,16)=? AND expires_at>?
                     AND EXISTS(SELECT 1 ''' + INVITE_ASSIGNMENT + ' AND s.id=rental_account_invites.stay_id)',
                (stay_id, revision, int(time()), g.house_a_id, g.house_a_id),
            ).rowcount
        flash('Đã hủy mã mời.' if changed else 'Mã mời đã hết hạn hoặc đã thay đổi.',
              'success' if changed else 'error')
        return redirect(url_for('accounts.index', _anchor='invite'), code=303)

    @bp.route('/register', methods=['GET','POST'])
    def register():
        if g.actor:
            return redirect(url_for('dashboard'))
        if request.method == 'GET':
            session.setdefault('csrf', secrets.token_urlsafe(24))
            return render_template('register.html')

        username = request.form.get('username', '').strip().lower()
        code = request.form.get('code', '').strip()
        password = request.form.get('password', '')
        if not USERNAME.fullmatch(username) or username == 'chu-tro':
            error = 'Chọn tên đăng nhập từ 3 đến 32 ký tự, bắt đầu bằng chữ cái.'
        elif not valid_password(password):
            error = 'Mật khẩu cần có từ 12 đến 128 ký tự và không có khoảng trắng ở hai đầu.'
        elif password != request.form.get('confirmation', ''):
            error = 'Hai mật khẩu chưa khớp.'
        elif not INVITE_CODE.fullmatch(code):
            error = 'Kiểm tra lại mã mời do chủ trọ cấp.'
        else:
            code_hash = digest(code.encode())
            query = ('SELECT s.id, s.name ' + INVITE_ASSIGNMENT + '''
                     AND EXISTS(SELECT 1 FROM rental_account_invites i
                                WHERE i.stay_id=s.id AND i.code_hash=? AND i.expires_at>?)''')
            with store.connect() as db:
                resident = db.execute(query, (g.house_a_id, g.house_a_id, code_hash, int(time()))).fetchone()
            error = 'Mã mời đã hết hạn, đã dùng hoặc không hợp lệ. Bạn có thể xin chủ trọ cấp mã mới.'
            if resident is not None:
                password_hash = generate_password_hash(password)
                with store.write() as db:
                    resident = db.execute(query, (g.house_a_id, g.house_a_id, code_hash, int(time()))).fetchone()
                    if resident is not None:
                        if db.execute('SELECT 1 FROM rental_accounts WHERE username=?', (username,)).fetchone():
                            error = 'Tên đăng nhập đã có người dùng. Hãy chọn tên khác.'
                        else:
                            db.execute('''INSERT INTO rental_accounts(username,display_name,password_hash,role,stay_id)
                                          VALUES(?,?,?,'tenant',?)''',
                                       (username, resident['name'], password_hash, resident['id']))
                            db.execute('DELETE FROM rental_account_invites WHERE stay_id=?', (resident['id'],))
                            flash('Đã tạo tài khoản người thuê. Bạn có thể đăng nhập.', 'success')
                            return redirect(url_for('login'), code=303)
        return render_template('register.html', error=error, username=username[:32],
                               code=code if INVITE_CODE.fullmatch(code) else ''), 400

    @bp.post('/accounts')
    def add():
        require_roles('owner')
        username = request.form.get('username', '').strip().lower()
        display_name = request.form.get('display_name', '').strip()
        role, house_id, stay_id = read_assignment()
        password = request.form.get('password', '')
        valid = (USERNAME.fullmatch(username) and username != 'chu-tro' and 1 <= len(display_name) <= 100
                 and display_name.isprintable() and valid_password(password)
                 and (house_id or stay_id) and role in {'manager','technician','tenant'})
        if not valid:
            flash('Kiểm tra tên đăng nhập, mật khẩu và phạm vi tài khoản.', 'error')
            return redirect(url_for('accounts.index'), code=303)
        password_hash = generate_password_hash(password)
        with store.write() as db:
            assigned = active_assignment(db, house_id, stay_id)
            existing = db.execute('SELECT 1 FROM rental_accounts WHERE username=? OR (stay_id=? AND enabled=1)',
                                  (username, stay_id)).fetchone()
            if assigned and not existing:
                db.execute('''INSERT INTO rental_accounts(username, display_name, password_hash, role, house_id, stay_id)
                              VALUES(?,?,?,?,?,?)''', (username, display_name, password_hash, role, house_id, stay_id))
                db.execute('DELETE FROM rental_account_invites WHERE stay_id=?', (stay_id,))
        flash('Đã tạo tài khoản.' if assigned and not existing else 'Tên đăng nhập hoặc lượt thuê đã có tài khoản, hoặc phạm vi không còn hiệu lực.',
              'success' if assigned and not existing else 'error')
        return redirect(url_for('accounts.index'), code=303)

    @bp.post('/accounts/<int:account_id>/scope')
    def change_scope(account_id):
        require_roles('owner')
        role, house_id, stay_id = read_assignment()
        display_name = request.form.get('display_name', '').strip()
        password = request.form.get('password', '')
        if not 1 <= len(display_name) <= 100 or not display_name.isprintable() or not (house_id or stay_id) or not valid_password(password):
            flash('Kiểm tra tên, phạm vi và mật khẩu mới của tài khoản.', 'error')
            return redirect(url_for('accounts.index'), code=303)
        target = lookup_account(store, account_id=account_id, house_id=g.house_a_id)
        if target is None:
            abort(404)
        if check_password_hash(target['password_hash'], password):
            flash('Khi đổi quyền, mật khẩu mới phải khác mật khẩu đang dùng.', 'error')
            return redirect(url_for('accounts.index'), code=303)
        password_hash = generate_password_hash(password)
        with store.write() as db:
            occupied = db.execute('SELECT 1 FROM rental_accounts WHERE stay_id=? AND id!=? AND enabled=1',
                                  (stay_id,account_id)).fetchone()
            saved = active_assignment(db, house_id, stay_id) and not occupied
            if saved:
                saved = db.execute('''UPDATE rental_accounts SET display_name=?,role=?,house_id=?,stay_id=?,password_hash=?,version=version+1
                                      WHERE id=? AND enabled=1 AND version=?''',
                                   (display_name,role,house_id,stay_id,password_hash,account_id,target['version'])).rowcount
                if saved:
                    db.execute('DELETE FROM rental_account_invites WHERE stay_id=?', (stay_id,))
        flash('Đã cập nhật quyền và kết thúc phiên cũ.' if saved else 'Không cập nhật được. Kiểm tra phạm vi, lượt thuê hoặc tải lại trang nếu tài khoản vừa thay đổi.',
              'success' if saved else 'error')
        return redirect(url_for('accounts.index'), code=303)

    @bp.post('/accounts/<int:account_id>/disable')
    def disable(account_id):
        require_roles('owner')
        target = lookup_account(store, account_id=account_id, house_id=g.house_a_id)
        if target is None:
            abort(404)
        with store.write() as db:
            changed = db.execute('UPDATE rental_accounts SET enabled=0, version=version+1 WHERE id=? AND version=? AND enabled=1',
                                 (account_id, target['version'])).rowcount
        if not changed:
            abort(404)
        flash('Đã khóa tài khoản và kết thúc các phiên của tài khoản.', 'success')
        return redirect(url_for('accounts.index'), code=303)

    @bp.post('/accounts/<int:account_id>/password')
    def reset_password(account_id):
        require_roles('owner')
        password = request.form.get('password', '')
        if not valid_password(password):
            flash('Mật khẩu mới cần có ít nhất 12 ký tự.', 'error')
            return redirect(url_for('accounts.index'), code=303)
        target = lookup_account(store, account_id=account_id, house_id=g.house_a_id)
        if target is None:
            abort(404)
        password_hash = generate_password_hash(password)
        with store.write() as db:
            changed = db.execute('UPDATE rental_accounts SET password_hash=?,version=version+1 WHERE id=? AND version=? AND enabled=1',
                                 (password_hash, account_id, target['version'])).rowcount
        if not changed:
            abort(404)
        flash('Đã đặt lại mật khẩu.', 'success')
        return redirect(url_for('accounts.index'), code=303)

    @bp.post('/accounts/<int:account_id>/enable')
    def enable(account_id):
        require_roles('owner')
        password = request.form.get('password', '')
        if not valid_password(password):
            flash('Nhập mật khẩu mới từ 12 đến 128 ký tự để mở khóa.', 'error')
            return redirect(url_for('accounts.index'), code=303)
        row = lookup_account(store, account_id=account_id, house_id=g.house_a_id, enabled=False)
        if row is None:
            abort(404)
        if check_password_hash(row['password_hash'], password):
            flash('Khi mở khóa, mật khẩu mới phải khác mật khẩu cũ.', 'error')
            return redirect(url_for('accounts.index'), code=303)
        password_hash = generate_password_hash(password)
        with store.write() as db:
            occupied = db.execute('SELECT 1 FROM rental_accounts WHERE stay_id=? AND enabled=1', (row['stay_id'],)).fetchone()
            saved = active_assignment(db, row['house_id'], row['stay_id']) and not occupied
            if saved:
                saved = db.execute('''UPDATE rental_accounts SET enabled=1,password_hash=?,version=version+1
                                      WHERE id=? AND enabled=0 AND version=?''',
                                   (password_hash,account_id,row['version'])).rowcount
                if saved:
                    db.execute('DELETE FROM rental_account_invites WHERE stay_id=?', (row['stay_id'],))
        flash('Đã mở khóa tài khoản.' if saved else 'Không mở khóa được. Kiểm tra phạm vi, lượt thuê hoặc tải lại trang nếu tài khoản vừa thay đổi.',
              'success' if saved else 'error')
        return redirect(url_for('accounts.index'), code=303)

    @bp.route('/account/password', methods=['GET','POST'])
    def password():
        actor = require_roles('manager','technician','tenant')
        if request.method == 'GET':
            return render_template('account_password.html')
        current = request.form.get('current', '')
        new = request.form.get('new', '')
        row = lookup_account(store, account_id=actor['id'])
        if not row or row['version'] != session.get('account_version'):
            session.clear()
            return redirect(url_for('login'), code=303)
        if len(current) > 128 or not valid_password(new) or not check_password_hash(row['password_hash'], current):
            flash('Kiểm tra mật khẩu hiện tại và độ dài mật khẩu mới.', 'error')
            return redirect(url_for('accounts.password'), code=303)
        updated_hash = generate_password_hash(new)
        with store.write() as db:
            changed = db.execute('''UPDATE rental_accounts SET password_hash=?,version=version+1
                                    WHERE id=? AND enabled=1 AND version=?''',
                                 (updated_hash, row['id'], row['version'])).rowcount
        session.clear()
        if changed:
            session.update(account_id=row['id'], account_version=row['version']+1, csrf=secrets.token_urlsafe(24))
            session.permanent = True
            flash('Đã đổi mật khẩu.', 'success')
        return redirect(url_for('accounts.password' if changed else 'login'), code=303)

    return bp
