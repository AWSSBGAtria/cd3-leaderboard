"""
CD3 Leaderboard Hub — local-first multi-leaderboard manager.
Stack: Flask + SQLite (stdlib) + ReportLab (PDF) + Tailwind CDN + Lucide SVG.
Run:  python app.py   ->  http://127.0.0.1:5000
"""
import sqlite3, json, os, re, time
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, request, jsonify, session, redirect, url_for,
                   render_template, send_file, g)
from werkzeug.security import generate_password_hash, check_password_hash
from io import BytesIO

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH") or os.path.join(BASE_DIR, "leaderboard.db")
DEFAULT_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cd3-local-leaderboard-secret-change-me")
app.config["JSON_SORT_KEYS"] = False

# ---------------------------------------------------------------- DB helpers
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db

@app.teardown_appcontext
def close_db(_e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        icon TEXT NOT NULL DEFAULT 'trophy',
        score_label TEXT NOT NULL DEFAULT 'Score',
        unit TEXT NOT NULL DEFAULT 'pts',
        sort_dir TEXT NOT NULL DEFAULT 'DESC' CHECK (sort_dir IN ('DESC','ASC')),
        description TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        last_reset_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        player_name TEXT NOT NULL,
        score REAL NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_entries_event_score ON entries(event_id, score);
    CREATE TABLE IF NOT EXISTS metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        label TEXT NOT NULL DEFAULT 'Score',
        unit TEXT NOT NULL DEFAULT 'pts',
        sort_dir TEXT NOT NULL DEFAULT 'DESC' CHECK (sort_dir IN ('DESC','ASC')),
        position INTEGER NOT NULL DEFAULT 0
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_metrics_event_pos ON metrics(event_id, position);
    CREATE TABLE IF NOT EXISTS snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER NOT NULL,
        event_name TEXT NOT NULL,
        score_label TEXT NOT NULL DEFAULT 'Score',
        unit TEXT NOT NULL DEFAULT 'pts',
        taken_at TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT 'manual',
        entry_count INTEGER NOT NULL DEFAULT 0,
        data_json TEXT NOT NULL DEFAULT '[]'
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS registrations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        usn_norm TEXT NOT NULL DEFAULT '',
        phone_norm TEXT NOT NULL DEFAULT '',
        player_name TEXT NOT NULL DEFAULT '',
        first_seen TEXT NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_reg_event_usn ON registrations(event_id, usn_norm);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_reg_event_phone ON registrations(event_id, phone_norm) WHERE phone_norm <> '';
    """)
    # lightweight migration for DBs created before usn/phone/values existed
    for _col in ("usn TEXT NOT NULL DEFAULT ''", "phone TEXT NOT NULL DEFAULT ''",
                 "values_json TEXT NOT NULL DEFAULT '{}'"):
        try:
            db.execute(f"ALTER TABLE entries ADD COLUMN {_col}")
        except sqlite3.OperationalError:
            pass
    try:
        db.execute("ALTER TABLE snapshots ADD COLUMN metrics_json TEXT NOT NULL DEFAULT '[]'")
    except sqlite3.OperationalError:
        pass
    db.commit()
    # self-heal: drop entries/snapshots orphaned by any external edit
    try:
        db.execute("DELETE FROM entries WHERE event_id NOT IN (SELECT id FROM events)")
        db.commit()
    except sqlite3.OperationalError:
        pass
    # backfill: every event gets at least one metric (from its legacy columns);
    # entries without values inherit their legacy score under that metric.
    try:
        for _ev in db.execute("SELECT * FROM events").fetchall():
            db.execute("""INSERT OR IGNORE INTO metrics(event_id,label,unit,sort_dir,position)
                          VALUES(?,?,?,?,0)""",
                       (_ev["id"], _ev["score_label"] or "Score", _ev["unit"] or "pts",
                        _ev["sort_dir"] if _ev["sort_dir"] in ("ASC", "DESC") else "DESC"))
            _mid = db.execute("SELECT id FROM metrics WHERE event_id=? ORDER BY position LIMIT 1",
                              (_ev["id"],)).fetchone()
            if _mid:
                db.execute("""UPDATE entries SET values_json = json_object(?, score)
                              WHERE event_id=? AND (values_json IS NULL OR values_json='{}')""",
                           (str(_mid["id"]), _ev["id"]))
        db.commit()
    except sqlite3.OperationalError:
        pass
    db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('auto_reset_enabled','0')")
    db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('auto_reset_hours','1')")
    backfill_registrations(db)
    cur = db.execute("SELECT value FROM settings WHERE key='admin_password_hash'")
    if not cur.fetchone():
        try:
            db.execute("INSERT INTO settings(key,value) VALUES('admin_password_hash',?)",
                       (generate_password_hash(DEFAULT_PASSWORD),))
        except sqlite3.IntegrityError:
            pass  # concurrent worker won the first-boot race
        db.commit()
    db.close()

def get_setting(key, default=""):
    db = get_db()
    cur = db.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    return row["value"] if row else default

def set_setting(key, value):
    db = get_db()
    db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, value))
    db.commit()

def now_iso():
    return datetime.now().isoformat(timespec="seconds")

def norm_usn(usn):
    """Canonical USN: uppercase, whitespace removed."""
    return re.sub(r"\s+", "", (usn or "").upper())

def norm_phone(phone):
    """Canonical phone: digits only."""
    return re.sub(r"\D", "", phone or "")

def backfill_registrations(db):
    """Record every participant ever seen (live entries + archived snapshots)
    so the one-entry-per-person rule survives resets and restarts."""
    try:
        for r in db.execute("SELECT event_id, player_name, usn, phone, created_at FROM entries").fetchall():
            u, p = norm_usn(r["usn"]), norm_phone(r["phone"])
            if not u and not p:
                continue
            db.execute("""INSERT OR IGNORE INTO registrations(event_id,usn_norm,phone_norm,player_name,first_seen)
                          VALUES(?,?,?,?,?)""",
                       (r["event_id"], u, p, r["player_name"] or "", r["created_at"] or now_iso()))
        for s in db.execute("SELECT event_id, data_json, taken_at FROM snapshots").fetchall():
            try:
                data = json.loads(s["data_json"] or "[]")
            except Exception:
                continue
            if not isinstance(data, list):
                continue
            for e in data:
                if not isinstance(e, dict):
                    continue
                u, p = norm_usn(e.get("usn")), norm_phone(e.get("phone"))
                if not u and not p:
                    continue
                db.execute("""INSERT OR IGNORE INTO registrations(event_id,usn_norm,phone_norm,player_name,first_seen)
                              VALUES(?,?,?,?,?)""",
                           (s["event_id"], u, p, e.get("player_name") or "", s["taken_at"]))
        db.commit()
    except sqlite3.OperationalError:
        pass

# ------------------------------------------------------- metrics helpers
def get_metrics(db, ev):
    """Ordered metric defs for an event; falls back to legacy columns."""
    rows = db.execute("SELECT id, event_id, label, unit, sort_dir, position FROM metrics WHERE event_id=? ORDER BY position",
                      (ev["id"],)).fetchall()
    ms = [dict(r) for r in rows]
    if not ms:
        ms = [{"id": 0, "event_id": ev["id"], "label": ev["score_label"] or "Score",
               "unit": ev["unit"] or "pts",
               "sort_dir": ev["sort_dir"] if ev["sort_dir"] in ("ASC", "DESC") else "DESC",
               "position": 0}]
    return ms

def order_sql(metrics):
    """ORDER BY fragment: metric 1, then 2, ... each in its own direction. Missing values sort last."""
    parts = []
    for m in metrics:
        d = m.get("sort_dir") if m.get("sort_dir") in ("ASC", "DESC") else "DESC"
        try:
            mid = int(m["id"])
        except (ValueError, TypeError):
            continue
        fallback = "1e18" if d == "ASC" else "-1e18"
        parts.append(f"COALESCE(CAST(json_extract(values_json, '$.\"{mid}\"') AS REAL), {fallback}) {d}")
    parts.append("created_at ASC")
    return ", ".join(parts)

def parse_values(row):
    try:
        v = json.loads(row["values_json"] or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}

def primary_value(vals, metrics, legacy):
    if metrics:
        v = vals.get(str(metrics[0]["id"]))
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
    return legacy

def entry_public(row, metrics):
    vals = parse_values(row)
    return {"id": row["id"], "player_name": row["player_name"], "usn": row["usn"],
            "phone": row["phone"], "score": primary_value(vals, metrics, row["score"]),
            "values": vals, "created_at": row["created_at"]}

def archive_event(db, ev, metrics, reason):
    """Snapshot an event's ordered entries (with values + metric defs). Returns archived count."""
    rows = db.execute(f"SELECT player_name, usn, phone, score, values_json, created_at FROM entries WHERE event_id=? ORDER BY {order_sql(metrics)}",
                      (ev["id"],)).fetchall()
    data = []
    for r in rows:
        d = dict(r)
        d["values"] = parse_values(r)
        d.pop("values_json", None)
        data.append(d)
    db.execute("""INSERT INTO snapshots(event_id,event_name,score_label,unit,metrics_json,taken_at,reason,entry_count,data_json)
                  VALUES(?,?,?,?,?,?,?,?,?)""",
               (ev["id"], ev["name"], ev["score_label"], ev["unit"], json.dumps(metrics),
                now_iso(), reason, len(data), json.dumps(data)))
    return len(data)

# ------------------------------------------------------- auto hourly reset
def maybe_auto_reset():
    """If hourly auto-reset is enabled, snapshot+clear stale events."""
    try:
        enabled = get_setting("auto_reset_enabled", "0") == "1"
        if not enabled:
            return []
        hours = float(get_setting("auto_reset_hours", "1") or "1")
        if hours <= 0:
            return []
        db = get_db()
        cutoff = datetime.now() - timedelta(hours=hours)
        reset_done = []
        for ev in db.execute("SELECT * FROM events").fetchall():
            try:
                last = datetime.fromisoformat(ev["last_reset_at"])
            except Exception:
                last = datetime.now()
            if last <= cutoff:
                metrics = get_metrics(db, ev)
                n = archive_event(db, dict(ev), metrics, f"auto-{hours:g}h")
                db.execute("DELETE FROM entries WHERE event_id=?", (ev["id"],))
                db.execute("UPDATE events SET last_reset_at=? WHERE id=?", (now_iso(), ev["id"]))
                reset_done.append(ev["name"])
        if reset_done:
            db.commit()
        return reset_done
    except Exception:
        return []

@app.before_request
def _auto():
    if request.path.startswith("/static"):
        return
    try:
        maybe_auto_reset()
    except Exception:
        pass

# ---------------------------------------------------------------- auth
def admin_required_api(f):
    @wraps(f)
    def w(*a, **kw):
        if not session.get("is_admin"):
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        return f(*a, **kw)
    return w

def admin_required_page(f):
    @wraps(f)
    def w(*a, **kw):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return f(*a, **kw)
    return w

# ---------------------------------------------------------------- pages
@app.route("/")
def viewer():
    return render_template("viewer.html")

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.is_json:
            pw = (request.get_json(silent=True) or {}).get("password", "")
        else:
            pw = request.form.get("password", "")
        db = get_db()
        cur = db.execute("SELECT value FROM settings WHERE key='admin_password_hash'")
        row = cur.fetchone()
        if row and check_password_hash(row["value"], pw or ""):
            session["is_admin"] = True
            if request.is_json:
                return jsonify({"ok": True})
            return redirect(url_for("admin_dash"))
        if request.is_json:
            return jsonify({"ok": False, "error": "Incorrect password"}), 401
        return render_template("admin_login.html", error="Incorrect password"), 401
    if session.get("is_admin"):
        return redirect(url_for("admin_dash"))
    return render_template("admin_login.html", error=None)

@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("viewer"))

