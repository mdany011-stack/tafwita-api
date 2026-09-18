"""TAFWITA API v7 - main.py
Mise à jour avec :
1. Clôıııture de journée (annulation tickets + notification patients)
2. Arrêııîııt / Reprise de la prise de tickets (accept_tickets)

Installation :
pip install fastapi uvicorn sqlalchemy pydantic python-dotenv

Variables d'environnement (.env) :
DATABASE_URL=postgresql://user:password@host:port/dbname
"""

import os
from datetime import datetime, date
from typing import Optional, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, func
from sqlalchemy.orm import sessionmaker, Session, declarative_base, relationship

# ============================================================================
# CONFIGURATION
# ============================================================================

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost/tafwita")
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ============================================================================
# MODÈıîııLES DE DONNÉıîııES
# ============================================================================

class Cabinet(Base):
    __tablename__ = "cabinets"

    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String, unique=True, index=True, nullable=False)
    nom = Column(String, nullable=False)
    code_pin = Column(String, nullable=False)
    adresse = Column(String, nullable=True)
    heure_ouverture = Column(String, nullable=True)
    heure_fermeture = Column(String, nullable=True)
    photo_url = Column(String, nullable=True)
    accept_tickets = Column(Boolean, default=True)  # NOUVEAU CHAMP v7

    tickets = relationship("Ticket", back_populates="cabinet")


class Ticket(Base):
    __tablename__ = "tickets"

    id = Column(Integer, primary_key=True, index=True)
    cabinet_slug = Column(String, ForeignKey("cabinets.slug"), nullable=False)
    user_id = Column(Integer, nullable=True)  # NULL si ticket papier
    nom_patient = Column(String, nullable=False)
    telephone = Column(String, nullable=True)
    ticket_num = Column(Integer, nullable=False)
    statut = Column(String, default="waiting")  # waiting, serving, suspended, returned, completed, cancelled
    duree_min = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    cabinet = relationship("Cabinet", back_populates="tickets")
    events = relationship("TicketEvent", back_populates="ticket")


class TicketEvent(Base):
    __tablename__ = "ticket_events"

    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), nullable=False)
    event_type = Column(String, nullable=False)  # created, called, suspended, returned, completed, cancelled, day_closed, etc.
    reason_code = Column(String, nullable=True)
    reason_note = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    ticket = relationship("Ticket", back_populates="events")


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False)
    title = Column(String, nullable=False)
    message = Column(String, nullable=False)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


# ============================================================================
# SCHÉıîııMAS PYDANTIC
# ============================================================================

class CabinetSettingsUpdate(BaseModel):
    adresse: Optional[str] = None
    heure_ouverture: Optional[str] = None
    heure_fermeture: Optional[str] = None
    accept_tickets: Optional[bool] = None
    photo_url: Optional[str] = None


class CloseDayRequest(BaseModel):
    reason: str = "cabinet_closed"
    notify_patients: bool = True


class VerifyPinRequest(BaseModel):
    code_pin: str


class AddManualTicketRequest(BaseModel):
    cabinet_slug: str
    nom_patient: str
    telephone: Optional[str] = None


# ============================================================================
# APPLICATION FASTAPI
# ============================================================================

