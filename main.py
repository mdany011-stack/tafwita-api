import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, List
import bcrypt
from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from html import escape
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, text
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
    # waiting, returned, suspended, urgent_requested, urgent, serving, completed, cancelled
    notification_count = Column(Integer, default=0)
    suivi_token_hash = Column(String, nullable=True)
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


class SupportTicket(Base):
    __tablename__ = "support_tickets"
    id = Column(Integer, primary_key=True, index=True)
    nom = Column(String)
    email = Column(String, nullable=True)
    sujet = Column(String)
    message = Column(String)
    statut = Column(String, default="ouvert", index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(bind=engine)


def assurer_colonnes():
    with engine.begin() as conn:
        try:
            conn.execute(text("ALTER TABLE patient_tickets ADD COLUMN suivi_token_hash VARCHAR"))
        except Exception:
            pass
        try:
            conn.execute(text("UPDATE patient_tickets SET nom_patient = NULL, telephone = NULL"))
        except Exception:
            pass


assurer_colonnes()

SESSION_HOURS = 12
MAX_PIN_FAILURES = 5
LOCK_MINUTES = 15
TZ_ALGERIE = timezone(timedelta(hours=1))
STATUTS_OUVERTS = (
    "waiting", "returned", "suspended", "urgent_requested", "urgent", "serving",
)
STATUTS_FILE = STATUTS_OUVERTS
STATUTS_AVANT_VOUS = ("waiting", "returned", "urgent_requested", "urgent")
STATUTS_APPELABLES = ("waiting", "returned", "urgent_requested")
# Compteur memoire : (slug, ip) -> {fails, locked_until}
_pin_attempts = {}
# Nom / telephone patients : jamais en SQL, seulement en RAM le temps du relais cabinet.
_pii_ephemere = {}

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


_DIR_CODE = os.path.dirname(os.path.abspath(__file__))
_PAGES_PUBLIQUES = {
    "patient.html": "text/html; charset=utf-8",
    "admin.html": "text/html; charset=utf-8",
    "service-worker.js": "application/javascript; charset=utf-8",
    "manifest.webmanifest": "application/manifest+json",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
}


def servir_page_publique(nom: str):
    if nom not in _PAGES_PUBLIQUES:
        raise HTTPException(status_code=404, detail="Page introuvable.")
    chemin = os.path.join(_DIR_CODE, nom)
    if not os.path.isfile(chemin):
        raise HTTPException(status_code=404, detail="Page introuvable.")
    return FileResponse(chemin, media_type=_PAGES_PUBLIQUES[nom])


@app.get("/patient.html")
@app.get("/admin.html")
@app.get("/service-worker.js")
@app.get("/manifest.webmanifest")
@app.get("/icon-192.png")
@app.get("/icon-512.png")
def page_statique(request: Request):
    return servir_page_publique(request.url.path.lstrip("/"))

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


def abonnement_ok(cabinet: Cabinet) -> bool:
    if not cabinet.is_active:
        return False
    if cabinet.subscription_end and cabinet.subscription_end < datetime.utcnow():
        return False
    return True


def exiger_abonnement(cabinet: Cabinet) -> None:
    if not abonnement_ok(cabinet):
        raise HTTPException(
            status_code=403,
            detail="Abonnement expire ou cabinet inactif.",
        )


def heure_algerie() -> str:
    return datetime.now(TZ_ALGERIE).strftime("%H:%M")


def prise_autorisee_horaire(cabinet: Cabinet) -> bool:
    if cabinet.ticket_mode != "fixed":
        return True
    maintenant = heure_algerie()
    debut = cabinet.ticket_start_time or "00:00"
    if maintenant < debut:
        return False
    if cabinet.ticket_end_time and maintenant >= cabinet.ticket_end_time:
        return False
    return True


def extraire_suivi(request: Request, body=None) -> Optional[str]:
    header = request.headers.get("x-suivi-token")
    if header and header.strip():
        return header.strip()
    if body is not None:
        tok = getattr(body, "suivi_token", None)
        if tok and str(tok).strip():
            return str(tok).strip()
    return None


def exiger_suivi_si_present(ticket: PatientTicket, request: Request, body=None) -> None:
    if not ticket.suivi_token_hash:
        return
    tok = extraire_suivi(request, body)
    if not tok or hash_jeton(tok) != ticket.suivi_token_hash:
        raise HTTPException(status_code=404, detail="Ticket introuvable.")


def memoriser_pii(ticket_id: int, nom: Optional[str], tel: Optional[str]) -> None:
    nom_ok = (nom or "").strip() or None
    tel_ok = (tel or "").strip() or None
    if nom_ok or tel_ok:
        _pii_ephemere[ticket_id] = {"nom_patient": nom_ok, "telephone": tel_ok}


def pii_ram(ticket_id: int) -> dict:
    return _pii_ephemere.get(ticket_id) or {}


def ticket_public_dict(t: PatientTicket) -> dict:
    d = ticket_to_dict(t)
    d["nom_patient"] = None
    d["telephone"] = None
    return d


def ticket_cabinet_dict(t: PatientTicket) -> dict:
    d = ticket_to_dict(t)
    extra = pii_ram(t.id)
    if extra.get("nom_patient"):
        d["nom_patient"] = extra["nom_patient"]
    if extra.get("telephone"):
        d["telephone"] = extra["telephone"]
    return d


def ticket_echo_dict(t: PatientTicket, nom: Optional[str], tel: Optional[str]) -> dict:
    d = ticket_to_dict(t)
    d["nom_patient"] = nom
    d["telephone"] = tel
    return d


def annuler_tickets_ouverts(db: Session, slug: str, event_type: str, reason_note: Optional[str] = None) -> None:
    remaining = db.query(PatientTicket).filter(
        PatientTicket.cabinet_slug == slug,
        PatientTicket.statut.in_(list(STATUTS_OUVERTS)),
    ).all()
    for t in remaining:
        t.statut = "cancelled"
        log_event(db, t.id, event_type, reason_note)


def log_event(db: Session, ticket_id: int, event_type: str, reason_note: Optional[str] = None):
    db.add(TicketEvent(ticket_id=ticket_id, event_type=event_type, reason_note=reason_note))
    db.commit()


def ticket_to_dict(t: PatientTicket) -> dict:
    return {
        "id": t.id,
        "cabinet_slug": t.cabinet_slug,
        "ticket_num": t.ticket_num,
        "nom_patient": None,
        "telephone": None,
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
    suivi_token: Optional[str] = None


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


class SupportCreate(BaseModel):
    nom: str
    email: Optional[str] = None
    sujet: str
    message: str


class SupportStatut(BaseModel):
    statut: str


class PinUnlock(BaseModel):
    slug: str
    ip: str


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


def html_prise_ticket(cabinet: Cabinet) -> str:
    nom = escape(cabinet.nom_medecin or "Cabinet")
    spec = escape(cabinet.specialite or "")
    slug = escape(cabinet.slug or "")
    ouvert = (
        bool(cabinet.accept_tickets)
        and not cabinet.day_closed
        and abonnement_ok(cabinet)
        and prise_autorisee_horaire(cabinet)
    )
    etat = "ouverte" if ouvert else "fermee"
    return f"""<!doctype html>
<html lang="fr"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TAFWITA — {nom}</title>
<style>
body{{margin:0;background:#f5f8f8;color:#28373e;font:16px "Segoe UI",Arial,sans-serif}}
.box{{max-width:420px;margin:0 auto;padding:28px 18px 40px}}
.logo{{font-size:22px;font-weight:900;letter-spacing:.4px}}.logo span{{color:#329f9a}}
h1{{font-size:26px;margin:18px 0 6px}}
.sub{{color:#607078;margin:0 0 22px}}
.card{{background:#fff;border:1px solid #dbeae7;border-radius:20px;padding:20px;box-shadow:0 12px 30px rgba(31,92,85,.09)}}
label{{display:block;font-weight:700;margin:12px 0 6px}}
input{{width:100%;height:52px;border:1px solid #dbeae7;border-radius:13px;padding:0 14px;font:inherit;box-sizing:border-box}}
button{{width:100%;height:52px;margin-top:18px;border:0;border-radius:99px;background:linear-gradient(90deg,#329f9a,#55bcb2);color:#fff;font-weight:800;font-size:16px}}
button:disabled{{opacity:.5}}
.msg{{min-height:20px;margin-top:12px;color:#bc5d62;text-align:center;font-size:14px}}
.num{{font-size:48px;font-weight:900;color:#237873;text-align:center;margin:12px 0}}
.ok{{background:#e4f5ee;color:#2c8b69;border-radius:12px;padding:12px;text-align:center}}
.warn{{background:#fff4de;color:#755714;border-radius:12px;padding:12px}}
</style></head>
<body data-slug="{slug}" data-ouvert="{etat}">
<div class="box">
<div class="logo">TAF<span>WITA</span></div>
<h1>{nom}</h1>
<p class="sub">{spec}</p>
<div class="card" id="panel"></div>
</div>
<script>
const slug=document.body.dataset.slug, ouvert=document.body.dataset.ouvert==="ouverte";
const K="tafwita_qr_"+slug;
function panel(h){{document.getElementById("panel").innerHTML=h}}
function saved(){{try{{return JSON.parse(localStorage.getItem(K)||"null")}}catch(e){{return null}}}}
async function j(r){{try{{return await r.json()}}catch(e){{return {{}}}}}}
function form(){{
  if(!ouvert){{panel('<div class="warn">La prise de tickets n est pas disponible actuellement.</div>');return;}}
  panel('<label>Votre nom complet</label><input id="n" placeholder="Ex : Farid Cherif" required><label>Telephone (optionnel)</label><input id="t" placeholder="Ex : 0555 66 55 99"><button id="go" type="button">Prendre mon ticket</button><p class="msg" id="m"></p>');
  document.getElementById("go").onclick=take;
}}
async function take(){{
  const n=document.getElementById("n").value.trim(), t=document.getElementById("t").value.trim(), b=document.getElementById("go"), m=document.getElementById("m");
  if(!n){{m.textContent="Le nom est obligatoire.";return;}}
  b.disabled=true;m.textContent="";
  try{{
    const r=await fetch("/api/tickets/take",{{method:"POST",headers:{{"Content-Type":"application/json"}},body:JSON.stringify({{cabinet_slug:slug,nom_patient:n,telephone:t||null}})}});
    const d=await j(r);
    if(!r.ok)throw Error(d.detail||"Erreur");
    const rec={{id:d.ticket.id,token:d.suivi_token,num:d.ticket.ticket_num,nom:n,tel:t||null}};
    localStorage.setItem(K,JSON.stringify(rec));
    show(rec,d.ticket);setInterval(function(){{follow(rec)}},6000);
  }}catch(e){{m.textContent=e.message;b.disabled=false;}}
}}
function show(rec,t){{
  const st=(t&&t.statut)||"waiting";
  const before=(t&&t.waiting_before_you!=null)?t.waiting_before_you:"";
  let extra="";
  if(st==="serving")extra='<div class="ok">C est votre tour. Presentez-vous au cabinet.</div>';
  else if(st==="cancelled")extra='<div class="warn">Votre ticket a ete annule.</div>';
  else if(st==="completed")extra='<div class="ok">Consultation terminee.</div>';
  else extra='<p class="sub" style="text-align:center">'+(before===""?"":before+" patient(s) avant vous")+'</p>';
  panel('<p class="sub" style="text-align:center">Votre numero</p><div class="num">P-'+String(rec.num).padStart(2,"0")+'</div>'+extra);
}}
async function follow(rec){{
  try{{
    const r=await fetch("/api/tickets/"+rec.id+"/patient",{{headers:{{"X-Suivi-Token":rec.token||""}}}});
    const d=await j(r);
    if(r.ok&&d.ticket)show(rec,d.ticket);
  }}catch(e){{}}
}}
const rec=saved();
if(rec&&rec.id){{show(rec,null);follow(rec);setInterval(function(){{follow(rec)}},6000);}}
else form();
</script></body></html>"""


@app.get("/p/{slug}", response_class=HTMLResponse)
def page_prise_qr(slug: str, db: Session = Depends(get_db)):
    cabinet = get_cabinet_or_404(db, slug)
    return HTMLResponse(html_prise_ticket(cabinet))


# D. PRENDRE UN TICKET (patient)
@app.post("/api/tickets/take")
def take_ticket(data: PatientTicketCreate, db: Session = Depends(get_db)):
    cabinet = get_cabinet_or_404(db, data.cabinet_slug)
    exiger_abonnement(cabinet)
    if cabinet.day_closed or not cabinet.accept_tickets:
        raise HTTPException(status_code=400, detail="La prise de tickets n'est pas disponible actuellement.")
    if not prise_autorisee_horaire(cabinet):
        raise HTTPException(status_code=400, detail="La prise de tickets n'est pas ouverte a cette heure.")
    if cabinet.max_tickets_per_day and cabinet.total_issued >= cabinet.max_tickets_per_day:
        raise HTTPException(status_code=400, detail="Nombre maximum de tickets atteint pour aujourd'hui.")

    brut = secrets.token_urlsafe(24)
    cabinet.total_issued += 1
    new_ticket = PatientTicket(
        cabinet_slug=cabinet.slug,
        ticket_num=cabinet.total_issued,
        nom_patient=None,
        telephone=None,
        suivi_token_hash=hash_jeton(brut),
    )
    db.add(new_ticket)
    db.commit()
    db.refresh(new_ticket)
    memoriser_pii(new_ticket.id, data.nom_patient, data.telephone)
    log_event(db, new_ticket.id, "created")

    return {
        "status": "success",
        "ticket": ticket_echo_dict(new_ticket, data.nom_patient, data.telephone),
        "suivi_token": brut,
    }


# E. SUIVI D'UN TICKET (patient)
@app.get("/api/tickets/{ticket_id}/patient")
def track_ticket(ticket_id: int, request: Request, db: Session = Depends(get_db)):
    ticket = get_ticket_or_404(db, ticket_id)
    exiger_suivi_si_present(ticket, request)
    cabinet = db.query(Cabinet).filter(Cabinet.slug == ticket.cabinet_slug).first()
    waiting_before_you = 0
    if cabinet and ticket.statut in STATUTS_AVANT_VOUS:
        waiting_before_you = db.query(PatientTicket).filter(
            PatientTicket.cabinet_slug == cabinet.slug,
            PatientTicket.statut.in_(list(STATUTS_AVANT_VOUS)),
            PatientTicket.ticket_num < ticket.ticket_num,
        ).count()
    payload = ticket_to_dict(ticket)
    payload["waiting_before_you"] = waiting_before_you
    return {"ticket": payload, "waiting_before_you": waiting_before_you}


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
    exiger_abonnement(cabinet)
    cabinet.total_issued += 1
    new_ticket = PatientTicket(
        cabinet_slug=cabinet.slug,
        ticket_num=cabinet.total_issued,
        nom_patient=None,
        telephone=None,
    )
    db.add(new_ticket)
    db.commit()
    db.refresh(new_ticket)
    memoriser_pii(new_ticket.id, data.nom_patient, data.telephone)
    log_event(db, new_ticket.id, "created_manual")
    return {
        "status": "success",
        "ticket": ticket_echo_dict(new_ticket, data.nom_patient, data.telephone),
    }


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
    return [ticket_cabinet_dict(t) for t in tickets]


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
    elif action in ACTIONS_PATIENT:
        exiger_suivi_si_present(ticket, request, body)
    else:
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
        ticket.statut = "urgent_requested"
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
    exiger_abonnement(cabinet)
    annuler_tickets_ouverts(db, cabinet.slug, "start_day_cancel")
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
        PatientTicket.statut.in_(list(STATUTS_OUVERTS)),
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
def get_queue_state(
    slug: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    cabinet = get_cabinet_or_404(db, slug)
    tickets = db.query(PatientTicket).filter(
        PatientTicket.cabinet_slug == cabinet.slug,
        PatientTicket.statut.in_(list(STATUTS_FILE)),
    ).order_by(PatientTicket.ticket_num.asc()).all()

    waiting = db.query(PatientTicket).filter(
        PatientTicket.cabinet_slug == cabinet.slug,
        PatientTicket.statut.in_(list(STATUTS_APPELABLES)),
    ).count()

    prive = False
    token = extraire_bearer(authorization)
    if token:
        cab = cabinet_depuis_jeton(db, token)
        prive = cab is not None and cab.slug == cabinet.slug

    tickets_out = [
        ticket_cabinet_dict(t) if prive else ticket_public_dict(t) for t in tickets
    ]

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
        "tickets": tickets_out,
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
    exiger_abonnement(cabinet)

    # Le patient actuellement en consultation est termine.
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

    next_ticket = (
        db.query(PatientTicket)
        .filter(
            PatientTicket.cabinet_slug == slug,
            PatientTicket.statut == "urgent",
        )
        .order_by(PatientTicket.ticket_num.asc())
        .first()
    )

    if not next_ticket:
        next_ticket = (
            db.query(PatientTicket)
            .filter(
                PatientTicket.cabinet_slug == slug,
                PatientTicket.statut.in_(list(STATUTS_APPELABLES)),
            )
            .order_by(PatientTicket.ticket_num.asc())
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
        "nom_patient": pii_ram(next_ticket.id).get("nom_patient"),
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


@app.get("/api/admin/patients")
def admin_list_patients(db: Session = Depends(get_db), auth: bool = Depends(check_admin)):
    tickets = (
        db.query(PatientTicket)
        .order_by(PatientTicket.created_at.desc())
        .limit(400)
        .all()
    )
    return [
        {
            "id": t.id,
            "cabinet_slug": t.cabinet_slug,
            "ticket_num": t.ticket_num,
            "statut": t.statut,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in tickets
    ]


@app.get("/api/admin/payments")
def admin_list_payments(db: Session = Depends(get_db), auth: bool = Depends(check_admin)):
    prix = {"monthly": 2500, "annual": 36000}
    now = datetime.utcnow()
    rows = []
    for c in db.query(Cabinet).order_by(Cabinet.id.desc()).all():
        montant = prix.get(c.subscription_type)
        if not montant:
            continue
        expire = c.subscription_end and c.subscription_end < now
        rows.append({
            "id": c.id,
            "cabinet": c.nom_medecin,
            "slug": c.slug,
            "type": c.subscription_type,
            "montant_da": montant,
            "echeance": c.subscription_end.strftime("%Y-%m-%d") if c.subscription_end else None,
            "statut": "expire" if expire else ("actif" if c.is_active else "inactif"),
        })
    return rows


@app.get("/api/admin/security")
def admin_security(db: Session = Depends(get_db), auth: bool = Depends(check_admin)):
    now = datetime.utcnow()
    sessions = (
        db.query(CabinetSession)
        .filter(CabinetSession.expires_at > now)
        .order_by(CabinetSession.created_at.desc())
        .all()
    )
    tentatives = []
    for (slug, ip), rec in _pin_attempts.items():
        locked_until = rec.get("locked_until")
        tentatives.append({
            "slug": slug,
            "ip": ip,
            "fails": rec.get("fails", 0),
            "locked": bool(locked_until and locked_until > now),
        })
    return {
        "sessions": [
            {
                "id": s.id,
                "cabinet_slug": s.cabinet_slug,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "expires_at": s.expires_at.isoformat() if s.expires_at else None,
            }
            for s in sessions
        ],
        "pin_attempts": tentatives,
        "session_hours": SESSION_HOURS,
        "max_pin_failures": MAX_PIN_FAILURES,
        "lock_minutes": LOCK_MINUTES,
    }


@app.post("/api/admin/security/unlock")
def admin_unlock_pin(data: PinUnlock, auth: bool = Depends(check_admin)):
    reset_echec_pin(data.slug, data.ip)
    return {"status": "success"}


@app.delete("/api/admin/sessions/{session_id}")
def admin_revoke_session(session_id: int, db: Session = Depends(get_db), auth: bool = Depends(check_admin)):
    session = db.query(CabinetSession).filter(CabinetSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session introuvable.")
    db.delete(session)
    db.commit()
    return {"status": "success"}


@app.get("/api/admin/support")
def admin_list_support(db: Session = Depends(get_db), auth: bool = Depends(check_admin)):
    tickets = db.query(SupportTicket).order_by(SupportTicket.id.desc()).all()
    return [
        {
            "id": t.id,
            "nom": t.nom,
            "email": t.email,
            "sujet": t.sujet,
            "message": t.message,
            "statut": t.statut,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in tickets
    ]


@app.post("/api/admin/support")
def admin_create_support(data: SupportCreate, db: Session = Depends(get_db), auth: bool = Depends(check_admin)):
    ticket = SupportTicket(
        nom=data.nom,
        email=data.email,
        sujet=data.sujet,
        message=data.message,
        statut="ouvert",
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return {"status": "success", "id": ticket.id}


@app.post("/api/admin/support/{ticket_id}/statut")
def admin_support_statut(
    ticket_id: int,
    data: SupportStatut,
    db: Session = Depends(get_db),
    auth: bool = Depends(check_admin),
):
    if data.statut not in ("ouvert", "ferme"):
        raise HTTPException(status_code=400, detail="Statut invalide.")
    ticket = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket support introuvable.")
    ticket.statut = data.statut
    db.commit()
    return {"status": "success", "statut": ticket.statut}