@app.route("/admin")
@admin_required_page
def admin_dash():
    return render_template("admin.html")

# ---------------------------------------------------------------- public API
@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})

@app.route("/api/meta")
def api_meta():
    db = get_db()
    n_events = db.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    n_entries = db.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"]
    n_players = db.execute("SELECT COUNT(DISTINCT player_name) c FROM entries").fetchone()["c"]
    return jsonify({"ok": True, "events": n_events, "entries": n_entries, "players": n_players})

@app.route("/api/events")
def api_events():
    db = get_db()
    rows = db.execute("SELECT * FROM events ORDER BY created_at ASC").fetchall()
    out = []
    for r in rows:
        metrics = get_metrics(db, r)
        cnt = db.execute("SELECT COUNT(*) c FROM entries WHERE event_id=?", (r["id"],)).fetchone()["c"]
        top = db.execute(f"SELECT MAX(score) mx, MIN(score) mn FROM entries WHERE event_id=?", (r["id"],)).fetchone()
        d = dict(r)
        d["metrics"] = metrics
        d["entry_count"] = cnt
        d["best"] = top["mx"]
        d["worst"] = top["mn"]
        out.append(d)
    return jsonify({"ok": True, "events": out})

@app.route("/api/unified")
def api_unified():
    """All leaderboards, top N each (default 10), ordered per metric config."""
    limit = max(1, min(25, int(request.args.get("limit", 10))))
    db = get_db()
    events = db.execute("SELECT * FROM events ORDER BY created_at ASC").fetchall()
    boards = []
    for ev in events:
        metrics = get_metrics(db, ev)
        rows = db.execute(
            f"SELECT id, player_name, usn, phone, score, values_json, created_at FROM entries WHERE event_id=? ORDER BY {order_sql(metrics)} LIMIT ?",
            (ev["id"], limit)).fetchall()
        evd = dict(ev)
        evd["metrics"] = metrics
        boards.append({"event": evd, "top": [entry_public(r, metrics) for r in rows]})
    return jsonify({"ok": True, "boards": boards})

