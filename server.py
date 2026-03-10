"""
AgentPulse Backend — Railway-ready
Deploy: connect GitHub repo to Railway, it auto-deploys.
Default manager login: admin / admin123
"""

import json, uuid, hashlib, os
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict

from fastapi import FastAPI, HTTPException, Depends, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, Integer, Boolean, JSON, DateTime, Text
from sqlalchemy.orm import declarative_base, Session, sessionmaker
import pandas as pd
from fpdf import FPDF

# ── DB ─────────────────────────────────────────────────────────
DB_URL = os.getenv("DATABASE_URL", "sqlite:///./agentpulse.db")
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if "sqlite" in DB_URL else {}
engine = create_engine(DB_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

class Agent(Base):
    __tablename__ = "agents"
    id         = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name       = Column(String)
    email      = Column(String, index=True)
    machine_id = Column(String, index=True)
    os_user    = Column(String)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class AgentSession(Base):
    __tablename__ = "sessions"
    id         = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id   = Column(String, index=True)
    machine_id = Column(String)
    os_user    = Column(String)
    started_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    ended_at   = Column(DateTime, nullable=True)

class Heartbeat(Base):
    __tablename__ = "heartbeats"
    id              = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id      = Column(String, index=True)
    agent_id        = Column(String, index=True)
    machine_id      = Column(String, index=True)
    os_user         = Column(String)
    state           = Column(String)
    idle_seconds    = Column(Integer, default=0)
    active_app      = Column(String, nullable=True)
    active_title    = Column(Text, nullable=True)
    is_browser      = Column(Boolean, default=False)
    app_usage       = Column(JSON, default=dict)
    browser_profile = Column(JSON, nullable=True)
    timestamp       = Column(DateTime, index=True)

class Manager(Base):
    __tablename__ = "managers"
    id            = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    username      = Column(String, unique=True)
    password_hash = Column(String)

Base.metadata.create_all(engine)

def seed():
    db = SessionLocal()
    if not db.query(Manager).filter_by(username="admin").first():
        db.add(Manager(username="admin",
                       password_hash=hashlib.sha256(b"admin123").hexdigest()))
        db.commit()
        print("Default manager: admin / admin123")
    db.close()
seed()

app = FastAPI(title="AgentPulse")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

security = HTTPBearer(auto_error=False)
_tokens: Dict[str, str] = {}

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

def require_manager(creds: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    if not creds or creds.credentials not in _tokens:
        raise HTTPException(401, "Unauthorized")
    return _tokens[creds.credentials]

class SessionStartReq(BaseModel):
    machine_id: str
    os_user: str

class SessionEndReq(BaseModel):
    session_id: str

class HeartbeatReq(BaseModel):
    session_id: str
    machine_id: str
    os_user: str
    state: str
    idle_seconds: int = 0
    active_app: Optional[str] = None
    active_title: Optional[str] = None
    is_browser: bool = False
    app_usage: Dict[str, float] = {}
    browser_profile: Optional[Dict] = None
    timestamp: str

class BrowserEventReq(BaseModel):
    email: Optional[str] = None
    name: Optional[str] = None
    url: Optional[str] = None
    title: Optional[str] = None
    machine_id: Optional[str] = None
    ts: Optional[str] = None

class LoginReq(BaseModel):
    username: str
    password: str

@app.post("/api/session/start")
def session_start(req: SessionStartReq, db: Session = Depends(get_db)):
    agent = db.query(Agent).filter_by(machine_id=req.machine_id).first()
    if not agent:
        agent = Agent(machine_id=req.machine_id, os_user=req.os_user, name=req.os_user)
        db.add(agent); db.commit(); db.refresh(agent)
    s = AgentSession(agent_id=agent.id, machine_id=req.machine_id, os_user=req.os_user)
    db.add(s); db.commit(); db.refresh(s)
    return {"session_id": s.id, "agent_id": agent.id}

@app.post("/api/session/end")
def session_end(req: SessionEndReq, db: Session = Depends(get_db)):
    s = db.query(AgentSession).filter_by(id=req.session_id).first()
    if s: s.ended_at = datetime.now(timezone.utc); db.commit()
    return {"ok": True}

@app.post("/api/heartbeat")
def heartbeat(req: HeartbeatReq, db: Session = Depends(get_db)):
    agent = db.query(Agent).filter_by(machine_id=req.machine_id).first()
    agent_id = agent.id if agent else None
    if req.browser_profile and agent:
        email = req.browser_profile.get("email")
        if email:
            agent.email = email
            agent.name  = req.browser_profile.get("name") or agent.name
            db.commit()
    ts = datetime.fromisoformat(req.timestamp.replace("Z", "+00:00"))
    hb = Heartbeat(
        session_id=req.session_id, agent_id=agent_id,
        machine_id=req.machine_id, os_user=req.os_user,
        state=req.state, idle_seconds=req.idle_seconds,
        active_app=req.active_app, active_title=req.active_title,
        is_browser=req.is_browser, app_usage=req.app_usage,
        browser_profile=req.browser_profile, timestamp=ts,
    )
    db.add(hb); db.commit()
    return {"ok": True, "agent_id": agent_id}

@app.post("/api/browser-event")
def browser_event(req: BrowserEventReq, db: Session = Depends(get_db)):
    if req.email:
        agent = db.query(Agent).filter_by(email=req.email).first()
        if not agent and req.machine_id:
            agent = db.query(Agent).filter_by(machine_id=req.machine_id).first()
        if agent:
            agent.email = req.email
            if req.name: agent.name = req.name
            db.commit()
    return {"ok": True}

@app.post("/api/manager/login")
def manager_login(req: LoginReq, db: Session = Depends(get_db)):
    mgr = db.query(Manager).filter_by(username=req.username).first()
    if not mgr or mgr.password_hash != hashlib.sha256(req.password.encode()).hexdigest():
        raise HTTPException(401, "Invalid credentials")
    token = str(uuid.uuid4())
    _tokens[token] = req.username
    return {"token": token, "username": req.username}

@app.get("/api/live")
def live(db: Session = Depends(get_db), _=Depends(require_manager)):
    agents = db.query(Agent).all()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=3)
    result = []
    for a in agents:
        hb = (db.query(Heartbeat).filter_by(agent_id=a.id)
              .order_by(Heartbeat.timestamp.desc()).first())
        if hb and hb.timestamp.tzinfo is None:
            ts = hb.timestamp.replace(tzinfo=timezone.utc)
        elif hb:
            ts = hb.timestamp
        else:
            ts = None
        online = bool(ts and ts > cutoff)
        result.append({
            "id": a.id, "name": a.name or a.os_user or "Unknown",
            "email": a.email, "os_user": a.os_user,
            "status": hb.state if (hb and online) else "offline",
            "active_app": hb.active_app if hb else None,
            "active_title": (hb.active_title or "")[:100] if hb else None,
            "idle_seconds": hb.idle_seconds if hb else 0,
            "last_seen": ts.isoformat() if ts else None,
            "online": online,
        })
    return result

@app.get("/api/agent/{agent_id}/stats")
def agent_stats(agent_id: str, date: Optional[str] = None,
                db: Session = Depends(get_db), _=Depends(require_manager)):
    day = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc) if date \
          else datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    nxt = day + timedelta(days=1)
    hbs = (db.query(Heartbeat).filter(Heartbeat.agent_id == agent_id)
           .filter(Heartbeat.timestamp >= day).filter(Heartbeat.timestamp < nxt)
           .order_by(Heartbeat.timestamp).all())
    counts = {"active": 0, "idle": 0, "break": 0}
    apps: Dict[str, int] = {}
    timeline = []
    for hb in hbs:
        counts[hb.state] = counts.get(hb.state, 0) + 1
        if hb.app_usage:
            for k, v in hb.app_usage.items(): apps[k] = apps.get(k, 0) + int(v)
        timeline.append({"time": hb.timestamp.isoformat(), "state": hb.state,
                          "app": hb.active_app, "title": hb.active_title})
    total = sum(counts.values()) or 1
    return {
        "date": day.date().isoformat(), "counts": counts,
        "pct": {k: round(v / total * 100) for k, v in counts.items()},
        "app_usage": dict(sorted(apps.items(), key=lambda x: -x[1])[:15]),
        "timeline": timeline[-80:],
    }

