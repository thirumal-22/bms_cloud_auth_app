#!/usr/bin/env python3
"""
Cloud BMS Digital Twin with PostgreSQL/SQLite + Authentication.
Run locally: pip install -r requirements.txt && python app.py
Default login: admin / admin12345. Change ADMIN_PASSWORD before deployment.
"""
from __future__ import annotations

import json, math, os, random, threading, time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_socketio import SocketIO, emit
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import desc
from werkzeug.security import check_password_hash, generate_password_hash

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False

APP_DIR = Path(__file__).resolve().parent
REPORT_DIR = APP_DIR / "reports"
REPORT_DIR.mkdir(exist_ok=True)
PORT = int(os.environ.get("PORT", os.environ.get("BMS_PORT", "5001")))
HOST = os.environ.get("BMS_HOST", "0.0.0.0")


def normalize_database_url(url: str | None) -> str:
    if not url:
        return "sqlite:///" + str(APP_DIR / "bms_history.db")
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg2://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "replace-this-secret-before-production")
app.config["SQLALCHEMY_DATABASE_URI"] = normalize_database_url(os.environ.get("DATABASE_URL"))
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
socketio = SocketIO(
    app,
    cors_allowed_origins=os.environ.get("CORS_ALLOWED_ORIGINS", "*"),
    async_mode="gevent",
    ping_timeout=60,
    ping_interval=20,
)
LOCAL_TIMEZONE_NOTE = "Local server timezone"


class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(30), nullable=False, default="engineer")
    created_at = db.Column(db.String(40), nullable=False)


class BMSLog(db.Model):
    __tablename__ = "bms_logs"
    id = db.Column(db.Integer, primary_key=True)
    timestamp_local = db.Column(db.String(40), nullable=False, index=True)
    mode = db.Column(db.String(40), nullable=False)
    pack_voltage = db.Column(db.Float, nullable=False)
    pack_current = db.Column(db.Float, nullable=False)
    pack_power_kw = db.Column(db.Float, nullable=False)
    soc = db.Column(db.Float, nullable=False)
    soh = db.Column(db.Float, nullable=False)
    max_temp_c = db.Column(db.Float, nullable=False)
    min_cell_v = db.Column(db.Float, nullable=False)
    max_cell_v = db.Column(db.Float, nullable=False)
    delta_v = db.Column(db.Float, nullable=False)
    avg_cell_resistance_ohm = db.Column(db.Float, nullable=False)
    total_internal_resistance_ohm = db.Column(db.Float, nullable=False)
    total_cycles = db.Column(db.Float, nullable=False)
    runtime_text = db.Column(db.String(80), nullable=False)
    cells_json = db.Column(db.Text, nullable=False)
    temps_json = db.Column(db.Text, nullable=False)
    balancing_cells_json = db.Column(db.Text, nullable=False)
    alarms_json = db.Column(db.Text, nullable=False)
    fault_code = db.Column(db.String(120), nullable=False)
    contactor_closed = db.Column(db.Boolean, nullable=False)


def local_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def current_user() -> User | None:
    uid = session.get("user_id")
    return db.session.get(User, uid) if uid else None


@app.before_request
def protect_routes():
    public = {"/login", "/api/login", "/health", "/favicon.ico"}
    if request.path in public or request.path.startswith("/static/"):
        return None
    if session.get("user_id"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "Authentication required"}), 401
    return redirect(url_for("login_page"))


def create_default_admin() -> None:
    username = os.environ.get("ADMIN_USERNAME", "admin")
    password = os.environ.get("ADMIN_PASSWORD", "admin12345")
    if User.query.filter_by(username=username).first():
        return
    db.session.add(User(username=username, password_hash=generate_password_hash(password), role="admin", created_at=local_timestamp()))
    db.session.commit()
    print(f"[AUTH] Admin user ready: {username}")


LFP_OCV_SOC_TABLE: List[Tuple[float, float]] = [
    (0.00, 2.500), (0.05, 2.820), (0.10, 3.000), (0.15, 3.100),
    (0.20, 3.160), (0.25, 3.200), (0.30, 3.220), (0.35, 3.240),
    (0.40, 3.255), (0.45, 3.265), (0.50, 3.275), (0.55, 3.285),
    (0.60, 3.295), (0.65, 3.305), (0.70, 3.315), (0.75, 3.325),
    (0.80, 3.340), (0.85, 3.360), (0.90, 3.400), (0.95, 3.450),
    (1.00, 3.650),
]


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def interp(x: float, table: List[Tuple[float, float]]) -> float:
    if x <= table[0][0]: return table[0][1]
    if x >= table[-1][0]: return table[-1][1]
    for i in range(len(table) - 1):
        x0, y0 = table[i]; x1, y1 = table[i + 1]
        if x0 <= x <= x1:
            a = (x - x0) / (x1 - x0)
            return y0 + a * (y1 - y0)
    return table[-1][1]