@app.route("/api/leaderboard")
def api_leaderboard():
    try:
        event_id = int(request.args.get("event_id", 0))
    except ValueError:
        return jsonify({"ok": False, "error": "bad event"}), 400
    page = max(1, int(request.args.get("page", 1)))
    per = max(1, min(500, int(request.args.get("per_page", 10))))
    q = (request.args.get("q") or "").strip()
    db = get_db()
    ev = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not ev:
        return jsonify({"ok": False, "error": "event not found"}), 404
    metrics = get_metrics(db, ev)
    where, params = "event_id=?", [event_id]
    if q:
        where += " AND player_name LIKE ?"
        params.append(f"%{q}%")
    total = db.execute(f"SELECT COUNT(*) c FROM entries WHERE {where}", params).fetchone()["c"]
    pages = max(1, (total + per - 1) // per)
    page = min(page, pages)
    rows = db.execute(
        f"SELECT id, player_name, usn, phone, score, values_json, created_at FROM entries WHERE {where} ORDER BY {order_sql(metrics)} LIMIT ? OFFSET ?",
        (*params, per, (page - 1) * per)).fetchall()
    evd = dict(ev)
    evd["metrics"] = metrics
    return jsonify({"ok": True, "event": evd, "rows": [entry_public(r, metrics) for r in rows],
                    "page": page, "per_page": per, "total": total, "pages": pages})

def _clean_metric(m, pos):
    """Validate one metric dict from the client. Returns normalized dict or raises ValueError."""
    label = str(m.get("label", "")).strip()[:30]
    if not label:
        raise ValueError("Each metric needs a name.")
    unit = str(m.get("unit", "")).strip()[:15] or "pts"
    sdir = str(m.get("sort_dir", "DESC")).upper()
    if sdir not in ("ASC", "DESC"):
        sdir = "DESC"
    out = {"label": label, "unit": unit, "sort_dir": sdir, "position": pos}
    if m.get("id") is not None:
        try:
            out["id"] = int(m["id"])
        except (ValueError, TypeError):
            pass
    return out

def _save_event_metrics(db, event_id, raw_metrics):
    """Replace an event's metric set (1-3), preserving ids sent by the client. Returns final defs."""
    if not isinstance(raw_metrics, list) or not (1 <= len(raw_metrics) <= 3):
        raise ValueError("An event needs 1 to 3 metrics.")
    cleaned = [_clean_metric(m, i) for i, m in enumerate(raw_metrics)]
    owned = {r["id"] for r in db.execute("SELECT id FROM metrics WHERE event_id=?", (event_id,))}
    keep = {c["id"] for c in cleaned if c.get("id") in owned}
    # remove dropped metrics first so their positions free up
    if owned - keep:
        db.execute(f"DELETE FROM metrics WHERE event_id=? AND id IN ({','.join('?'*len(owned-keep))})",
                   (event_id, *(owned-keep)))
    # park kept rows at negative positions so reorders never collide mid-write
    for c in cleaned:
        if c.get("id") in keep:
            db.execute("UPDATE metrics SET position=? WHERE id=?", (-1000 - c["position"], c["id"]))
    for c in cleaned:
        if c.get("id") in keep:
            db.execute("UPDATE metrics SET label=?, unit=?, sort_dir=?, position=? WHERE id=?",
                       (c["label"], c["unit"], c["sort_dir"], c["position"], c["id"]))
        else:
            c["id"] = db.execute("INSERT INTO metrics(event_id,label,unit,sort_dir,position) VALUES(?,?,?,?,?)",
                                 (event_id, c["label"], c["unit"], c["sort_dir"], c["position"])).lastrowid
    # keep legacy single-metric columns in sync with the primary metric
    db.execute("UPDATE events SET score_label=?, unit=?, sort_dir=? WHERE id=?",
               (cleaned[0]["label"], cleaned[0]["unit"], cleaned[0]["sort_dir"], event_id))
    return cleaned

@app.route("/api/register", methods=["POST"])
def api_register():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("player_name") or "").strip()
    usn = (data.get("usn") or "").strip()
    phone = (data.get("phone") or "").strip()
    try:
        event_id = int(data.get("event_id") or 0)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Pick a valid event."}), 400
    if not name or len(name) > 60:
        return jsonify({"ok": False, "error": "Please enter the participant name (1–60 chars)."}), 400
    if not usn or len(usn) > 30:
        return jsonify({"ok": False, "error": "USN is required (max 30 chars)."}), 400
    if len(phone) > 20:
        return jsonify({"ok": False, "error": "Phone number is too long."}), 400
    db = get_db()
    ev = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not ev:
        return jsonify({"ok": False, "error": "Pick a valid event."}), 400
    metrics = get_metrics(db, ev)
    # values per metric id; legacy single `score` fills a one-metric event
    raw = data.get("values") or {}
    if not isinstance(raw, dict):
        return jsonify({"ok": False, "error": "Scores must be sent per metric."}), 400
    if data.get("score") is not None and len(metrics) == 1 and str(metrics[0]["id"]) not in raw:
        raw = {**raw, str(metrics[0]["id"]): data.get("score")}
    values = {}
    for m in metrics:
        try:
            v = float(raw.get(str(m["id"])))
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": f"Enter a valid number for {m['label']}."}), 400
        values[str(m["id"])] = v
    primary = values[str(metrics[0]["id"])]
    # one entry per person per leaderboard, forever: USN or phone match blocks,
    # even after resets (registry table outlives entries). Same person may join
    # *other* events freely — the check is scoped to this event_id.
    u_norm, p_norm = norm_usn(usn), norm_phone(phone)
    dup = db.execute("""SELECT player_name FROM registrations
                        WHERE event_id=? AND (usn_norm=? OR (? <> '' AND phone_norm=?))""",
                     (event_id, u_norm, p_norm, p_norm)).fetchone()
    if dup:
        return jsonify({"ok": False, "error": f"{dup['player_name'] or 'This participant'} (USN {usn}) is already on this leaderboard — one entry per person."}), 409
    cur = db.execute("INSERT INTO entries(event_id,player_name,usn,phone,score,values_json,created_at) VALUES(?,?,?,?,?,?,?)",
                     (event_id, name, usn, phone, primary, json.dumps(values), now_iso()))
    new_id = cur.lastrowid
    try:
        db.execute("""INSERT INTO registrations(event_id,usn_norm,phone_norm,player_name,first_seen)
                      VALUES(?,?,?,?,?)""", (event_id, u_norm, p_norm, name, now_iso()))
    except sqlite3.IntegrityError:
        db.rollback()
        return jsonify({"ok": False, "error": "This USN or phone number is already on this leaderboard — one entry per person."}), 409
    db.commit()
    rank = db.execute(f"""SELECT rnk FROM (SELECT id, ROW_NUMBER() OVER (ORDER BY {order_sql(metrics)}) AS rnk
                        FROM entries WHERE event_id=?) WHERE id=?""", (event_id, new_id)).fetchone()
    return jsonify({"ok": True, "rank": rank["rnk"] if rank else 1,
                    "score_label": metrics[0]["label"], "unit": metrics[0]["unit"]})