@app.get("/api/export/csv")
def export_csv(date: Optional[str] = None, db: Session = Depends(get_db), _=Depends(require_manager)):
    day = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc) if date \
          else datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    nxt = day + timedelta(days=1)
    rows = []
    for a in db.query(Agent).all():
        hbs = db.query(Heartbeat).filter_by(agent_id=a.id)\
                .filter(Heartbeat.timestamp >= day).filter(Heartbeat.timestamp < nxt).all()
        counts = {"active": 0, "idle": 0, "break": 0}
        apps: Dict[str, int] = {}
        for hb in hbs:
            counts[hb.state] = counts.get(hb.state, 0) + 1
            if hb.app_usage:
                for k, v in hb.app_usage.items(): apps[k] = apps.get(k, 0) + int(v)
        total = sum(counts.values()) or 1
        rows.append({
            "Date": day.date().isoformat(), "Agent": a.name or a.os_user,
            "Email": a.email or "",
            "Active %": round(counts["active"] / total * 100),
            "Idle %": round(counts["idle"] / total * 100),
            "Break %": round(counts["break"] / total * 100),
            "Top App": max(apps, key=apps.get) if apps else "N/A",
        })
    csv_str = pd.DataFrame(rows).to_csv(index=False)
    return Response(csv_str, media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=agentpulse_{day.date()}.csv"})

@app.get("/api/export/pdf")
def export_pdf(date: Optional[str] = None, db: Session = Depends(get_db), _=Depends(require_manager)):
    day = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc) if date \
          else datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    nxt = day + timedelta(days=1)
    pdf = FPDF(); pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "AgentPulse Productivity Report", ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, f"Date: {day.date()}  |  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", ln=True)
    pdf.ln(4)
    pdf.set_fill_color(30, 30, 60); pdf.set_text_color(255, 255, 255); pdf.set_font("Helvetica", "B", 9)
    for col, w in [("Agent",45),("Email",55),("Active%",22),("Idle%",22),("Break%",22),("Top App",30)]:
        pdf.cell(w, 8, col, border=1, fill=True)
    pdf.ln(); pdf.set_text_color(0, 0, 0); pdf.set_font("Helvetica", "", 9)
    for i, a in enumerate(db.query(Agent).all()):
        hbs = db.query(Heartbeat).filter_by(agent_id=a.id)\
                .filter(Heartbeat.timestamp >= day).filter(Heartbeat.timestamp < nxt).all()
        counts = {"active": 0, "idle": 0, "break": 0}
        apps: Dict[str, int] = {}
        for hb in hbs:
            counts[hb.state] = counts.get(hb.state, 0) + 1
            if hb.app_usage:
                for k, v in hb.app_usage.items(): apps[k] = apps.get(k, 0) + int(v)
        total = sum(counts.values()) or 1
        top = max(apps, key=apps.get) if apps else "N/A"
        fill = i % 2 == 0
        if fill: pdf.set_fill_color(240, 240, 248)
        for val, w in [(a.name or a.os_user or "?", 45), (a.email or "-", 55),
                       (f"{round(counts['active']/total*100)}%", 22),
                       (f"{round(counts['idle']/total*100)}%", 22),
                       (f"{round(counts['break']/total*100)}%", 22), (top[:18], 30)]:
            pdf.cell(w, 7, str(val), border=1, fill=fill)
        pdf.ln()
    return Response(bytes(pdf.output()), media_type="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename=agentpulse_{day.date()}.pdf"})

@app.get("/health")
def health(): return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