app = FastAPI(title="TAFWITA API", version="7.0.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================================
# DÉí»ıíııPENDANCES
# ============================================================================

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ============================================================================
# ENDPOINTS - CABINETS
# ============================================================================

@app.post("/api/cabinets/{slug}/verify-pin")
async def verify_cabinet_pin(
    slug: str,
    request: VerifyPinRequest,
    db: Session = Depends(get_db)
):
    """Véı»iifie le PIN du cabinet"""
    cabinet = db.query(Cabinet).filter(Cabinet.slug == slug).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet non trouvé")

    if cabinet.code_pin != request.code_pin:
        raise HTTPException(status_code=403, detail="PIN incorrect")

    return {
        "cabinet_slug": cabinet.slug,
        "cabinet_nom": cabinet.nom,
        "code_pin": cabinet.code_pin
    }


@app.put("/api/cabinets/{slug}/settings")
async def update_cabinet_settings(
    slug: str,
    pin: str = Query(...),
    payload: CabinetSettingsUpdate = Body(...),
    db: Session = Depends(get_db)
):
    """Met à jour les paramètres du cabinet (adresse, horaires, accept_tickets)"""
    cabinet = db.query(Cabinet).filter(Cabinet.slug == slug).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet non trouvé")

    # Véí»ıifie le PIN
    if cabinet.code_pin != pin:
        raise HTTPException(status_code=403, detail="PIN incorrect")

    # Met à jour les champs optionnels
    if payload.adresse is not None:
        cabinet.adresse = payload.adresse
    if payload.heure_ouverture is not None:
        cabinet.heure_ouverture = payload.heure_ouverture
    if payload.heure_fermeture is not None:
        cabinet.heure_fermeture = payload.heure_fermeture
    if payload.accept_tickets is not None:
        cabinet.accept_tickets = payload.accept_tickets
    if payload.photo_url is not None:
        cabinet.photo_url = payload.photo_url

    db.commit()
    db.refresh(cabinet)

    return {
        "message": "Paramèııtres mis à jour",
        "cabinet": {
            "slug": cabinet.slug,
            "nom": cabinet.nom,
            "adresse": cabinet.adresse,
            "heure_ouverture": cabinet.heure_ouverture,
            "heure_fermeture": cabinet.heure_fermeture,
            "accept_tickets": cabinet.accept_tickets
        }
    }


@app.get("/api/cabinets/{slug}/settings")
async def get_cabinet_settings(
    slug: str,
    pin: str = Query(...),
    db: Session = Depends(get_db)
):
    """Rôıııcupèııre les paramètres du cabinet"""
    cabinet = db.query(Cabinet).filter(Cabinet.slug == slug).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet non trouvé")

    if cabinet.code_pin != pin:
        raise HTTPException(status_code=403, detail="PIN incorrect")

    return {
        "slug": cabinet.slug,
        "nom": cabinet.nom,
        "adresse": cabinet.adresse,
        "heure_ouverture": cabinet.heure_ouverture,
        "heure_fermeture": cabinet.heure_fermeture,
        "accept_tickets": cabinet.accept_tickets,
        "photo_url": cabinet.photo_url
    }


@app.post("/api/cabinets/{slug}/close-day")
async def close_cabinet_day(
    slug: str,
    pin: str = Query(...),
    payload: CloseDayRequest = Body(...),
    db: Session = Depends(get_db)
):
    """
    Clôıııture la journôıııe - annule tous les tickets en attente et notifie les patients
    """
    cabinet = db.query(Cabinet).filter(Cabinet.slug == slug).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet non trouvé")

    # Véí»ıifie le PIN
    if cabinet.code_pin != pin:
        raise HTTPException(status_code=403, detail="PIN incorrect")

    # Rôıııcupèııre les tickets du jour en attente
    today = datetime.now().date()
    pending_tickets = db.query(Ticket).filter(
        Ticket.cabinet_slug == slug,
        Ticket.statut.in_(["waiting", "suspended", "returned", "serving"]),
        func.date(Ticket.created_at) == today
    ).all()

    closed_count = 0
    notified_count = 0

    for ticket in pending_tickets:
        # Annuler le ticket
        ticket.statut = "cancelled"

        # Créíııer un ôíııvéııinement
        event = TicketEvent(
            ticket_id=ticket.id,
            event_type="day_closed",
            reason_code=payload.reason,
            reason_note="Cabinet fermé pour aujourd'hui"
        )
        db.add(event)

        closed_count += 1

        # Notifier le patient si demandé
        if payload.notify_patients and ticket.user_id:
            try:
                notification = Notification(
                    user_id=ticket.user_id,
                    title="Ticket annuléıı",
                    message=f"Votre ticket P-{str(ticket.ticket_num).zfill(2)} a été annuléıı car le cabinet a fermé pour aujourd'hui."
                )
                db.add(notification)
                notified_count += 1
            except Exception as e:
                # Logger l'erreur mais continuer
                print(f"Erreur notification ticket {ticket.id}: {e}")

    db.commit()

    return {
        "message": "Journôıııe clô1turôıııe avec succèııs",
        "closed_count": closed_count,
        "notified_count": notified_count
    }


# ============================================================================
# ENDPOINTS - QUEUE
# ============================================================================

@app.get("/api/queue/{slug}")
async def get_queue(
    slug: str,
    db: Session = Depends(get_db)
):
    """
    Rôıııcupèııre la file d'attente du cabinet
    Inclut accept_tickets pour l'app desktop v7
    """
    cabinet = db.query(Cabinet).filter(Cabinet.slug == slug).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet non trouvé")

    # Rôıııcupèııre les tickets du jour
    today = datetime.now().date()
    tickets = db.query(Ticket).filter(
        Ticket.cabinet_slug == slug,
        func.date(Ticket.created_at) == today
    ).all()

    # Calculer les statistiques
    serving = next((t for t in tickets if t.statut == "serving"), None)
    waiting_count = len([t for t in tickets if t.statut in ["waiting", "returned"]])
    total_issued = len(tickets)

    return {
        "cabinet_slug": slug,
        "cabinet_nom": cabinet.nom,
        "display_serving": f"P-{str(serving.ticket_num).zfill(2)}" if serving else "-",
        "waiting_count": waiting_count,
        "total_issued": total_issued,
        "accept_tickets": cabinet.accept_tickets,  # NOUVEAU CHAMP v7
        "adresse": cabinet.adresse,
        "heure_ouverture": cabinet.heure_ouverture,
        "heure_fermeture": cabinet.heure_fermeture
    }


@app.post("/api/queue/{slug}/next")
async def call_next_patient(
    slug: str,
    pin: str = Query(...),
    db: Session = Depends(get_db)
):
    """Appelle le patient suivant dans la file"""
    cabinet = db.query(Cabinet).filter(Cabinet.slug == slug).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet non trouvé")

    if cabinet.code_pin != pin:
        raise HTTPException(status_code=403, detail="PIN incorrect")

    # Trouver le premier ticket en attente
    next_ticket = db.query(Ticket).filter(
        Ticket.cabinet_slug == slug,
        Ticket.statut.in_(["waiting", "returned"])
    ).order_by(Ticket.ticket_num).first()

    if not next_ticket:
        return {
            "message": "Aucun patient en attente",
            "ticket": None
        }

    # Mettre à jour le statut
    next_ticket.statut = "serving"

    # Créíııer un ôíııvéııinement
    event = TicketEvent(
        ticket_id=next_ticket.id,
        event_type="called",
        reason_note="Appeléıı par le cabinet"
    )
    db.add(event)
    db.commit()

    return {
        "message": f"Patient P-{str(next_ticket.ticket_num).zfill(2)} appelé",
        "ticket": {
            "id": next_ticket.id,
            "ticket_num": next_ticket.ticket_num,
            "nom_patient": next_ticket.nom_patient
        }
    }


# ============================================================================
# ENDPOINTS - TICKETS
# ============================================================================

@app.get("/api/cabinets/{slug}/tickets")
async def get_cabinet_tickets(
    slug: str,
    db: Session = Depends(get_db)
):
    """Rôıııcupèııre tous les tickets d'un cabinet"""
    cabinet = db.query(Cabinet).filter(Cabinet.slug == slug).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet non trouvé")

    tickets = db.query(Ticket).filter(Ticket.cabinet_slug == slug).all()

    return [
        {
            "id": t.id,
            "cabinet_slug": t.cabinet_slug,
            "user_id": t.user_id,
            "nom_patient": t.nom_patient,
            "telephone": t.telephone,
            "ticket_num": t.ticket_num,
            "statut": t.statut,
            "duree_min": t.duree_min,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None
        }
        for t in tickets
    ]


@app.get("/api/cabinets/{slug}/alerts")
async def get_cabinet_alerts(
    slug: str,
    pin: str = Query(...),
    db: Session = Depends(get_db)
):
    """Rôıııcupèııre les alertes du cabinet (prôııısence confirmôıııe, urgences)"""
    cabinet = db.query(Cabinet).filter(Cabinet.slug == slug).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet non trouvé")

    if cabinet.code_pin != pin:
        raise HTTPException(status_code=403, detail="PIN incorrect")

    # Rôıııcupôııırer les tickets du jour
    today = datetime.now().date()
    tickets = db.query(Ticket).filter(
        Ticket.cabinet_slug == slug,
        func.date(Ticket.created_at) == today
    ).all()

    presence_confirmed = [t for t in tickets if t.statut == "waiting"]
    urgent_requests = [t for t in tickets if t.statut == "urgent"]

    return {
        "presence_confirmed": len(presence_confirmed),
        "urgent_requests": len(urgent_requests)
    }


@app.post("/api/tickets/add-manual")
async def add_manual_ticket(
    request: AddManualTicketRequest,
    db: Session = Depends(get_db)
):
    """
    Ajoute un ticket papier (manuel) pour un cabinet
    BLOQUÉıîıı si accept_tickets == False
    """
    cabinet = db.query(Cabinet).filter(Cabinet.slug == request.cabinet_slug).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet non trouvé")

    # VÉıîııRIFICATION v7 : Le cabinet accepte-t-il les tickets ?
    if not cabinet.accept_tickets:
        raise HTTPException(
            status_code=400,
            detail="La prise de tickets est actuellement arrêııîııtôıııe pour ce cabinet."
        )

    # Trouver le prochain numôıııro de ticket
    today = datetime.now().date()
    last_ticket = db.query(Ticket).filter(
        Ticket.cabinet_slug == request.cabinet_slug,
        func.date(Ticket.created_at) == today
    ).order_by(Ticket.ticket_num.desc()).first()

    next_num = (last_ticket.ticket_num + 1) if last_ticket else 1

    # Créíııer le ticket
    ticket = Ticket(
        cabinet_slug=request.cabinet_slug,
        user_id=None,  # Ticket papier
        nom_patient=request.nom_patient,
        telephone=request.telephone,
        ticket_num=next_num,
        statut="waiting"
    )
    db.add(ticket)

    # Créíııer l'ôıııvéııinement
    event = TicketEvent(
        ticket_id=ticket.id,
        event_type="created",
        reason_code="manual_add",
        reason_note="Ticket papier ajoutôııı manuellement"
    )
    db.add(event)
    db.commit()
    db.refresh(ticket)

    return {
        "message": "Ticket papier ajoutôııı avec succèııs",
        "ticket": {
            "id": ticket.id,
            "ticket_num": ticket.ticket_num,
            "nom_patient": ticket.nom_patient
        }
    }


@app.post("/api/tickets/{ticket_id}/{action}")
async def ticket_action(
    ticket_id: int,
    action: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db)
):
    """
    Actions sur un ticket : suspend, return-to-queue, cabinet-cancel, approve-urgent
    """
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket non trouvé")

    cabinet = db.query(Cabinet).filter(Cabinet.slug == ticket.cabinet_slug).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet non trouvé")

    # Véí»ıifie le PIN
    if cabinet.code_pin != payload.get("code_pin"):
        raise HTTPException(status_code=403, detail="PIN incorrect")

    reason_code = payload.get("reason_code", "unknown")
    reason_note = payload.get("reason_note", "")

    if action == "suspend":
        ticket.statut = "suspended"
        event = TicketEvent(
            ticket_id=ticket.id,
            event_type="suspended",
            reason_code=reason_code,
            reason_note=reason_note
        )
        db.add(event)
        db.commit()
        return {"message": "Ticket suspendu"}

    elif action == "return-to-queue":
        ticket.statut = "returned"
        event = TicketEvent(
            ticket_id=ticket.id,
            event_type="returned",
            reason_code=reason_code,
            reason_note=reason_note
        )
        db.add(event)
        db.commit()
        return {"message": "Ticket remis dans la file"}

    elif action == "cabinet-cancel":
        ticket.statut = "cancelled"
        event = TicketEvent(
            ticket_id=ticket.id,
            event_type="cancelled",
            reason_code=reason_code,
            reason_note=reason_note
        )
        db.add(event)
        db.commit()
        return {"message": "Ticket annuléıı"}

    elif action == "approve-urgent":
        ticket.statut = "urgent"
        urgent_reason = payload.get("urgent_reason", "")
        event = TicketEvent(
            ticket_id=ticket.id,
            event_type="urgent_approved",
            reason_code="urgent",
            reason_note=urgent_reason
        )
        db.add(event)
        db.commit()
        return {"message": "Urgence approuvôıııe"}

    else:
        raise HTTPException(status_code=400, detail=f"Action {action} non reconnue")


