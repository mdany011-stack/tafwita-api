import base64
import hashlib
import io
import json
import os
import secrets
import ssl
import smtplib
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Optional, List
import bcrypt
from fastapi import FastAPI, HTTPException, Depends, Header, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from html import escape
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session


def _pages_base() -> str:
    return os.getenv(
        "TAFWITA_PATIENT_URL",
        "https://tafwita.pages.dev",
    ).rstrip("/")


def patient_take_url(slug: str) -> str:
    from urllib.parse import quote
    return f"{_pages_base()}/patient.html?cabinet={quote(slug, safe='')}&take=1"


def cabinet_console_url() -> str:
    return f"{_pages_base()}/cabinet.html"


def tv_screen_url(slug: str) -> str:
    from urllib.parse import quote
    return f"{_pages_base()}/tv.html?cabinet={quote(slug, safe='')}"


def make_affiche_png(nom: str, specialite: str, url: str) -> bytes:
    import qrcode
    from PIL import Image, ImageDraw, ImageFont

    W, H = 1240, 1754
    bg, dark, pale, soft, border = "#F5F8F8", "#237873", "#DDF2EE", "#607078", "#DBEAE7"
    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    draw.rectangle((0, 0, W, 210), fill=dark)

    def centre(y, text, fill):
        bbox = draw.textbbox((0, 0), text, font=font)
        draw.text(((W - (bbox[2] - bbox[0])) / 2, y), text, font=font, fill=fill)

    centre(50, "TAFWITA", "white")
    centre(110, "Votre consultation, au bon moment", pale)
    centre(260, nom or "Cabinet", dark)
    if specialite:
        centre(300, specialite, soft)
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=14, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    qimg = qr.make_image(fill_color=dark, back_color="white").convert("RGB").resize((640, 640))
    qx, qy = (W - 640) // 2, 420
    box = (qx - 28, qy - 28, qx + 668, qy + 668)
    if hasattr(draw, "rounded_rectangle"):
        draw.rounded_rectangle(box, radius=28, fill="white", outline=border, width=3)
    else:
        draw.rectangle(box, fill="white", outline=border, width=3)
    img.paste(qimg, (qx, qy))
    centre(1140, "Scannez pour prendre un ticket", dark)
    centre(1180, "Entrez votre nom sur votre telephone.", soft)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def smtp_autorise() -> bool:
    # Render gratuit bloque les ports SMTP (465/587). HTTPS (Resend/Brevo) reste possible.
    return os.getenv("SMTP_ENABLED", "").strip().lower() in ("1", "true", "oui", "yes")


def email_https_ok() -> bool:
    return bool(os.getenv("RESEND_API_KEY") or os.getenv("BREVO_API_KEY"))


def email_configure() -> bool:
    return email_https_ok() or smtp_autorise()


def _mail_from_https() -> str:
    # pages.dev / Gmail / example.com ne peuvent pas etre expediteur Resend.
    # Sans domaine DNS verifie : uniquement beth.t@example.com.
    brut = (os.getenv("MAIL_FROM") or "").strip()
    bas = brut.lower()
    interdit = (
        "gmail.com", "googlemail.com", "example.com", "pages.dev",
        "github.io", "http://", "https://",
    )
    if os.getenv("RESEND_API_KEY"):
        if "resend.dev" in bas and not any(x in bas for x in ("example.com", "pages.dev")):
            return brut if "<" in brut else f"TAFWITA <{brut}>"
        return "TAFWITA <beth.t@example.com>"
    if brut and "@" in brut and not any(x in bas for x in interdit):
        return brut if "<" in brut else f"TAFWITA <{brut}>"
    return "TAFWITA <beth.t@example.com>"


def _http_json(url: str, headers: dict, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    for cle, val in headers.items():
        req.add_header(cle, val)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "TAFWITA/1.0 (+https://tafwita-api.onrender.com)")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")[:800]
        if "1010" in raw:
            raise RuntimeError(
                "Cloudflare 1010 : requete bloquee vers Resend."
            ) from e
        try:
            data = json.loads(raw)
            msg = data.get("message") or data.get("name") or raw
        except json.JSONDecodeError:
            msg = raw
        raise RuntimeError("HTTP %s: %s" % (e.code, msg)) from e


