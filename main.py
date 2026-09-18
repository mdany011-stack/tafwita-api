import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, or_
from sqlalchemy.orm import declarative_base, sessionmaker, Session

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL or "sqlite:///./fallback.db")
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Cabinet(Base):
    __tablename__ = "cabinets"
    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String, unique=True, index=True)
    nom_medecin = Column(String)
    specialite = Column(String)
    telephone = Column(String, unique=True)
    wilaya = Column(String)
    adresse = Column(String, nullable=True)
    heure_ouverture = Column(String, default="08:00")
    heure_fermeture = Column(String, default="17:00")
    photo_url = Column(String, nullable=True)
    code_pin = Column(String)
    is_active = Column(Boolean, default=True)
    subscription_type = Column(String, default="trial_14d")
    subscription_end = Column(DateTime)
    serving_num = Column(Integer, default=0)
    total_issued = Column(Integer, default=0)


class PatientTicket(Base):
    __tablename__ = "patient_tickets"
    id = Column(Integer, primary_key=True, index=True)
    cabinet_slug = Column(String, index=True)
    ticket_num = Column(Integer)
    nom_patient = Column(String)
    telephone = Column(String, nullable=True)
    statut = Column(String, default="waiting")
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


Base.metadata.create_all(bind=engine)

app = FastAPI(title="TAFWITA API", description="API de gestion de file d'attente pour cabinets medicaux", version="1.3.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class CabinetRegister(BaseModel):
    nom_medecin: str
    specialite: str
    telephone: str
    wilaya: str
    code_pin: str


class CabinetSettingsUpdate(BaseModel):
    adresse: Optional[str] = None
    heure_ouverture: Optional[str] = None
    heure_fermeture: Optional[str] = None
    photo_url: Optional[str] = None


class PatientTicketCreate(BaseModel):
    cabinet_slug: str
    nom_patient: str
    telephone: Optional[str] = None


class PinVerifyRequest(BaseModel):
    code_pin: str


def cabinet_public_data(cabinet):
    waiting = max(0, cabinet.total_issued - cabinet.serving_num)
    return {
        "id": cabinet.id,
        "slug": cabinet.slug,
        "nom_medecin": cabinet.nom_medecin,
        "specialite": cabinet.specialite,
        "telephone": cabinet.telephone,
        "wilaya": cabinet.wilaya,
        "adresse": cabinet.adresse,
        "heure_ouverture": cabinet.heure_ouverture,
        "heure_fermeture": cabinet.heure_fermeture,
        "photo_url": cabinet.photo_url,
        "waiting_count": waiting,
        "is_active": cabinet.is_active,
    }


@app.get("/")
def home():
    return {"message": "API TAFWITA connectee a PostgreSQL avec succes."}


@app.get("/api/cabinets")
def list_public_cabinets(q: Optional[str] = Query(default=None, max_length=100), specialite: Optional[str] = Query(default=None, max_length=100), wilaya: Optional[str] = Query(default=None, max_length=100), db: Session = Depends(get_db)):
    query = db.query(Cabinet).filter(Cabinet.is_active.is_(True))
    if q:
        pattern = "%" + q.strip() + "%"
        query = query.filter(or_(Cabinet.nom_medecin.ilike(pattern), Cabinet.specialite.ilike(pattern), Cabinet.wilaya.ilike(pattern), Cabinet.adresse.ilike(pattern)))
    if specialite:
        query = query.filter(Cabinet.specialite.ilike("%" + specialite.strip() + "%"))
    if wilaya:
        query = query.filter(Cabinet.wilaya.ilike("%" + wilaya.strip() + "%"))
    cabinets = query.order_by(Cabinet.nom_medecin.asc()).limit(100).all()
    return [cabinet_public_data(c) for c in cabinets]


@app.get("/api/cabinets/{cabinet_slug}/public")
def get_public_cabinet(cabinet_slug: str, db: Session = Depends(get_db)):
    cabinet = db.query(Cabinet).filter(Cabinet.slug == cabinet_slug, Cabinet.is_active.is_(True)).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet introuvable.")
    return cabinet_public_data(cabinet)


# Verification du PIN pour l'ecran de connexion : ne modifie jamais la file d'attente.
@app.post("/api/cabinets/{cabinet_slug}/verify-pin")
def verify_pin(cabinet_slug: str, data: PinVerifyRequest, db: Session = Depends(get_db)):
    cabinet = db.query(Cabinet).filter(Cabinet.slug == cabinet_slug).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet introuvable.")
    if cabinet.code_pin != data.code_pin:
        raise HTTPException(status_code=403, detail="Code PIN incorrect.")
    return {
        "status": "success",
        "cabinet_slug": cabinet.slug,
        "cabinet_nom": cabinet.nom_medecin,
        "specialite": cabinet.specialite,
    }


@app.post("/api/cabinets/register")
def register_cabinet(data: CabinetRegister, db: Session = Depends(get_db)):
    slug = data.nom_medecin.lower().replace(" ", "-").replace(".", "")
    existing = db.query(Cabinet).filter(Cabinet.telephone == data.telephone).first()
    if existing:
        raise HTTPException(status_code=400, detail="Ce numero de telephone est deja enregistre.")
    if db.query(Cabinet).filter(Cabinet.slug == slug).first():
        slug = slug + "-" + str(int(datetime.utcnow().timestamp()))
    trial_end = datetime.utcnow() + timedelta(days=14)
    cabinet = Cabinet(slug=slug, nom_medecin=data.nom_medecin, specialite=data.specialite, telephone=data.telephone, wilaya=data.wilaya, code_pin=data.code_pin, subscription_end=trial_end)
    db.add(cabinet)
    db.commit()
    db.refresh(cabinet)
    return {"status": "success", "message": "Cabinet cree avec succes ! 14 jours d'essai actives.", "cabinet_slug": cabinet.slug, "tv_url": "/tv?cabinet=" + cabinet.slug, "trial_end": trial_end.strftime("%Y-%m-%d")}


@app.post("/api/tickets/take")
def take_ticket(data: PatientTicketCreate, db: Session = Depends(get_db)):
    cabinet = db.query(Cabinet).filter(Cabinet.slug == data.cabinet_slug, Cabinet.is_active.is_(True)).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet introuvable ou ferme.")
    cabinet.total_issued += 1
    ticket = PatientTicket(cabinet_slug=cabinet.slug, ticket_num=cabinet.total_issued, nom_patient=data.nom_patient, telephone=data.telephone)
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    waiting = max(0, cabinet.total_issued - cabinet.serving_num)
    return {"status": "success", "ticket_id": ticket.id, "ticket_num": ticket.ticket_num, "display_ticket": "N. P-" + format(ticket.ticket_num, "02d"), "waiting_before_you": max(0, waiting - 1), "estimated_wait_min": max(0, waiting - 1) * 12}


@app.post("/api/tickets/add-manual")
def add_manual_ticket(data: PatientTicketCreate, db: Session = Depends(get_db)):
    cabinet = db.query(Cabinet).filter(Cabinet.slug == data.cabinet_slug).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet introuvable.")
    cabinet.total_issued += 1
    ticket = PatientTicket(cabinet_slug=cabinet.slug, ticket_num=cabinet.total_issued, nom_patient=data.nom_patient or "Patient (guichet)", telephone=data.telephone)
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return {"status": "success", "ticket_id": ticket.id, "ticket_num": ticket.ticket_num, "display_ticket": "N. P-" + format(ticket.ticket_num, "02d")}


@app.get("/api/queue/{cabinet_slug}")
def get_queue_state(cabinet_slug: str, db: Session = Depends(get_db)):
    cabinet = db.query(Cabinet).filter(Cabinet.slug == cabinet_slug).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet introuvable.")
    waiting = max(0, cabinet.total_issued - cabinet.serving_num)
    return {"cabinet_name": cabinet.nom_medecin, "specialite": cabinet.specialite, "adresse": cabinet.adresse, "heure_ouverture": cabinet.heure_ouverture, "heure_fermeture": cabinet.heure_fermeture, "photo_url": cabinet.photo_url, "serving_num": cabinet.serving_num, "display_serving": "N. P-" + format(cabinet.serving_num, "02d"), "total_issued": cabinet.total_issued, "waiting_count": waiting, "is_active": cabinet.is_active}


@app.post("/api/queue/{cabinet_slug}/next")
def call_next(cabinet_slug: str, pin: str, db: Session = Depends(get_db)):
    cabinet = db.query(Cabinet).filter(Cabinet.slug == cabinet_slug).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet introuvable.")
    if cabinet.code_pin != pin:
        raise HTTPException(status_code=403, detail="Code PIN incorrect.")
    previous = db.query(PatientTicket).filter(PatientTicket.cabinet_slug == cabinet_slug, PatientTicket.statut == "serving").first()
    if previous:
        previous.statut = "completed"
        previous.completed_at = datetime.utcnow()
    if cabinet.serving_num < cabinet.total_issued:
        cabinet.serving_num += 1
        next_ticket = db.query(PatientTicket).filter(PatientTicket.cabinet_slug == cabinet_slug, PatientTicket.ticket_num == cabinet.serving_num).first()
        if next_ticket:
            next_ticket.statut = "serving"
            next_ticket.started_at = datetime.utcnow()
        db.commit()
    return {"status": "success", "serving_num": cabinet.serving_num, "display_serving": "N. P-" + format(cabinet.serving_num, "02d")}


@app.get("/api/cabinets/{cabinet_slug}/tickets")
def list_tickets(cabinet_slug: str, db: Session = Depends(get_db)):
    cabinet = db.query(Cabinet).filter(Cabinet.slug == cabinet_slug).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet introuvable.")
    tickets = db.query(PatientTicket).filter(PatientTicket.cabinet_slug == cabinet_slug).order_by(PatientTicket.ticket_num.desc()).limit(200).all()
    result = []
    for ticket in tickets:
        duration = None
        if ticket.started_at is not None and ticket.completed_at is not None:
            duration = round((ticket.completed_at - ticket.started_at).total_seconds() / 60, 1)
        result.append({"id": ticket.id, "ticket_num": ticket.ticket_num, "nom_patient": ticket.nom_patient, "telephone": ticket.telephone, "statut": ticket.statut, "created_at": ticket.created_at.isoformat(), "started_at": ticket.started_at.isoformat() if ticket.started_at else None, "completed_at": ticket.completed_at.isoformat() if ticket.completed_at else None, "duree_min": duration})
    return result


@app.put("/api/cabinets/{cabinet_slug}/settings")
def update_cabinet_settings(cabinet_slug: str, pin: str, data: CabinetSettingsUpdate, db: Session = Depends(get_db)):
    cabinet = db.query(Cabinet).filter(Cabinet.slug == cabinet_slug).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet introuvable.")
    if cabinet.code_pin != pin:
        raise HTTPException(status_code=403, detail="Code PIN incorrect.")
    if data.adresse is not None:
        cabinet.adresse = data.adresse
    if data.heure_ouverture is not None:
        cabinet.heure_ouverture = data.heure_ouverture
    if data.heure_fermeture is not None:
        cabinet.heure_fermeture = data.heure_fermeture
    if data.photo_url is not None:
        cabinet.photo_url = data.photo_url
    db.commit()
    return {"status": "success", "adresse": cabinet.adresse, "heure_ouverture": cabinet.heure_ouverture, "heure_fermeture": cabinet.heure_fermeture, "photo_url": cabinet.photo_url}


@app.post("/api/tickets/{ticket_id}/complete")
def complete_ticket(ticket_id: int, db: Session = Depends(get_db)):
    ticket = db.query(PatientTicket).filter(PatientTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket introuvable.")
    if not ticket.started_at:
        ticket.started_at = datetime.utcnow()
    ticket.completed_at = datetime.utcnow()
    ticket.statut = "completed"
    db.commit()
    duration = round((ticket.completed_at - ticket.started_at).total_seconds() / 60, 1)
    return {"status": "success", "ticket_num": ticket.ticket_num, "duree_min": duration}