# ---------------------------------------------------------------- export (public current)
def _fetch_board(event_id=None):
    db = get_db()
    if event_id:
        events = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchall()
    else:
        events = db.execute("SELECT * FROM events ORDER BY created_at ASC").fetchall()
    boards = []
    for ev in events:
        metrics = get_metrics(db, ev)
        rows = db.execute(f"SELECT id, player_name, usn, phone, score, values_json, created_at FROM entries WHERE event_id=? ORDER BY {order_sql(metrics)}",
                          (ev["id"],)).fetchall()
        evd = dict(ev)
        evd["metrics"] = metrics
        boards.append((evd, [entry_public(r, metrics) for r in rows]))
    return boards

@app.route("/export/json")
def export_json():
    event_id = request.args.get("event_id", type=int)
    boards = _fetch_board(event_id)
    payload = {"exported_at": now_iso(), "leaderboards": [
        {"event": dict(ev), "entries": rows} for ev, rows in boards]}
    buf = BytesIO(json.dumps(payload, indent=2).encode())
    fname = f"leaderboard-{'all' if not event_id else event_id}-{datetime.now().strftime('%Y%m%d-%H%M')}.json"
    return send_file(buf, mimetype="application/json", as_attachment=True, download_name=fname)

def build_pdf(boards, title="CD3 Leaderboard Report"):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                    TableStyle, HRFlowable, KeepTogether)
    from reportlab.lib.enums import TA_CENTER
    buf = BytesIO()
    ACCENT = colors.HexColor("#f59e0b")
    INK = colors.HexColor("#111827")
    SUBTLE = colors.HexColor("#6b7280")
    BG = colors.HexColor("#f8fafc")
    GOLD, SILVER, BRONZE = colors.HexColor("#fef3c7"), colors.HexColor("#f1f5f9"), colors.HexColor("#ffedd5")

    def _metrics(evd):
        ms = evd.get("metrics") or [{"id": 0, "label": evd.get("score_label", "Score"),
                                     "unit": evd.get("unit", "pts"), "sort_dir": "DESC"}]
        return ms[:3]

    def _val(row, mid):
        vals = row.get("values") or {}
        v = vals.get(str(mid))
        if v is None and mid == 0:
            v = row.get("score")
        return "-" if v is None else (int(v) if isinstance(v, float) and v.is_integer() else v)

    max_m = max([len(_metrics(dict(ev))) for ev, _ in boards] or [1])
    page = landscape(A4) if max_m > 2 else A4
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=18*mm, rightMargin=18*mm,
                            topMargin=16*mm, bottomMargin=16*mm,
                            title=title, author="CD3 Leaderboard Hub")
    # column widths per metric count (rank, player, usn, metrics..., date)
    widths = {1: [26, 168, 88, 100, 106],
              2: [24, 148, 76, 64, 64, 98],
              3: [28, 180, 84, 80, 80, 80, 104]}[max_m]
    styles = getSampleStyleSheet()
    s_title = ParagraphStyle("Title2", parent=styles["Title"], fontSize=26, leading=30,
                             textColor=INK, spaceAfter=2, fontName="Helvetica-Bold")
    s_sub = ParagraphStyle("Sub", parent=styles["Normal"], fontSize=10.5, leading=15,
                           textColor=SUBTLE, fontName="Helvetica")
    s_h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=14, leading=17,
                          textColor=INK, fontName="Helvetica-Bold", spaceBefore=14, spaceAfter=4)
    s_meta = ParagraphStyle("Meta", parent=styles["Normal"], fontSize=9, leading=12,
                            textColor=SUBTLE, fontName="Helvetica")
    s_cell = ParagraphStyle("Cell", parent=styles["Normal"], fontSize=9.5, leading=12.5,
                            textColor=INK, fontName="Helvetica")
    s_cell_b = ParagraphStyle("CellB", parent=s_cell, fontName="Helvetica-Bold")
    s_small_c = ParagraphStyle("SmallC", parent=styles["Normal"], fontSize=8.5, leading=11,
                               textColor=SUBTLE, alignment=TA_CENTER, fontName="Helvetica")
    story = []
    story.append(Paragraph(title, s_title))
    story.append(Paragraph(f"Generated {datetime.now().strftime('%d %b %Y · %I:%M %p')} &nbsp;·&nbsp; {sum(len(r) for _, r in boards)} total entries across {len(boards)} leaderboard(s)", s_sub))
    story.append(Spacer(1, 4))
    story.append(HRFlowable(width="100%", thickness=2.2, color=ACCENT, spaceAfter=6))
    if not boards:
        story.append(Paragraph("No leaderboards yet. Create an event in the admin panel to get started.", s_sub))
    for ev, rows in boards:
        evd = dict(ev)
        ms = _metrics(evd)
        spec = " + ".join(f"{m['label']} in {m['unit']} ({'lowest' if m.get('sort_dir') == 'ASC' else 'highest'} wins)"
                          for m in ms)
        header = f"{evd.get('name','Event')} &nbsp;<font color=\"#92400e\" size=\"9\">[{spec}]</font>"
        story.append(Paragraph(header, s_h2))
        story.append(Paragraph(f"{len(rows)} entries" + (f" · Top: <b>{rows[0]['player_name']}</b>" if rows else " · No scores yet"), s_meta))
        story.append(Spacer(1, 4))
        head = [Paragraph("<b>#</b>", s_small_c), Paragraph("<b>PLAYER</b>", s_cell_b),
                Paragraph("<b>USN</b>", s_cell_b)]
        for m in ms:
            head.append(Paragraph(f"<b>{(m['label'] or '').upper()} ({m['unit']})</b>", s_cell_b))
        head.append(Paragraph("<b>DATE</b>", s_small_c))
        data = [head]
        if not rows:
            data.append(["-"] + [Paragraph("No entries yet. Be the first to score.", s_cell)] + ["-"] * (len(ms) + 1))
        for i, r in enumerate(rows[:100], 1):
            try:
                dt = datetime.fromisoformat(r["created_at"]).strftime("%d %b, %H:%M")
            except Exception:
                dt = str(r.get("created_at", ""))[:16]
            line = [Paragraph(f"<b>{i}</b>", s_small_c),
                    Paragraph(str(r["player_name"])[:40], s_cell),
                    Paragraph(str(r.get("usn") or "-")[:20], s_cell)]
            for m in ms:
                line.append(Paragraph(f"<b>{_val(r, m['id'])}</b>", s_cell))
            line.append(Paragraph(dt, s_small_c))
            data.append(line)
        t = Table(data, colWidths=widths[:len(head)], repeatRows=1)
        style = [("BACKGROUND", (0, 0), (-1, 0), INK),
                 ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                 ("FONTSIZE", (0, 0), (-1, -1), 9.5),
                 ("ALIGN", (0, 0), (0, -1), "CENTER"),
                 ("ALIGN", (3, 1), (-1, -1), "CENTER"),
                 ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                 ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#e5e7eb")),
                 ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BG]),
                 ("TOPPADDING", (0, 0), (-1, -1), 5),
                 ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                 ("LEFTPADDING", (0, 0), (-1, -1), 6),
                 ("RIGHTPADDING", (0, 0), (-1, -1), 6)]
        # highlight podiums
        for i in range(min(3, len(rows))):
            bg = [GOLD, SILVER, BRONZE][i]
            style.append(("BACKGROUND", (0, i + 1), (-1, i + 1), bg))
        t.setStyle(TableStyle(style))
        story.append(KeepTogether(t) if len(rows) <= 12 else t)
        story.append(Spacer(1, 2))
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=0.7, color=colors.HexColor("#e5e7eb")))
    story.append(Paragraph("CD3 Leaderboard Hub · Local-first SQLite build · Exported from the viewer / admin panel", s_small_c))
    def _foot(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(SUBTLE)
        canvas.drawCentredString(page[0] / 2, 12*mm, f"Page {doc.page}  ·  {title}")
        canvas.restoreState()
    doc.build(story, onFirstPage=_foot, onLaterPages=_foot)
    buf.seek(0)
    return buf

@app.route("/export/pdf")
def export_pdf():
    event_id = request.args.get("event_id", type=int)
    boards = _fetch_board(event_id)
    scope = "all-boards" if not event_id else f"event-{event_id}"
    pdf = build_pdf(boards, title="CD3 Leaderboard Report" if not event_id else f"{boards[0][0]['name']} Leaderboard" if boards else "Leaderboard")
    fname = f"leaderboard-{scope}-{datetime.now().strftime('%Y%m%d-%H%M')}.pdf"
    return send_file(pdf, mimetype="application/pdf", as_attachment=True, download_name=fname)

# ---------------------------------------------------------------- admin API
@app.route("/admin/api/events", methods=["GET", "POST"])
@admin_required_api
def admin_events():
    db = get_db()
    if request.method == "GET":
        rows = db.execute("SELECT * FROM events ORDER BY created_at ASC").fetchall()
        out = []
        for r in rows:
            cnt = db.execute("SELECT COUNT(*) c FROM entries WHERE event_id=?", (r["id"],)).fetchone()["c"]
            d = dict(r)
            d["metrics"] = get_metrics(db, r)
            d["entry_count"] = cnt
            out.append(d)
        auto = {"enabled": get_setting("auto_reset_enabled", "0") == "1",
                "hours": get_setting("auto_reset_hours", "1")}
        return jsonify({"ok": True, "events": out, "auto_reset": auto})
    d = request.get_json(force=True, silent=True) or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Event name is required."}), 400
    icon = (d.get("icon") or "trophy").strip()[:40]
    desc = (d.get("description") or "").strip()[:300]
    # metrics array preferred; legacy single-metric fields accepted as fallback
    raw_metrics = d.get("metrics")
    if raw_metrics is None:
        raw_metrics = [{"label": (d.get("score_label") or "Score"), "unit": (d.get("unit") or "pts"),
                        "sort_dir": d.get("sort_dir", "DESC")}]
    try:
        cleaned_preview = [_clean_metric(m, i) for i, m in enumerate(raw_metrics)]
        if not (1 <= len(cleaned_preview) <= 3):
            raise ValueError("An event needs 1 to 3 metrics.")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    m0 = cleaned_preview[0]
    try:
        eid = db.execute("""INSERT INTO events(name,icon,score_label,unit,sort_dir,description,created_at,last_reset_at)
                      VALUES(?,?,?,?,?,?,?,?)""",
                   (name, icon, m0["label"], m0["unit"], m0["sort_dir"], desc, now_iso(), now_iso())).lastrowid
        for c in cleaned_preview:
            db.execute("INSERT INTO metrics(event_id,label,unit,sort_dir,position) VALUES(?,?,?,?,?)",
                       (eid, c["label"], c["unit"], c["sort_dir"], c["position"]))
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return jsonify({"ok": False, "error": "An event with that name already exists."}), 400
    return jsonify({"ok": True})

@app.route("/admin/api/events/<int:eid>", methods=["PUT", "DELETE"])
@admin_required_api
def admin_event_one(eid):
    db = get_db()
    if request.method == "DELETE":
        mode = (request.args.get("mode") or "archive")
        ev = db.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        if not ev:
            return jsonify({"ok": False, "error": "not found"}), 404
        if mode == "archive":
            archive_event(db, dict(ev), get_metrics(db, ev), "event-deleted")
        db.execute("DELETE FROM events WHERE id=?", (eid,))
        db.commit()
        return jsonify({"ok": True})
    d = request.get_json(force=True, silent=True) or {}
    fields = {}
    for k in ("name", "icon", "description"):
        if k in d and isinstance(d[k], str):
            fields[k] = d[k].strip()[: (300 if k == "description" else 60)]
    try:
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            db.execute(f"UPDATE events SET {sets} WHERE id=?", (*fields.values(), eid))
        if "metrics" in d:
            _save_event_metrics(db, eid, d["metrics"])
        elif any(k in d for k in ("score_label", "unit", "sort_dir")):
            # legacy single-metric edit: patch the primary metric in place
            cur = get_metrics(db, {"id": eid, "score_label": "Score", "unit": "pts", "sort_dir": "DESC"})
            patch = {"id": cur[0]["id"], "label": d.get("score_label", cur[0]["label"]),
                     "unit": d.get("unit", cur[0]["unit"]), "sort_dir": d.get("sort_dir", cur[0]["sort_dir"])}
            _save_event_metrics(db, eid, [patch])
        if not fields and "metrics" not in d and not any(k in d for k in ("score_label", "unit", "sort_dir")):
            return jsonify({"ok": False, "error": "nothing to update"}), 400
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return jsonify({"ok": False, "error": "Name already in use."}), 400
    except ValueError as e:
        db.rollback()
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})