def envoyer_email(
    to_email: str,
    subject: str,
    body: str,
    png_bytes: Optional[bytes] = None,
    png_name: str = "tafwita-affiche-qr.png",
    reply_to: Optional[str] = None,
) -> Optional[str]:
    dest = (to_email or "").strip()
    if not dest or "@" not in dest:
        return "adresse e-mail invalide"
    resend = (os.getenv("RESEND_API_KEY") or "").strip()
    brevo = (os.getenv("BREVO_API_KEY") or "").strip()
    repondre = (reply_to or "").strip() or None
    try:
        if resend:
            payload = {
                "from": _mail_from_https(),
                "to": [dest],
                "subject": subject,
                "text": body,
            }
            if repondre:
                payload["reply_to"] = repondre
            if png_bytes:
                payload["attachments"] = [{
                    "filename": png_name,
                    "content": base64.b64encode(png_bytes).decode("ascii"),
                }]
            _http_json(
                "https://api.resend.com/emails",
                {"Authorization": "Bearer " + resend},
                payload,
            )
            return None
        if brevo:
            expediteur = _mail_from_https()
            if "<" in expediteur and ">" in expediteur:
                nom_exp = expediteur.split("<", 1)[0].strip() or "TAFWITA"
                mail_exp = expediteur.split("<", 1)[1].rstrip(">").strip()
            else:
                nom_exp, mail_exp = "TAFWITA", expediteur
            payload = {
                "sender": {"name": nom_exp, "email": mail_exp},
                "to": [{"email": dest}],
                "subject": subject,
                "textContent": body,
            }
            if repondre:
                payload["replyTo"] = {"email": repondre}
            if png_bytes:
                payload["attachment"] = [{
                    "name": png_name,
                    "content": base64.b64encode(png_bytes).decode("ascii"),
                }]
            _http_json(
                "https://api.brevo.com/v3/smtp/email",
                {"api-key": brevo, "accept": "application/json"},
                payload,
            )
            return None
        if not smtp_autorise():
            return "aucun service e-mail configure (RESEND_API_KEY)"
        smtp_host = os.getenv("SMTP_HOST")
        smtp_user = os.getenv("SMTP_USER")
        smtp_pass = os.getenv("SMTP_PASS")
        if not (smtp_host and smtp_user and smtp_pass):
            return "SMTP incomplet"
        smtp_port = int(os.getenv("SMTP_PORT", "465"))
        mail_from = os.getenv("MAIL_FROM", smtp_user)
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = mail_from
        msg["To"] = dest
        if repondre:
            msg["Reply-To"] = repondre
        msg.set_content(body)
        if png_bytes:
            msg.add_attachment(png_bytes, maintype="image", subtype="png", filename=png_name)
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as server:
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        return None
    except Exception as e:
        return str(e)[:400]


def noter_echec_email(nom: str, email: str, detail: str) -> None:
    db = SessionLocal()
    try:
        db.add(SupportTicket(
            nom=nom or "TAFWITA",
            email=email,
            sujet="Echec e-mail inscription",
            message=(detail or "envoi refuse")[:500],
        ))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def send_trial_welcome_email(to_email: str, slug: str, nom: str, specialite: str):
    take_url = patient_take_url(slug)
    console_url = cabinet_console_url()
    body = (
        "Bonjour,\n\n"
        "Votre cabinet TAFWITA est pret (essai 45 jours).\n\n"
        f"Identifiant cabinet : {slug}\n"
        f"Console secretaire (telephone) : {console_url}\n"
        f"Page patient pour le QR : {take_url}\n\n"
        "L'affiche QR est en piece jointe si le serveur a pu la generer. "
        "Vous pouvez aussi l'imprimer depuis la console.\n"
        "Le mot de passe n'est pas envoye par e-mail. Utilisez celui choisi a l'inscription.\n\n"
        "TAFWITA"
    )
    if not email_configure():
        noter_echec_email(nom, to_email, "RESEND_API_KEY absent sur le serveur")
        return
    png = None
    try:
        png = make_affiche_png(nom, specialite, take_url)
    except Exception:
        png = None
    err = envoyer_email(
        to_email,
        "TAFWITA — votre cabinet et votre affiche QR",
        body,
        png_bytes=png,
    )
    if err and png:
        err = envoyer_email(
            to_email,
            "TAFWITA — votre cabinet",
            body,
            png_bytes=None,
        )
    if err:
        noter_echec_email(nom, to_email, err)


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

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN") or ""


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
    subscription_type = Column(String, default="trial_45d")
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