def ocv_from_soc(soc: float) -> float:
    return interp(clamp(soc, 0, 1), LFP_OCV_SOC_TABLE)


def soc_from_ocv(v: float) -> float:
    return interp(v, [(v0, s0) for s0, v0 in LFP_OCV_SOC_TABLE])


@dataclass
class Cell:
    idx: int
    soc: float
    cap_ah: float
    r_ohm: float
    temp_c: float
    voltage_v: float = 3.25
    balancing: bool = False
    cycles: float = 0.0


@dataclass
class BatteryConfig:
    cells: int = 16
    cap_ah: float = 100.0
    ambient_c: float = 28.0
    thermal_mass_j_c: float = 1450.0
    cooling_w_c: float = 0.28
    balance_threshold_v: float = 0.020
    balance_bleed_a: float = 0.20


class BatteryTwin:
    OVP = 3.650; UVP = 2.500; OCP = 95.0; OTP = 60.0; CRIT = 70.0; IMBAL = 0.080
    def __init__(self, cfg: BatteryConfig):
        self.cfg = cfg; self.lock = threading.RLock(); self.last = time.monotonic(); self.elapsed = 0.0
        self.commanded_mode = "AUTO"; self.mode = "Idle"; self.contactor_closed = True; self.soh = 100.0; self.total_cycles = 0.0; self.fault = "NONE"
        self.latch = {k: False for k in ["OVP", "UVP", "OCP", "OTP", "IMBALANCE", "THERMAL_RUNAWAY"]}
        self.cells = [Cell(i+1, clamp(0.78+random.uniform(-.015,.015), .05, .98), cfg.cap_ah*(1+random.uniform(-.012,.008)), .0026+random.uniform(-.00015,.00025), cfg.ambient_c+random.uniform(-.8,.8)) for i in range(cfg.cells)]

    def set_mode(self, mode: str):
        if mode.upper() in {"AUTO", "IDLE", "CHARGE", "DISCHARGE", "RUNAWAY_TEST"}: self.commanded_mode = mode.upper()
    def set_ambient(self, c: float): self.cfg.ambient_c = clamp(float(c), -20, 65)
    def reset_faults(self):
        self.latch = {k: False for k in self.latch}; self.fault = "NONE"; self.contactor_closed = True
        for c in self.cells: c.temp_c = min(c.temp_c, self.cfg.ambient_c + 5)
    def toggle_contactor(self) -> bool:
        self.contactor_closed = not self.contactor_closed; return self.contactor_closed

    def current_profile(self) -> float:
        t = self.elapsed; avg_soc = sum(c.soc for c in self.cells)/len(self.cells)
        if self.commanded_mode == "IDLE": return random.uniform(-.15,.15)
        if self.commanded_mode == "CHARGE": return -(12 + 10*(1-avg_soc))
        if self.commanded_mode == "DISCHARGE": return min(80, 25 + 18*abs(math.sin(t/18)))
        if self.commanded_mode == "RUNAWAY_TEST": return 130.0
        pos = t % 150
        if pos < 65: return 18 + 16*math.sin(t/8)**2 + random.uniform(-1.5,1.5)
        if pos < 105: return -(10 + 14*(1-avg_soc) + random.uniform(-1,1))
        return random.uniform(-.2,.2)

    def step(self) -> Dict:
        with self.lock:
            now = time.monotonic(); dt = clamp(now - self.last, .05, 2); self.last = now; self.elapsed += dt
            current = 0.0 if not self.contactor_closed else self.current_profile()
            avg_soc = sum(c.soc for c in self.cells)/len(self.cells); top_chg = current < -.5 and avg_soc > .82
            ocvs = [ocv_from_soc(c.soc) for c in self.cells]; avg_v = sum(ocvs)/len(ocvs)
            for c, ocv in zip(self.cells, ocvs):
                c.balancing = bool(top_chg and ocv > avg_v + self.cfg.balance_threshold_v)
                cell_i = current + (self.cfg.balance_bleed_a if c.balancing else 0.0)
                eta = .985 if cell_i < 0 else 1.0
                c.soc = clamp(c.soc - (cell_i * dt * eta)/(c.cap_ah*3600), .02, .995)
                c.cycles += abs(cell_i)*dt/(2*c.cap_ah*3600)
                tf = 1 + max(0, c.temp_c-35)*.025; cf = 1 + max(0, abs(current)-40)*.006
                c.cap_ah = max(self.cfg.cap_ah*.55, c.cap_ah - .000012*abs(current)*dt/3600*tf*cf)
                c.r_ohm = min(.020, c.r_ohm + 1e-9*abs(current)*dt*tf*cf)
                c.voltage_v = clamp(ocv_from_soc(c.soc) - current*c.r_ohm + random.uniform(-.0008,.0008), 2.30, 3.85)
                heat_w = current*current*c.r_ohm + (c.voltage_v*self.cfg.balance_bleed_a if c.balancing else 0)
                cool_w = self.cfg.cooling_w_c*(c.temp_c - self.cfg.ambient_c)
                dtemp = ((heat_w - cool_w)*dt)/self.cfg.thermal_mass_j_c
                if c.temp_c > self.OTP: dtemp += .018*(c.temp_c-self.OTP)*dt
                c.temp_c = clamp(c.temp_c + dtemp + random.uniform(-.015,.015), self.cfg.ambient_c-2, 125)
            if abs(current) <= .35:
                for c in self.cells: c.soc = clamp(.96*c.soc + .04*soc_from_ocv(c.voltage_v), .02, .995)
            avg_cap = sum(c.cap_ah for c in self.cells)/len(self.cells); avg_ir = sum(c.r_ohm for c in self.cells)/len(self.cells)
            self.soh = round(clamp(min(100*avg_cap/self.cfg.cap_ah, 100 - max(0, avg_ir-.0026)/.010*55), 0, 100), 2)
            self.total_cycles = round(sum(c.cycles for c in self.cells)/len(self.cells), 4)
            cells = [round(c.voltage_v,4) for c in self.cells]; temps = [round(c.temp_c,2) for c in self.cells]
            maxv, minv, maxt = max(cells), min(cells), max(temps); dv = round(maxv-minv,4)
            alarms = {"OVP": maxv>=self.OVP, "UVP": minv<=self.UVP, "OCP": abs(current)>=self.OCP, "OTP": maxt>=self.OTP, "IMBALANCE": dv>=self.IMBAL, "THERMAL_RUNAWAY": maxt>=self.CRIT or (maxt>=self.OTP and abs(current)>=self.OCP)}
            for k,v in alarms.items(): self.latch[k] = self.latch[k] or v
            active = [k for k,v in alarms.items() if v]; self.fault = ",".join(active) if active else "NONE"
            if alarms["THERMAL_RUNAWAY"]: self.contactor_closed = False
            if self.latch["THERMAL_RUNAWAY"]: self.mode = "Thermal Runaway"
            elif abs(current) < .5: self.mode = "Idle"
            elif current < 0: self.mode = "Charging"
            else: self.mode = "Discharging"
            pack_v = round(sum(cells),3); power = round(pack_v*current/1000,4); soc = round(100*sum(c.soc for c in self.cells)/len(self.cells),2)
            if current > .5: rt = self.cfg.cap_ah*(soc/100)/current
            elif current < -.5: rt = self.cfg.cap_ah*(1-soc/100)/abs(current)
            else: rt = None
            runtime = "Idle / no load" if rt is None else (f"{int(rt*60)//60}h {int(rt*60)%60}m remaining" if current > 0 else f"{int(rt*60)//60}h {int(rt*60)%60}m to full")
            return {"timestamp": local_timestamp(), "timezone": LOCAL_TIMEZONE_NOTE, "chemistry":"LFP", "series_cells":16, "mode": self.mode, "commanded_mode": self.commanded_mode, "pack_voltage":pack_v, "pack_current":round(current,3), "pack_power_kw":power, "soc":soc, "soh":self.soh, "cells":cells, "temperatures":temps, "max_temp_c":round(maxt,2), "min_cell_v":minv, "max_cell_v":maxv, "delta_v":dv, "avg_cell_resistance_ohm":round(avg_ir,6), "total_internal_resistance_ohm":round(sum(c.r_ohm for c in self.cells),6), "total_cycles":self.total_cycles, "runtime_text":runtime, "balancing_cells":[c.idx for c in self.cells if c.balancing], "contactor_closed":self.contactor_closed, "alarms":alarms, "alarm_latch":dict(self.latch), "fault_code":self.fault, "ambient_c":self.cfg.ambient_c}