@app.get("/api/tickets/{ticket_id}/events")
async def get_ticket_events(
    ticket_id: int,
    db: Session = Depends(get_db)
):
    """Rôıııcupèııre l'historique des ôíııvéııịnements d'un ticket"""
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket non trouvé")

    events = db.query(TicketEvent).filter(TicketEvent.ticket_id == ticket_id).all()

    return [
        {
            "id": e.id,
            "ticket_id": e.ticket_id,
            "event_type": e.event_type,
            "reason_code": e.reason_code,
            "reason_note": e.reason_note,
            "created_at": e.created_at.isoformat() if e.created_at else None
        }
        for e in events
    ]


# ============================================================================
# ENDPOINTS - UTILISATEURS (APP PATIENT)
# ============================================================================

@app.post("/api/tickets")
async def create_ticket(
    payload: dict,
    db: Session = Depends(get_db)
):
    """
    Créíııe un ticket pour un patient (app patient)
    BLOQUÉıîıı si accept_tickets == False
    """
    cabinet_slug = payload.get("cabinet_slug")
    user_id = payload.get("user_id")

    cabinet = db.query(Cabinet).filter(Cabinet.slug == cabinet_slug).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet non trouvé")

    # VÉıîııRIFICATION v7 : Le cabinet accepte-t-il les tickets ?
    if not cabinet.accept_tickets:
        raise HTTPException(
            status_code=400,
            detail="La prise de tickets est actuellement arrêııîııtôıııe pour ce cabinet. Veuillez rô1essayer plus tard."
        )

    # Trouver le prochain numôıııro de ticket
    today = datetime.now().date()
    last_ticket = db.query(Ticket).filter(
        Ticket.cabinet_slug == cabinet_slug,
        func.date(Ticket.created_at) == today
    ).order_by(Ticket.ticket_num.desc()).first()

    next_num = (last_ticket.ticket_num + 1) if last_ticket else 1

    # Créíııer le ticket
    ticket = Ticket(
        cabinet_slug=cabinet_slug,
        user_id=user_id,
        nom_patient=payload.get("nom_patient", "Patient"),
        telephone=payload.get("telephone"),
        ticket_num=next_num,
        statut="waiting"
    )
    db.add(ticket)

    # Créíııer l'ôıııvéııinement
    event = TicketEvent(
        ticket_id=ticket.id,
        event_type="created",
        reason_code="patient_app",
        reason_note="Ticket cré1ôııı depuis l'application patient"
    )
    db.add(event)
    db.commit()
    db.refresh(ticket)

    return {
        "message": "Ticket cré1ôııı avec succèııs",
        "ticket": {
            "id": ticket.id,
            "ticket_num": ticket.ticket_num,
            "nom_patient": ticket.nom_patient
        }
    }


@app.get("/api/users/{user_id}/notifications")
async def get_user_notifications(
    user_id: int,
    db: Session = Depends(get_db)
):
    """Rôıııcupèııre les notifications d'un utilisateur"""
    notifications = db.query(Notification).filter(
        Notification.user_id == user_id
    ).order_by(Notification.created_at.desc()).all()

    return [
        {
            "id": n.id,
            "user_id": n.user_id,
            "title": n.title,
            "message": n.message,
            "is_read": n.is_read,
            "created_at": n.created_at.isoformat() if n.created_at else None
        }
        for n in notifications
    ]


# ============================================================================
# POINT D'ENTRÉıîııE
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