TRIAL_DAYS = 45
SESSION_HOURS = 12
MAX_PIN_FAILURES = 5
LOCK_MINUTES = 15
MAX_SIGNUP_PER_HOUR = 3
MAX_CONTACT_PER_HOUR = 5
MAX_TAKE_PER_HOUR = 30
PRIX_MENSUEL_DA = 2500
PRIX_ANNUEL_DA = 25000
TZ_ALGERIE = timezone(timedelta(hours=1))
STATUTS_OUVERTS = (
    "waiting", "returned", "suspended", "urgent_requested", "urgent", "serving",
)
STATUTS_FILE = STATUTS_OUVERTS
STATUTS_AVANT_VOUS = ("waiting", "returned", "urgent_requested", "urgent")
STATUTS_APPELABLES = ("waiting", "returned", "urgent_requested")
# Compteur memoire : (slug, ip) -> {fails, locked_until}
_pin_attempts = {}
# Inscriptions : ip -> [horodatages]
_signup_attempts = {}
_contact_attempts = {}
_take_attempts = {}
# Nom / telephone patients : jamais en SQL, seulement en RAM le temps du relais cabinet.
_pii_ephemere = {}

# ============================================================
# 3. APP
# ============================================================
app = FastAPI(title="TAFWITA API Cloud", description="Gestion des files et des abonnements")
_CORS_EXTRAS = [o.strip() for o in os.getenv("TAFWITA_CORS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://tafwita.pages.dev",
        "https://mdany011-stack.github.io",
        "http://localhost",
        "http://127.0.0.1",
        *_CORS_EXTRAS,
    ],
    allow_origin_regex=(
        r"https://mdany011-stack\.github\.io"
        r"|https://([a-z0-9-]+\.)*(pages\.dev|github\.io)"
        r"|https://tafwita-api\.onrender\.com"
        r"|http://(localhost|127\.0\.0\.1)(:\d+)?"
    ),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {
        "status": "healthy",
        "service": "tafwita-api",
        "email_ok": email_configure(),
    }


_DIR_CODE = os.path.dirname(os.path.abspath(__file__))
_PAGES_PUBLIQUES = {
    "patient.html": "text/html; charset=utf-8",
    "cabinet.html": "text/html; charset=utf-8",
    "tv.html": "text/html; charset=utf-8",
    "admin.html": "text/html; charset=utf-8",
    "mentions.html": "text/html; charset=utf-8",
    "confidentialite.html": "text/html; charset=utf-8",
    "service-worker.js": "application/javascript; charset=utf-8",
    "manifest.webmanifest": "application/manifest+json",
    "manifest-cabinet.webmanifest": "application/manifest+json",
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
@app.get("/cabinet.html")
@app.get("/tv.html")
@app.get("/admin.html")
@app.get("/mentions.html")
@app.get("/confidentialite.html")
@app.get("/service-worker.js")
@app.get("/manifest.webmanifest")
@app.get("/manifest-cabinet.webmanifest")
@app.get("/icon-192.png")
@app.get("/icon-512.png")
def page_statique(request: Request):
    return servir_page_publique(request.url.path.lstrip("/"))








def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def secrets_egaux(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b:
        return False
    try:
        return secrets.compare_digest(str(a), str(b))
    except (TypeError, ValueError):
        return False


def check_admin(x_admin_token: Optional[str] = Header(None)):
    if not ADMIN_TOKEN or not secrets_egaux(x_admin_token, ADMIN_TOKEN):
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


def valider_mot_de_passe(mdp: str) -> str:
    secret = (mdp or "").strip()
    if len(secret) < 8 or len(secret) > 128:
        raise HTTPException(
            status_code=400,
            detail="Le mot de passe doit contenir entre 8 et 128 caracteres.",
        )
    if not any(c.isalpha() for c in secret) or not any(c.isdigit() for c in secret):
        raise HTTPException(
            status_code=400,
            detail="Le mot de passe doit contenir au moins une lettre et un chiffre.",
        )
    return secret


def secret_depuis_body(body) -> Optional[str]:
    if body is None:
        return None
    for name in ("password", "code_pin"):
        val = getattr(body, name, None)
        if val and str(val).strip():
            return str(val).strip()
    return None


def slugifier(texte: str) -> str:
    brut = (texte or "").strip().lower()
    parties = []
    for ch in brut:
        if ch.isalnum():
            parties.append(ch)
        elif ch in " ._-":
            parties.append("-")
    slug = "".join(parties)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-") or "cabinet"


def slug_unique(db: Session, base: str) -> str:
    slug = slugifier(base)
    if not db.query(Cabinet).filter(Cabinet.slug == slug).first():
        return slug
    n = 2
    while db.query(Cabinet).filter(Cabinet.slug == f"{slug}-{n}").first():
        n += 1
        if n > 999:
            raise HTTPException(status_code=400, detail="Impossible de creer un identifiant unique.")
    return f"{slug}-{n}"


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


def derriere_proxy() -> bool:
    # Render pose X-Forwarded-For. En local, on ignore l'en-tete (spoofable).
    if os.getenv("RENDER", "").strip():
        return True
    return os.getenv("TRUST_PROXY", "").strip().lower() in ("1", "true", "oui", "yes")


def client_ip(request: Request) -> str:
    if derriere_proxy():
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "inconnu"


def verifier_limite_inscription(ip: str) -> None:
    maintenant = datetime.utcnow()
    seuil = maintenant - timedelta(hours=1)
    horaires = [t for t in _signup_attempts.get(ip, []) if t > seuil]
    _signup_attempts[ip] = horaires
    if len(horaires) >= MAX_SIGNUP_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail="Trop de demandes, reessayez plus tard.",
        )


def noter_inscription(ip: str) -> None:
    _signup_attempts.setdefault(ip, []).append(datetime.utcnow())


def verifier_limite_contact(ip: str) -> None:
    maintenant = datetime.utcnow()
    seuil = maintenant - timedelta(hours=1)
    horaires = [t for t in _contact_attempts.get(ip, []) if t > seuil]
    _contact_attempts[ip] = horaires
    if len(horaires) >= MAX_CONTACT_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail="Trop de messages, reessayez plus tard.",
        )


def noter_contact(ip: str) -> None:
    _contact_attempts.setdefault(ip, []).append(datetime.utcnow())


def verifier_limite_prise(ip: str) -> None:
    maintenant = datetime.utcnow()
    seuil = maintenant - timedelta(hours=1)
    horaires = [t for t in _take_attempts.get(ip, []) if t > seuil]
    _take_attempts[ip] = horaires
    if len(horaires) >= MAX_TAKE_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail="Trop de tickets, reessayez plus tard.",
        )


