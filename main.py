import os
from datetime import datetime, timedelta
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# ============================================================
# 1. CONNEXION BASE DE DONNEES
# ============================================================
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL or "sqlite:///./fallback.db")
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "tafwita-admin-2026")


# ============================================================
# 2. MODELES
# ============================================================
class Cabinet(Base):
    __tablename__ = "cabinets"
    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String, unique=True, index=True)
    nom_medecin = Column(String)
    specialite = Column(String)
    telephone = Column(String, unique=True)
    wilaya = Column(String)
    code_pin = Column(String)
    is_active = Column(Boolean, default=True)
    subscription_type = Column(String, default="trial_14d")
    subscription_end = Column(DateTime)
    serving_num = Column(Integer, default=0)
    total_issued = Column(Integer, default=0)

    # Gestion journee / horaires
    accept_tickets = Column(Boolean, default=True)
    day_closed = Column(Boolean, default=True)
    ticket_mode = Column(String, default="manual")           # manual | fixed
    ticket_start_time = Column(String, default="06:00")
    ticket_end_time = Column(String, nullable=True)
    opening_time = Column(String, default="08:00")
    closing_time = Column(String, nullable=True)
    max_tickets_per_day = Column(Integer, nullable=True)


class PatientTicket(Base):
    __tablename__ = "patient_tickets"
    id = Column(Integer, primary_key=True, index=True)
    cabinet_slug = Column(String, index=True)
    ticket_num = Column(Integer)
    nom_patient = Column(String)
    telephone = Column(String, nullable=True)
    statut = Column(String, default="waiting")
    # waiting, returned, suspended, urgent, serving, completed, cancelled
    notification_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class TicketEvent(Base):
    __tablename__ = "ticket_events"
    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(Integer, index=True)
    event_type = Column(String)
    reason_note = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(bind=engine)

# ============================================================
# 3. APP
# ============================================================
app = FastAPI(title="TAFWITA API Cloud", description="Gestion des files et des abonnements")

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


def check_admin(x_admin_token: Optional[str] = Header(None)):
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Non autorise.")
    return True


def get_cabinet_or_404(db: Session, slug: str) -> Cabinet:
    cabinet = db.query(Cabinet).filter(Cabinet.slug == slug).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet introuvable.")
    return cabinet


