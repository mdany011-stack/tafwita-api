"""TAFWITA API unifiee.
Compatible avec l'ancien schema cabinets/patient_tickets et l'app desktop v7.

Variables Render:
  DATABASE_URL=postgresql://...
Demarrage:
  uvicorn main:app --host 0.0.0.0 --port 10000
"""
import os
import re
import unicodedata
from datetime import datetime, timedelta, date
from typing import Optional, Any

from fastapi import FastAPI, HTTPException, Depends, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import (
    create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey,
    Text, func, inspect, text
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship

DATABASE_URL = os.getenv("DATABASE_URL") or "sqlite:///./fallback.db"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

class Cabinet(Base):
    __tablename__ = "cabinets"
    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String, unique=True, index=True, nullable=False)
    nom_medecin = Column(String, nullable=False)
    specialite = Column(String, nullable=True)
    telephone = Column(String, unique=True, nullable=True)
    wilaya = Column(String, nullable=True)
    code_pin = Column(String, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    accept_tickets = Column(Boolean, default=True, nullable=False)
    subscription_type = Column(String, default="trial_14d")
    subscription_end = Column(DateTime, nullable=True)
    serving_num = Column(Integer, default=0, nullable=False)
    total_issued = Column(Integer, default=0, nullable=False)
    day_closed = Column(Boolean, default=False, nullable=False)
    day_closed_at = Column(DateTime, nullable=True)
    tickets = relationship("PatientTicket", back_populates="cabinet", cascade="all, delete-orphan")

class PatientTicket(Base):
    __tablename__ = "patient_tickets"
    id = Column(Integer, primary_key=True, index=True)
    cabinet_slug = Column(String, ForeignKey("cabinets.slug"), index=True, nullable=False)
    ticket_num = Column(Integer, nullable=False)
    nom_patient = Column(String, nullable=False)
    telephone = Column(String, nullable=True)
    user_id = Column(Integer, nullable=True)
    statut = Column(String, default="waiting", nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    suspended_at = Column(DateTime, nullable=True)
    notification_count = Column(Integer, default=0, nullable=False)
    events = relationship("TicketEvent", back_populates="ticket", cascade="all, delete-orphan")
    cabinet = relationship("Cabinet", back_populates="tickets")

class TicketEvent(Base):
    __tablename__ = "ticket_events"
    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(Integer, ForeignKey("patient_tickets.id"), nullable=False)
    event_type = Column(String, nullable=False)
    reason_code = Column(String, nullable=True)
    reason_note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    ticket = relationship("PatientTicket", back_populates="events")

class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=True)
    ticket_id = Column(Integer, nullable=True)
    title = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    is_read = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

class CabinetRegister(BaseModel):
    nom_medecin: str
    specialite: str
    telephone: str
    wilaya: str
    code_pin: str

class CreateCabinetRequest(CabinetRegister):
    slug: Optional[str] = None
    adresse: Optional[str] = None
    heure_ouverture: Optional[str] = None
    heure_fermeture: Optional[str] = None

class PatientTicketCreate(BaseModel):
    cabinet_slug: str
    nom_patient: str
    telephone: Optional[str] = None
    user_id: Optional[int] = None

class VerifyPinRequest(BaseModel):
    code_pin: str

class AddManualTicketRequest(BaseModel):
    cabinet_slug: str
    nom_patient: str
    telephone: Optional[str] = None

class SettingsUpdate(BaseModel):
    adresse: Optional[str] = None
    heure_ouverture: Optional[str] = None
    heure_fermeture: Optional[str] = None
    photo_url: Optional[str] = None
    accept_tickets: Optional[bool] = None

class CloseDayRequest(BaseModel):
    reason: str = "cabinet_closed"
    notify_patients: bool = True

app = FastAPI(title="TAFWITA API", version="8.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "cabinet"

def today_range():
    start = datetime.combine(date.today(), datetime.min.time())
    return start, start + timedelta(days=1)

def cabinet_or_404(db: Session, slug: str):
    cabinet = db.query(Cabinet).filter(Cabinet.slug == slug).first()
    if not cabinet:
        raise HTTPException(404, "Cabinet introuvable")
    return cabinet

def check_pin(cabinet: Cabinet, pin: str):
    if str(cabinet.code_pin) != str(pin):
        raise HTTPException(403, "Code PIN médecin incorrect")

def add_event(db, ticket, event_type, reason_code=None, reason_note=None):
    event = TicketEvent(ticket_id=ticket.id, event_type=event_type, reason_code=reason_code, reason_note=reason_note)
    db.add(event)
    return event

def notify_patient(db, ticket, title, message):
    if ticket.user_id is None:
        return False
    db.add(Notification(user_id=ticket.user_id, ticket_id=ticket.id, title=title, message=message))
    return True

def ticket_dict(t):
    return {
        "id": t.id, "cabinet_slug": t.cabinet_slug, "ticket_num": t.ticket_num,
        "nom_patient": t.nom_patient, "telephone": t.telephone, "user_id": t.user_id,
        "statut": t.statut, "duree_min": None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        "notification_count": t.notification_count or 0,
    }

def ensure_schema():
    Base.metadata.create_all(bind=engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            statements = [
                "ALTER TABLE cabinets ADD COLUMN IF NOT EXISTS accept_tickets BOOLEAN NOT NULL DEFAULT TRUE",
                "ALTER TABLE cabinets ADD COLUMN IF NOT EXISTS day_closed BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE cabinets ADD COLUMN IF NOT EXISTS day_closed_at TIMESTAMP NULL",
                "ALTER TABLE patient_tickets ADD COLUMN IF NOT EXISTS user_id INTEGER NULL",
                "ALTER TABLE patient_tickets ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NULL",
                "ALTER TABLE patient_tickets ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMP NULL",
                "ALTER TABLE patient_tickets ADD COLUMN IF NOT EXISTS notification_count INTEGER NOT NULL DEFAULT 0",
                "CREATE TABLE IF NOT EXISTS ticket_events (id SERIAL PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES patient_tickets(id), event_type VARCHAR NOT NULL, reason_code VARCHAR NULL, reason_note TEXT NULL, created_at TIMESTAMP NOT NULL DEFAULT NOW())",
                "CREATE TABLE IF NOT EXISTS notifications (id SERIAL PRIMARY KEY, user_id INTEGER NULL, ticket_id INTEGER NULL, title VARCHAR NOT NULL, message TEXT NOT NULL, is_read BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMP NOT NULL DEFAULT NOW())",
            ]
            for statement in statements:
                conn.execute(text(statement))

ensure_schema()

@app.get("/")
def home():
    return {"status": "ok", "message": "API TAFWITA connectee", "version": app.version}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/api/cabinets/register")
def register_cabinet(data: CabinetRegister, db: Session = Depends(get_db)):
    slug = slugify(data.nom_medecin)
    if db.query(Cabinet).filter(Cabinet.telephone == data.telephone).first():
        raise HTTPException(400, "Ce numero de telephone est deja enregistre.")
    if db.query(Cabinet).filter(Cabinet.slug == slug).first():
        slug = f"{slug}-{int(datetime.utcnow().timestamp())}"
    trial_end = datetime.utcnow() + timedelta(days=14)
    cabinet = Cabinet(slug=slug, nom_medecin=data.nom_medecin, specialite=data.specialite, telephone=data.telephone, wilaya=data.wilaya, code_pin=data.code_pin, subscription_end=trial_end, accept_tickets=True)
    db.add(cabinet); db.commit(); db.refresh(cabinet)
    return {"status":"success", "message":"Cabinet cree avec succes", "cabinet_slug":slug, "tv_url":f"/tv?cabinet={slug}", "trial_end":trial_end.date().isoformat()}

@app.post("/api/cabinets")
def create_cabinet(data: CreateCabinetRequest, db: Session = Depends(get_db)):
    slug = slugify(data.slug or data.nom_medecin)
    if db.query(Cabinet).filter(Cabinet.slug == slug).first():
        raise HTTPException(400, "Ce slug existe deja")
    if data.telephone and db.query(Cabinet).filter(Cabinet.telephone == data.telephone).first():
        raise HTTPException(400, "Ce numero de telephone est deja enregistre")
    cabinet = Cabinet(slug=slug, nom_medecin=data.nom_medecin, specialite=data.specialite, telephone=data.telephone, wilaya=data.wilaya, code_pin=data.code_pin, accept_tickets=True)
    db.add(cabinet); db.commit(); db.refresh(cabinet)
    return {"status":"success", "message":"Cabinet cree avec succes", "cabinet_slug":slug, "cabinet_nom":cabinet.nom_medecin}

@app.get("/api/cabinets")
def list_cabinets(db: Session = Depends(get_db)):
    return [{"slug":c.slug, "nom":c.nom_medecin, "nom_medecin":c.nom_medecin, "specialite":c.specialite, "wilaya":c.wilaya, "accept_tickets":c.accept_tickets, "is_active":c.is_active} for c in db.query(Cabinet).all()]

@app.post("/api/cabinets/{slug}/verify-pin")
def verify_pin(slug: str, request: VerifyPinRequest, db: Session = Depends(get_db)):
    c = cabinet_or_404(db, slug); check_pin(c, request.code_pin)
    return {"cabinet_slug":c.slug, "cabinet_nom":c.nom_medecin, "nom_medecin":c.nom_medecin, "code_pin":c.code_pin, "accept_tickets":c.accept_tickets}

@app.put("/api/cabinets/{slug}/settings")
def update_settings(slug: str, pin: str = Query(...), payload: SettingsUpdate = Body(...), db: Session = Depends(get_db)):
    c = cabinet_or_404(db, slug); check_pin(c, pin)
    if payload.accept_tickets is not None: c.accept_tickets = payload.accept_tickets
    db.commit(); db.refresh(c)
    return {"message":"Parametres mis a jour", "cabinet":{"slug":c.slug, "nom":c.nom_medecin, "accept_tickets":c.accept_tickets}}

@app.get("/api/cabinets/{slug}/settings")
def get_settings(slug: str, pin: str = Query(...), db: Session = Depends(get_db)):
    c = cabinet_or_404(db, slug); check_pin(c, pin)
    return {"slug":c.slug, "nom":c.nom_medecin, "nom_medecin":c.nom_medecin, "specialite":c.specialite, "accept_tickets":c.accept_tickets, "is_active":c.is_active}

@app.get("/api/queue/{cabinet_slug}")
def get_queue(cabinet_slug: str, db: Session = Depends(get_db)):
    c = cabinet_or_404(db, cabinet_slug)
    start, end = today_range()
    tickets = db.query(PatientTicket).filter(PatientTicket.cabinet_slug==cabinet_slug, PatientTicket.created_at>=start, PatientTicket.created_at<end).all()
    active = [t for t in tickets if t.statut in ("waiting","returned","suspended","urgent","serving")]
    waiting = len([t for t in tickets if t.statut in ("waiting","returned")])
    serving = next((t for t in tickets if t.statut == "serving"), None)
    return {"cabinet_slug":c.slug, "cabinet_name":c.nom_medecin, "cabinet_nom":c.nom_medecin, "specialite":c.specialite, "serving_num":serving.ticket_num if serving else c.serving_num, "display_serving":f"P-{serving.ticket_num:02d}" if serving else (f"P-{c.serving_num:02d}" if c.serving_num else "-"), "total_issued":c.total_issued, "waiting_count":waiting, "accept_tickets":bool(c.accept_tickets), "is_active":bool(c.is_active), "day_closed":bool(c.day_closed), "tickets": [ticket_dict(t) for t in active]}

@app.get("/api/cabinets/{slug}/tickets")
def cabinet_tickets(slug: str, db: Session = Depends(get_db)):
    cabinet_or_404(db, slug)
    return [ticket_dict(t) for t in db.query(PatientTicket).filter(PatientTicket.cabinet_slug==slug).order_by(PatientTicket.created_at.desc()).all()]

@app.get("/api/cabinets/{slug}/alerts")
def cabinet_alerts(slug: str, pin: str = Query(...), db: Session = Depends(get_db)):
    c = cabinet_or_404(db, slug); check_pin(c, pin)
    start, end = today_range()
    tickets = db.query(PatientTicket).filter(PatientTicket.cabinet_slug==slug, PatientTicket.created_at>=start, PatientTicket.created_at<end).all()
    return {"presence_confirmed": [t.id for t in tickets if t.statut=="waiting"], "urgent_requests": [t.id for t in tickets if t.statut=="urgent"]}

@app.post("/api/tickets/take")
def take_ticket(data: PatientTicketCreate, db: Session = Depends(get_db)):
    return _create_ticket(data, db)

@app.post("/api/tickets")
def create_ticket(data: PatientTicketCreate, db: Session = Depends(get_db)):
    return _create_ticket(data, db)

def _create_ticket(data, db):
    c = cabinet_or_404(db, data.cabinet_slug)
    if not c.is_active or not c.accept_tickets or c.day_closed:
        raise HTTPException(400, "La prise de tickets est actuellement arretee pour ce cabinet.")
    c.total_issued = (c.total_issued or 0) + 1
    t = PatientTicket(cabinet_slug=c.slug, ticket_num=c.total_issued, nom_patient=data.nom_patient, telephone=data.telephone, user_id=data.user_id, statut="waiting")
    db.add(t); db.flush(); add_event(db, t, "created", "patient_app", "Ticket cree depuis l application patient"); db.commit(); db.refresh(t)
    waiting = db.query(PatientTicket).filter(PatientTicket.cabinet_slug==c.slug, PatientTicket.statut.in_(["waiting","returned"]), PatientTicket.id != t.id).count()
    return {"status":"success", "message":"Ticket cree avec succes", "ticket_num":t.ticket_num, "display_ticket":f"N° P-{t.ticket_num:02d}", "waiting_before_you":waiting, "estimated_wait_min":waiting*12, "ticket":ticket_dict(t)}

@app.post("/api/tickets/add-manual")
def add_manual(data: AddManualTicketRequest, db: Session = Depends(get_db)):
    return _create_ticket(PatientTicketCreate(cabinet_slug=data.cabinet_slug, nom_patient=data.nom_patient, telephone=data.telephone), db)

@app.post("/api/queue/{cabinet_slug}/next")
def call_next(cabinet_slug: str, pin: str = Query(...), db: Session = Depends(get_db)):
    c = cabinet_or_404(db, cabinet_slug); check_pin(c, pin)
    current = db.query(PatientTicket).filter(PatientTicket.cabinet_slug==cabinet_slug, PatientTicket.statut=="serving").first()
    if current:
        current.statut = "completed"; add_event(db, current, "completed", "next_called", "Consultation terminee")
    t = db.query(PatientTicket).filter(PatientTicket.cabinet_slug==cabinet_slug, PatientTicket.statut.in_(["waiting","returned","urgent"])).order_by(PatientTicket.ticket_num).first()
    if not t:
        db.commit(); return {"message":"Aucun patient en attente", "ticket":None}
    t.statut="serving"; c.serving_num=t.ticket_num; add_event(db,t,"called","cabinet_call","Patient appele par le cabinet"); db.commit()
    if t.user_id: notify_patient(db,t,"Patient appele",f"Votre ticket P-{t.ticket_num:02d} est appele.") ; db.commit()
    return {"message":f"Patient P-{t.ticket_num:02d} appele", "ticket":ticket_dict(t), "serving_num":t.ticket_num, "display_serving":f"P-{t.ticket_num:02d}"}

@app.post("/api/tickets/{ticket_id}/{action}")
def ticket_action(ticket_id: int, action: str, payload: dict = Body(...), db: Session = Depends(get_db)):
    t = db.query(PatientTicket).filter(PatientTicket.id==ticket_id).first()
    if not t: raise HTTPException(404,"Ticket introuvable")
    c = cabinet_or_404(db,t.cabinet_slug); check_pin(c, payload.get("code_pin", ""))
    reason = payload.get("reason_code", "desktop_action"); note = payload.get("reason_note", "")
    if action == "suspend": t.statut="suspended"; t.suspended_at=datetime.utcnow(); add_event(db,t,"suspended",reason,note)
    elif action == "return-to-queue": t.statut="returned"; add_event(db,t,"returned",reason,note)
    elif action == "approve-urgent": t.statut="urgent"; add_event(db,t,"urgent_approved","urgent",payload.get("urgent_reason",""))
    elif action == "cabinet-cancel": raise HTTPException(400,"Annulation autorisee uniquement apres 5 notifications sans reponse et confirmation.")
    else: raise HTTPException(400,"Action non reconnue")
    db.commit(); return {"message":"Action enregistree", "ticket":ticket_dict(t)}

@app.post("/api/tickets/{ticket_id}/notify")
def notify_ticket(ticket_id: int, pin: str = Query(...), db: Session = Depends(get_db)):
    t = db.query(PatientTicket).filter(PatientTicket.id==ticket_id).first()
    if not t: raise HTTPException(404,"Ticket introuvable")
    c=cabinet_or_404(db,t.cabinet_slug); check_pin(c,pin)
    t.notification_count=(t.notification_count or 0)+1; add_event(db,t,"patient_notified","no_response",f"Passage {t.notification_count} sans reponse")
    if t.user_id: notify_patient(db,t,"Rappel de presence",f"Veuillez confirmer votre arrivee pour le ticket P-{t.ticket_num:02d}.")
    db.commit(); return {"notification_count":t.notification_count, "ticket":ticket_dict(t)}

@app.post("/api/tickets/{ticket_id}/cabinet-cancel")
def confirmed_cancel(ticket_id: int, payload: dict = Body(...), db: Session = Depends(get_db)):
    t=db.query(PatientTicket).filter(PatientTicket.id==ticket_id).first()
    if not t: raise HTTPException(404,"Ticket introuvable")
    c=cabinet_or_404(db,t.cabinet_slug); check_pin(c,payload.get("code_pin",""))
    passes=t.notification_count or 0
    if t.statut != "suspended" or passes < 5:
        raise HTTPException(400,"Annulation autorisee uniquement pour un ticket suspendu apres 5 notifications sans reponse.")
    t.statut="cancelled"; add_event(db,t,"cancelled","no_response_after_5",payload.get("reason_note","Patient non confirme apres 5 passages"))
    if t.user_id: notify_patient(db,t,"Ticket annule",f"Votre ticket P-{t.ticket_num:02d} a ete annule car le cabinet n a pas recu votre confirmation.")
    db.commit(); return {"message":"Ticket annule", "ticket":ticket_dict(t)}

@app.get("/api/tickets/{ticket_id}/events")
def ticket_events(ticket_id: int, db: Session=Depends(get_db)):
    if not db.query(PatientTicket).filter(PatientTicket.id==ticket_id).first(): raise HTTPException(404,"Ticket introuvable")
    return [{"id":e.id,"ticket_id":e.ticket_id,"event_type":e.event_type,"reason_code":e.reason_code,"reason_note":e.reason_note,"created_at":e.created_at.isoformat() if e.created_at else None} for e in db.query(TicketEvent).filter(TicketEvent.ticket_id==ticket_id).order_by(TicketEvent.created_at).all()]

@app.post("/api/cabinets/{slug}/close-day")
def close_day(slug: str, pin: str = Query(...), payload: CloseDayRequest = Body(...), db: Session = Depends(get_db)):
    c=cabinet_or_404(db,slug); check_pin(c,pin)
    start,end=today_range()
    tickets=db.query(PatientTicket).filter(PatientTicket.cabinet_slug==slug,PatientTicket.created_at>=start,PatientTicket.created_at<end,PatientTicket.statut.in_(["waiting","returned","suspended","urgent"])).all()
    closed=notified=0
    for t in tickets:
        t.statut="cancelled"; add_event(db,t,"day_closed",payload.reason,"Cabinet ferme pour aujourd hui"); closed+=1
        if payload.notify_patients and t.user_id:
            if notify_patient(db,t,"Ticket annule",f"Votre ticket P-{t.ticket_num:02d} a ete annule car le cabinet a ferme pour aujourd hui."): notified+=1
    c.day_closed=True; c.day_closed_at=datetime.utcnow(); c.accept_tickets=False; db.commit()
    return {"message":"Journee cloturee", "closed_count":closed, "notified_count":notified}

@app.get("/api/users/{user_id}/notifications")
def notifications(user_id: int, db: Session=Depends(get_db)):
    return [{"id":n.id,"user_id":n.user_id,"ticket_id":n.ticket_id,"title":n.title,"message":n.message,"is_read":n.is_read,"created_at":n.created_at.isoformat()} for n in db.query(Notification).filter(Notification.user_id==user_id).order_by(Notification.created_at.desc()).all()]

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