def noter_prise(ip: str) -> None:
    _take_attempts.setdefault(ip, []).append(datetime.utcnow())


def send_contact_email(nom: str, email: str, message: str) -> None:
    if not email_configure():
        return
    inbox = os.getenv("CONTACT_EMAIL", "tafwitacontact@gmail.com")
    envoyer_email(
        inbox,
        "TAFWITA — message du site",
        f"Nom : {nom}\nE-mail : {email}\n\n{message}\n",
        reply_to=email,
    )


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
) -> Cabinet:
    token = extraire_bearer(authorization)
    if token:
        cabinet = cabinet_depuis_jeton(db, token)
        if not cabinet:
            raise HTTPException(status_code=401, detail="Session invalide ou expiree.")
        if cabinet.slug != slug:
            raise HTTPException(status_code=403, detail="Non autorise.")
        return cabinet

    if not code_pin:
        raise HTTPException(status_code=401, detail="Authentification requise.")

    ip = client_ip(request)
    verifier_limite_pin(slug, ip)
    cabinet = get_cabinet_or_404(db, slug)
    if not pin_ok(code_pin, cabinet.code_pin):
        noter_echec_pin(slug, ip)
        raise HTTPException(status_code=403, detail="Mot de passe incorrect.")
    reset_echec_pin(slug, ip)
    rehash_si_clair(db, cabinet, code_pin)
    return cabinet


