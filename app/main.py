"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real
      environment variables take priority because load_dotenv does not override).

All application code lives in this file. See openapi.yaml for the HTTP contract
and business rules R1-R10, DATA_MODEL.md for the stored data.
"""
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from datetime import date as date_type
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, IndexModel, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# --------------------------------------------------------------------------- #
# Configuration and database
# --------------------------------------------------------------------------- #
load_dotenv()  # does not override real environment variables (override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: every BSON date comes back as an aware UTC datetime, so naive
# vs aware comparisons can never silently succeed/fail depending on the host TZ.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
employees = db["employees"]
logs = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
PRESENCE_EXPR = {"$in": ["$status", PRESENCE]}
WEEKDAY_EXPR = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]}
LATE_POS_EXPR = {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}
HALF_EXPR = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}

# present units (half day = 0.5) restricted to weekdays, for monthly/summary
PRESENT_UNIT_WEEKDAY = {
    "$cond": [
        {"$and": [PRESENCE_EXPR, WEEKDAY_EXPR]},
        {"$cond": [HALF_EXPR, 0.5, 1]},
        0,
    ]
}
# present units on any day, for the daily trend
PRESENT_UNIT_ANYDAY = {
    "$cond": [PRESENCE_EXPR, {"$cond": [HALF_EXPR, 0.5, 1]}, 0]
}
HAS_WORK_HOURS = {
    "$and": [PRESENCE_EXPR, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]
}

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    """Create all indexes idempotently at startup (one bulk request per collection)."""
    specs = [
        (employees, [
            IndexModel([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code"),
            IndexModel([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp"),
            IndexModel([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined"),
            IndexModel([("joined_on", ASCENDING)], name="joined_on"),
        ]),
        (logs, [
            IndexModel([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date"),
            IndexModel([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp"),
            IndexModel([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp"),
        ]),
    ]
    for coll, indexes in specs:
        try:
            coll.create_indexes(indexes)
        except Exception as exc:  # pragma: no cover - startup must not crash
            print(f"index creation failed for {coll.name}: {exc}")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Attendance Admin</title>
<style>
  :root { --bg:#0f172a; --card:#1e293b; --line:#334155; --fg:#e2e8f0; --mut:#94a3b8; --acc:#38bdf8; --ok:#22c55e; --err:#f87171; }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--fg); }
  header { display:flex; align-items:center; gap:12px; padding:14px 20px; background:var(--card); border-bottom:1px solid var(--line); position:sticky; top:0; z-index:5; flex-wrap:wrap; }
  header h1 { font-size:18px; margin:0; }
  header .sp { flex:1; }
  a { color:var(--acc); }
  main { padding:20px; max-width:1200px; margin:0 auto; display:grid; gap:20px; }
  section { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; }
  h2 { margin:0 0 12px; font-size:15px; text-transform:uppercase; letter-spacing:.05em; color:var(--mut); }
  .row { display:flex; flex-wrap:wrap; gap:8px; align-items:flex-end; margin-bottom:12px; }
  label { display:flex; flex-direction:column; gap:4px; font-size:12px; color:var(--mut); }
  input,select { background:#0b1220; color:var(--fg); border:1px solid var(--line); border-radius:6px; padding:7px 9px; font:inherit; }
  button { background:var(--acc); color:#04283a; border:0; border-radius:6px; padding:8px 12px; font-weight:600; cursor:pointer; }
  button.sec { background:#334155; color:var(--fg); }
  button.danger { background:#7f1d1d; color:#fecaca; }
  button:hover { filter:brightness(1.1); }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { text-align:left; padding:7px 9px; border-bottom:1px solid var(--line); white-space:nowrap; }
  th { color:var(--mut); font-weight:600; }
  tbody tr:hover { background:#0b1220; }
  .pill { padding:2px 9px; border-radius:99px; font-size:12px; background:#334155; }
  .pill.ok { background:#14532d; color:#bbf7d0; }
  .pill.bad { background:#7f1d1d; color:#fecaca; }
  .tag { padding:1px 7px; border-radius:5px; font-size:12px; background:#0b1220; border:1px solid var(--line); }
  #toast { position:fixed; right:16px; bottom:16px; display:flex; flex-direction:column; gap:8px; z-index:50; }
  .toast { padding:10px 14px; border-radius:8px; background:var(--ok); color:#04280f; font-weight:600; box-shadow:0 8px 24px #0006; }
  .toast.err { background:var(--err); color:#2a0505; }
  #overlay { position:fixed; inset:0; background:#0009; display:none; align-items:center; justify-content:center; z-index:40; padding:16px; }
  #overlay.on { display:flex; }
  #modal { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:18px; width:min(520px,100%); }
  .muted { color:var(--mut); }
</style>
</head>
<body>
<header>
  <h1>Employee Attendance Admin</h1>
  <span id="health" class="pill">checking...</span>
  <span class="sp"></span>
  <a href="/docs" target="_blank">API docs / Swagger</a>
</header>
<main>
  <section>
    <h2>Employees</h2>
    <form id="empForm" class="row">
      <label>Code<input name="emp_code" placeholder="EMP0007" required></label>
      <label>Name<input name="name" placeholder="Jane Doe" required></label>
      <label>Email<input name="email" placeholder="jane@example.com" required></label>
      <label>Department<input name="department" placeholder="Engineering" required></label>
      <label>Shift start<input name="shift_start" value="09:30"></label>
      <label>Shift end<input name="shift_end" value="18:30"></label>
      <label>Joined on<input name="joined_on" type="date" required></label>
      <button type="submit">Add employee</button>
    </form>
    <table>
      <thead><tr><th>Code</th><th>Name</th><th>Email</th><th>Department</th><th>Shift</th><th>Joined</th></tr></thead>
      <tbody id="empBody"><tr><td colspan="6" class="muted">Loading...</td></tr></tbody>
    </table>
  </section>

  <section>
    <h2>Attendance</h2>
    <div class="row">
      <label>Employee<input id="fEmp" placeholder="EMP0001"></label>
      <label>From<input id="fFrom" type="date"></label>
      <label>To<input id="fTo" type="date"></label>
      <label>Status<select id="fStatus">
        <option value="">any</option><option>PRESENT</option><option>ABSENT</option>
        <option>LEAVE</option><option>WFH</option><option>ON_DUTY</option>
      </select></label>
      <button id="btnFilter">Search</button>
      <button id="btnClear" class="sec">Clear</button>
    </div>
    <div class="row" style="border-top:1px solid var(--line); padding-top:12px;">
      <label>Punch-in employee<input id="piEmp" placeholder="EMP0001"></label>
      <label>Status<select id="piStatus"><option>PRESENT</option><option>WFH</option><option>ON_DUTY</option></select></label>
      <label>At (optional)<input id="piAt" type="datetime-local"></label>
      <button id="btnPunchIn">Punch in</button>
    </div>
    <div class="row">
      <label>Punch-out employee<input id="poEmp" placeholder="EMP0001"></label>
      <label>At (optional)<input id="poAt" type="datetime-local"></label>
      <button id="btnPunchOut">Punch out</button>
    </div>
    <p class="muted" id="attCount"></p>
    <table>
      <thead><tr><th>Code</th><th>Date</th><th>Status</th><th>In (IST)</th><th>Out (IST)</th><th>Hours</th><th>Late</th><th>OT</th><th>Half</th><th>History</th><th>Actions</th></tr></thead>
      <tbody id="attBody"><tr><td colspan="11" class="muted">Loading...</td></tr></tbody>
    </table>
  </section>
</main>
<div id="toast"></div>
<div id="overlay">
  <div id="modal">
    <h2 id="modalTitle">Edit attendance</h2>
    <form id="editForm">
      <div class="row">
        <label>Status<select name="status"><option>PRESENT</option><option>ABSENT</option><option>LEAVE</option><option>WFH</option><option>ON_DUTY</option></select></label>
        <label>Punch in (IST)<input name="punch_in" type="datetime-local"></label>
        <label>Punch out (IST)<input name="punch_out" type="datetime-local"></label>
      </div>
      <div class="row">
        <label>Reason<input name="reason" minlength="5" maxlength="200" placeholder="why this change" required></label>
        <label>Regularized by<input name="regularized_by" maxlength="50" placeholder="hr.admin" required></label>
      </div>
      <div class="row">
        <button type="submit">Save changes</button>
        <button type="button" class="sec" id="editCancel">Cancel</button>
      </div>
    </form>
  </div>
</div>
<script>
const $ = (s, r=document) => r.querySelector(s);
function toast(msg, ok=true){
  const el = document.createElement('div');
  el.className = 'toast' + (ok ? '' : ' err');
  el.textContent = msg;
  $('#toast').appendChild(el);
  setTimeout(() => el.remove(), 4000);
}
async function api(path, opts={}){
  const res = await fetch(path, opts);
  const text = await res.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  if(!res.ok){
    let msg = (body && body.detail) || res.statusText || ('HTTP ' + res.status);
    if(Array.isArray(msg)) msg = msg.map(x => x.msg || JSON.stringify(x)).join('; ');
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return body;
}
function ist(ms){
  if(ms === null || ms === undefined) return '';
  return new Date(ms).toLocaleString('sv-SE', { timeZone:'Asia/Kolkata' }).slice(0, 16);
}
function msToInput(ms){
  if(ms === null || ms === undefined) return '';
  return ist(ms).replace(' ', 'T');
}
function inputToMs(v){
  if(!v) return undefined;
  return new Date(v + ':00+05:30').getTime();
}
function esc(s){ return String(s == null ? '' : s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

async function loadHealth(){
  try { await api('/health'); $('#health').className = 'pill ok'; $('#health').textContent = 'API ok'; }
  catch { $('#health').className = 'pill bad'; $('#health').textContent = 'API down'; }
}
async function loadEmployees(){
  const b = $('#empBody');
  try {
    const data = await api('/employees?page_size=100');
    if(!data.items.length){ b.innerHTML = '<tr><td colspan="6" class="muted">No employees</td></tr>'; return; }
    b.innerHTML = data.items.map(e => '<tr><td>' + esc(e.emp_code) + '</td><td>' + esc(e.name) +
      '</td><td>' + esc(e.email) + '</td><td>' + esc(e.department) + '</td><td>' + esc(e.shift_start) +
      '-' + esc(e.shift_end) + '</td><td>' + esc(e.joined_on) + '</td></tr>').join('');
  } catch(err){ b.innerHTML = '<tr><td colspan="6" class="muted">' + esc(err.message) + '</td></tr>'; }
}
async function loadAttendance(){
  const b = $('#attBody');
  const q = new URLSearchParams();
  if($('#fEmp').value) q.set('emp_code', $('#fEmp').value.trim());
  if($('#fFrom').value) q.set('date_from', $('#fFrom').value);
  if($('#fTo').value) q.set('date_to', $('#fTo').value);
  if($('#fStatus').value) q.set('status', $('#fStatus').value);
  q.set('page_size', '100');
  try {
    const data = await api('/attendance?' + q.toString());
    $('#attCount').textContent = data.total + ' record(s)';
    if(!data.items.length){ b.innerHTML = '<tr><td colspan="11" class="muted">No attendance records</td></tr>'; return; }
    b.innerHTML = data.items.map(r => {
      const hist = (r.history || []).length
        ? '<details><summary>' + r.history.length + ' edit(s)</summary>' + r.history.map(h =>
            '<div class="muted" style="white-space:normal">' + esc(ist(h.at)) + ' by ' + esc(h.by) + ': ' + esc(h.reason) + '</div>').join('') + '</details>'
        : '0';
      return '<tr><td>' + esc(r.emp_code) + '</td><td>' + esc(r.date) + '</td><td><span class="tag">' + esc(r.status) +
        '</span></td><td>' + (ist(r.punch_in) || '-') + '</td><td>' + (ist(r.punch_out) || '-') +
        '</td><td>' + (r.work_hours ?? '-') + '</td><td>' + (r.late_minutes ?? 0) + '</td><td>' + (r.overtime_minutes ?? 0) +
        '</td><td>' + (r.half_day ? 'yes' : 'no') + '</td><td>' + hist + '</td><td>' +
        '<button class="sec" data-act="edit" data-code="' + esc(r.emp_code) + '" data-date="' + esc(r.date) + '">Edit</button> ' +
        '<button class="danger" data-act="del" data-code="' + esc(r.emp_code) + '" data-date="' + esc(r.date) + '">Delete</button>' +
        '</td></tr>';
    }).join('');
  } catch(err){ b.innerHTML = '<tr><td colspan="11" class="muted">' + esc(err.message) + '</td></tr>'; }
}

$('#empForm').addEventListener('submit', async e => {
  e.preventDefault();
  const fd = Object.fromEntries(new FormData(e.target));
  if(!fd.shift_start) fd.shift_start = '09:30';
  if(!fd.shift_end) fd.shift_end = '18:30';
  try { await api('/employees', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(fd) });
    toast('Employee created'); e.target.reset(); loadEmployees(); }
  catch(err){ toast(err.message, false); }
});
$('#btnPunchIn').addEventListener('click', async () => {
  const body = { emp_code: $('#piEmp').value.trim(), status: $('#piStatus').value };
  const at = inputToMs($('#piAt').value); if(at) body.punched_at = at;
  try { const r = await api('/attendance/punch-in', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    toast('Punched in ' + r.emp_code + ' ' + r.date); loadAttendance(); }
  catch(err){ toast(err.message, false); }
});
$('#btnPunchOut').addEventListener('click', async () => {
  const body = { emp_code: $('#poEmp').value.trim() };
  const at = inputToMs($('#poAt').value); if(at) body.punched_at = at;
  try { const r = await api('/attendance/punch-out', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    toast('Punched out ' + r.emp_code + ' ' + r.date); loadAttendance(); }
  catch(err){ toast(err.message, false); }
});
$('#btnFilter').addEventListener('click', loadAttendance);
$('#btnClear').addEventListener('click', () => { $('#fEmp').value=''; $('#fFrom').value=''; $('#fTo').value=''; $('#fStatus').value=''; loadAttendance(); });

let editing = null;
document.addEventListener('click', async e => {
  const btn = e.target.closest('button[data-act]');
  if(!btn) return;
  const code = btn.dataset.code, date = btn.dataset.date;
  if(btn.dataset.act === 'del'){
    if(!confirm('Delete attendance for ' + code + ' on ' + date + '?')) return;
    try { await api('/attendance/' + encodeURIComponent(code) + '/' + encodeURIComponent(date), { method:'DELETE' });
      toast('Deleted ' + code + ' ' + date); loadAttendance(); }
    catch(err){ toast(err.message, false); }
    return;
  }
  try {
    const data = await api('/attendance?emp_code=' + encodeURIComponent(code) + '&date_from=' + date + '&date_to=' + date);
    const rec = data.items[0];
    if(!rec){ toast('Record not found', false); return; }
    editing = rec;
    const f = $('#editForm');
    f.status.value = rec.status;
    f.punch_in.value = msToInput(rec.punch_in);
    f.punch_out.value = msToInput(rec.punch_out);
    f.reason.value = ''; f.regularized_by.value = 'hr.admin';
    $('#modalTitle').textContent = 'Edit ' + code + ' / ' + date;
    $('#overlay').classList.add('on');
  } catch(err){ toast(err.message, false); }
});
$('#editCancel').addEventListener('click', () => $('#overlay').classList.remove('on'));
$('#editForm').addEventListener('submit', async e => {
  e.preventDefault();
  const f = e.target;
  const body = { reason: f.reason.value, regularized_by: f.regularized_by.value, status: f.status.value };
  const pin = inputToMs(f.punch_in.value); const pout = inputToMs(f.punch_out.value);
  if(pin) body.punch_in = pin;
  if(pout) body.punch_out = pout;
  try {
    await api('/attendance/' + encodeURIComponent(editing.emp_code) + '/' + encodeURIComponent(editing.date),
      { method:'PATCH', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    toast('Attendance updated'); $('#overlay').classList.remove('on'); loadAttendance();
  } catch(err){ toast(err.message, false); }
});

loadHealth(); loadEmployees(); loadAttendance();
</script>
</body>
</html>"""


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def from_millis(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime truncated to whole seconds."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc).replace(microsecond=0)


def to_millis(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return int(dt.timestamp()) * 1000 + dt.microsecond // 1000


def round_half_up(value, places: int):
    """R8: round half-up (not banker's rounding) to `places` decimals."""
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def parse_hhmm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def shift_datetime(day: date, hhmm: str) -> datetime:
    h, m = parse_hhmm(hhmm)
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    """R1: the IST calendar date the shift belongs to."""
    ist = punch_in.astimezone(IST)
    d = ist.date()
    if shift_end <= shift_start:  # overnight shift
        if ist.strftime("%H:%M") < shift_end:
            d = d - timedelta(days=1)
    return d


def compute_late_minutes(punch_in: datetime, day: date, shift_start: str) -> int:
    """R2: 0 inside the 10-minute grace, otherwise floor minutes since shift start."""
    start = shift_datetime(day, shift_start)
    seconds = int((punch_in.astimezone(IST) - start).total_seconds())
    if seconds > 600:
        return seconds // 60
    return 0


def compute_overtime_minutes(punch_out: datetime, day: date, shift_start: str, shift_end: str) -> int:
    """R3: floor minutes past shift end, only when >= 30."""
    end_day = day + timedelta(days=1) if shift_end <= shift_start else day
    end = shift_datetime(end_day, shift_end)
    seconds = int((punch_out.astimezone(IST) - end).total_seconds())
    if seconds >= 1800:
        return seconds // 60
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: duration in hours, 2 decimals half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def month_bounds(month: str) -> tuple[date, date]:
    year, mon = int(month[:4]), int(month[5:7])
    first = date(year, mon, 1)
    last = date(year, 12, 31) if mon == 12 else date(year, mon + 1, 1) - timedelta(days=1)
    return first, last


def working_days(joined_on: str, month: str) -> int:
    """R7: Mon-Fri in the month, counted from joined_on."""
    first, last = month_bounds(month)
    start = max(first, date.fromisoformat(joined_on))
    if start > last:
        return 0
    total = 0
    d = start
    while d <= last:
        if d.isoweekday() <= 5:
            total += 1
        d += timedelta(days=1)
    return total


def validate_iso_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise HTTPException(422, f"{field} must be a date in YYYY-MM-DD format")


def record_to_api(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_millis(doc.get("punch_in")),
        "punch_out": to_millis(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_to_api(h) for h in (doc.get("history") or [])],
    }


def history_to_api(entry: dict) -> dict:
    changes = {}
    for key, value in (entry.get("changes") or {}).items():
        frm, to = value.get("from"), value.get("to")
        if key in ("punch_in", "punch_out"):
            frm, to = to_millis(frm), to_millis(to)
        changes[key] = {"from": frm, "to": to}
    return {
        "at": to_millis(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def employee_to_api(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_millis(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
TIME_RE = r"^([01]\d|2[0-3]):[0-5]\d$"
DATE_RE = r"^\d{4}-\d{2}-\d{2}$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=TIME_RE)
    shift_end: str = Field(default="18:30", pattern=TIME_RE)
    joined_on: str = Field(pattern=DATE_RE)

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: EpochMs = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: EpochMs = None


class RegularizeIn(BaseModel):
    status: Status = None
    punch_in: EpochMs = None
    punch_out: EpochMs = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    return INDEX_HTML


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_to_api(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    total = employees.count_documents(q)
    cursor = (
        employees.find(q, {"_id": 0})
        .sort([("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [employee_to_api(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    ts = from_millis(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, day, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        logs.insert_one(doc)  # unique index (emp_code, date) makes this race-safe
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_to_api(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    ts = from_millis(body.punched_at) if body.punched_at is not None else now_utc()

    rec = logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        # No record at or before the requested instant. If the employee did punch in
        # but only later, the time itself is invalid (422); otherwise 404.
        if logs.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None}}):
            raise HTTPException(422, "punched_at is before the employee's punch-in")
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "record already punched out")

    punch_in = rec["punch_in"]
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)
    delta = (ts - punch_in).total_seconds()
    if delta <= 0 or delta > 86400:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")

    day = date.fromisoformat(rec["date"])
    work_hours = compute_work_hours(punch_in, ts)
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},  # atomic: only one punch-out wins
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": compute_overtime_minutes(
                    ts, day, emp["shift_start"], emp["shift_end"]
                ),
                "half_day": work_hours < 4.50,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record already punched out")
    return record_to_api(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to:
        validate_iso_date(date_from, "date_from")
        validate_iso_date(date_to, "date_to")
        if date_from > date_to:
            raise HTTPException(422, "date_from must be on or before date_to")
    elif date_from:
        validate_iso_date(date_from, "date_from")
    elif date_to:
        validate_iso_date(date_to, "date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        rng = {}
        if date_from:
            rng["$gte"] = date_from
        if date_to:
            rng["$lte"] = date_to
        q["date"] = rng
    if status:
        q["status"] = status

    total = logs.count_documents(q)
    cursor = (
        logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [record_to_api(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    if not _re_full(DATE_RE, date):
        raise HTTPException(422, "date must be YYYY-MM-DD")
    validate_iso_date(date, "date")
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    rec = logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    provided = body.model_fields_set
    new_status = body.status if "status" in provided else rec["status"]

    if new_status in ("ABSENT", "LEAVE"):
        if "punch_in" in provided or "punch_out" in provided:
            raise HTTPException(422, "ABSENT/LEAVE records cannot have punch times")
        new_punch_in = None
        new_punch_out = None
    else:
        new_punch_in = from_millis(body.punch_in) if "punch_in" in provided else rec.get("punch_in")
        new_punch_out = from_millis(body.punch_out) if "punch_out" in provided else rec.get("punch_out")
        if new_punch_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")

    if new_punch_in is not None:
        if attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"]).isoformat() != date:
            raise HTTPException(422, "punch_in must fall on the record's attendance date")
    if new_punch_in is not None and new_punch_out is not None:
        pin = new_punch_in if new_punch_in.tzinfo else new_punch_in.replace(tzinfo=timezone.utc)
        pout = new_punch_out if new_punch_out.tzinfo else new_punch_out.replace(tzinfo=timezone.utc)
        delta = (pout - pin).total_seconds()
        if delta <= 0 or delta > 86400:
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    # Recompute derived fields from the final times (R2-R5).
    if new_punch_in is not None:
        new_late = compute_late_minutes(new_punch_in, date_type.fromisoformat(date), emp["shift_start"])
    else:
        new_late = 0
    if new_punch_in is not None and new_punch_out is not None:
        new_work = compute_work_hours(new_punch_in, new_punch_out)
        new_ot = compute_overtime_minutes(
            new_punch_out, date_type.fromisoformat(date), emp["shift_start"], emp["shift_end"]
        )
        new_half = new_work < 4.50
    else:
        new_work, new_ot, new_half = None, 0, False

    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes", 0),
        "overtime_minutes": rec.get("overtime_minutes", 0),
        "half_day": bool(rec.get("half_day", False)),
    }
    new = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": new_work,
        "late_minutes": new_late,
        "overtime_minutes": new_ot,
        "half_day": new_half,
    }
    changes = {k: {"from": old[k], "to": new[k]} for k in old if old[k] != new[k]}
    if not changes:
        raise HTTPException(422, "the request does not change anything")

    entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    original_history = rec.get("history", [])
    history_filter = (
        {"history": original_history}
        if rec.get("history") is not None
        else {"history": {"$exists": False}}
    )
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], **history_filter},  # optimistic concurrency on history
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work,
                "late_minutes": new_late,
                "overtime_minutes": new_ot,
                "half_day": new_half,
                "history": original_history + [entry],
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, please retry")
    return record_to_api(updated)


def _re_full(pattern: str, value: str) -> bool:
    return re.fullmatch(pattern, value) is not None


@app.delete("/attendance/{emp_code}/{date}")
def delete_attendance(emp_code: str, date: str):
    if not _re_full(DATE_RE, date):
        raise HTTPException(422, "date must be YYYY-MM-DD")
    validate_iso_date(date, "date")
    result = logs.delete_one({"emp_code": emp_code, "date": date})
    if result.deleted_count == 0:
        raise HTTPException(404, "no attendance record for that date")
    return {"deleted": True, "emp_code": emp_code, "date": date}


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    start, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": last.isoformat()}}},
        {
            "$group": {
                "_id": None,
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [LATE_POS_EXPR, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                "present_units": {"$sum": PRESENT_UNIT_WEEKDAY},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    rows = list(logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    agg = rows[0] if rows else {}
    present_units = agg.get("present_units", 0)
    wd = working_days(emp["joined_on"], month)
    pct = round_half_up(Decimal(str(present_units)) / Decimal(wd) * 100, 4) if wd > 0 else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": present_units,
        "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


def department_summary_pipeline(start: str, end: str, department: Optional[str]) -> list:
    headcount_match = {"joined_on": {"$lte": end}}
    metrics_match = []
    if department:
        headcount_match["department"] = department
        metrics_match = [{"$match": {"emp.department": department}}]
    return [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
        *metrics_match,
        {
            "$group": {
                "_id": "$emp.department",
                "present_units": {"$sum": PRESENT_UNIT_WEEKDAY},
                "late_count": {"$sum": {"$cond": [LATE_POS_EXPR, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                "work_hours_sum": {"$sum": {"$cond": [HAS_WORK_HOURS, "$work_hours", 0]}},
                "work_hours_cnt": {"$sum": {"$cond": [HAS_WORK_HOURS, 1, 0]}},
            }
        },
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": headcount_match},
                    {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
                    {"$project": {"department": "$_id", "headcount": 1, "_id": 0}},
                ],
            }
        },
        {
            "$group": {
                "_id": {"$ifNull": ["$department", "$_id"]},
                "headcount": {"$max": {"$ifNull": ["$headcount", 0]}},
                "present_units": {"$sum": "$present_units"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_cnt": {"$sum": "$work_hours_cnt"},
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    start, last = month_bounds(month)
    rows = list(
        logs.aggregate(
            department_summary_pipeline(start.isoformat(), last.isoformat(), department)
        )
    )
    items = []
    for r in rows:
        cnt = r.get("work_hours_cnt", 0)
        avg = round_half_up(Decimal(str(r.get("work_hours_sum", 0))) / Decimal(cnt), 2) if cnt else None
        items.append(
            {
                "department": r["_id"],
                "headcount": r["headcount"],
                "present_days": r.get("present_units", 0),
                "avg_work_hours": avg,
                "late_count": r.get("late_count", 0),
                "total_late_minutes": r.get("total_late_minutes", 0),
                "leave_count": r.get("leave_count", 0),
                "on_duty_count": r.get("on_duty_count", 0),
            }
        )
    return {"month": month, "items": items}


def late_leaderboard_pipeline(start: str, end: str, department: Optional[str], limit: int) -> list:
    dept_match = [{"$match": {"emp.department": department}}] if department else []
    return [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": {"$cond": [LATE_POS_EXPR, 1, 0]}},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs whose employee does not exist are ignored
        *dept_match,
        {
            "$set": {
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": 1,
                "name": 1,
                "department": 1,
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, last = month_bounds(month)
    rows = list(
        logs.aggregate(
            late_leaderboard_pipeline(start.isoformat(), last.isoformat(), department, limit)
        )
    )
    return {"month": month, "items": rows}


def department_trend_pipeline(department: str, start: str, end: str, base_headcount: int) -> list:
    start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    end_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
    return [
        {
            "$documents": [
                {"date": start_dt, "present_units": 0, "late_count": 0, "joined": 0},
                {"date": end_dt, "present_units": 0, "late_count": 0, "joined": 0},
            ]
        },
        {
            "$unionWith": {
                "coll": "attendance_logs",
                "pipeline": [
                    {"$match": {"date": {"$gte": start, "$lte": end}}},
                    {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
                    {"$unwind": "$emp"},
                    {"$match": {"emp.department": department}},
                    {
                        "$group": {
                            "_id": "$date",
                            "present_units": {"$sum": PRESENT_UNIT_ANYDAY},
                            "late_count": {"$sum": {"$cond": [LATE_POS_EXPR, 1, 0]}},
                        }
                    },
                    {
                        "$project": {
                            "date": {"$dateFromString": {"dateString": "$_id"}},
                            "present_units": 1,
                            "late_count": 1,
                            "_id": 0,
                        }
                    },
                ],
            }
        },
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department, "joined_on": {"$gte": start, "$lte": end}}},
                    {"$group": {"_id": "$joined_on", "joined": {"$sum": 1}}},
                    {"$project": {"date": {"$dateFromString": {"dateString": "$_id"}}, "joined": 1, "_id": 0}},
                ],
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_units": {"$sum": "$present_units"},
                "late_count": {"$sum": "$late_count"},
                "joined": {"$sum": "$joined"},
            }
        },
        {
            "$project": {
                "date": "$_id",
                "present_units": {"$ifNull": ["$present_units", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "joined": {"$ifNull": ["$joined", 0]},
                "_id": 0,
            }
        },
        {"$sort": {"date": 1}},
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt]}}},
        {
            "$set": {
                "present_units": {"$ifNull": ["$present_units", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "joined": {"$ifNull": ["$joined", 0]},
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {"cum_joined": {"$sum": "$joined", "window": {"documents": ["unbounded", "current"]}}},
            }
        },
        {"$set": {"headcount": {"$add": ["$cum_joined", base_headcount]}}},
        {
            "$set": {
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$date"}, 5]},
                "attendance_rate": {
                    "$cond": [
                        {"$and": [{"$lte": [{"$isoDayOfWeek": "$date"}, 5]}, {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_units", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {"$sort": {"date": 1}},
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"date": "$date", "format": "%Y-%m-%d", "timezone": "UTC"}},
                "is_working_day": 1,
                "headcount": 1,
                "present_units": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(alias="from", pattern=DATE_RE),
    to: str = Query(pattern=DATE_RE),
):
    from_date = validate_iso_date(from_, "from")
    to_date = validate_iso_date(to, "to")
    span = (to_date - from_date).days
    if span < 0:
        raise HTTPException(422, "to must be on or after from")
    if span > 92:
        raise HTTPException(422, "range must not exceed 92 days")

    if employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "unknown department")

    base = employees.count_documents({"department": department, "joined_on": {"$lt": from_}})
    rows = list(
        # Database-level aggregation: the pipeline starts with $documents (seed rows).
        db.aggregate(department_trend_pipeline(department, from_, to, base))
    )
    items = []
    for r in rows:
        rate = r.get("attendance_rate")
        mov = r.get("moving_avg_7d")
        items.append(
            {
                "date": r["date"],
                "is_working_day": r["is_working_day"],
                "headcount": r["headcount"],
                "present_count": r.get("present_units", 0),
                "late_count": r.get("late_count", 0),
                "attendance_rate": round_half_up(rate, 4) if rate is not None else None,
                "moving_avg_7d": round_half_up(mov, 4) if mov is not None else None,
            }
        )
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
EXPLAINABLE = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from", pattern=DATE_RE),
    to: Optional[str] = Query(None, pattern=DATE_RE),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint not in EXPLAINABLE:
        raise HTTPException(422, f"endpoint must be one of {sorted(EXPLAINABLE)}")

    if endpoint == "attendance_list":
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            rng = {}
            if date_from:
                rng["$gte"] = date_from
            if date_to:
                rng["$lte"] = date_to
            q["date"] = rng
        if status:
            q["status"] = status
        explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
    else:
        if endpoint == "employee_monthly":
            if not emp_code or not month:
                raise HTTPException(422, "emp_code and month are required")
            pipeline = employee_monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            if not month:
                raise HTTPException(422, "month is required")
            start, last = month_bounds(month)
            pipeline = department_summary_pipeline(start.isoformat(), last.isoformat(), department)
        elif endpoint == "late_leaderboard":
            if not month:
                raise HTTPException(422, "month is required")
            start, last = month_bounds(month)
            pipeline = late_leaderboard_pipeline(start.isoformat(), last.isoformat(), department, limit)
        else:  # department_trend
            if not department or not from_ or not to:
                raise HTTPException(422, "department, from and to are required")
            base = employees.count_documents({"department": department, "joined_on": {"$lt": from_}})
            pipeline = department_trend_pipeline(department, from_, to, base)
        if endpoint == "department_trend":
            explain = db.command(
                "explain",
                {"aggregate": 1, "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )
        else:
            explain = db.command(
                "explain",
                {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )

    safe = json_util.loads(json_util.dumps(explain))
    return {"endpoint": endpoint, "collection": "attendance_logs", "explain": safe}