@app.route("/admin/api/events/<int:eid>/reset", methods=["POST"])
@admin_required_api
def admin_reset(eid):
    db = get_db()
    ev = db.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
    if not ev:
        return jsonify({"ok": False, "error": "not found"}), 404
    d = request.get_json(force=True, silent=True) or {}
    reason = (d.get("reason") or "manual").strip()[:40]
    n = archive_event(db, dict(ev), get_metrics(db, ev), reason)
    db.execute("DELETE FROM entries WHERE event_id=?", (eid,))
    db.execute("UPDATE events SET last_reset_at=? WHERE id=?", (now_iso(), eid))
    db.commit()
    return jsonify({"ok": True, "archived": n})

@app.route("/admin/api/entries")
@admin_required_api
def admin_entries():
    event_id = request.args.get("event_id", type=int)
    page = max(1, int(request.args.get("page", 1)))
    per = max(1, min(100, int(request.args.get("per_page", 20))))
    db = get_db()
    where, params = ("1=1", []) if not event_id else ("e.event_id=?", [event_id])
    total = db.execute(f"SELECT COUNT(*) c FROM entries e WHERE {where}", params).fetchone()["c"]
    rows = db.execute(f"""SELECT e.*, v.name event_name FROM entries e JOIN events v ON v.id=e.event_id
                          WHERE {where} ORDER BY e.created_at DESC LIMIT ? OFFSET ?""",
                      (*params, per, (page - 1) * per)).fetchall()
    return jsonify({"ok": True, "rows": [dict(r) for r in rows], "total": total,
                    "page": page, "pages": max(1, (total + per - 1) // per)})

@app.route("/admin/api/entries/<int:eid>", methods=["DELETE"])
@admin_required_api
def admin_entry_del(eid):
    db = get_db()
    db.execute("DELETE FROM entries WHERE id=?", (eid,))
    db.commit()
    return jsonify({"ok": True})

@app.route("/admin/api/history")
@admin_required_api
def admin_history():
    event_id = request.args.get("event_id", type=int)
    db = get_db()
    q = "SELECT * FROM snapshots ORDER BY taken_at DESC"
    params = []
    if event_id:
        q = "SELECT * FROM snapshots WHERE event_id=? ORDER BY taken_at DESC"
        params = [event_id]
    rows = db.execute(q, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["entries"] = json.loads(d["data_json"] or "[]")
        except Exception:
            d["entries"] = []
        try:
            metrics = json.loads(d.get("metrics_json") or "[]")
        except Exception:
            metrics = []
        if not metrics:
            # legacy snapshot: single metric from stored columns
            metrics = [{"id": 0, "event_id": d["event_id"], "label": d.get("score_label") or "Score",
                        "unit": d.get("unit") or "pts", "sort_dir": "DESC", "position": 0}]
            for e in d["entries"]:
                if "values" not in e and e.get("score") is not None:
                    e["values"] = {"0": e["score"]}
        d["metrics"] = metrics
        d.pop("data_json", None)
        d.pop("metrics_json", None)
        out.append(d)
    return jsonify({"ok": True, "snapshots": out})

@app.route("/admin/api/history/<int:sid>", methods=["DELETE"])
@admin_required_api
def admin_history_del(sid):
    db = get_db()
    db.execute("DELETE FROM snapshots WHERE id=?", (sid,))
    db.commit()
    return jsonify({"ok": True})

@app.route("/admin/api/history/<int:sid>/export/<string:fmt>")
@admin_required_api
def admin_history_export(sid, fmt):
    from flask import abort
    db = get_db()
    s = db.execute("SELECT * FROM snapshots WHERE id=?", (sid,)).fetchone()
    if not s:
        return jsonify({"ok": False}), 404
    entries = json.loads(s["data_json"] or "[]")
    try:
        metrics = json.loads(s["metrics_json"] or "[]")
    except Exception:
        metrics = []
    if not metrics:
        metrics = [{"id": 0, "event_id": s["event_id"], "label": s["score_label"] or "Score",
                    "unit": s["unit"] or "pts", "sort_dir": "DESC", "position": 0}]
        for e in entries:
            if "values" not in e and e.get("score") is not None:
                e["values"] = {"0": e["score"]}
    if fmt == "json":
        buf = BytesIO(json.dumps({"snapshot": {k: s[k] for k in s.keys() if k not in ('data_json', 'metrics_json')},
                                   "metrics": metrics, "entries": entries}, indent=2).encode())
        return send_file(buf, mimetype="application/json", as_attachment=True,
                         download_name=f"history-{s['event_name']}-{sid}.json")
    # pdf: event shaped with its archived metric defs
    ev = {"id": s["event_id"], "name": s["event_name"], "score_label": s["score_label"],
          "unit": s["unit"], "sort_dir": "DESC", "metrics": metrics}
    pdf = build_pdf([(ev, entries)], title=f"{s['event_name']} · Archived ({s['taken_at'][:10]})")
    return send_file(pdf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"history-{s['event_name']}-{sid}.pdf")

@app.route("/admin/api/settings", methods=["GET", "POST"])
@admin_required_api
def admin_settings():
    if request.method == "GET":
        return jsonify({"ok": True, "auto_reset_enabled": get_setting("auto_reset_enabled", "0") == "1",
                        "auto_reset_hours": get_setting("auto_reset_hours", "1")})
    d = request.get_json(force=True, silent=True) or {}
    if "auto_reset_enabled" in d:
        set_setting("auto_reset_enabled", "1" if d["auto_reset_enabled"] else "0")
    if "auto_reset_hours" in d:
        try:
            h = float(d["auto_reset_hours"])
            h = min(168, max(0.25, h))
            set_setting("auto_reset_hours", str(h))
            # stamp resets so countdown restarts
            get_db().execute("UPDATE events SET last_reset_at=?", (now_iso(),))
            get_db().commit()
        except (ValueError, TypeError):
            pass
    if d.get("new_password"):
        if len(d["new_password"]) < 4:
            return jsonify({"ok": False, "error": "Password must be 4+ characters."}), 400
        set_setting("admin_password_hash", generate_password_hash(d["new_password"]))
    return jsonify({"ok": True})

# ---------------------------------------------------------------- seed
@app.route("/admin/api/seed", methods=["POST"])
@admin_required_api
def admin_seed():
    db = get_db()
    if db.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]:
        return jsonify({"ok": False, "error": "Already has events. Reset first."}), 400
    import random
    random.seed(7)
    # (name, icon, metrics[(label, unit, sort_dir)], description)
    events = [
        ("Speed Coding Sprint", "zap",
         [("Time", "sec", "ASC")],
         "Solve the bug-fix gauntlet as fast as possible."),
        ("AI Prompt Battle", "brain",
         [("Score", "pts", "DESC"), ("Time", "sec", "ASC")],
         "Highest judged score wins, faster output breaks ties."),
        ("Cloud Quiz Blitz", "cloud",
         [("Score", "pts", "DESC")],
         "20 rapid-fire AWS questions."),
        ("Drone Obstacle Run", "rocket",
         [("Time", "sec", "ASC")],
         "Fastest clean flight through the course."),
    ]
    names = ["Aarav", "Diya", "Kabir", "Ananya", "Vikram", "Meera", "Arjun", "Ishaan", "Priya", "Rohan",
             "Sneha", "Aditya", "Kavya", "Nikhil", "Pooja", "Rahul", "Sanya", "Varun", "Tanvi", "Karan"]
    for name, icon, metric_defs, desc in events:
        m0 = metric_defs[0]
        cur = db.execute("""INSERT INTO events(name,icon,score_label,unit,sort_dir,description,created_at,last_reset_at)
                            VALUES(?,?,?,?,?,?,?,?)""",
                         (name, icon, m0[0], m0[1], m0[2], desc, now_iso(), now_iso()))
        eid = cur.lastrowid
        mids = []
        for pos, (label, unit, sdir) in enumerate(metric_defs):
            mids.append(db.execute("INSERT INTO metrics(event_id,label,unit,sort_dir,position) VALUES(?,?,?,?,?)",
                                   (eid, label, unit, sdir, pos)).lastrowid)
        for n in random.sample(names, k=12):
            values = {}
            for (label, unit, sdir), mid in zip(metric_defs, mids):
                values[str(mid)] = round(random.uniform(28, 180), 1) if unit == "sec" else random.randint(40, 980)
            # spread timestamps over last 2h
            ts = (datetime.now() - timedelta(minutes=random.randint(2, 120))).isoformat(timespec="seconds")
            usn = f"1AT{random.randint(21,24)}CS{random.randint(1,180):03d}"
            phone = f"9{random.randint(100000000, 999999999)}"
            db.execute("INSERT INTO entries(event_id,player_name,usn,phone,score,values_json,created_at) VALUES(?,?,?,?,?,?,?)",
                       (eid, f"{n} {random.choice(['S','K','M','R','P'])}.", usn, phone,
                        values[str(mids[0])], json.dumps(values), ts))
            db.execute("""INSERT OR IGNORE INTO registrations(event_id,usn_norm,phone_norm,player_name,first_seen)
                          VALUES(?,?,?,?,?)""",
                       (eid, norm_usn(usn), norm_phone(phone), n, ts))
    db.commit()
    return jsonify({"ok": True})

# ---------------------------------------------------------------- main
init_db()  # idempotent: safe on import (gunicorn) and on `python app.py`

def _port():
    try:
        return int(os.environ.get("PORT") or 5000)
    except (ValueError, TypeError):
        return 5000

if __name__ == "__main__":
    port = _port()
    print("\n  CD3 Leaderboard Hub")
    print(f"  Viewer : http://127.0.0.1:{port}/")
    print(f"  Admin  : http://127.0.0.1:{port}/admin   (default password: admin123)\n")
    app.run(host="0.0.0.0", port=port, debug=True)