battery = BatteryTwin(BatteryConfig())
latest_frame: Dict = {}
stream_started = False


def init_database():
    with app.app_context():
        db.create_all(); create_default_admin()


def log_frame(f: Dict):
    with app.app_context():
        db.session.add(BMSLog(timestamp_local=f["timestamp"], mode=f["mode"], pack_voltage=f["pack_voltage"], pack_current=f["pack_current"], pack_power_kw=f["pack_power_kw"], soc=f["soc"], soh=f["soh"], max_temp_c=f["max_temp_c"], min_cell_v=f["min_cell_v"], max_cell_v=f["max_cell_v"], delta_v=f["delta_v"], avg_cell_resistance_ohm=f["avg_cell_resistance_ohm"], total_internal_resistance_ohm=f["total_internal_resistance_ohm"], total_cycles=f["total_cycles"], runtime_text=f["runtime_text"], cells_json=json.dumps(f["cells"]), temps_json=json.dumps(f["temperatures"]), balancing_cells_json=json.dumps(f["balancing_cells"]), alarms_json=json.dumps(f["alarms"]), fault_code=f["fault_code"], contactor_closed=f["contactor_closed"]))
        db.session.commit()


def fetch_history(limit=100):
    rows = BMSLog.query.order_by(desc(BMSLog.id)).limit(int(clamp(limit,1,500))).all()
    return [{"timestamp_local":r.timestamp_local,"mode":r.mode,"pack_voltage":r.pack_voltage,"pack_current":r.pack_current,"pack_power_kw":r.pack_power_kw,"soc":r.soc,"soh":r.soh,"max_temp_c":r.max_temp_c,"delta_v":r.delta_v,"total_cycles":r.total_cycles,"fault_code":r.fault_code} for r in reversed(rows)]


