import hashlib
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional, List
import bcrypt
from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, Session
#=============================================================


import ssl
import smtplib
from email.message import EmailMessage
from fastapi import BackgroundTasks


def send_confirmation_email(to_email: str, verify_url: str):
    print("=== EMAIL TASK START ===")
    print("to_email =", to_email)
    print("verify_url =", verify_url)

    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "465"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")
    mail_from = os.getenv("MAIL_FROM", smtp_user)

    print("smtp_host =", smtp_host)
    print("smtp_port =", smtp_port)
    print("smtp_user =", smtp_user)
    print("mail_from =", mail_from)
    print("smtp_pass exists =", bool(smtp_pass))

    try:
        msg = EmailMessage()
        msg["Subject"] = "Confirmez votre inscription TAFWITA"
        msg["From"] = mail_from
        msg["To"] = to_email

        body = (
            "Bonjour,\n\n"
            "Cliquez sur ce lien pour confirmer votre inscription TAFWITA :\n"
            f"{verify_url}\n\n"
            "Si vous n'êtes pas à l'origine de cette demande, ignorez cet e-mail."
        )

        msg.set_content(body)

        context = ssl.create_default_context()

        print("Opening SMTP connection...")
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as server:
            server.set_debuglevel(1)
            print("SMTP connection opened")
            print("Logging in...")
            server.login(smtp_user, smtp_pass)
            print("Login success")
            server.send_message(msg)
            print("Message sent successfully")

        print("=== EMAIL TASK END SUCCESS ===")

    except Exception as e:
        print("=== EMAIL TASK ERROR ===")
        print(repr(e))
        raise





#=============================================================
# ============================================================
# 1. CONNEXION BASE DE DONNEES
# ============================================================
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL and DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

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
    statut = Column(String, default="waiting", index=True)
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


class CabinetSession(Base):
    __tablename__ = "cabinet_sessions"
    id = Column(Integer, primary_key=True, index=True)
    cabinet_slug = Column(String, index=True)
    token_hash = Column(String, unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, index=True)


Base.metadata.create_all(bind=engine)

SESSION_HOURS = 12
MAX_PIN_FAILURES = 5
LOCK_MINUTES = 15
# Compteur memoire : (slug, ip) -> {fails, locked_until}
_pin_attempts = {}

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

@app.get("/health")
def health():
    return {"status": "healthy", "service": "tafwita-api"}

@app.get("/api/test-email")
def test_email(email: str, background_tasks: BackgroundTasks):
    verify_url = f"{os.getenv('PUBLIC_SITE_URL')}/confirm-email.html?token=test123"

    background_tasks.add_task(
        send_confirmation_email,
        email,
        verify_url
    )

    return {
        "status": "ok",
        "message": "E-mail en cours d'envoi."
    }







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


def est_hash_bcrypt(stored: Optional[str]) -> bool:
    return bool(stored) and stored.startswith("$2")


