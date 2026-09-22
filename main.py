import os
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# 1. Connexion a PostgreSQL via la variable d'environnement Render
DATABASE_URL = os.getenv("DATABASE_URL")

# Si on est en local ou si DATABASE_URL commence par postgres://, corriger pour SQLAlchemy
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL or "sqlite:///./fallback.db")
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "tafwita-admin-2026")  # a changer en variable d'env Render

# 2. Modeles de la Base de Donnees
class Cabinet(Base):
    __tablename__ = "cabinets"
    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String, unique=True, index=True)  # ex: "dr-benali"
    nom_medecin = Column(String)
    specialite = Column(String)
    telephone = Column(String, unique=True)
    wilaya = Column(String)
    code_pin = Column(String)  # Pour securiser la touche F1 du medecin
    is_active = Column(Boolean, default=True)
    subscription_type = Column(String, default="trial_14d")  # trial, monthly, annual
    subscription_end = Column(DateTime)
    serving_num = Column(Integer, default=0)
    total_issued = Column(Integer, default=0)


class PatientTicket(Base):
    __tablename__ = "patient_tickets"
    id = Column(Integer, primary_key=True, index=True)
    cabinet_slug = Column(String, index=True)
    ticket_num = Column(Integer)
    nom_patient = Column(String)
    telephone = Column(String, nullable=True)  # Optionnel pour SMS/Alertes
    statut = Column(String, default="waiting")  # waiting, serving, completed, absent
    created_at = Column(DateTime, default=datetime.utcnow)


# Creer automatiquement les tables dans PostgreSQL
Base.metadata.create_all(bind=engine)

# 3. Application FastAPI
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


# Schemas de validation
class CabinetRegister(BaseModel):
    nom_medecin: str
    specialite: str
    telephone: str
    wilaya: str
    code_pin: str


class PatientTicketCreate(BaseModel):
    cabinet_slug: str
    nom_patient: str
    telephone: Optional[str] = None


class SubscriptionUpdate(BaseModel):
    subscription_type: str  # "trial_14d", "monthly", "annual"
    extend_days: Optional[int] = None


class AdminLogin(BaseModel):
    password: str


# --- ENDPOINTS API ---

@app.get("/")
def home():
    return {"message": "API TAFWITA & Base PostgreSQL connectees avec succes !"}


# A. INSCRIPTION D'UN NOUVEAU CABINET MEDICAL (Client B2B)
@app.post("/api/cabinets/register")
def register_cabinet(data: CabinetRegister, db: Session = Depends(get_db)):
    slug = data.nom_medecin.lower().replace(" ", "-").replace(".", "")

    existing = db.query(Cabinet).filter(Cabinet.telephone == data.telephone).first()
    if existing:
        raise HTTPException(status_code=400, detail="Ce numero de telephone est deja enregistre.")

    trial_end = datetime.utcnow() + timedelta(days=14)

    cabinet = Cabinet(
        slug=slug,
        nom_medecin=data.nom_medecin,
        specialite=data.specialite,
        telephone=data.telephone,
        wilaya=data.wilaya,
        code_pin=data.code_pin,
        subscription_end=trial_end,
    )

    db.add(cabinet)
    db.commit()
    db.refresh(cabinet)

    return {
        "status": "success",
        "message": "Cabinet cree avec succes ! 14 jours d'essai actives.",
        "cabinet_slug": cabinet.slug,
        "tv_url": f"/tv?cabinet={cabinet.slug}",
        "trial_end": trial_end.strftime("%Y-%m-%d"),
    }


# B. INSCRIPTION D'UN PATIENT (Prise de ticket Android ou Web)
@app.post("/api/tickets/take")
def take_ticket(data: PatientTicketCreate, db: Session = Depends(get_db)):
    cabinet = db.query(Cabinet).filter(Cabinet.slug == data.cabinet_slug).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet introuvable.")

    cabinet.total_issued += 1
    new_ticket = PatientTicket(
        cabinet_slug=cabinet.slug,
        ticket_num=cabinet.total_issued,
        nom_patient=data.nom_patient,
        telephone=data.telephone,
    )

    db.add(new_ticket)
    db.commit()

    waiting = max(0, cabinet.total_issued - cabinet.serving_num)

    return {
        "status": "success",
        "ticket_num": cabinet.total_issued,
        "display_ticket": f"N deg P-{cabinet.total_issued:02d}",
        "waiting_before_you": max(0, waiting - 1),
        "estimated_wait_min": max(0, waiting - 1) * 12,
    }