def telemetry_loop():
    global latest_frame
    while True:
        try:
            frame = battery.step(); latest_frame = frame; log_frame(frame); socketio.emit("bms_update", frame)
            if frame["alarms"]["THERMAL_RUNAWAY"] or frame["alarms"]["OTP"]: socketio.emit("bms_alarm", {"timestamp":frame["timestamp"],"fault_code":frame["fault_code"],"max_temp_c":frame["max_temp_c"],"mode":frame["mode"]})
            socketio.sleep(1)
        except Exception as e:
            print("[telemetry_loop]", e); socketio.sleep(1)


def start_stream_once():
    global stream_started
    if not stream_started:
        stream_started = True; socketio.start_background_task(telemetry_loop)


@app.route("/health")
def health(): return jsonify({"ok":True,"timestamp":local_timestamp()})

@app.route("/login")
def login_page():
    if session.get("user_id"): return redirect(url_for("index"))
    return render_template("login.html")

@app.route("/api/login", methods=["POST"])
def api_login():
    body = request.get_json(silent=True) or request.form
    user = User.query.filter_by(username=str(body.get("username","")).strip()).first()
    if not user or not check_password_hash(user.password_hash, str(body.get("password",""))): return jsonify({"ok":False,"error":"Invalid username or password"}), 401
    session.clear(); session["user_id"] = user.id; session["username"] = user.username; session["role"] = user.role
    return jsonify({"ok":True,"username":user.username,"role":user.role})

@app.route("/logout", methods=["GET","POST"])
def logout():
    session.clear(); return jsonify({"ok":True}) if request.method == "POST" else redirect(url_for("login_page"))

@app.route("/")
def index(): return render_template("index.html", user=current_user())
@app.route("/api/me")
def api_me():
    u = current_user(); return jsonify({"username":u.username,"role":u.role})
@app.route("/api/latest")
def api_latest(): return jsonify(latest_frame or battery.step())
@app.route("/api/history")
def api_history(): return jsonify(fetch_history(int(request.args.get("limit",100))))
@app.route("/api/command/mode", methods=["POST"])
def api_set_mode(): battery.set_mode((request.get_json(silent=True) or {}).get("mode","AUTO")); return jsonify({"ok":True,"mode":battery.commanded_mode})
@app.route("/api/command/ambient", methods=["POST"])
def api_set_ambient(): battery.set_ambient(float((request.get_json(silent=True) or {}).get("ambient_c", battery.cfg.ambient_c))); return jsonify({"ok":True,"ambient_c":battery.cfg.ambient_c})
@app.route("/api/command/reset_faults", methods=["POST"])
def api_reset_faults(): battery.reset_faults(); return jsonify({"ok":True})
@app.route("/api/command/contactor", methods=["POST"])
def api_contactor(): return jsonify({"ok":True,"contactor_closed":battery.toggle_contactor()})