def hash_pin(pin: str) -> str:
    return bcrypt.hashpw(pin.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def pin_ok(pin: str, stored: Optional[str]) -> bool:
    if not pin or not stored:
        return False
    if est_hash_bcrypt(stored):
        try:
            return bcrypt.checkpw(pin.encode("utf-8"), stored.encode("utf-8"))
        except ValueError:
            return False
    return secrets.compare_digest(stored, pin)


def rehash_si_clair(db: Session, cabinet: Cabinet, pin: str) -> None:
    if cabinet.code_pin and not est_hash_bcrypt(cabinet.code_pin):
        cabinet.code_pin = hash_pin(pin)


def hash_jeton(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def extraire_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        return parts[1].strip()
    return None


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "inconnu"


def verifier_limite_pin(slug: str, ip: str) -> None:
    rec = _pin_attempts.get((slug, ip))
    if rec and rec.get("locked_until") and rec["locked_until"] > datetime.utcnow():
        raise HTTPException(
            status_code=403,
            detail="Trop de tentatives, reessayez dans 15 minutes.",
        )


def noter_echec_pin(slug: str, ip: str) -> None:
    key = (slug, ip)
    rec = _pin_attempts.get(key) or {"fails": 0, "locked_until": None}
    rec["fails"] += 1
    if rec["fails"] >= MAX_PIN_FAILURES:
        rec["locked_until"] = datetime.utcnow() + timedelta(minutes=LOCK_MINUTES)
    _pin_attempts[key] = rec


def reset_echec_pin(slug: str, ip: str) -> None:
    _pin_attempts.pop((slug, ip), None)


def cabinet_depuis_jeton(db: Session, token: str) -> Optional[Cabinet]:
    session = (
        db.query(CabinetSession)
        .filter(
            CabinetSession.token_hash == hash_jeton(token),
            CabinetSession.expires_at > datetime.utcnow(),
        )
        .first()
    )
    if not session:
        return None
    return db.query(Cabinet).filter(Cabinet.slug == session.cabinet_slug).first()


def creer_session(db: Session, cabinet: Cabinet) -> tuple:
    brut = secrets.token_urlsafe(32)
    expire = datetime.utcnow() + timedelta(hours=SESSION_HOURS)
    db.add(CabinetSession(
        cabinet_slug=cabinet.slug,
        token_hash=hash_jeton(brut),
        created_at=datetime.utcnow(),
        expires_at=expire,
    ))
    return brut, expire


def authentifier_cabinet(
    db: Session,
    slug: str,
    request: Request,
    authorization: Optional[str] = None,
    code_pin: Optional[str] = None,
    pin_query: Optional[str] = None,
) -> Cabinet:
    token = extraire_bearer(authorization)
    if token:
        cabinet = cabinet_depuis_jeton(db, token)
        if not cabinet:
            raise HTTPException(status_code=401, detail="Session invalide ou expiree.")
        if cabinet.slug != slug:
            raise HTTPException(status_code=403, detail="Non autorise.")
        return cabinet

    # PIN en query : temporaire, compatibilite ancienne app bureau.
    pin = code_pin or pin_query
    if not pin:
        raise HTTPException(status_code=401, detail="Authentification requise.")

    ip = client_ip(request)
    verifier_limite_pin(slug, ip)
    cabinet = get_cabinet_or_404(db, slug)
    if not pin_ok(pin, cabinet.code_pin):
        noter_echec_pin(slug, ip)
        raise HTTPException(status_code=403, detail="Code PIN incorrect.")
    reset_echec_pin(slug, ip)
    rehash_si_clair(db, cabinet, pin)
    return cabinet


def authentifier_pour_ticket(
    db: Session,
    ticket: PatientTicket,
    request: Request,
    authorization: Optional[str] = None,
    code_pin: Optional[str] = None,
    pin_query: Optional[str] = None,
) -> Cabinet:
    token = extraire_bearer(authorization)
    if token:
        cabinet = cabinet_depuis_jeton(db, token)
        if not cabinet:
            raise HTTPException(status_code=401, detail="Session invalide ou expiree.")
        if ticket.cabinet_slug != cabinet.slug:
            raise HTTPException(status_code=404, detail="Ticket introuvable.")
        return cabinet
    return authentifier_cabinet(
        db, ticket.cabinet_slug, request, None, code_pin, pin_query
    )


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


class PinCompat(BaseModel):
    code_pin: Optional[str] = None


class PatientTicketCreate(BaseModel):
    cabinet_slug: str
    nom_patient: str
    telephone: Optional[str] = None


class ManualTicketCreate(PatientTicketCreate):
    code_pin: Optional[str] = None


class TicketActionBody(BaseModel):
    code_pin: Optional[str] = None
    reason_code: Optional[str] = None
    reason_note: Optional[str] = None


class UrgentBody(BaseModel):
    code_pin: Optional[str] = None
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
        telephone=data.telephone, wilaya=data.wilaya, code_pin=hash_pin(data.code_pin),
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
        code_pin=hash_pin(data.password[:6] if data.password else "0000"),
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
def notify_ticket(
    ticket_id: int,
    request: Request,
    pin: Optional[str] = None,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    ticket = get_ticket_or_404(db, ticket_id)
    authentifier_pour_ticket(db, ticket, request, authorization, None, pin)
    ticket.notification_count += 1
    db.commit()
    log_event(db, ticket.id, "notify")
    return {"status": "success", "notification_count": ticket.notification_count}


# I. APPROUVER UNE PRIORITE MEDICALE (medecin)
@app.post("/api/tickets/{ticket_id}/approve-urgent")
def approve_urgent(
    ticket_id: int,
    request: Request,
    body: UrgentBody,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    ticket = get_ticket_or_404(db, ticket_id)
    authentifier_pour_ticket(db, ticket, request, authorization, body.code_pin)
    ticket.statut = "urgent"
    db.commit()
    log_event(db, ticket.id, "urgent_approved", body.urgent_reason)
    return {"status": "success", "ticket": ticket_to_dict(ticket)}


# ============================================================
# 7. ENDPOINTS CABINET (app desktop)
# ============================================================

# J. VERIFICATION DU CODE PIN (connexion desktop)
@app.post("/api/cabinets/{slug}/verify-pin")
def verify_pin(slug: str, body: PinCheck, request: Request, db: Session = Depends(get_db)):
    ip = client_ip(request)
    verifier_limite_pin(slug, ip)
    cabinet = get_cabinet_or_404(db, slug)
    if not pin_ok(body.code_pin, cabinet.code_pin):
        noter_echec_pin(slug, ip)
        raise HTTPException(status_code=403, detail="Code PIN incorrect.")
    reset_echec_pin(slug, ip)
    rehash_si_clair(db, cabinet, body.code_pin)
    token, expire = creer_session(db, cabinet)
    db.commit()
    return {
        "status": "success",
        "cabinet_nom": cabinet.nom_medecin,
        "token": token,
        "expires_at": expire.isoformat(),
    }


@app.post("/api/cabinets/logout")
def logout_cabinet(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    token = extraire_bearer(authorization)
    if token:
        db.query(CabinetSession).filter(
            CabinetSession.token_hash == hash_jeton(token)
        ).delete()
        db.commit()
    return {"status": "success"}


# K. AJOUTER UN TICKET MANUEL (papier)
@app.post("/api/tickets/add-manual")
def add_manual_ticket(
    data: ManualTicketCreate,
    request: Request,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    cabinet = authentifier_cabinet(
        db, data.cabinet_slug, request, authorization, data.code_pin
    )
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
def cabinet_tickets(
    slug: str,
    request: Request,
    pin: Optional[str] = None,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    cabinet = authentifier_cabinet(db, slug, request, authorization, None, pin)
    tickets = db.query(PatientTicket).filter(PatientTicket.cabinet_slug == cabinet.slug).order_by(PatientTicket.id.desc()).all()
    return [ticket_to_dict(t) for t in tickets]


# M. ANNULER UN TICKET APRES 5 PASSAGES (cabinet)
@app.post("/api/tickets/{ticket_id}/cabinet-cancel")
def cabinet_cancel_ticket(
    ticket_id: int,
    request: Request,
    body: TicketActionBody,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    ticket = get_ticket_or_404(db, ticket_id)
    authentifier_pour_ticket(db, ticket, request, authorization, body.code_pin)
    ticket.statut = "cancelled"
    db.commit()
    log_event(db, ticket.id, "cabinet_cancel", body.reason_note)
    return {"status": "success", "ticket": ticket_to_dict(ticket)}


# F. ACTIONS SUR UN TICKET (patient sans jeton, cabinet avec jeton)
# Placee apres /notify, /approve-urgent et /cabinet-cancel pour ne pas les masquer.
ACTIONS_CABINET = {"suspend", "return-to-queue", "cabinet-cancel"}
ACTIONS_PATIENT = {"patient-suspend", "patient-cancel", "patient-present", "request-urgent"}


@app.post("/api/tickets/{ticket_id}/{action}")
def patient_ticket_action(
    ticket_id: int,
    action: str,
    request: Request,
    body: TicketActionBody,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    ticket = get_ticket_or_404(db, ticket_id)

    if action in ACTIONS_CABINET:
        authentifier_pour_ticket(db, ticket, request, authorization, body.code_pin)
    elif action not in ACTIONS_PATIENT:
        raise HTTPException(status_code=400, detail="Action inconnue.")

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

    db.commit()
    return {"status": "success", "ticket": ticket_to_dict(ticket)}


# N. DEMARRER UNE NOUVELLE JOURNEE
@app.post("/api/cabinets/{slug}/start-day")
def start_day(
    slug: str,
    request: Request,
    body: PinCompat = PinCompat(),
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    cabinet = authentifier_cabinet(db, slug, request, authorization, body.code_pin)
    cabinet.day_closed = False
    cabinet.accept_tickets = True
    cabinet.serving_num = 0
    cabinet.total_issued = 0
    db.commit()
    return {"status": "success", "message": "Journee demarree."}


# O. CLOTURER LA JOURNEE
@app.post("/api/cabinets/{slug}/close-day")
def close_day(
    slug: str,
    request: Request,
    body: CloseDayBody,
    pin: Optional[str] = None,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    cabinet = authentifier_cabinet(db, slug, request, authorization, None, pin)

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
def update_settings(
    slug: str,
    request: Request,
    body: CabinetSettings,
    pin: Optional[str] = None,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    cabinet = authentifier_cabinet(db, slug, request, authorization, None, pin)

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
def call_next(
    slug: str,
    request: Request,
    pin: Optional[str] = None,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db)
):
    cabinet = authentifier_cabinet(db, slug, request, authorization, None, pin)

    # Le patient actuellement en consultation est terminé.
    current = (
        db.query(PatientTicket)
        .filter(
            PatientTicket.cabinet_slug == slug,
            PatientTicket.statut == "serving"
        )
        .first()
    )

    if current:
        current.statut = "completed"

    # Important :
    # on accepte waiting ET returned.
    next_ticket = (
        db.query(PatientTicket)
        .filter(
            PatientTicket.cabinet_slug == slug,
            PatientTicket.statut.in_(["waiting", "returned"])
        )
        .order_by(
            PatientTicket.ticket_num.asc()
        )
        .first()
    )

    if not next_ticket:
        db.commit()

        raise HTTPException(
            status_code=404,
            detail="Aucun patient en attente."
        )

    next_ticket.statut = "serving"

    cabinet.serving_num = next_ticket.ticket_num

    db.commit()
    db.refresh(next_ticket)

    return {
        "status": "success",
        "message": f"Patient P-{next_ticket.ticket_num:02d} appelé.",
        "ticket_id": next_ticket.id,
        "ticket_num": next_ticket.ticket_num,
        "nom_patient": next_ticket.nom_patient,
        "statut": next_ticket.statut,
        "display_serving": f"N deg P-{next_ticket.ticket_num:02d}"
    }


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
