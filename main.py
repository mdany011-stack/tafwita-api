from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime

app = FastAPI(title="TAFWITA API", description="API de file d'attente médicale")

# Autoriser votre application Web et mobile à communiquer avec l'API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# État de la file d'attente en mémoire
queue_state = {
    "serving_num": 14,
    "total_issued": 18,
    "cabinet_name": "Cabinet Médical Dr. Benali",
    "status": "consulting",
    "updated_at": datetime.now().strftime("%H:%M:%S")
}

@app.get("/")
def home():
    return {"message": "API TAFWITA en ligne 🇩🇿", "docs": "/docs"}

# 1. Consulter l'état actuel (TV et Patients)
@app.get("/api/state")
def get_state():
    waiting = max(0, queue_state["total_issued"] - queue_state["serving_num"])
    return {
        **queue_state,
        "waiting_count": waiting,
        "display_text": f"N° P-{queue_state['serving_num']:02d}",
        "estimated_wait_min": waiting * 10
    }

# 2. Appeler le patient suivant (Touche F1 du Médecin)
@app.post("/api/next")
def call_next():
    queue_state["serving_num"] += 1
    if queue_state["serving_num"] > queue_state["total_issued"]:
        queue_state["total_issued"] = queue_state["serving_num"]
    queue_state["updated_at"] = datetime.now().strftime("%H:%M:%S")
    return {"status": "success", "serving_num": queue_state["serving_num"]}

# 3. Distribuer un nouveau ticket (Patient ou Secrétaire)
@app.post("/api/ticket")
def take_ticket():
    queue_state["total_issued"] += 1
    queue_state["updated_at"] = datetime.now().strftime("%H:%M:%S")
    return {"status": "success", "ticket_num": queue_state["total_issued"]}

# 4. Réinitialiser la journée
@app.post("/api/reset")
def reset_queue():
    queue_state["serving_num"] = 0
    queue_state["total_issued"] = 0
    return {"status": "reset_done"}
    # 1. Inscription d'un nouveau cabinet médical
@app.post("/api/cabinets/register")
def register_cabinet(nom: str, specialite: str, telephone: str, wilaya: str):
    # Créer le cabinet avec 14 jours d'essai gratuit
    cabinet_id = f"cab_{telephone[-4:]}"
    return {
        "status": "success",
        "cabinet_id": cabinet_id,
        "pin_code": "1234",
        "message": "Cabinet créé avec succès ! Période d'essai activée."
    }

# 2. Inscription / Prise de ticket d'un patient
@app.post("/api/patients/ticket")
def register_patient_ticket(cabinet_id: str, nom_patient: str, telephone: str):
    # Attribuer le prochain numéro disponible pour ce cabinet
    return {
        "status": "success",
        "ticket_num": 16,
        "cabinet_id": cabinet_id,
        "patient": nom_patient
    }