def authentifier_pour_ticket(
    db: Session,
    ticket: PatientTicket,
    request: Request,
    authorization: Optional[str] = None,
    code_pin: Optional[str] = None,
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
        db, ticket.cabinet_slug, request, None, code_pin
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
        raise HTTPException(status_code=404, detail="Ticket introuvable.")
    tok = extraire_suivi(request, body)
    if not tok or not secrets_egaux(hash_jeton(tok), ticket.suivi_token_hash):
        raise HTTPException(status_code=404, detail="Ticket introuvable.")


def memoriser_pii(ticket_id: int, nom: Optional[str], tel: Optional[str]) -> None:
    nom_ok = (nom or "").strip()[:80] or None
    tel_ok = (tel or "").strip()[:20] or None
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
        "opening_time": c.opening_time,
        "closing_time": c.closing_time,
    }


# ============================================================
# 4. SCHEMAS
# ============================================================
class CabinetRegister(BaseModel):
    nom_medecin: str
    specialite: str
    telephone: str
    wilaya: str
    password: Optional[str] = None
    code_pin: Optional[str] = None


class TrialSignup(BaseModel):
    nom: str
    prenom: str
    cabinet: str
    telephone: str
    email: str
    specialite: str
    wilaya: str
    password: str
    website: Optional[str] = None


class PinCheck(BaseModel):
    password: Optional[str] = None
    code_pin: Optional[str] = None


class PinCompat(BaseModel):
    password: Optional[str] = None
    code_pin: Optional[str] = None


class PatientTicketCreate(BaseModel):
    cabinet_slug: str
    nom_patient: str
    telephone: Optional[str] = None


class ManualTicketCreate(PatientTicketCreate):
    password: Optional[str] = None
    code_pin: Optional[str] = None


class TicketActionBody(BaseModel):
    password: Optional[str] = None
    code_pin: Optional[str] = None
    reason_code: Optional[str] = None
    reason_note: Optional[str] = None
    suivi_token: Optional[str] = None


class UrgentBody(BaseModel):
    password: Optional[str] = None
    code_pin: Optional[str] = None
    urgent_reason: str


class CloseDayBody(BaseModel):
    reason: Optional[str] = None


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


class ContactPublic(BaseModel):
    nom: str
    email: str
    message: str
    website: Optional[str] = None


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
def register_cabinet(data: CabinetRegister, request: Request, db: Session = Depends(get_db)):
    ip = client_ip(request)
    verifier_limite_inscription(ip)
    noter_inscription(ip)
    secret = valider_mot_de_passe(secret_depuis_body(data) or "")
    existing = db.query(Cabinet).filter(Cabinet.telephone == data.telephone).first()
    if existing:
        raise HTTPException(status_code=400, detail="Ce numero de telephone est deja enregistre.")
    slug = slug_unique(db, data.nom_medecin)

    trial_end = datetime.utcnow() + timedelta(days=TRIAL_DAYS)
    cabinet = Cabinet(
        slug=slug, nom_medecin=data.nom_medecin, specialite=data.specialite,
        telephone=data.telephone, wilaya=data.wilaya, code_pin=hash_pin(secret),
        subscription_type="trial_45d",
        subscription_end=trial_end,
    )
    db.add(cabinet)
    db.commit()
    db.refresh(cabinet)
    return {
        "status": "success",
        "message": f"Cabinet cree avec succes ! {TRIAL_DAYS} jours d'essai actives (promotion lancement).",
        "cabinet_slug": cabinet.slug,
        "trial_end": trial_end.strftime("%Y-%m-%d"),
    }