# C. ETAT EN DIRECT (Ecran TV & Application Patient)
@app.get("/api/queue/{cabinet_slug}")
def get_queue_state(cabinet_slug: str, db: Session = Depends(get_db)):
    cabinet = db.query(Cabinet).filter(Cabinet.slug == cabinet_slug).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet introuvable.")

    waiting = max(0, cabinet.total_issued - cabinet.serving_num)

    return {
        "cabinet_name": cabinet.nom_medecin,
        "specialite": cabinet.specialite,
        "serving_num": cabinet.serving_num,
        "display_serving": f"N deg P-{cabinet.serving_num:02d}",
        "total_issued": cabinet.total_issued,
        "waiting_count": waiting,
        "is_active": cabinet.is_active,
    }


# D. APPELER LE SUIVANT (Touche F1 du Medecin securisee par son code PIN)
@app.post("/api/queue/{cabinet_slug}/next")
def call_next(cabinet_slug: str, pin: str, db: Session = Depends(get_db)):
    cabinet = db.query(Cabinet).filter(Cabinet.slug == cabinet_slug).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet introuvable.")
    if cabinet.code_pin != pin:
        raise HTTPException(status_code=403, detail="Code PIN medecin incorrect.")

    if cabinet.serving_num < cabinet.total_issued:
        cabinet.serving_num += 1
        db.commit()

    return {
        "status": "success",
        "serving_num": cabinet.serving_num,
        "display_serving": f"N deg P-{cabinet.serving_num:02d}",
    }


# ============================================================
# ============  ENDPOINTS DU DASHBOARD ADMIN  ================
# ============================================================

# E. LISTE DE TOUS LES CABINETS (PRATICIENS)
@app.get("/api/admin/cabinets")
def admin_list_cabinets(db: Session = Depends(get_db), auth: bool = Depends(check_admin)):
    cabinets = db.query(Cabinet).order_by(Cabinet.id.desc()).all()
    result = []
    for c in cabinets:
        jours_restants = None
        if c.subscription_end:
            jours_restants = (c.subscription_end - datetime.utcnow()).days
        result.append({
            "id": c.id,
            "slug": c.slug,
            "nom_medecin": c.nom_medecin,
            "specialite": c.specialite,
            "telephone": c.telephone,
            "wilaya": c.wilaya,
            "is_active": c.is_active,
            "subscription_type": c.subscription_type,
            "subscription_end": c.subscription_end.strftime("%Y-%m-%d") if c.subscription_end else None,
            "jours_restants": jours_restants,
            "total_issued": c.total_issued,
        })
    return result


# F. ACTIVER / DESACTIVER UN CABINET
@app.post("/api/admin/cabinets/{cabinet_id}/toggle")
def admin_toggle_cabinet(cabinet_id: int, db: Session = Depends(get_db), auth: bool = Depends(check_admin)):
    cabinet = db.query(Cabinet).filter(Cabinet.id == cabinet_id).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet introuvable.")
    cabinet.is_active = not cabinet.is_active
    db.commit()
    return {"status": "success", "is_active": cabinet.is_active}


# G. CHANGER LE TYPE D'ABONNEMENT MANUELLEMENT
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
        "status": "success",
        "subscription_type": cabinet.subscription_type,
        "subscription_end": cabinet.subscription_end.strftime("%Y-%m-%d") if cabinet.subscription_end else None,
    }


# H. SUPPRIMER UN CABINET
@app.delete("/api/admin/cabinets/{cabinet_id}")
def admin_delete_cabinet(cabinet_id: int, db: Session = Depends(get_db), auth: bool = Depends(check_admin)):
    cabinet = db.query(Cabinet).filter(Cabinet.id == cabinet_id).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet introuvable.")
    db.delete(cabinet)
    db.commit()
    return {"status": "success", "message": "Cabinet supprime."}


# I. STATISTIQUES GLOBALES (Dashboard)
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
        "total_cabinets": total_cabinets,
        "cabinets_actifs": actifs,
        "cabinets_en_essai": en_essai,
        "cabinets_payants": payants,
        "total_tickets_emis": total_tickets,
        "ca_estime_mensuel_da": round(ca_estime_mensuel, 2),
        "repartition_wilaya": par_wilaya,
    }


# J. LOGIN ADMIN SIMPLE (verifie le mot de passe et renvoie le token)
@app.post("/api/admin/login")
def admin_login(data: AdminLogin):
    if data.password != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Mot de passe incorrect.")
    return {"status": "success", "token": ADMIN_TOKEN}