@app.route("/api/report.pdf")
def report_pdf():
    if not REPORTLAB_AVAILABLE: return jsonify({"error":"ReportLab missing. pip install reportlab"}), 500
    rows = BMSLog.query.order_by(desc(BMSLog.id)).limit(250).all(); now = datetime.now().astimezone(); path = REPORT_DIR / f"bms_report_{now.strftime('%Y%m%d_%H%M%S')}.pdf"
    doc = SimpleDocTemplate(str(path), pagesize=A4, rightMargin=.45*inch, leftMargin=.45*inch, topMargin=.45*inch, bottomMargin=.45*inch); styles = getSampleStyleSheet(); story=[]
    story += [Paragraph("Battery Management System Engineering Inspection Report", styles["Title"]), Spacer(1,.15*inch), Paragraph(f"Generated: {now.isoformat(timespec='seconds')} ({LOCAL_TIMEZONE_NOTE})", styles["Normal"]), Paragraph("16S LiFePO4 Cloud Digital Twin | SQLAlchemy PostgreSQL/SQLite ledger", styles["Normal"]), Spacer(1,.2*inch)]
    if rows:
        r=rows[0]; summary=[["Metric","Value"],["Timestamp",r.timestamp_local],["Mode",r.mode],["Pack Voltage",f"{r.pack_voltage:.3f} V"],["Pack Current",f"{r.pack_current:.3f} A"],["Power",f"{r.pack_power_kw:.4f} kW"],["SoC",f"{r.soc:.2f}%"],["SoH",f"{r.soh:.2f}%"],["Max Temp",f"{r.max_temp_c:.2f} C"],["Cell Spread",f"{r.delta_v*1000:.1f} mV"],["Avg Cell IR",f"{r.avg_cell_resistance_ohm:.6f} ohm"],["Cycles",f"{r.total_cycles:.4f}"],["Fault",r.fault_code]]
        t=Table(summary,colWidths=[2.2*inch,4.5*inch]); t.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#111827")),("TEXTCOLOR",(0,0),(-1,0),colors.white),("GRID",(0,0),(-1,-1),.25,colors.grey),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold")])) ; story += [t,Spacer(1,.25*inch)]
    data=[["Time","Mode","V","A","SoC","SoH","Temp","Fault"]]
    for r in reversed(rows[:35]): data.append([r.timestamp_local.replace("T"," ").split("+")[0],r.mode,f"{r.pack_voltage:.2f}",f"{r.pack_current:.1f}",f"{r.soc:.1f}%",f"{r.soh:.1f}%",f"{r.max_temp_c:.1f}C",r.fault_code])
    t2=Table(data, repeatRows=1); t2.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#065F46")),("TEXTCOLOR",(0,0),(-1,0),colors.white),("GRID",(0,0),(-1,-1),.25,colors.grey),("FONTSIZE",(0,0),(-1,-1),7)])); story.append(t2); doc.build(story)
    return send_file(path, as_attachment=True, download_name=path.name)

@socketio.on("connect")
def on_connect():
    if not session.get("user_id"): return False
    emit("bms_update", latest_frame or battery.step()); emit("history", fetch_history(100))
@socketio.on("request_history")
def on_history(data=None): emit("history", fetch_history(100))
@socketio.on("set_mode")
def on_mode(data): battery.set_mode(str((data or {}).get("mode","AUTO"))); emit("command_ack", {"type":"mode","mode":battery.commanded_mode}, broadcast=True)
@socketio.on("set_ambient")
def on_ambient(data): battery.set_ambient(float((data or {}).get("ambient_c",battery.cfg.ambient_c))); emit("command_ack", {"type":"ambient","ambient_c":battery.cfg.ambient_c}, broadcast=True)
@socketio.on("reset_faults")
def on_reset(): battery.reset_faults(); emit("command_ack", {"type":"reset_faults","ok":True}, broadcast=True)
@socketio.on("toggle_contactor")
def on_contactor(): emit("command_ack", {"type":"contactor","contactor_closed":battery.toggle_contactor()}, broadcast=True)

init_database(); start_stream_once()
if __name__ == "__main__":
    print(f"[BMS] http://localhost:{PORT}"); socketio.run(app, host=HOST, port=PORT, debug=False, allow_unsafe_werkzeug=True)
