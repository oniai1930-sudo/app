"""Smart Key Box API: Flask + SQLite.

Preserves the original users/events API and adds six-key borrowing/returning.
The ESP32/AS608 performs fingerprint matching locally. This server stores only
fingerprint slot IDs, never biometric images or templates.

Environment:
  ADMIN_API_KEY, DEVICE_API_KEY (required)
  DATABASE_PATH (optional; use persistent disk on Render)
  PORT (optional; default 5000)
"""
import os
import sqlite3
import secrets
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from functools import wraps
from pathlib import Path
from flask import Flask, jsonify, request, session, send_from_directory, abort

app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get("SECRET_KEY", "")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1") == "1")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

DB_PATH = Path(os.environ.get("DATABASE_PATH", str(Path(__file__).with_name("fingerprints.db"))))
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")
DEVICE_API_KEY = os.environ.get("DEVICE_API_KEY", "")
KEY_COUNT = 6
THAILAND = ZoneInfo("Asia/Bangkok")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_db():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 10000")
    return db


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                student_id TEXT NOT NULL UNIQUE,
                phone TEXT NOT NULL DEFAULT '',
                fingerprint_id INTEGER UNIQUE,
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                fingerprint_id INTEGER,
                action TEXT NOT NULL CHECK (action IN ('verify','open','close')),
                success INTEGER NOT NULL CHECK (success IN (0,1)),
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC);
            CREATE TABLE IF NOT EXISTS keys (
                key_id INTEGER PRIMARY KEY CHECK (key_id BETWEEN 1 AND 6),
                key_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'available'
                    CHECK (status IN ('available','borrowed')),
                borrower_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                borrowed_at TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_id INTEGER NOT NULL REFERENCES keys(key_id),
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                fingerprint_id INTEGER NOT NULL,
                action TEXT NOT NULL CHECK (action IN ('borrow','return')),
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_transactions_created ON transactions(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_transactions_key ON transactions(key_id, id DESC);
            CREATE TABLE IF NOT EXISTS cabinet_control (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                mode TEXT NOT NULL CHECK (mode IN ('auto','special','off')),
                special_until TEXT,
                changed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS control_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mode TEXT NOT NULL,
                actor TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                special_until TEXT
            );
            CREATE TABLE IF NOT EXISTS enrollment_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                status TEXT NOT NULL CHECK(status IN ('pending','in_progress','success','failed','cancelled')),
                fingerprint_id INTEGER UNIQUE,
                message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_enrollment_jobs_status
                ON enrollment_jobs(status, id);
            CREATE INDEX IF NOT EXISTS idx_enrollment_jobs_user
                ON enrollment_jobs(user_id, id DESC);
        """)
        columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
        if "phone" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN phone TEXT NOT NULL DEFAULT ''")
        now = utc_now()
        db.execute("INSERT OR IGNORE INTO cabinet_control (id,mode,special_until,changed_at) VALUES (1,'auto',NULL,?)", (now,))
        room_names = {1: "ห้อง 213", 2: "ห้อง 214", 3: "ห้อง 215",
                      4: "ห้อง 216", 5: "ห้อง 222", 6: "ห้อง 223"}
        for key_id in range(1, KEY_COUNT + 1):
            db.execute(
                """INSERT OR IGNORE INTO keys
                   (key_id, key_name, status, updated_at)
                   VALUES (?, ?, 'available', ?)""",
                (key_id, room_names[key_id], now),
            )
            # Rename only the original default names; preserve custom names and all history.
            db.execute(
                "UPDATE keys SET key_name=?, updated_at=? WHERE key_id=? AND key_name=?",
                (room_names[key_id], now, key_id, f"กุญแจช่อง {key_id}"),
            )


def require_key(expected):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            provided = request.headers.get("X-API-Key", "")
            if not expected or not secrets.compare_digest(provided, expected):
                return jsonify(error="Unauthorized"), 401
            return fn(*args, **kwargs)
        return wrapped
    return decorator


def admin(fn):
    return require_key(ADMIN_API_KEY)(fn)


def device(fn):
    return require_key(DEVICE_API_KEY)(fn)


def as_user(row):
    return {k: row[k] for k in (
        "id", "full_name", "student_id", "fingerprint_id", "active", "created_at"
    )}


def find_active_user(db, fingerprint_id):
    return db.execute(
        "SELECT * FROM users WHERE fingerprint_id = ? AND active = 1",
        (fingerprint_id,),
    ).fetchone()


def positive_int(data, field):
    value = data.get(field)
    return value if type(value) is int and value > 0 else None


def control_state(db):
    row = db.execute("SELECT * FROM cabinet_control WHERE id=1").fetchone()
    now = datetime.now(timezone.utc)
    local = now.astimezone(THAILAND)
    mode = row["mode"]
    until = datetime.fromisoformat(row["special_until"]) if row["special_until"] else None
    if mode == "special" and (until is None or now >= until):
        # Persist expiration, even if no scheduler is running.
        db.execute("UPDATE cabinet_control SET mode='auto',special_until=NULL,changed_at=? WHERE id=1", (utc_now(),))
        db.execute("INSERT INTO control_audit(mode,actor,changed_at,special_until) VALUES ('auto','system-expiry',?,NULL)", (utc_now(),))
        mode, until = "auto", None
    allowed = (7 <= local.hour < 19) if mode == "auto" else mode == "special"
    return {"mode": mode, "enabled": allowed,
            "label": "Admin เปิดพิเศษ" if mode == "special" else ("เปิดใช้งาน" if allowed else "ปิดใช้งาน"),
            "local_time": local.isoformat(timespec="seconds"),
            "service_hours": "07:00-19:00 Asia/Bangkok",
            "special_until": until.isoformat() if until else None}


@app.get("/api/control/status")
@device
def device_control_status():
    with connect_db() as db:
        state = control_state(db)
    return jsonify(state)


@app.get("/api/admin/control")
@admin
def admin_control_status():
    with connect_db() as db:
        state = control_state(db)
    return jsonify(state)


@app.post("/api/admin/control")
@admin
def set_control():
    data = request.get_json(silent=True) or {}
    mode = data.get("mode")
    if mode not in ("auto", "special", "off"):
        return jsonify(error="mode must be auto, special or off"), 400
    now = datetime.now(timezone.utc)
    expiry = (now + timedelta(minutes=30)).isoformat(timespec="seconds") if mode == "special" else None
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE cabinet_control SET mode=?,special_until=?,changed_at=? WHERE id=1", (mode, expiry, now.isoformat(timespec="seconds")))
        db.execute("INSERT INTO control_audit(mode,actor,changed_at,special_until) VALUES (?,?,?,?)", (mode, "admin-api-key", now.isoformat(timespec="seconds"), expiry))
        state = control_state(db)
    return jsonify(state)


@app.get("/api/admin/control/history")
@admin
def control_history():
    with connect_db() as db:
        rows = db.execute("SELECT * FROM control_audit ORDER BY id DESC LIMIT 100").fetchall()
    return jsonify(history=[dict(row) for row in rows])


@app.post("/api/device/authorize")
@device
def device_authorize():
    data = request.get_json(silent=True) or {}
    fp = positive_int(data, "fingerprint_id")
    if fp is None:
        return jsonify(error="fingerprint_id must be a positive integer"), 400
    with connect_db() as db:
        state = control_state(db)
        user = find_active_user(db, fp)
        allowed = bool(state["enabled"] and user)
        db.execute("INSERT INTO events(user_id,fingerprint_id,action,success,created_at) VALUES (?,?,'open',?,?)",
                   (user["id"] if user else None, fp, int(allowed), utc_now()))
    return jsonify(allowed=allowed, reason="allowed" if allowed else ("cabinet_disabled" if not state["enabled"] else "user_not_registered"), control=state)


@app.get("/")
def index():
    return send_from_directory(Path(__file__).parent / "templates", "index.html")

@app.get("/status")
def public_status_page():
    return send_from_directory(Path(__file__).parent / "templates", "status.html")

@app.get("/api/public/status")
def public_status_data():
    with connect_db() as db:
        state = control_state(db)
        rows = db.execute("SELECT key_id,key_name,status FROM keys ORDER BY key_id").fetchall()
        history_rows = db.execute("""SELECT t.created_at, k.key_name, t.action
            FROM transactions t JOIN keys k ON k.key_id=t.key_id
            ORDER BY t.id DESC LIMIT 50""").fetchall()
    return jsonify(control={k:state[k] for k in ("label", "service_hours")},
                   keys=[dict(r) for r in rows],
                   history=[dict(r) for r in history_rows])

@app.post("/web/login")
def web_login():
    data = request.get_json(silent=True) or {}
    if not ADMIN_PASSWORD or not secrets.compare_digest(str(data.get("password", "")), ADMIN_PASSWORD):
        return jsonify(error="รหัสผ่านไม่ถูกต้อง"), 401
    session.clear()
    session["admin"] = True
    session["csrf"] = secrets.token_urlsafe(32)
    return jsonify(ok=True, csrf=session["csrf"])

@app.post("/web/logout")
def web_logout():
    session.clear()
    return jsonify(ok=True)

@app.get("/web/session")
def web_session():
    return jsonify(authenticated=bool(session.get("admin")), csrf=session.get("csrf") if session.get("admin") else None)

def web_admin(fn):
    @wraps(fn)
    def inner(*args, **kwargs):
        if not session.get("admin"):
            return jsonify(error="กรุณาเข้าสู่ระบบ Admin"), 401
        if request.method not in ("GET", "HEAD") and not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), session.get("csrf", "")):
            return jsonify(error="CSRF token ไม่ถูกต้อง"), 403
        return fn(*args, **kwargs)
    return inner

@app.get("/web/data")
@web_admin
def web_data():
    with connect_db() as db:
        control = control_state(db)
        users = db.execute("SELECT id,full_name,student_id,phone,fingerprint_id,active FROM users ORDER BY id DESC").fetchall()
        keys = db.execute("SELECT k.key_id,k.key_name,k.status,u.full_name borrower_name FROM keys k LEFT JOIN users u ON u.id=k.borrower_id ORDER BY k.key_id").fetchall()
        logs = db.execute("SELECT t.id,t.key_id,k.key_name,u.full_name,t.action,t.created_at, CASE WHEN t.fingerprint_id=0 THEN 'web-admin' ELSE 'esp32' END source FROM transactions t LEFT JOIN keys k ON k.key_id=t.key_id LEFT JOIN users u ON u.id=t.user_id ORDER BY t.id DESC LIMIT 200").fetchall()
        audit = db.execute("SELECT mode,actor,changed_at FROM control_audit ORDER BY id DESC LIMIT 30").fetchall()
        jobs = db.execute("""SELECT j.id,j.user_id,j.status,j.fingerprint_id,j.message,j.created_at,j.updated_at
            FROM enrollment_jobs j JOIN (SELECT user_id,MAX(id) id FROM enrollment_jobs GROUP BY user_id) latest
            ON latest.id=j.id""").fetchall()
    return jsonify(control=control, users=[dict(x) for x in users],keys=[dict(x) for x in keys],logs=[dict(x) for x in logs],audit=[dict(x) for x in audit],enrollments=[dict(x) for x in jobs])

@app.post("/web/users")
@web_admin
def web_add_user():
    data=request.get_json(silent=True) or {}
    name=str(data.get("full_name", "")).strip()
    student=str(data.get("student_id", "")).strip()
    phone=str(data.get("phone", "")).strip()
    if not (len(phone)==10 and phone.isascii() and phone.isdigit()):
        return jsonify(error="เบอร์โทรศัพท์ไม่ถูกต้อง"),400
    if not name or len(name)>150 or not student or len(student)>50:
        return jsonify(error="กรอกชื่อและรหัสนักศึกษาให้ถูกต้อง"),400
    try:
        with connect_db() as db:
            db.execute("INSERT INTO users(full_name,student_id,phone,created_at) VALUES(?,?,?,?)",(name,student,phone,utc_now()))
    except sqlite3.IntegrityError:
        return jsonify(error="รหัสนักศึกษาซ้ำ"),409
    return jsonify(ok=True),201

@app.patch("/web/users/<int:user_id>")
@web_admin
def web_update_user(user_id):
    data=request.get_json(silent=True) or {}
    if set(data)!={"active"} or type(data["active"]) is not bool:
        return jsonify(error="อนุญาตเฉพาะ active แบบ boolean"),400
    with connect_db() as db:
        cur=db.execute("UPDATE users SET active=? WHERE id=?",(int(data["active"]),user_id))
        if not cur.rowcount:return jsonify(error="ไม่พบผู้ใช้"),404
    return jsonify(ok=True)

@app.post("/web/enrollments")
@web_admin
def web_create_enrollment():
    data = request.get_json(silent=True) or {}
    user_id = positive_int(data, "user_id")
    if user_id is None:
        return jsonify(error="user_id ไม่ถูกต้อง"), 400
    now = utc_now()
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        user = db.execute("SELECT id,full_name,student_id,fingerprint_id,active FROM users WHERE id=?", (user_id,)).fetchone()
        if user is None or not user["active"]:
            return jsonify(error="ไม่พบผู้ใช้หรือบัญชีถูกปิดใช้งาน"), 404
        if user["fingerprint_id"] is not None:
            return jsonify(error="นักศึกษาคนนี้มีลายนิ้วมือแล้ว"), 409
        active_job = db.execute("SELECT id FROM enrollment_jobs WHERE status IN ('pending','in_progress') LIMIT 1").fetchone()
        if active_job:
            return jsonify(error=f"มีงานลงทะเบียนหมายเลข {active_job['id']} กำลังดำเนินการ กรุณารอให้เสร็จก่อน"), 409
        cur = db.execute("INSERT INTO enrollment_jobs(user_id,status,message,created_at,updated_at) VALUES(?,'pending','รอ ESP32 รับคำสั่ง',?,?)", (user_id,now,now))
        job_id = cur.lastrowid
    return jsonify(ok=True, job_id=job_id, status="pending"), 201


@app.get("/web/enrollments/<int:job_id>")
@web_admin
def web_enrollment_status(job_id):
    with connect_db() as db:
        row = db.execute("SELECT id,user_id,status,fingerprint_id,message,created_at,updated_at FROM enrollment_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return jsonify(error="ไม่พบงานลงทะเบียน"), 404
    return jsonify(job=dict(row))


@app.post("/web/enrollments/<int:job_id>/cancel")
@web_admin
def web_cancel_enrollment(job_id):
    now = utc_now()
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT status FROM enrollment_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return jsonify(error="ไม่พบงานลงทะเบียน"), 404
        if row["status"] != "pending":
            return jsonify(error="ยกเลิกได้เฉพาะงานที่ยังรอ ESP32 รับคำสั่ง"), 409
        db.execute("UPDATE enrollment_jobs SET status='cancelled',message='ยกเลิกโดย Admin',updated_at=? WHERE id=?", (now,job_id))
    return jsonify(ok=True,status="cancelled")


@app.get("/api/device/enrollments/next")
@device
def device_next_enrollment():
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        # Recover jobs the ESP32 accepted but never reported (reset, power loss).
        stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
        db.execute(
            "UPDATE enrollment_jobs SET status='failed', message='หมดเวลา ESP32 ไม่ส่งผล', updated_at=? "
            "WHERE status='in_progress' AND updated_at < ?",
            (utc_now(), stale),
        )
        # Only one physical enrollment may run at a time.
        running = db.execute("SELECT id FROM enrollment_jobs WHERE status='in_progress' LIMIT 1").fetchone()
        if running:
            return jsonify(busy=True, job_id=running["id"]), 200
        row = db.execute("""SELECT j.id,j.user_id,u.full_name,u.student_id
            FROM enrollment_jobs j JOIN users u ON u.id=j.user_id
            WHERE j.status='pending' AND u.active=1 ORDER BY j.id LIMIT 1""").fetchone()
        if row is None:
            return ("", 204)
        now = utc_now()
        db.execute("UPDATE enrollment_jobs SET status='in_progress',message='ESP32 รับงานแล้ว กรุณาสแกนนิ้ว',updated_at=? WHERE id=? AND status='pending'", (now,row["id"]))
        used = [r[0] for r in db.execute("SELECT fingerprint_id FROM users WHERE fingerprint_id IS NOT NULL").fetchall()]
    return jsonify(job_id=row["id"],user_id=row["user_id"],full_name=row["full_name"],student_id=row["student_id"],used_fingerprint_ids=used), 200


@app.post("/api/device/enrollments/<int:job_id>/result")
@device
def device_enrollment_result(job_id):
    data = request.get_json(silent=True) or {}
    success = data.get("success")
    if type(success) is not bool:
        return jsonify(error="success ต้องเป็น true หรือ false"), 400
    fp_id = data.get("fingerprint_id")
    message = str(data.get("message", ""))[:250]
    now = utc_now()
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        job = db.execute("SELECT * FROM enrollment_jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            return jsonify(error="ไม่พบงานลงทะเบียน"), 404
        if job["status"] == "success":
            return jsonify(ok=True,status="success",fingerprint_id=job["fingerprint_id"],idempotent=True), 200
        if job["status"] != "in_progress":
            return jsonify(error="งานนี้ไม่ได้อยู่ในสถานะกำลังลงทะเบียน"), 409
        if success:
            if type(fp_id) is not int or not 1 <= fp_id <= 127:
                return jsonify(error="fingerprint_id ต้องอยู่ระหว่าง 1 ถึง 127"), 400
            user = db.execute("SELECT fingerprint_id,active FROM users WHERE id=?", (job["user_id"],)).fetchone()
            if user is None or not user["active"] or user["fingerprint_id"] is not None:
                return jsonify(error="บัญชีไม่พร้อมผูกหมายเลขลายนิ้วมือ"), 409
            duplicate = db.execute("SELECT id FROM users WHERE fingerprint_id=?", (fp_id,)).fetchone()
            if duplicate:
                return jsonify(error="หมายเลข Fingerprint ID นี้ถูกใช้งานแล้ว"), 409
            db.execute("UPDATE users SET fingerprint_id=? WHERE id=?", (fp_id,job["user_id"]))
            db.execute("UPDATE enrollment_jobs SET status='success',fingerprint_id=?,message=?,updated_at=? WHERE id=?", (fp_id,message or "ลงทะเบียนสำเร็จ",now,job_id))
        else:
            db.execute("UPDATE enrollment_jobs SET status='failed',message=?,updated_at=? WHERE id=?", (message or "เซนเซอร์รายงานว่าลงทะเบียนไม่สำเร็จ",now,job_id))
    return jsonify(ok=True,status="success" if success else "failed",fingerprint_id=fp_id if success else None), 200


@app.post("/web/control")
@web_admin
def web_control():
    data=request.get_json(silent=True) or {}
    mode=data.get("mode")
    if mode not in ("auto","special","off"):
        return jsonify(error="โหมดไม่ถูกต้อง"),400
    now=datetime.now(timezone.utc)
    expiry=(now+timedelta(minutes=30)).isoformat(timespec="seconds") if mode=="special" else None
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE cabinet_control SET mode=?,special_until=?,changed_at=? WHERE id=1",(mode,expiry,now.isoformat(timespec="seconds")))
        db.execute("INSERT INTO control_audit(mode,actor,changed_at,special_until) VALUES (?,?,?,?)",(mode,"web-admin",now.isoformat(timespec="seconds"),expiry))
        state=control_state(db)
    return jsonify(control=state)




@app.post("/web/transactions")
@web_admin
def web_transaction():
    """Admin-confirmed web checkout/checkin; never claims fingerprint authentication."""
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    key_id = positive_int(data, "key_id")
    user_id = positive_int(data, "user_id")
    if action not in ("borrow", "return") or key_id is None or key_id > KEY_COUNT or user_id is None:
        return jsonify(error="กรุณาเลือกนักศึกษา ห้อง และรายการยืม/คืน"), 400
    db = connect_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        control = control_state(db)
        if not control["enabled"]:
            db.rollback()
            return jsonify(error="ตู้อยู่นอกเวลาใช้งาน หรือผู้ดูแลปิดระบบ"), 403
        user = db.execute("SELECT * FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
        key = db.execute("SELECT * FROM keys WHERE key_id=?", (key_id,)).fetchone()
        if user is None:
            db.rollback()
            return jsonify(error="ไม่พบผู้ใช้ที่เปิดใช้งาน"), 403
        if key is None:
            db.rollback()
            return jsonify(error="ไม่พบช่องกุญแจ"), 404
        if action == "borrow" and key["status"] != "available":
            db.rollback()
            return jsonify(error="กุญแจถูกยืมอยู่แล้ว"), 409
        if action == "return" and (key["status"] != "borrowed" or key["borrower_id"] != user_id):
            db.rollback()
            return jsonify(error="ต้องเลือกนักศึกษาที่เป็นผู้ยืมกุญแจนี้"), 409
        now = utc_now()
        if action == "borrow":
            db.execute("UPDATE keys SET status='borrowed',borrower_id=?,borrowed_at=?,updated_at=? WHERE key_id=?", (user_id,now,now,key_id))
        else:
            db.execute("UPDATE keys SET status='available',borrower_id=NULL,borrowed_at=NULL,updated_at=? WHERE key_id=?", (now,key_id))
        # 0 is reserved for web/admin transactions; sensor template IDs are positive.
        cur = db.execute("INSERT INTO transactions(key_id,user_id,fingerprint_id,action,created_at) VALUES (?,?,0,?,?)", (key_id,user_id,action,now))
        db.commit()
        return jsonify(success=True,transaction_id=cur.lastrowid,action=action,source="web-admin",physical_unlock=False),201
    except sqlite3.Error:
        db.rollback()
        app.logger.exception("Web transaction failed")
        return jsonify(error="บันทึกข้อมูลไม่สำเร็จ"),500
    finally:
        db.close()

@app.get("/health")
def health():
    return jsonify(status="ok", service="smart-key-box-api")


@app.post("/api/users")
@admin
def register_user():
    data = request.get_json(silent=True) or {}
    name, student_id = data.get("full_name"), data.get("student_id")
    fingerprint_id = data.get("fingerprint_id")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 150:
        return jsonify(error="full_name is required (max 150 characters)"), 400
    if not isinstance(student_id, str) or not student_id.strip() or len(student_id.strip()) > 50:
        return jsonify(error="student_id is required (max 50 characters)"), 400
    if fingerprint_id is not None and (type(fingerprint_id) is not int or fingerprint_id < 1):
        return jsonify(error="fingerprint_id must be a positive integer"), 400
    try:
        with connect_db() as db:
            cur = db.execute(
                """INSERT INTO users
                   (full_name, student_id, fingerprint_id, created_at)
                   VALUES (?, ?, ?, ?)""",
                (name.strip(), student_id.strip(), fingerprint_id, utc_now()),
            )
            row = db.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
        return jsonify(user=as_user(row)), 201
    except sqlite3.IntegrityError:
        return jsonify(error="student_id or fingerprint_id already exists"), 409


@app.get("/api/users")
@admin
def list_users():
    with connect_db() as db:
        rows = db.execute("SELECT * FROM users ORDER BY id DESC").fetchall()
    return jsonify(users=[as_user(r) for r in rows])


@app.patch("/api/users/<int:user_id>")
@admin
def update_user(user_id):
    data = request.get_json(silent=True) or {}
    allowed = {"active", "fingerprint_id"}
    if not data or set(data) - allowed:
        return jsonify(error="Only active and fingerprint_id can be updated"), 400
    if "active" in data and type(data["active"]) is not bool:
        return jsonify(error="active must be true or false"), 400
    if "fingerprint_id" in data and data["fingerprint_id"] is not None:
        value = data["fingerprint_id"]
        if type(value) is not int or value < 1:
            return jsonify(error="fingerprint_id must be a positive integer or null"), 400
    try:
        with connect_db() as db:
            row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                return jsonify(error="User not found"), 404
            active = int(data["active"]) if "active" in data else row["active"]
            fp = data.get("fingerprint_id", row["fingerprint_id"])
            db.execute("UPDATE users SET active = ?, fingerprint_id = ? WHERE id = ?",
                       (active, fp, user_id))
            updated = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return jsonify(user=as_user(updated))
    except sqlite3.IntegrityError:
        return jsonify(error="fingerprint_id already assigned"), 409


@app.post("/api/verify")
@device
def verify_fingerprint():
    data = request.get_json(silent=True) or {}
    fp = positive_int(data, "fingerprint_id")
    if fp is None:
        return jsonify(error="fingerprint_id must be a positive integer"), 400
    with connect_db() as db:
        state = control_state(db)
        user = find_active_user(db, fp)
        db.execute(
            """INSERT INTO events
               (user_id, fingerprint_id, action, success, created_at)
               VALUES (?, ?, 'verify', ?, ?)""",
            (user["id"] if user else None, fp, int(user is not None), utc_now()),
        )
    return jsonify(
        registered=bool(user), user_id=user["id"] if user else None,
        full_name=user["full_name"] if user else None,
        student_id=user["student_id"] if user else None,
        status="registered" if user else "unknown",
        cabinet_enabled=state["enabled"],
    )


@app.get("/api/keys")
@device
def get_keys():
    with connect_db() as db:
        rows = db.execute("""
            SELECT k.key_id, k.key_name, k.status, k.borrower_id,
                   u.full_name AS borrower_name, k.borrowed_at, k.updated_at
            FROM keys k LEFT JOIN users u ON u.id = k.borrower_id
            ORDER BY k.key_id
        """).fetchall()
    return jsonify(keys=[dict(r) for r in rows])


@app.post("/api/borrow")
@device
def borrow_key():
    data = request.get_json(silent=True) or {}
    fp, key_id = positive_int(data, "fingerprint_id"), positive_int(data, "key_id")
    if fp is None or key_id is None or key_id > KEY_COUNT:
        return jsonify(error="fingerprint_id and key_id (1-6) are required"), 400
    now = utc_now()
    db = connect_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        if not control_state(db)["enabled"]:
            db.rollback()
            return jsonify(error="Cabinet is outside permitted hours or disabled by admin"), 403
        user = find_active_user(db, fp)
        if user is None:
            db.rollback()
            return jsonify(error="Fingerprint is not registered or user inactive"), 403
        key = db.execute("SELECT * FROM keys WHERE key_id = ?", (key_id,)).fetchone()
        if key is None:
            db.rollback()
            return jsonify(error="Key not found"), 404
        if key["status"] != "available":
            db.rollback()
            return jsonify(error="Key is already borrowed"), 409
        cur_update = db.execute(
            """UPDATE keys SET status = 'borrowed', borrower_id = ?,
               borrowed_at = ?, updated_at = ?
               WHERE key_id = ? AND status = 'available'""",
            (user["id"], now, now, key_id),
        )
        if cur_update.rowcount != 1:
            db.rollback()
            return jsonify(error="Key is already borrowed"), 409
        cur = db.execute(
            """INSERT INTO transactions
               (key_id, user_id, fingerprint_id, action, created_at)
               VALUES (?, ?, ?, 'borrow', ?)""",
            (key_id, user["id"], fp, now),
        )
        db.commit()
        return jsonify(
            success=True, action="borrow", transaction_id=cur.lastrowid,
            key_id=key_id, user_id=user["id"], full_name=user["full_name"],
            borrowed_at=now,
            message="Borrow recorded; device may now perform its local unlock sequence.",
        ), 201
    except sqlite3.Error:
        db.rollback()
        app.logger.exception("Borrow database error")
        return jsonify(error="Database error"), 500
    finally:
        db.close()


@app.post("/api/return")
@device
def return_key():
    data = request.get_json(silent=True) or {}
    fp, key_id = positive_int(data, "fingerprint_id"), positive_int(data, "key_id")
    if fp is None or key_id is None or key_id > KEY_COUNT:
        return jsonify(error="fingerprint_id and key_id (1-6) are required"), 400
    now = utc_now()
    db = connect_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        if not control_state(db)["enabled"]:
            db.rollback()
            return jsonify(error="Cabinet is outside permitted hours or disabled by admin"), 403
        user = find_active_user(db, fp)
        if user is None:
            db.rollback()
            return jsonify(error="Fingerprint is not registered or user inactive"), 403
        key = db.execute("SELECT * FROM keys WHERE key_id = ?", (key_id,)).fetchone()
        if key is None:
            db.rollback()
            return jsonify(error="Key not found"), 404
        if key["status"] != "borrowed":
            db.rollback()
            return jsonify(error="Key is not currently borrowed"), 409
        if key["borrower_id"] != user["id"]:
            db.rollback()
            return jsonify(error="Only the borrowing user can return this key"), 403
        db.execute(
            """UPDATE keys SET status = 'available', borrower_id = NULL,
               borrowed_at = NULL, updated_at = ?
               WHERE key_id = ? AND status = 'borrowed'""",
            (now, key_id),
        )
        cur = db.execute(
            """INSERT INTO transactions
               (key_id, user_id, fingerprint_id, action, created_at)
               VALUES (?, ?, ?, 'return', ?)""",
            (key_id, user["id"], fp, now),
        )
        db.commit()
        return jsonify(
            success=True, action="return", transaction_id=cur.lastrowid,
            key_id=key_id, user_id=user["id"], full_name=user["full_name"],
            returned_at=now,
            message="Return recorded from user confirmation; no physical return sensor is installed.",
        ), 201
    except sqlite3.Error:
        db.rollback()
        app.logger.exception("Return database error")
        return jsonify(error="Database error"), 500
    finally:
        db.close()


@app.get("/api/transactions")
@admin
def transactions():
    try:
        limit = min(max(int(request.args.get("limit", "50")), 1), 200)
    except ValueError:
        return jsonify(error="limit must be an integer"), 400
    with connect_db() as db:
        rows = db.execute("""
            SELECT t.id, t.key_id, k.key_name, t.user_id, u.full_name,
                   u.student_id, t.fingerprint_id, t.action, t.created_at
            FROM transactions t
            LEFT JOIN keys k ON k.key_id = t.key_id
            LEFT JOIN users u ON u.id = t.user_id
            ORDER BY t.id DESC LIMIT ?
        """, (limit,)).fetchall()
    return jsonify(transactions=[dict(r) for r in rows])


@app.get("/api/status")
@admin
def status():
    with connect_db() as db:
        users = db.execute(
            "SELECT COUNT(*) total, COALESCE(SUM(active),0) active FROM users"
        ).fetchone()
        events = db.execute(
            "SELECT COUNT(*) total, COALESCE(SUM(success),0) successful FROM events"
        ).fetchone()
        keys = db.execute("""
            SELECT COUNT(*) total,
              COALESCE(SUM(CASE WHEN status='available' THEN 1 ELSE 0 END),0) available,
              COALESCE(SUM(CASE WHEN status='borrowed' THEN 1 ELSE 0 END),0) borrowed
            FROM keys
        """).fetchone()
        tx_total = db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    return jsonify(
        users_total=users["total"], users_active=users["active"],
        events_total=events["total"], successful_events=events["successful"],
        keys_total=keys["total"], keys_available=keys["available"],
        keys_borrowed=keys["borrowed"], transactions_total=tx_total,
    )


@app.get("/api/history")
@admin
def history():
    try:
        limit = min(max(int(request.args.get("limit", "50")), 1), 200)
    except ValueError:
        return jsonify(error="limit must be an integer"), 400
    with connect_db() as db:
        rows = db.execute("""
            SELECT e.id, e.fingerprint_id, e.action, e.success, e.created_at,
                   u.id AS user_id, u.full_name, u.student_id
            FROM events e LEFT JOIN users u ON u.id = e.user_id
            ORDER BY e.id DESC LIMIT ?
        """, (limit,)).fetchall()
    return jsonify(history=[dict(r) for r in rows])


@app.errorhandler(404)
def not_found(_error):
    return jsonify(error="Endpoint not found"), 404


@app.errorhandler(405)
def method_not_allowed(_error):
    return jsonify(error="Method not allowed"), 405


if not all((ADMIN_API_KEY, DEVICE_API_KEY, ADMIN_PASSWORD, app.secret_key)):
    raise RuntimeError("Missing ADMIN_API_KEY, DEVICE_API_KEY, ADMIN_PASSWORD or SECRET_KEY")
if ADMIN_API_KEY == DEVICE_API_KEY:
    raise RuntimeError("ADMIN_API_KEY and DEVICE_API_KEY must differ")
init_db()

if __name__ == "__main__":
    if not all((ADMIN_API_KEY, DEVICE_API_KEY, ADMIN_PASSWORD, app.secret_key)):
        raise SystemExit("Set ADMIN_API_KEY, DEVICE_API_KEY, ADMIN_PASSWORD and SECRET_KEY")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