def get_ticket_or_404(db: Session, ticket_id: int) -> PatientTicket:
    ticket = db.query(PatientTicket).filter(PatientTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket introuvable.")
    return ticket


def log_event(db: Session, ticket_id: int, event_type: str, reason_note: Optional[str] = None):
    db.add(TicketEvent(ticket_id=ticket_id, event_type=event_type, reason_note=reason_note))
    db.commit()


def ticket_to_dict(t: PatientTicket) -> dict:
    return {
        "id": t.id,
        "cabinet_slug": t.cabinet_slug,
        "ticket_num": t.ticket_num,
        "nom_patient": t.nom_patient,
        "telephone": t.telephone,
        "statut": t.statut,
        "notification_count": t.notification_count,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


def cabinet_public_dict(c: Cabinet) -> dict:
    return {
        "slug": c.slug,
        "nom_medecin": c.nom_medecin,
        "specialite": c.specialite,
        "wilaya": c.wilaya,
        "accept_tickets": c.accept_tickets,
        "day_closed": c.day_closed,
        "ticket_mode": c.ticket_mode,
        "ticket_start_time": c.ticket_start_time,
        "ticket_end_time": c.ticket_end_time,
    }


# ============================================================
# 4. SCHEMAS
# ============================================================
class CabinetRegister(BaseModel):
    nom_medecin: str
    specialite: str
    telephone: str
    wilaya: str
    code_pin: str


class TrialSignup(BaseModel):
    nom: str
    prenom: str
    cabinet: str
    telephone: str
    email: str
    specialite: str
    wilaya: str
    password: str


class PinCheck(BaseModel):
    code_pin: str


class PatientTicketCreate(BaseModel):
    cabinet_slug: str
    nom_patient: str
    telephone: Optional[str] = None


class TicketActionBody(BaseModel):
    code_pin: Optional[str] = None
    reason_code: Optional[str] = None
    reason_note: Optional[str] = None


class UrgentBody(BaseModel):
    code_pin: str
    urgent_reason: str


class CloseDayBody(BaseModel):
    reason: Optional[str] = None
    notify_patients: Optional[bool] = True


class CabinetSettings(BaseModel):
    accept_tickets: Optional[bool] = None
    ticket_mode: Optional[str] = None
    ticket_start_time: Optional[str] = None
    ticket_end_time: Optional[str] = None
    opening_time: Optional[str] = None
    closing_time: Optional[str] = None
    max_tickets_per_day: Optional[int] = None


class SubscriptionUpdate(BaseModel):
    subscription_type: str
    extend_days: Optional[int] = None


class AdminLogin(BaseModel):
    password: str


# ============================================================
# 5. ENDPOINTS GENERAUX
# ============================================================
@app.get("/")
def home():
    return {"message": "API TAFWITA & Base PostgreSQL connectees avec succes !"}


# A. INSCRIPTION D'UN NOUVEAU CABINET (formulaire complet, B2B)
@app.post("/api/cabinets/register")
def register_cabinet(data: CabinetRegister, db: Session = Depends(get_db)):
    slug = data.nom_medecin.lower().replace(" ", "-").replace(".", "")
    existing = db.query(Cabinet).filter(Cabinet.telephone == data.telephone).first()
    if existing:
        raise HTTPException(status_code=400, detail="Ce numero de telephone est deja enregistre.")

    trial_end = datetime.utcnow() + timedelta(days=14)
    cabinet = Cabinet(
        slug=slug, nom_medecin=data.nom_medecin, specialite=data.specialite,
        telephone=data.telephone, wilaya=data.wilaya, code_pin=data.code_pin,
        subscription_end=trial_end,
    )
    db.add(cabinet)
    db.commit()
    db.refresh(cabinet)
    return {
        "status": "success",
        "message": "Cabinet cree avec succes ! 14 jours d'essai actives.",
        "cabinet_slug": cabinet.slug,
        "trial_end": trial_end.strftime("%Y-%m-%d"),
    }


# A2. INSCRIPTION ESSAI GRATUIT (popup du site vitrine)
@app.post("/api/trial-signup")
def trial_signup(data: TrialSignup, db: Session = Depends(get_db)):
    slug = data.cabinet.lower().replace(" ", "-").replace(".", "")
    existing = db.query(Cabinet).filter(Cabinet.telephone == data.telephone).first()
    if existing:
        raise HTTPException(status_code=400, detail="Ce numero de telephone est deja enregistre.")

    trial_end = datetime.utcnow() + timedelta(days=14)
    cabinet = Cabinet(
        slug=slug,
        nom_medecin=f"{data.prenom} {data.nom}",
        specialite=data.specialite,
        telephone=data.telephone,
        wilaya=data.wilaya,
        code_pin=data.password[:6] if data.password else "0000",
        subscription_end=trial_end,
    )
    db.add(cabinet)
    db.commit()
    db.refresh(cabinet)
    return {
        "status": "success",
        "message": "Demande envoyee. Verifiez votre e-mail.",
        "cabinet_slug": cabinet.slug,
    }


# ============================================================
# 6. ENDPOINTS PUBLICS / PATIENT
# ============================================================

# B. LISTE DES CABINETS (recherche patient)
@app.get("/api/cabinets")
def list_cabinets(q: Optional[str] = None, db: Session = Depends(get_db)):
    query = db.query(Cabinet).filter(Cabinet.is_active == True)
    if q:
        like = f"%{q.lower()}%"
        query = query.filter(
            (Cabinet.nom_medecin.ilike(like)) |
            (Cabinet.specialite.ilike(like)) |
            (Cabinet.wilaya.ilike(like))
        )
    return [cabinet_public_dict(c) for c in query.all()]


# C. DETAIL PUBLIC D'UN CABINET
@app.get("/api/cabinets/{slug}/public")
def cabinet_public(slug: str, db: Session = Depends(get_db)):
    cabinet = get_cabinet_or_404(db, slug)
    return cabinet_public_dict(cabinet)


# D. PRENDRE UN TICKET (patient)
@app.post("/api/tickets/take")
def take_ticket(data: PatientTicketCreate, db: Session = Depends(get_db)):
    cabinet = get_cabinet_or_404(db, data.cabinet_slug)
    if cabinet.day_closed or not cabinet.accept_tickets:
        raise HTTPException(status_code=400, detail="La prise de tickets n'est pas disponible actuellement.")
    if cabinet.max_tickets_per_day and cabinet.total_issued >= cabinet.max_tickets_per_day:
        raise HTTPException(status_code=400, detail="Nombre maximum de tickets atteint pour aujourd'hui.")

    cabinet.total_issued += 1
    new_ticket = PatientTicket(
        cabinet_slug=cabinet.slug,
        ticket_num=cabinet.total_issued,
        nom_patient=data.nom_patient,
        telephone=data.telephone,
    )
    db.add(new_ticket)
    db.commit()
    db.refresh(new_ticket)
    log_event(db, new_ticket.id, "created")

    return {"status": "success", "ticket": ticket_to_dict(new_ticket)}


# E. SUIVI D'UN TICKET (patient)
@app.get("/api/tickets/{ticket_id}/patient")
def track_ticket(ticket_id: int, db: Session = Depends(get_db)):
    ticket = get_ticket_or_404(db, ticket_id)
    cabinet = db.query(Cabinet).filter(Cabinet.slug == ticket.cabinet_slug).first()
    waiting_before_you = 0
    if cabinet and ticket.statut in ("waiting", "returned"):
        waiting_before_you = db.query(PatientTicket).filter(
            PatientTicket.cabinet_slug == cabinet.slug,
            PatientTicket.statut.in_(["waiting", "returned"]),
            PatientTicket.ticket_num < ticket.ticket_num,
        ).count()
    payload = ticket_to_dict(ticket)
    payload["waiting_before_you"] = waiting_before_you
    return {"ticket": payload}


# F. ACTIONS PATIENT SUR SON TICKET (retard, annulation, present, urgence)
@app.post("/api/tickets/{ticket_id}/{action}")
def patient_ticket_action(ticket_id: int, action: str, body: TicketActionBody, db: Session = Depends(get_db)):
    ticket = get_ticket_or_404(db, ticket_id)

    if action == "patient-suspend":
        ticket.statut = "suspended"
        log_event(db, ticket.id, "patient_suspend", body.reason_note)
    elif action == "patient-cancel":
        ticket.statut = "cancelled"
        log_event(db, ticket.id, "patient_cancel", body.reason_note)
    elif action == "patient-present":
        ticket.statut = "returned"
        log_event(db, ticket.id, "patient_present", body.reason_note)
    elif action == "request-urgent":
        ticket.statut = "urgent"
        log_event(db, ticket.id, "urgent_requested", body.reason_note)
    elif action == "suspend":
        ticket.statut = "suspended"
        log_event(db, ticket.id, "suspend", body.reason_note)
    elif action == "return-to-queue":
        ticket.statut = "returned"
        log_event(db, ticket.id, "return_to_queue", body.reason_note)
    elif action == "cabinet-cancel":
        ticket.statut = "cancelled"
        log_event(db, ticket.id, "cabinet_cancel", body.reason_note)
    else:
        raise HTTPException(status_code=400, detail="Action inconnue.")

    db.commit()
    return {"status": "success", "ticket": ticket_to_dict(ticket)}


# G. HISTORIQUE / EVENEMENTS D'UN TICKET
@app.get("/api/tickets/{ticket_id}/events")
def ticket_events(ticket_id: int, db: Session = Depends(get_db)):
    events = db.query(TicketEvent).filter(TicketEvent.ticket_id == ticket_id).order_by(TicketEvent.created_at.asc()).all()
    return [
        {
            "id": e.id,
            "event_type": e.event_type,
            "reason_note": e.reason_note,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in events
    ]


# H. NOTIFIER UN PASSAGE SANS REPONSE
@app.post("/api/tickets/{ticket_id}/notify")
def notify_ticket(ticket_id: int, pin: str, db: Session = Depends(get_db)):
    ticket = get_ticket_or_404(db, ticket_id)
    cabinet = get_cabinet_or_404(db, ticket.cabinet_slug)
    if cabinet.code_pin != pin:
        raise HTTPException(status_code=403, detail="Code PIN incorrect.")
    ticket.notification_count += 1
    db.commit()
    log_event(db, ticket.id, "notify")
    return {"status": "success", "notification_count": ticket.notification_count}


# I. APPROUVER UNE PRIORITE MEDICALE (medecin)
@app.post("/api/tickets/{ticket_id}/approve-urgent")
def approve_urgent(ticket_id: int, body: UrgentBody, db: Session = Depends(get_db)):
    ticket = get_ticket_or_404(db, ticket_id)
    cabinet = get_cabinet_or_404(db, ticket.cabinet_slug)
    if cabinet.code_pin != body.code_pin:
        raise HTTPException(status_code=403, detail="Code PIN incorrect.")
    ticket.statut = "urgent"
    db.commit()
    log_event(db, ticket.id, "urgent_approved", body.urgent_reason)
    return {"status": "success", "ticket": ticket_to_dict(ticket)}


# ============================================================
# 7. ENDPOINTS CABINET (app desktop)
# ============================================================

# J. VERIFICATION DU CODE PIN (connexion desktop)
@app.post("/api/cabinets/{slug}/verify-pin")
def verify_pin(slug: str, body: PinCheck, db: Session = Depends(get_db)):
    cabinet = get_cabinet_or_404(db, slug)
    if cabinet.code_pin != body.code_pin:
        raise HTTPException(status_code=403, detail="Code PIN incorrect.")
    return {"status": "success", "cabinet_nom": cabinet.nom_medecin}


# K. AJOUTER UN TICKET MANUEL (papier)
@app.post("/api/tickets/add-manual")
def add_manual_ticket(data: PatientTicketCreate, db: Session = Depends(get_db)):
    cabinet = get_cabinet_or_404(db, data.cabinet_slug)
    cabinet.total_issued += 1
    new_ticket = PatientTicket(
        cabinet_slug=cabinet.slug,
        ticket_num=cabinet.total_issued,
        nom_patient=data.nom_patient,
        telephone=data.telephone,
    )
    db.add(new_ticket)
    db.commit()
    db.refresh(new_ticket)
    log_event(db, new_ticket.id, "created_manual")
    return {"status": "success", "ticket": ticket_to_dict(new_ticket)}


# L. HISTORIQUE COMPLET DES TICKETS D'UN CABINET
@app.get("/api/cabinets/{slug}/tickets")
def cabinet_tickets(slug: str, db: Session = Depends(get_db)):
    cabinet = get_cabinet_or_404(db, slug)
    tickets = db.query(PatientTicket).filter(PatientTicket.cabinet_slug == cabinet.slug).order_by(PatientTicket.id.desc()).all()
    return [ticket_to_dict(t) for t in tickets]


# M. ANNULER UN TICKET APRES 5 PASSAGES (cabinet)
@app.post("/api/tickets/{ticket_id}/cabinet-cancel")
def cabinet_cancel_ticket(ticket_id: int, body: TicketActionBody, db: Session = Depends(get_db)):
    ticket = get_ticket_or_404(db, ticket_id)
    cabinet = get_cabinet_or_404(db, ticket.cabinet_slug)
    if body.code_pin and cabinet.code_pin != body.code_pin:
        raise HTTPException(status_code=403, detail="Code PIN incorrect.")
    ticket.statut = "cancelled"
    db.commit()
    log_event(db, ticket.id, "cabinet_cancel", body.reason_note)
    return {"status": "success", "ticket": ticket_to_dict(ticket)}


# N. DEMARRER UNE NOUVELLE JOURNEE
@app.post("/api/cabinets/{slug}/start-day")
def start_day(slug: str, body: PinCheck, db: Session = Depends(get_db)):
    cabinet = get_cabinet_or_404(db, slug)
    if cabinet.code_pin != body.code_pin:
        raise HTTPException(status_code=403, detail="Code PIN incorrect.")
    cabinet.day_closed = False
    cabinet.accept_tickets = True
    cabinet.serving_num = 0
    cabinet.total_issued = 0
    db.commit()
    return {"status": "success", "message": "Journee demarree."}


# O. CLOTURER LA JOURNEE
@app.post("/api/cabinets/{slug}/close-day")
def close_day(slug: str, pin: str, body: CloseDayBody, db: Session = Depends(get_db)):
    cabinet = get_cabinet_or_404(db, slug)
    if cabinet.code_pin != pin:
        raise HTTPException(status_code=403, detail="Code PIN incorrect.")

    remaining = db.query(PatientTicket).filter(
        PatientTicket.cabinet_slug == cabinet.slug,
        PatientTicket.statut.in_(["waiting", "returned", "suspended", "urgent"]),
    ).all()
    for t in remaining:
        t.statut = "cancelled"
        log_event(db, t.id, "day_closed_cancel", body.reason)

    cabinet.day_closed = True
    cabinet.accept_tickets = False
    db.commit()
    return {"status": "success", "message": "Journee cloturee."}


# P. MODIFIER LES REGLES / HORAIRES DU CABINET
@app.put("/api/cabinets/{slug}/settings")
def update_settings(slug: str, pin: str, body: CabinetSettings, db: Session = Depends(get_db)):
    cabinet = get_cabinet_or_404(db, slug)
    if cabinet.code_pin != pin:
        raise HTTPException(status_code=403, detail="Code PIN incorrect.")

    data = body.dict(exclude_unset=True)
    for key, value in data.items():
        setattr(cabinet, key, value)
    db.commit()
    return {"status": "success", "message": "Parametres mis a jour."}


# Q. ETAT EN DIRECT DE LA FILE (utilise par TV, patient et desktop)
@app.get("/api/queue/{slug}")
def get_queue_state(slug: str, db: Session = Depends(get_db)):
    cabinet = get_cabinet_or_404(db, slug)
    tickets = db.query(PatientTicket).filter(
        PatientTicket.cabinet_slug == cabinet.slug,
        PatientTicket.statut.in_(["waiting", "returned", "suspended", "urgent", "serving"]),
    ).order_by(PatientTicket.ticket_num.asc()).all()

    waiting = db.query(PatientTicket).filter(
        PatientTicket.cabinet_slug == cabinet.slug,
        PatientTicket.statut.in_(["waiting", "returned"]),
    ).count()

    return {
        "cabinet_name": cabinet.nom_medecin,
        "specialite": cabinet.specialite,
        "serving_num": cabinet.serving_num,
        "display_serving": f"N deg P-{cabinet.serving_num:02d}",
        "total_issued": cabinet.total_issued,
        "waiting_count": waiting,
        "is_active": cabinet.is_active,
        "accept_tickets": cabinet.accept_tickets,
        "day_closed": cabinet.day_closed,
        "ticket_mode": cabinet.ticket_mode,
        "ticket_start_time": cabinet.ticket_start_time,
        "ticket_end_time": cabinet.ticket_end_time,
        "opening_time": cabinet.opening_time,
        "closing_time": cabinet.closing_time,
        "max_tickets_per_day": cabinet.max_tickets_per_day,
        "tickets": [ticket_to_dict(t) for t in tickets],
    }


# R. APPELER LE SUIVANT (touche F1 medecin)
@app.post("/api/queue/{slug}/next")
def call_next(slug: str, pin: str, db: Session = Depends(get_db)):
    cabinet = get_cabinet_or_404(db, slug)
    if cabinet.code_pin != pin:
        raise HTTPException(status_code=403, detail="Code PIN medecin incorrect.")
    if cabinet.serving_num < cabinet.total_issued:
        cabinet.serving_num += 1
        db.commit()
    return {"status": "success", "serving_num": cabinet.serving_num, "display_serving": f"N deg P-{cabinet.serving_num:02d}"}


# ============================================================
# 8. ENDPOINTS ADMIN (dashboard)
# ============================================================

@app.get("/api/admin/cabinets")
def admin_list_cabinets(db: Session = Depends(get_db), auth: bool = Depends(check_admin)):
    cabinets = db.query(Cabinet).order_by(Cabinet.id.desc()).all()
    result = []
    for c in cabinets:
        jours_restants = None
        if c.subscription_end:
            jours_restants = (c.subscription_end - datetime.utcnow()).days
        result.append({
            "id": c.id, "slug": c.slug, "nom_medecin": c.nom_medecin,
            "specialite": c.specialite, "telephone": c.telephone, "wilaya": c.wilaya,
            "is_active": c.is_active, "subscription_type": c.subscription_type,
            "subscription_end": c.subscription_end.strftime("%Y-%m-%d") if c.subscription_end else None,
            "jours_restants": jours_restants, "total_issued": c.total_issued,
        })
    return result


@app.post("/api/admin/cabinets/{cabinet_id}/toggle")
def admin_toggle_cabinet(cabinet_id: int, db: Session = Depends(get_db), auth: bool = Depends(check_admin)):
    cabinet = db.query(Cabinet).filter(Cabinet.id == cabinet_id).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet introuvable.")
    cabinet.is_active = not cabinet.is_active
    db.commit()
    return {"status": "success", "is_active": cabinet.is_active}


@app.post("/api/admin/cabinets/{cabinet_id}/subscription")
def admin_update_subscription(cabinet_id: int, data: SubscriptionUpdate, db: Session = Depends(get_db), auth: bool = Depends(check_admin)):
    cabinet = db.query(Cabinet).filter(Cabinet.id == cabinet_id).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet introuvable.")
    cabinet.subscription_type = data.subscription_type
    if data.extend_days:
        base = cabinet.subscription_end if cabinet.subscription_end and cabinet.subscription_end > datetime.utcnow() else datetime.utcnow()
        cabinet.subscription_end = base + timedelta(days=data.extend_days)
    db.commit()
    return {
        "status": "success", "subscription_type": cabinet.subscription_type,
        "subscription_end": cabinet.subscription_end.strftime("%Y-%m-%d") if cabinet.subscription_end else None,
    }


@app.delete("/api/admin/cabinets/{cabinet_id}")
def admin_delete_cabinet(cabinet_id: int, db: Session = Depends(get_db), auth: bool = Depends(check_admin)):
    cabinet = db.query(Cabinet).filter(Cabinet.id == cabinet_id).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet introuvable.")
    db.delete(cabinet)
    db.commit()
    return {"status": "success", "message": "Cabinet supprime."}


@app.get("/api/admin/stats")
def admin_stats(db: Session = Depends(get_db), auth: bool = Depends(check_admin)):
    total_cabinets = db.query(Cabinet).count()
    actifs = db.query(Cabinet).filter(Cabinet.is_active == True).count()
    en_essai = db.query(Cabinet).filter(Cabinet.subscription_type == "trial_14d").count()
    payants = db.query(Cabinet).filter(Cabinet.subscription_type.in_(["monthly", "annual"])).count()
    total_tickets = db.query(PatientTicket).count()

    PRIX_MENSUEL = 2500
    PRIX_ANNUEL = 36000
    mensuels = db.query(Cabinet).filter(Cabinet.subscription_type == "monthly").count()
    annuels = db.query(Cabinet).filter(Cabinet.subscription_type == "annual").count()
    ca_estime_mensuel = mensuels * PRIX_MENSUEL + (annuels * PRIX_ANNUEL) / 12

    par_wilaya = {}
    for c in db.query(Cabinet).all():
        par_wilaya[c.wilaya] = par_wilaya.get(c.wilaya, 0) + 1

    return {
        "total_cabinets": total_cabinets, "cabinets_actifs": actifs,
        "cabinets_en_essai": en_essai, "cabinets_payants": payants,
        "total_tickets_emis": total_tickets,
        "ca_estime_mensuel_da": round(ca_estime_mensuel, 2),
        "repartition_wilaya": par_wilaya,
    }


@app.post("/api/admin/login")
def admin_login(data: AdminLogin):
    if data.password != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Mot de passe incorrect.")
    return {"status": "success", "token": ADMIN_TOKEN}
