import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, Text, or_, func
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# ============================================================
# CONFIGURATION POSTGRESQL / RENDER
# ============================================================

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL or "sqlite:///./fallback.db")
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ============================================================
# MODELES
# ============================================================

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
    queue_order = Column(Integer, index=True, nullable=True)
    nom_patient = Column(String)
    telephone = Column(String, nullable=True)
    statut = Column(String, default="waiting")
    priority_level = Column(String, default="normal")
    urgent_request_status = Column(String, nullable=True)
    urgent_reason = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    called_at = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    suspended_at = Column(DateTime, nullable=True)
    presence_confirmed_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)


class TicketEvent(Base):
    __tablename__ = "ticket_events"

    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(Integer, index=True)
    cabinet_slug = Column(String, index=True)
    event_type = Column(String, index=True)
    previous_status = Column(String, nullable=True)
    new_status = Column(String, nullable=True)
    actor_type = Column(String)
    actor_name = Column(String, nullable=True)
    reason_code = Column(String, nullable=True)
    reason_note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(bind=engine)


# ============================================================
# APPLICATION FASTAPI
# ============================================================

app = FastAPI(
    title="TAFWITA API",
    description="API de gestion equitable de file d attente medicale",
    version="2.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ============================================================
# SCHEMAS
# ============================================================

class CabinetRegister(BaseModel):
    nom_medecin: str
    specialite: str
    telephone: str
    wilaya: str
    code_pin: str


class PinVerifyRequest(BaseModel):
    code_pin: str


class CabinetSettingsUpdate(BaseModel):
    adresse: Optional[str] = None
    heure_ouverture: Optional[str] = None
    heure_fermeture: Optional[str] = None
    photo_url: Optional[str] = None


class PatientTicketCreate(BaseModel):
    cabinet_slug: str
    nom_patient: str = Field(min_length=2, max_length=100)
    telephone: Optional[str] = Field(default=None, max_length=30)


class PatientTicketAction(BaseModel):
    reason_code: Optional[str] = None
    reason_note: Optional[str] = Field(default=None, max_length=500)


class CabinetTicketAction(BaseModel):
    code_pin: str
    reason_code: Optional[str] = None
    reason_note: Optional[str] = Field(default=None, max_length=500)


class UrgentAction(BaseModel):
    code_pin: str
    urgent_reason: str = Field(min_length=3, max_length=500)
    reason_note: Optional[str] = Field(default=None, max_length=500)


# ============================================================
# OUTILS
# ============================================================

def get_cabinet(db, cabinet_slug):
    cabinet = db.query(Cabinet).filter(Cabinet.slug == cabinet_slug).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet introuvable.")
    return cabinet


def require_pin(cabinet, pin):
    if cabinet.code_pin != pin:
        raise HTTPException(status_code=403, detail="Code PIN incorrect.")


def add_event(db, ticket, event_type, old_status, new_status, actor_type, actor_name=None, reason_code=None, reason_note=None):
    db.add(TicketEvent(
        ticket_id=ticket.id,
        cabinet_slug=ticket.cabinet_slug,
        event_type=event_type,
        previous_status=old_status,
        new_status=new_status,
        actor_type=actor_type,
        actor_name=actor_name,
        reason_code=reason_code,
        reason_note=reason_note,
    ))


def next_order(db, cabinet_slug):
    maximum = db.query(func.max(PatientTicket.queue_order)).filter(
        PatientTicket.cabinet_slug == cabinet_slug,
        PatientTicket.queue_order.isnot(None),
    ).scalar()
    return (maximum or 0) + 1


def queue_status_filter():
    return PatientTicket.statut.in_(["waiting", "returned", "urgent"])


def ticket_to_dict(ticket):
    return {
        "id": ticket.id,
        "cabinet_slug": ticket.cabinet_slug,
        "ticket_num": ticket.ticket_num,
        "queue_order": ticket.queue_order,
        "nom_patient": ticket.nom_patient,
        "telephone": ticket.telephone,
        "statut": ticket.statut,
        "priority_level": ticket.priority_level,
        "urgent_request_status": ticket.urgent_request_status,
        "urgent_reason": ticket.urgent_reason,
        "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
        "called_at": ticket.called_at.isoformat() if ticket.called_at else None,
        "started_at": ticket.started_at.isoformat() if ticket.started_at else None,
        "completed_at": ticket.completed_at.isoformat() if ticket.completed_at else None,
        "suspended_at": ticket.suspended_at.isoformat() if ticket.suspended_at else None,
        "presence_confirmed_at": ticket.presence_confirmed_at.isoformat() if ticket.presence_confirmed_at else None,
        "cancelled_at": ticket.cancelled_at.isoformat() if ticket.cancelled_at else None,
    }


def public_cabinet_dict(cabinet, db):
    waiting = db.query(PatientTicket).filter(
        PatientTicket.cabinet_slug == cabinet.slug,
        queue_status_filter(),
    ).count()
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


# ============================================================
# SANTE ET LISTE PUBLIQUE
# ============================================================

@app.get("/")
def home():
    return {"message": "API TAFWITA v2.1 connectee a PostgreSQL avec succes."}


@app.get("/api/cabinets")
def list_cabinets(
    q: Optional[str] = Query(default=None, max_length=100),
    specialite: Optional[str] = Query(default=None, max_length=100),
    wilaya: Optional[str] = Query(default=None, max_length=100),
    db: Session = Depends(get_db),
):
    query = db.query(Cabinet).filter(Cabinet.is_active.is_(True))
    if q:
        pattern = "%" + q.strip() + "%"
        query = query.filter(or_(
            Cabinet.nom_medecin.ilike(pattern),
            Cabinet.specialite.ilike(pattern),
            Cabinet.wilaya.ilike(pattern),
            Cabinet.adresse.ilike(pattern),
        ))
    if specialite:
        query = query.filter(Cabinet.specialite.ilike("%" + specialite.strip() + "%"))
    if wilaya:
        query = query.filter(Cabinet.wilaya.ilike("%" + wilaya.strip() + "%"))
    cabinets = query.order_by(Cabinet.nom_medecin.asc()).limit(100).all()
    return [public_cabinet_dict(cabinet, db) for cabinet in cabinets]


@app.get("/api/cabinets/{cabinet_slug}/public")
def cabinet_public(cabinet_slug: str, db: Session = Depends(get_db)):
    cabinet = get_cabinet(db, cabinet_slug)
    if not cabinet.is_active:
        raise HTTPException(status_code=404, detail="Cabinet indisponible.")
    return public_cabinet_dict(cabinet, db)


# ============================================================
# CABINET : INSCRIPTION, CONNEXION, PARAMETRES
# ============================================================

@app.post("/api/cabinets/register")
def register_cabinet(data: CabinetRegister, db: Session = Depends(get_db)):
    if db.query(Cabinet).filter(Cabinet.telephone == data.telephone).first():
        raise HTTPException(status_code=400, detail="Ce numero de telephone est deja enregistre.")

    slug = data.nom_medecin.lower().replace(" ", "-").replace(".", "")
    if db.query(Cabinet).filter(Cabinet.slug == slug).first():
        slug = slug + "-" + str(int(datetime.utcnow().timestamp()))

    cabinet = Cabinet(
        slug=slug,
        nom_medecin=data.nom_medecin,
        specialite=data.specialite,
        telephone=data.telephone,
        wilaya=data.wilaya,
        code_pin=data.code_pin,
        subscription_end=datetime.utcnow() + timedelta(days=14),
    )
    db.add(cabinet)
    db.commit()
    db.refresh(cabinet)
    return {
        "status": "success",
        "cabinet_slug": cabinet.slug,
        "trial_end": cabinet.subscription_end.strftime("%Y-%m-%d"),
    }


@app.post("/api/cabinets/{cabinet_slug}/verify-pin")
def verify_pin(cabinet_slug: str, data: PinVerifyRequest, db: Session = Depends(get_db)):
    cabinet = get_cabinet(db, cabinet_slug)
    require_pin(cabinet, data.code_pin)
    return {
        "status": "success",
        "cabinet_slug": cabinet.slug,
        "cabinet_nom": cabinet.nom_medecin,
        "specialite": cabinet.specialite,
    }


@app.put("/api/cabinets/{cabinet_slug}/settings")
def update_settings(cabinet_slug: str, data: CabinetSettingsUpdate, pin: str, db: Session = Depends(get_db)):
    cabinet = get_cabinet(db, cabinet_slug)
    require_pin(cabinet, pin)
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


# ============================================================
# PATIENT : PRISE ET GESTION DU TICKET
# ============================================================

@app.post("/api/tickets/take")
def take_ticket(data: PatientTicketCreate, db: Session = Depends(get_db)):
    cabinet = get_cabinet(db, data.cabinet_slug)
    if not cabinet.is_active:
        raise HTTPException(status_code=400, detail="Le cabinet est actuellement ferme.")

    cabinet.total_issued += 1
    ticket = PatientTicket(
        cabinet_slug=cabinet.slug,
        ticket_num=cabinet.total_issued,
        queue_order=next_order(db, cabinet.slug),
        nom_patient=data.nom_patient,
        telephone=data.telephone,
        statut="waiting",
    )
    db.add(ticket)
    db.flush()
    add_event(db, ticket, "created", None, "waiting", "patient", "Patient")
    db.commit()
    db.refresh(ticket)

    patients_before = db.query(PatientTicket).filter(
        PatientTicket.cabinet_slug == cabinet.slug,
        queue_status_filter(),
        PatientTicket.queue_order < ticket.queue_order,
    ).count()
    return {"status": "success", "ticket": ticket_to_dict(ticket), "waiting_before_you": patients_before, "estimated_wait_min": patients_before * 12}


@app.post("/api/tickets/add-manual")
def add_manual_ticket(data: PatientTicketCreate, db: Session = Depends(get_db)):
    cabinet = get_cabinet(db, data.cabinet_slug)
    cabinet.total_issued += 1
    ticket = PatientTicket(
        cabinet_slug=cabinet.slug,
        ticket_num=cabinet.total_issued,
        queue_order=next_order(db, cabinet.slug),
        nom_patient=data.nom_patient,
        telephone=data.telephone,
        statut="waiting",
    )
    db.add(ticket)
    db.flush()
    add_event(db, ticket, "created_manual", None, "waiting", "secretary", cabinet.nom_medecin)
    db.commit()
    db.refresh(ticket)
    return {"status": "success", "ticket": ticket_to_dict(ticket)}


@app.get("/api/tickets/{ticket_id}/patient")
def get_patient_ticket(ticket_id: int, db: Session = Depends(get_db)):
    ticket = db.query(PatientTicket).filter(PatientTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket introuvable.")
    before = 0
    if ticket.statut in ["waiting", "returned", "urgent"] and ticket.queue_order is not None:
        before = db.query(PatientTicket).filter(
            PatientTicket.cabinet_slug == ticket.cabinet_slug,
            queue_status_filter(),
            PatientTicket.queue_order < ticket.queue_order,
        ).count()
    return {"ticket": ticket_to_dict(ticket), "waiting_before_you": before, "estimated_wait_min": before * 12}


@app.post("/api/tickets/{ticket_id}/patient-suspend")
def patient_suspend(ticket_id: int, data: PatientTicketAction, db: Session = Depends(get_db)):
    ticket = db.query(PatientTicket).filter(PatientTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket introuvable.")
    if ticket.statut not in ["waiting", "returned"]:
        raise HTTPException(status_code=400, detail="Ce ticket ne peut pas etre suspendu dans son etat actuel.")
    old = ticket.statut
    ticket.statut = "suspended"
    ticket.suspended_at = datetime.utcnow()
    ticket.queue_order = None
    add_event(db, ticket, "patient_requested_delay", old, "suspended", "patient", "Patient", data.reason_code or "patient_late", data.reason_note)
    db.commit()
    return {"status": "success", "ticket": ticket_to_dict(ticket)}


@app.post("/api/tickets/{ticket_id}/patient-present")
def patient_present(ticket_id: int, db: Session = Depends(get_db)):
    ticket = db.query(PatientTicket).filter(PatientTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket introuvable.")
    if ticket.statut != "suspended":
        raise HTTPException(status_code=400, detail="Le ticket doit etre suspendu avant confirmation de presence.")
    ticket.presence_confirmed_at = datetime.utcnow()
    add_event(db, ticket, "presence_confirmed", "suspended", "suspended", "patient", "Patient")
    db.commit()
    return {"status": "success", "message": "Votre presence a ete transmise au cabinet.", "ticket": ticket_to_dict(ticket)}


@app.post("/api/tickets/{ticket_id}/patient-cancel")
def patient_cancel(ticket_id: int, data: PatientTicketAction, db: Session = Depends(get_db)):
    ticket = db.query(PatientTicket).filter(PatientTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket introuvable.")
    if ticket.statut in ["serving", "completed", "cancelled"]:
        raise HTTPException(status_code=400, detail="Ce ticket ne peut plus etre annule.")
    old = ticket.statut
    ticket.statut = "cancelled"
    ticket.cancelled_at = datetime.utcnow()
    ticket.queue_order = None
    add_event(db, ticket, "cancelled_by_patient", old, "cancelled", "patient", "Patient", data.reason_code or "patient_cancelled", data.reason_note)
    db.commit()
    return {"status": "success", "ticket": ticket_to_dict(ticket)}


@app.post("/api/tickets/{ticket_id}/request-urgent")
def request_urgent(ticket_id: int, data: PatientTicketAction, db: Session = Depends(get_db)):
    ticket = db.query(PatientTicket).filter(PatientTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket introuvable.")
    if ticket.statut in ["completed", "cancelled"]:
        raise HTTPException(status_code=400, detail="Demande impossible pour ce ticket.")
    ticket.urgent_request_status = "pending"
    add_event(db, ticket, "urgent_requested", ticket.statut, ticket.statut, "patient", "Patient", "urgent_requested", data.reason_note)
    db.commit()
    return {"status": "success", "message": "Demande de priorite transmise au cabinet.", "ticket": ticket_to_dict(ticket)}


# ============================================================
# CABINET : FILE, ALERTES ET ACTIONS
# ============================================================

@app.get("/api/queue/{cabinet_slug}")
def queue_state(cabinet_slug: str, db: Session = Depends(get_db)):
    cabinet = get_cabinet(db, cabinet_slug)
    waiting = db.query(PatientTicket).filter(
        PatientTicket.cabinet_slug == cabinet_slug,
        queue_status_filter(),
    ).count()
    current = db.query(PatientTicket).filter(
        PatientTicket.cabinet_slug == cabinet_slug,
        PatientTicket.statut == "serving",
    ).first()
    display = "Aucun patient en cours"
    if current:
        display = "N. P-" + format(current.ticket_num, "02d")
    return {
        "cabinet_name": cabinet.nom_medecin,
        "specialite": cabinet.specialite,
        "adresse": cabinet.adresse,
        "heure_ouverture": cabinet.heure_ouverture,
        "heure_fermeture": cabinet.heure_fermeture,
        "photo_url": cabinet.photo_url,
        "serving_num": cabinet.serving_num,
        "display_serving": display,
        "total_issued": cabinet.total_issued,
        "waiting_count": waiting,
        "is_active": cabinet.is_active,
    }


@app.get("/api/cabinets/{cabinet_slug}/tickets")
def cabinet_tickets(cabinet_slug: str, status: Optional[str] = None, db: Session = Depends(get_db)):
    get_cabinet(db, cabinet_slug)
    query = db.query(PatientTicket).filter(PatientTicket.cabinet_slug == cabinet_slug)
    if status:
        query = query.filter(PatientTicket.statut == status)
    tickets = query.order_by(
        PatientTicket.queue_order.is_(None),
        PatientTicket.queue_order.asc(),
        PatientTicket.created_at.desc(),
    ).limit(300).all()
    return [ticket_to_dict(ticket) for ticket in tickets]


@app.get("/api/cabinets/{cabinet_slug}/alerts")
def cabinet_alerts(cabinet_slug: str, pin: str, db: Session = Depends(get_db)):
    cabinet = get_cabinet(db, cabinet_slug)
    require_pin(cabinet, pin)
    presence = db.query(PatientTicket).filter(
        PatientTicket.cabinet_slug == cabinet_slug,
        PatientTicket.statut == "suspended",
        PatientTicket.presence_confirmed_at.isnot(None),
    ).order_by(PatientTicket.presence_confirmed_at.desc()).all()
    urgent = db.query(PatientTicket).filter(
        PatientTicket.cabinet_slug == cabinet_slug,
        PatientTicket.urgent_request_status == "pending",
    ).order_by(PatientTicket.created_at.asc()).all()
    return {"presence_confirmed": [ticket_to_dict(t) for t in presence], "urgent_requests": [ticket_to_dict(t) for t in urgent]}


@app.post("/api/tickets/{ticket_id}/suspend")
def suspend_by_cabinet(ticket_id: int, data: CabinetTicketAction, db: Session = Depends(get_db)):
    ticket = db.query(PatientTicket).filter(PatientTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket introuvable.")
    cabinet = get_cabinet(db, ticket.cabinet_slug)
    require_pin(cabinet, data.code_pin)
    if ticket.statut not in ["waiting", "returned"]:
        raise HTTPException(status_code=400, detail="Ce ticket ne peut pas etre suspendu dans son etat actuel.")
    old = ticket.statut
    ticket.statut = "suspended"
    ticket.suspended_at = datetime.utcnow()
    ticket.queue_order = None
    add_event(db, ticket, "suspended_by_cabinet", old, "suspended", "secretary", cabinet.nom_medecin, data.reason_code or "patient_absent", data.reason_note)
    db.commit()
    return {"status": "success", "ticket": ticket_to_dict(ticket)}


@app.post("/api/tickets/{ticket_id}/return-to-queue")
def return_to_queue(ticket_id: int, data: CabinetTicketAction, db: Session = Depends(get_db)):
    ticket = db.query(PatientTicket).filter(PatientTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket introuvable.")
    cabinet = get_cabinet(db, ticket.cabinet_slug)
    require_pin(cabinet, data.code_pin)
    if ticket.statut != "suspended":
        raise HTTPException(status_code=400, detail="Seul un ticket suspendu peut etre remis dans la file.")
    old = ticket.statut
    ticket.statut = "returned"
    ticket.queue_order = next_order(db, ticket.cabinet_slug)
    add_event(db, ticket, "returned_to_queue", old, "returned", "secretary", cabinet.nom_medecin, data.reason_code or "returned_after_delay", data.reason_note)
    db.commit()
    return {"status": "success", "ticket": ticket_to_dict(ticket)}


@app.post("/api/tickets/{ticket_id}/cabinet-cancel")
def cabinet_cancel(ticket_id: int, data: CabinetTicketAction, db: Session = Depends(get_db)):
    ticket = db.query(PatientTicket).filter(PatientTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket introuvable.")
    cabinet = get_cabinet(db, ticket.cabinet_slug)
    require_pin(cabinet, data.code_pin)
    if ticket.statut in ["serving", "completed", "cancelled"]:
        raise HTTPException(status_code=400, detail="Ce ticket ne peut plus etre annule.")
    old = ticket.statut
    ticket.statut = "cancelled"
    ticket.cancelled_at = datetime.utcnow()
    ticket.queue_order = None
    add_event(db, ticket, "cancelled_by_cabinet", old, "cancelled", "secretary", cabinet.nom_medecin, data.reason_code or "cabinet_cancelled", data.reason_note)
    db.commit()
    return {"status": "success", "ticket": ticket_to_dict(ticket)}


@app.post("/api/tickets/{ticket_id}/approve-urgent")
def approve_urgent(ticket_id: int, data: UrgentAction, db: Session = Depends(get_db)):
    ticket = db.query(PatientTicket).filter(PatientTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket introuvable.")
    cabinet = get_cabinet(db, ticket.cabinet_slug)
    require_pin(cabinet, data.code_pin)
    if ticket.statut in ["serving", "completed", "cancelled"]:
        raise HTTPException(status_code=400, detail="Priorite impossible pour ce ticket.")
    old = ticket.statut
    ticket.statut = "urgent"
    ticket.priority_level = "urgent"
    ticket.urgent_request_status = "approved"
    ticket.urgent_reason = data.urgent_reason
    ticket.queue_order = 0
    add_event(db, ticket, "urgent_approved", old, "urgent", "doctor", cabinet.nom_medecin, "medical_priority", data.reason_note)
    db.commit()
    return {"status": "success", "ticket": ticket_to_dict(ticket)}


# IMPORTANT : action qui modifie la file -> POST, jamais GET.
@app.post("/api/queue/{cabinet_slug}/next")
def call_next(cabinet_slug: str, pin: str, db: Session = Depends(get_db)):
    cabinet = get_cabinet(db, cabinet_slug)
    require_pin(cabinet, pin)

    current = db.query(PatientTicket).filter(
        PatientTicket.cabinet_slug == cabinet_slug,
        PatientTicket.statut == "serving",
    ).first()
    if current:
        current.statut = "completed"
        current.completed_at = datetime.utcnow()
        add_event(db, current, "completed", "serving", "completed", "doctor", cabinet.nom_medecin)

    next_ticket = db.query(PatientTicket).filter(
        PatientTicket.cabinet_slug == cabinet_slug,
        queue_status_filter(),
    ).order_by(PatientTicket.queue_order.asc()).first()

    if not next_ticket:
        db.commit()
        return {"status": "empty", "message": "Aucun patient en attente.", "serving_num": cabinet.serving_num}

    old = next_ticket.statut
    next_ticket.statut = "serving"
    next_ticket.called_at = datetime.utcnow()
    next_ticket.started_at = datetime.utcnow()
    cabinet.serving_num = next_ticket.ticket_num
    add_event(db, next_ticket, "called_and_serving", old, "serving", "doctor", cabinet.nom_medecin)
    db.commit()
    return {"status": "success", "ticket": ticket_to_dict(next_ticket), "display_serving": "N. P-" + format(next_ticket.ticket_num, "02d")}


@app.get("/api/tickets/{ticket_id}/events")
def ticket_events(ticket_id: int, db: Session = Depends(get_db)):
    ticket = db.query(PatientTicket).filter(PatientTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket introuvable.")
    events = db.query(TicketEvent).filter(TicketEvent.ticket_id == ticket_id).order_by(TicketEvent.created_at.desc()).all()
    return [{
        "id": item.id,
        "event_type": item.event_type,
        "previous_status": item.previous_status,
        "new_status": item.new_status,
        "actor_type": item.actor_type,
        "actor_name": item.actor_name,
        "reason_code": item.reason_code,
        "reason_note": item.reason_note,
        "created_at": item.created_at.isoformat(),
    } for item in events]
