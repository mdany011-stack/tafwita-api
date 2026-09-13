import os
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# 1. Connexion à PostgreSQL via la variable d'environnement Render
DATABASE_URL = os.getenv("DATABASE_URL")

# Si on est en local ou si DATABASE_URL commence par postgres://, corriger pour SQLAlchemy
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL or "sqlite:///./fallback.db")
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# 2. Modèles de la Base de Données
class Cabinet(Base):
    __tablename__ = "cabinets"
    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String, unique=True, index=True) # ex: "dr-benali"
    nom_medecin = Column(String)
    specialite = Column(String)
    telephone = Column(String, unique=True)
    wilaya = Column(String)
    code_pin = Column(String) # Pour sécuriser la touche F1 du médecin
    is_active = Column(Boolean, default=True)
    subscription_type = Column(String, default="trial_14d") # trial, monthly, annual
    subscription_end = Column(DateTime)
    serving_num = Column(Integer, default=0)
    total_issued = Column(Integer, default=0)

class PatientTicket(Base):
    __tablename__ = "patient_tickets"
    id = Column(Integer, primary_key=True, index=True)
    cabinet_slug = Column(String, index=True)
    ticket_num = Column(Integer)
    nom_patient = Column(String)
    telephone = Column(String, nullable=True) # Optionnel pour SMS/Alertes
    statut = Column(String, default="waiting") # waiting, serving, completed, absent
    created_at = Column(DateTime, default=datetime.utcnow)

# Créer automatiquement les tables dans PostgreSQL
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

# Schémas de validation
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

# --- ENDPOINTS API ---

@app.get("/")
def home():
    return {"message": "API TAFWITA & Base PostgreSQL connectées avec succès ! 🇩🇿"}

# A. INSCRIPTION D'UN NOUVEAU CABINET MÉDICAL (Client B2B)
@app.post("/api/cabinets/register")
def register_cabinet(data: CabinetRegister, db: Session = Depends(get_db)):
    slug = data.nom_medecin.lower().replace(" ", "-").replace(".", "")
    
    # Vérifier si le téléphone existe déjà
    existing = db.query(Cabinet).filter(Cabinet.telephone == data.telephone).first()
    if existing:
        raise HTTPException(status_code=400, detail="Ce numéro de téléphone est déjà enregistré.")

    # 14 jours d'essai gratuit offerts
    trial_end = datetime.utcnow() + timedelta(days=14)

    cabinet = Cabinet(
        slug=slug,
        nom_medecin=data.nom_medecin,
        specialite=data.specialite,
        telephone=data.telephone,
        wilaya=data.wilaya,
        code_pin=data.code_pin,
        subscription_end=trial_end
    )
    db.add(cabinet)
    db.commit()
    db.refresh(cabinet)

    return {
        "status": "success",
        "message": "Cabinet créé avec succès ! 14 jours d'essai activés.",
        "cabinet_slug": cabinet.slug,
        "tv_url": f"/tv?cabinet={cabinet.slug}",
        "trial_end": trial_end.strftime("%Y-%m-%d")
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
        telephone=data.telephone
    )
    db.add(new_ticket)
    db.commit()

    waiting = max(0, cabinet.total_issued - cabinet.serving_num)

    return {
        "status": "success",
        "ticket_num": cabinet.total_issued,
        "display_ticket": f"N° P-{cabinet.total_issued:02d}",
        "waiting_before_you": max(0, waiting - 1),
        "estimated_wait_min": max(0, waiting - 1) * 12
    }

# C. ÉTAT EN DIRECT (Écran TV & Application Patient)
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
        "display_serving": f"N° P-{cabinet.serving_num:02d}",
        "total_issued": cabinet.total_issued,
        "waiting_count": waiting,
        "is_active": cabinet.is_active
    }

# D. APPELER LE SUIVANT (Touche F1 du Médecin sécurisée par son code PIN)
@app.post("/api/queue/{cabinet_slug}/next")
def call_next(cabinet_slug: str, pin: str, db: Session = Depends(get_db)):
    cabinet = db.query(Cabinet).filter(Cabinet.slug == cabinet_slug).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet introuvable.")
    if cabinet.code_pin != pin:
        raise HTTPException(status_code=403, detail="Code PIN médecin incorrect.")

    if cabinet.serving_num < cabinet.total_issued:
        cabinet.serving_num += 1
        db.commit()

    return {
        "status": "success",
        "serving_num": cabinet.serving_num,
        "display_serving": f"N° P-{cabinet.serving_num:02d}"
    }