# A2. INSCRIPTION ESSAI GRATUIT (popup du site vitrine)
@app.post("/api/trial-signup")
def trial_signup(data: TrialSignup, request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    if (data.website or "").strip():
        raise HTTPException(status_code=400, detail="Demande invalide.")
    ip = client_ip(request)
    verifier_limite_inscription(ip)
    noter_inscription(ip)
    secret = valider_mot_de_passe(data.password)
    existing = db.query(Cabinet).filter(Cabinet.telephone == data.telephone).first()
    if existing:
        raise HTTPException(status_code=400, detail="Ce numero de telephone est deja enregistre.")
    slug = slug_unique(db, data.cabinet)

    trial_end = datetime.utcnow() + timedelta(days=TRIAL_DAYS)
    cabinet = Cabinet(
        slug=slug,
        nom_medecin=f"{data.prenom} {data.nom}",
        specialite=data.specialite,
        telephone=data.telephone,
        wilaya=data.wilaya,
        code_pin=hash_pin(secret),
        subscription_type="trial_45d",
        subscription_end=trial_end,
    )
    db.add(cabinet)
    db.commit()
    db.refresh(cabinet)
    db.add(SupportTicket(
        nom=cabinet.nom_medecin or data.cabinet,
        email=(data.email or "").strip() or None,
        sujet="Inscription essai",
        message=(
            f"Identifiant : {cabinet.slug}\n"
            f"Telephone : {data.telephone}\n"
            f"Specialite : {cabinet.specialite}\n"
            f"Wilaya : {data.wilaya}"
        ),
    ))
    db.commit()
    background_tasks.add_task(
        send_trial_welcome_email,
        data.email,
        cabinet.slug,
        cabinet.nom_medecin or data.cabinet,
        cabinet.specialite or "",
    )
    return {
        "status": "success",
        "message": "Cabinet cree. Ouvrez la console pour imprimer l affiche QR.",
        "cabinet_slug": cabinet.slug,
        "console_url": cabinet_console_url(),
        "email_prevu": email_configure(),
    }


@app.post("/api/contact")
def contact_public(data: ContactPublic, request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    if (data.website or "").strip():
        raise HTTPException(status_code=400, detail="Demande invalide.")
    nom = (data.nom or "").strip()
    email = (data.email or "").strip()
    message = (data.message or "").strip()
    if not nom or not email or not message or "@" not in email:
        raise HTTPException(status_code=400, detail="Nom, e-mail et message sont obligatoires.")
    ip = client_ip(request)
    verifier_limite_contact(ip)
    noter_contact(ip)
    ticket = SupportTicket(
        nom=nom,
        email=email,
        sujet="Contact vitrine",
        message=message,
    )
    db.add(ticket)
    db.commit()
    background_tasks.add_task(send_contact_email, nom, email, message)
    return {"status": "success", "message": "Message envoye."}


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
def take_ticket(data: PatientTicketCreate, request: Request, db: Session = Depends(get_db)):
    ip = client_ip(request)
    verifier_limite_prise(ip)
    if not (data.nom_patient or "").strip():
        raise HTTPException(status_code=400, detail="Le nom est obligatoire.")
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
    noter_prise(ip)

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
    payload = ticket_public_dict(ticket)
    payload["waiting_before_you"] = waiting_before_you
    return {"ticket": payload, "waiting_before_you": waiting_before_you}


# G. HISTORIQUE / EVENEMENTS D'UN TICKET
@app.get("/api/tickets/{ticket_id}/events")
def ticket_events(
    ticket_id: int,
    request: Request,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    ticket = get_ticket_or_404(db, ticket_id)
    token = extraire_bearer(authorization)
    if token:
        cab = cabinet_depuis_jeton(db, token)
        if not cab or cab.slug != ticket.cabinet_slug:
            raise HTTPException(status_code=404, detail="Ticket introuvable.")
    else:
        exiger_suivi_si_present(ticket, request)
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
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    ticket = get_ticket_or_404(db, ticket_id)
    authentifier_pour_ticket(db, ticket, request, authorization)
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
    authentifier_pour_ticket(db, ticket, request, authorization, secret_depuis_body(body))
    ticket.statut = "urgent"
    db.commit()
    log_event(db, ticket.id, "urgent_approved", body.urgent_reason)
    return {"status": "success", "ticket": ticket_to_dict(ticket)}


# ============================================================
# 7. ENDPOINTS CABINET (app desktop)
# ============================================================

# J. CONNEXION CABINET (mot de passe, route historique verify-pin)
@app.post("/api/cabinets/{slug}/verify-pin")
def verify_pin(slug: str, body: PinCheck, request: Request, db: Session = Depends(get_db)):
    ip = client_ip(request)
    verifier_limite_pin(slug, ip)
    cabinet = get_cabinet_or_404(db, slug)
    secret = secret_depuis_body(body) or ""
    if not pin_ok(secret, cabinet.code_pin):
        noter_echec_pin(slug, ip)
        raise HTTPException(status_code=403, detail="Mot de passe incorrect.")
    reset_echec_pin(slug, ip)
    rehash_si_clair(db, cabinet, secret)
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
    if not (data.nom_patient or "").strip():
        raise HTTPException(status_code=400, detail="Le nom est obligatoire.")
    cabinet = authentifier_cabinet(
        db, data.cabinet_slug, request, authorization, secret_depuis_body(data)
    )
    exiger_abonnement(cabinet)
    if cabinet.day_closed:
        raise HTTPException(status_code=400, detail="La journee est cloturee.")
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
    log_event(db, new_ticket.id, "created_manual")
    return {
        "status": "success",
        "ticket": ticket_echo_dict(new_ticket, data.nom_patient, data.telephone),
        "suivi_token": brut,
    }


# L. HISTORIQUE COMPLET DES TICKETS D'UN CABINET
@app.get("/api/cabinets/{slug}/tickets")
def cabinet_tickets(
    slug: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    cabinet = authentifier_cabinet(db, slug, request, authorization)
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
    authentifier_pour_ticket(db, ticket, request, authorization, secret_depuis_body(body))
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
        authentifier_pour_ticket(db, ticket, request, authorization, secret_depuis_body(body))
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
    cabinet = authentifier_cabinet(db, slug, request, authorization, secret_depuis_body(body))
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
    body: CloseDayBody = CloseDayBody(),
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    cabinet = authentifier_cabinet(db, slug, request, authorization)

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
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    cabinet = authentifier_cabinet(db, slug, request, authorization)

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
        "display_serving": f"N° P-{cabinet.serving_num:02d}",
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
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db)
):
    cabinet = authentifier_cabinet(db, slug, request, authorization)
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
        "display_serving": f"N° P-{next_ticket.ticket_num:02d}"
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
    slug = cabinet.slug
    tickets = db.query(PatientTicket).filter(PatientTicket.cabinet_slug == slug).all()
    ids = [t.id for t in tickets]
    if ids:
        db.query(TicketEvent).filter(TicketEvent.ticket_id.in_(ids)).delete(synchronize_session=False)
    db.query(PatientTicket).filter(PatientTicket.cabinet_slug == slug).delete(synchronize_session=False)
    db.query(CabinetSession).filter(CabinetSession.cabinet_slug == slug).delete(synchronize_session=False)
    db.delete(cabinet)
    db.commit()
    return {"status": "success", "message": "Cabinet supprime."}


@app.get("/api/admin/stats")
def admin_stats(db: Session = Depends(get_db), auth: bool = Depends(check_admin)):
    total_cabinets = db.query(Cabinet).count()
    actifs = db.query(Cabinet).filter(Cabinet.is_active == True).count()
    en_essai = db.query(Cabinet).filter(Cabinet.subscription_type.in_(["trial_14d", "trial_45d"])).count()
    payants = db.query(Cabinet).filter(Cabinet.subscription_type.in_(["monthly", "annual"])).count()
    total_tickets = db.query(PatientTicket).count()

    mensuels = db.query(Cabinet).filter(Cabinet.subscription_type == "monthly").count()
    annuels = db.query(Cabinet).filter(Cabinet.subscription_type == "annual").count()
    ca_estime_mensuel = mensuels * PRIX_MENSUEL_DA + (annuels * PRIX_ANNUEL_DA) / 12

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
    if not ADMIN_TOKEN or not secrets_egaux(data.password, ADMIN_TOKEN):
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
    prix = {"monthly": PRIX_MENSUEL_DA, "annual": PRIX_ANNUEL_DA}
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
