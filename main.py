"""TAFWITA API - compatible patient HTML et desktop horaires.
Deploy Render: uvicorn main:app --host 0.0.0.0 --port $PORT
"""
import os, re, unicodedata
from datetime import datetime, date, time, timedelta
from typing import Optional, Literal
from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, Text, text, or_
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship

URL=os.getenv("DATABASE_URL") or "sqlite:///./tafwita.db"
if URL.startswith("postgres://"): URL=URL.replace("postgres://","postgresql://",1)
engine=create_engine(URL,pool_pre_ping=True,connect_args={"check_same_thread":False} if URL.startswith("sqlite") else {})
SessionLocal=sessionmaker(bind=engine,autoflush=False,autocommit=False); Base=declarative_base()

class Cabinet(Base):
 __tablename__="cabinets"
 id=Column(Integer,primary_key=True); slug=Column(String,unique=True,index=True,nullable=False)
 nom_medecin=Column(String,nullable=False); specialite=Column(String,default="Medecine generale"); telephone=Column(String,unique=True,nullable=True); wilaya=Column(String,default="")
 code_pin=Column(String,nullable=False); is_active=Column(Boolean,default=True); accept_tickets=Column(Boolean,default=True); day_closed=Column(Boolean,default=False); day_closed_at=Column(DateTime)
 subscription_type=Column(String,default="trial_14d"); subscription_end=Column(DateTime); serving_num=Column(Integer,default=0); total_issued=Column(Integer,default=0)
 ticket_mode=Column(String,default="manual")
 ticket_start_time=Column(String,default="06:00"); ticket_end_time=Column(String,nullable=True); opening_time=Column(String,default="08:00"); closing_time=Column(String,nullable=True)
 max_tickets_per_day=Column(Integer,nullable=True)
 tickets=relationship("PatientTicket",back_populates="cabinet",cascade="all,delete-orphan")
class PatientTicket(Base):
 __tablename__="patient_tickets"
 id=Column(Integer,primary_key=True); cabinet_slug=Column(String,ForeignKey("cabinets.slug"),index=True,nullable=False); ticket_num=Column(Integer,nullable=False); nom_patient=Column(String,nullable=False); telephone=Column(String); user_id=Column(Integer)
 statut=Column(String,default="waiting"); created_at=Column(DateTime,default=datetime.utcnow); updated_at=Column(DateTime,default=datetime.utcnow,onupdate=datetime.utcnow); suspended_at=Column(DateTime); notification_count=Column(Integer,default=0)
 cabinet=relationship("Cabinet",back_populates="tickets"); events=relationship("TicketEvent",back_populates="ticket",cascade="all,delete-orphan")
class TicketEvent(Base):
 __tablename__="ticket_events"
 id=Column(Integer,primary_key=True); ticket_id=Column(Integer,ForeignKey("patient_tickets.id"),nullable=False); event_type=Column(String,nullable=False); reason_code=Column(String); reason_note=Column(Text); created_at=Column(DateTime,default=datetime.utcnow); ticket=relationship("PatientTicket",back_populates="events")
class Notification(Base):
 __tablename__="notifications"
 id=Column(Integer,primary_key=True); user_id=Column(Integer); ticket_id=Column(Integer); title=Column(String,nullable=False); message=Column(Text,nullable=False); is_read=Column(Boolean,default=False); created_at=Column(DateTime,default=datetime.utcnow)

class Register(BaseModel): nom_medecin:str; specialite:str; telephone:str; wilaya:str; code_pin:str
class Take(BaseModel): cabinet_slug:str; nom_patient:str; telephone:Optional[str]=None; user_id:Optional[int]=None
class Pin(BaseModel): code_pin:str
class TicketAction(BaseModel): code_pin:Optional[str]=None; reason_code:Optional[str]=None; reason_note:Optional[str]=None; urgent_reason:Optional[str]=None
class Settings(BaseModel):
 accept_tickets:Optional[bool]=None; ticket_mode:Optional[Literal["manual","fixed"]]=None; ticket_start_time:Optional[str]=None; ticket_end_time:Optional[str]=None; opening_time:Optional[str]=None; closing_time:Optional[str]=None; max_tickets_per_day:Optional[int]=Field(default=None,ge=1,le=1000)
class Close(BaseModel): reason:str="cabinet_closed"; notify_patients:bool=True

app=FastAPI(title="TAFWITA API",version="9.0")
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_credentials=True,allow_methods=["*"],allow_headers=["*"])
def db():
 s=SessionLocal()
 try: yield s
 finally:s.close()
def slugify(x): return re.sub("[^a-z0-9]+","-",unicodedata.normalize("NFKD",x).encode("ascii","ignore").decode().lower()).strip("-") or "cabinet"
def get_cab(s,slug):
 c=s.query(Cabinet).filter(Cabinet.slug==slug).first()
 if not c: raise HTTPException(404,"Cabinet introuvable.")
 return c
def pin(c,p):
 if str(c.code_pin)!=str(p):raise HTTPException(403,"Code PIN medecin incorrect.")
def ev(s,t,kind,code=None,note=None):s.add(TicketEvent(ticket_id=t.id,event_type=kind,reason_code=code,reason_note=note))
def dayrange():
 a=datetime.combine(date.today(),time.min);return a,a+timedelta(days=1)
def parse_h(v):
 try:return datetime.strptime(v,"%H:%M").time()
 except:return None
def waiting(s,slug):
 a,b=dayrange();return s.query(PatientTicket).filter(PatientTicket.cabinet_slug==slug,PatientTicket.created_at>=a,PatientTicket.created_at<b,PatientTicket.statut.in_(["waiting","returned","urgent"])).count()
def tdict(t):return {"id":t.id,"cabinet_slug":t.cabinet_slug,"ticket_num":t.ticket_num,"nom_patient":t.nom_patient,"telephone":t.telephone,"statut":t.statut,"created_at":t.created_at.isoformat() if t.created_at else None,"updated_at":t.updated_at.isoformat() if t.updated_at else None,"notification_count":t.notification_count or 0,"duree_min":None}
def waiting_current(c):return max(0,(c.total_issued or 0)-(c.serving_num or 0))
def cdict(c):return {"slug":c.slug,"nom_medecin":c.nom_medecin,"nom":c.nom_medecin,"specialite":c.specialite,"telephone":c.telephone,"wilaya":c.wilaya,"waiting_count":waiting_current(c),"accept_tickets":bool(c.accept_tickets),"day_closed":bool(c.day_closed),"ticket_mode":c.ticket_mode,"ticket_start_time":c.ticket_start_time,"ticket_end_time":c.ticket_end_time,"opening_time":c.opening_time,"closing_time":c.closing_time,"max_tickets_per_day":c.max_tickets_per_day}
def can_take(c):
 now=datetime.now().time()
 if not c.is_active:return False,"Le cabinet est ferme."
 if c.day_closed:return False,"La journee est cloturee."
 if not c.accept_tickets:return False,"La prise de tickets est temporairement arretee."
 st=parse_h(c.ticket_start_time)
 if st and now<st:return False,f"La prise de tickets commence a {c.ticket_start_time}."
 if c.ticket_mode=="fixed" and c.ticket_end_time:
  end=parse_h(c.ticket_end_time)
  if end and now>=end:return False,f"La prise de tickets est terminee depuis {c.ticket_end_time}."
 if c.max_tickets_per_day and c.total_issued>=c.max_tickets_per_day:return False,"Le maximum de tickets pour aujourd hui est atteint."
 return True,None

def schema():
 Base.metadata.create_all(engine)
 if engine.dialect.name=="postgresql":
  with engine.begin() as x:
   for q in [
    "ALTER TABLE cabinets ADD COLUMN IF NOT EXISTS accept_tickets BOOLEAN NOT NULL DEFAULT TRUE","ALTER TABLE cabinets ADD COLUMN IF NOT EXISTS day_closed BOOLEAN NOT NULL DEFAULT FALSE","ALTER TABLE cabinets ADD COLUMN IF NOT EXISTS day_closed_at TIMESTAMP NULL","ALTER TABLE cabinets ADD COLUMN IF NOT EXISTS ticket_mode VARCHAR NOT NULL DEFAULT 'manual'","ALTER TABLE cabinets ADD COLUMN IF NOT EXISTS ticket_start_time VARCHAR NOT NULL DEFAULT '06:00'","ALTER TABLE cabinets ADD COLUMN IF NOT EXISTS ticket_end_time VARCHAR NULL","ALTER TABLE cabinets ADD COLUMN IF NOT EXISTS opening_time VARCHAR NOT NULL DEFAULT '08:00'","ALTER TABLE cabinets ADD COLUMN IF NOT EXISTS closing_time VARCHAR NULL","ALTER TABLE cabinets ADD COLUMN IF NOT EXISTS max_tickets_per_day INTEGER NULL","ALTER TABLE patient_tickets ADD COLUMN IF NOT EXISTS user_id INTEGER NULL","ALTER TABLE patient_tickets ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NULL","ALTER TABLE patient_tickets ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMP NULL","ALTER TABLE patient_tickets ADD COLUMN IF NOT EXISTS notification_count INTEGER NOT NULL DEFAULT 0","CREATE TABLE IF NOT EXISTS ticket_events (id SERIAL PRIMARY KEY,ticket_id INTEGER NOT NULL REFERENCES patient_tickets(id),event_type VARCHAR NOT NULL,reason_code VARCHAR,reason_note TEXT,created_at TIMESTAMP NOT NULL DEFAULT NOW())","CREATE TABLE IF NOT EXISTS notifications (id SERIAL PRIMARY KEY,user_id INTEGER,ticket_id INTEGER,title VARCHAR NOT NULL,message TEXT NOT NULL,is_read BOOLEAN NOT NULL DEFAULT FALSE,created_at TIMESTAMP NOT NULL DEFAULT NOW())"]:x.execute(text(q))
schema()

@app.get("/")
def root():return {"status":"ok","version":"9.0"}
@app.get("/health")
def health():return {"status":"ok"}
@app.post("/api/cabinets/register")
def register(d:Register,s:Session=Depends(db)):
 if s.query(Cabinet).filter(Cabinet.telephone==d.telephone).first():raise HTTPException(400,"Ce numero de telephone est deja enregistre.")
 sl=slugify(d.nom_medecin)
 if s.query(Cabinet).filter(Cabinet.slug==sl).first():sl+=f"-{int(datetime.utcnow().timestamp())}"
 c=Cabinet(slug=sl,nom_medecin=d.nom_medecin,specialite=d.specialite,telephone=d.telephone,wilaya=d.wilaya,code_pin=d.code_pin,subscription_end=datetime.utcnow()+timedelta(days=14));s.add(c);s.commit();return {"status":"success","cabinet_slug":sl,"trial_end":c.subscription_end.date().isoformat()}
@app.get("/api/cabinets")
def cabinets(q:Optional[str]=None,specialite:Optional[str]=None,s:Session=Depends(db)):
 z=s.query(Cabinet)
 if q:z=z.filter(or_(Cabinet.nom_medecin.ilike(f"%{q}%"),Cabinet.specialite.ilike(f"%{q}%"),Cabinet.wilaya.ilike(f"%{q}%")))
 if specialite:z=z.filter(Cabinet.specialite.ilike(f"%{specialite}%"))
 return [cdict(c) for c in z.filter(Cabinet.is_active==True).all()]
@app.get("/api/cabinets/{slug}/public")
def public(slug:str,s:Session=Depends(db)):return cdict(get_cab(s,slug))
@app.post("/api/cabinets/{slug}/verify-pin")
def verify(slug:str,d:Pin,s:Session=Depends(db)):
 c=get_cab(s,slug);pin(c,d.code_pin);return {"cabinet_slug":c.slug,"cabinet_nom":c.nom_medecin,"code_pin":c.code_pin,"accept_tickets":c.accept_tickets}
@app.get("/api/cabinets/{slug}/settings")
def getsettings(slug:str,pin_code:str=Query(...,alias="pin"),s:Session=Depends(db)):
 c=get_cab(s,slug);pin(c,pin_code);return cdict(c)
@app.put("/api/cabinets/{slug}/settings")
def settings(slug:str,d:Settings,pin_code:str=Query(...,alias="pin"),s:Session=Depends(db)):
 c=get_cab(s,slug);pin(c,pin_code)
 if d.accept_tickets is True and c.day_closed:raise HTTPException(400,"La journee est cloturee. Utilisez Demarrer la journee.")
 for k,v in d.model_dump(exclude_unset=True).items():setattr(c,k,v)
 s.commit();return {"message":"Parametres mis a jour","cabinet":cdict(c)}
@app.post("/api/cabinets/{slug}/start-day")
def startday(slug:str,d:Pin,s:Session=Depends(db)):
 c=get_cab(s,slug);pin(c,d.code_pin);c.day_closed=False;c.day_closed_at=None;c.accept_tickets=True;c.is_active=True;c.serving_num=0;c.total_issued=0;s.commit();return {"message":"Nouvelle journee demarree. Prise de tickets active.","accept_tickets":True,"day_closed":False}
@app.post("/api/cabinets/{slug}/close-day")
def closeday(slug:str,d:Close,pin_code:str=Query(...,alias="pin"),s:Session=Depends(db)):
 c=get_cab(s,slug);pin(c,pin_code);a,b=dayrange();ts=s.query(PatientTicket).filter(PatientTicket.cabinet_slug==slug,PatientTicket.created_at>=a,PatientTicket.created_at<b,PatientTicket.statut.in_(["waiting","returned","suspended","urgent"])).all();n=0
 for t in ts:t.statut="cancelled";ev(s,t,"day_closed",d.reason,"Cabinet ferme pour aujourd hui");n+=1
 c.day_closed=True;c.day_closed_at=datetime.utcnow();c.accept_tickets=False;s.commit();return {"message":"Journee cloturee","closed_count":n,"notified_count":0}
@app.get("/api/queue/{slug}")
def queue(slug:str,s:Session=Depends(db)):
 c=get_cab(s,slug);a,b=dayrange();ts=s.query(PatientTicket).filter(PatientTicket.cabinet_slug==slug,PatientTicket.created_at>=a,PatientTicket.created_at<b).all();serv=next((t for t in ts if t.statut=="serving"),None);active=[t for t in ts if t.statut in ["waiting","returned","suspended","urgent","serving"]]
 return {**cdict(c),"cabinet_name":c.nom_medecin,"display_serving":f"P-{serv.ticket_num:02d}" if serv else "-","serving_num":serv.ticket_num if serv else 0,"waiting_count":len([t for t in ts if t.statut in ["waiting","returned","urgent"]]),"total_issued":c.total_issued,"tickets":[tdict(t) for t in active]}
@app.post("/api/tickets/take")
@app.post("/api/tickets")
def take(d:Take,s:Session=Depends(db)):
 c=get_cab(s,d.cabinet_slug);ok,msg=can_take(c)
 if not ok:raise HTTPException(400,msg)
 c.total_issued=(c.total_issued or 0)+1;t=PatientTicket(cabinet_slug=c.slug,ticket_num=c.total_issued,nom_patient=d.nom_patient,telephone=d.telephone,user_id=d.user_id);s.add(t);s.flush();ev(s,t,"created","patient_app","Ticket cree");s.commit();before=waiting(s,c.slug)-1
 return {"status":"success","ticket_num":t.ticket_num,"display_ticket":f"P-{t.ticket_num:02d}","waiting_before_you":max(0,before),"estimated_wait_min":max(0,before)*12,"ticket":tdict(t)}
@app.post("/api/tickets/add-manual")
def manual(d:Take,s:Session=Depends(db)):return take(d,s)
@app.get("/api/cabinets/{slug}/tickets")
def tickets(slug:str,s:Session=Depends(db)):get_cab(s,slug);return [tdict(t) for t in s.query(PatientTicket).filter(PatientTicket.cabinet_slug==slug).order_by(PatientTicket.created_at.desc()).all()]
@app.get("/api/cabinets/{slug}/alerts")
def alerts(slug:str,pin_code:str=Query(...,alias="pin"),s:Session=Depends(db)):
 c=get_cab(s,slug);pin(c,pin_code);return {"presence_confirmed":[],"urgent_requests":[t.id for t in s.query(PatientTicket).filter(PatientTicket.cabinet_slug==slug,PatientTicket.statut=="urgent").all()]}
@app.post("/api/queue/{slug}/next")
def nextticket(slug:str,pin_code:str=Query(...,alias="pin"),s:Session=Depends(db)):
 c=get_cab(s,slug);pin(c,pin_code);cur=s.query(PatientTicket).filter(PatientTicket.cabinet_slug==slug,PatientTicket.statut=="serving").first()
 if cur:cur.statut="completed";ev(s,cur,"completed","next","Consultation terminee")
 t=s.query(PatientTicket).filter(PatientTicket.cabinet_slug==slug,PatientTicket.statut.in_(["waiting","returned","urgent"])).order_by(PatientTicket.ticket_num).first()
 if not t:s.commit();return {"message":"Aucun patient en attente"}
 t.statut="serving";c.serving_num=t.ticket_num;ev(s,t,"called","cabinet_call","Patient appele");s.commit();return {"message":f"Patient P-{t.ticket_num:02d} appele","ticket":tdict(t)}
@app.get("/api/tickets/{id}/patient")
def patientticket(id:int,s:Session=Depends(db)):
 t=s.get(PatientTicket,id)
 if not t:raise HTTPException(404,"Ticket introuvable")
 return {"ticket":tdict(t),"waiting_before_you":max(0,waiting(s,t.cabinet_slug)-1)}
@app.get("/api/tickets/{id}/events")
def events(id:int,s:Session=Depends(db)):
 return [{"id":e.id,"event_type":e.event_type,"new_status":e.event_type,"reason_note":e.reason_note,"created_at":e.created_at.isoformat()} for e in s.query(TicketEvent).filter(TicketEvent.ticket_id==id).order_by(TicketEvent.created_at).all()]
@app.post("/api/tickets/{id}/{action}")
def action(id:int,action:str,d:TicketAction,s:Session=Depends(db)):
 t=s.get(PatientTicket,id)
 if not t:raise HTTPException(404,"Ticket introuvable")
 c=get_cab(s,t.cabinet_slug)
 if action in ["suspend","return-to-queue","approve-urgent","cabinet-cancel"]:pin(c,d.code_pin or "")
 if action=="suspend":t.statut="suspended";t.suspended_at=datetime.utcnow();ev(s,t,"suspended",d.reason_code,d.reason_note)
 elif action=="return-to-queue":t.statut="returned";ev(s,t,"returned",d.reason_code,d.reason_note)
 elif action=="approve-urgent":t.statut="urgent";ev(s,t,"urgent_approved","urgent",d.urgent_reason)
 elif action=="patient-suspend":t.statut="suspended";ev(s,t,"patient_suspended",d.reason_code,d.reason_note)
 elif action=="patient-present":t.statut="returned";ev(s,t,"patient_present","present","")
 elif action=="patient-cancel":t.statut="cancelled";ev(s,t,"patient_cancelled",d.reason_code,d.reason_note)
 elif action=="request-urgent":t.statut="urgent";ev(s,t,"urgent_requested",d.reason_code,d.reason_note)
 elif action=="cabinet-cancel":
  if t.statut!="suspended" or (t.notification_count or 0)<5:raise HTTPException(400,"Annulation possible seulement apres suspension et 5 passages.")
  t.statut="cancelled";ev(s,t,"cancelled","no_response_after_5",d.reason_note)
 else:raise HTTPException(400,"Action inconnue")
 s.commit();return {"message":"Action enregistree","ticket":tdict(t)}
@app.post("/api/tickets/{id}/notify")
def notify(id:int,pin_code:str=Query(...,alias="pin"),s:Session=Depends(db)):
 t=s.get(PatientTicket,id)
 if not t:raise HTTPException(404,"Ticket introuvable")
 c=get_cab(s,t.cabinet_slug);pin(c,pin_code);t.notification_count=(t.notification_count or 0)+1;ev(s,t,"patient_notified","no_response",f"Passage {t.notification_count}");s.commit();return {"notification_count":t.notification_count}
if __name__=="__main__":
 import uvicorn;uvicorn.run(app,host="0.0.0.0",port=int(os.getenv("PORT","8000")))

# ============================================================
# ============  AJOUTS POUR LE DASHBOARD ADMIN  ==============
# ============================================================
from fastapi import Header

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "tafwita-admin-2026")  # a changer en variable d'env Render

def check_admin(x_admin_token: str = Header(None)):
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Non autorise.")
    return True

# --- E. LISTE DE TOUS LES CABINETS (PRATICIENS) ---
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

# --- F. ACTIVER / DESACTIVER UN CABINET ---
@app.post("/api/admin/cabinets/{cabinet_id}/toggle")
def admin_toggle_cabinet(cabinet_id: int, db: Session = Depends(get_db), auth: bool = Depends(check_admin)):
    cabinet = db.query(Cabinet).filter(Cabinet.id == cabinet_id).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet introuvable.")
    cabinet.is_active = not cabinet.is_active
    db.commit()
    return {"status": "success", "is_active": cabinet.is_active}

# --- G. CHANGER LE TYPE D'ABONNEMENT MANUELLEMENT ---
class SubscriptionUpdate(BaseModel):
    subscription_type: str   # "trial_14d", "monthly", "annual"
    extend_days: Optional[int] = None

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
    return {"status": "success", "subscription_type": cabinet.subscription_type,
            "subscription_end": cabinet.subscription_end.strftime("%Y-%m-%d") if cabinet.subscription_end else None}

# --- H. SUPPRIMER UN CABINET ---
@app.delete("/api/admin/cabinets/{cabinet_id}")
def admin_delete_cabinet(cabinet_id: int, db: Session = Depends(get_db), auth: bool = Depends(check_admin)):
    cabinet = db.query(Cabinet).filter(Cabinet.id == cabinet_id).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet introuvable.")
    db.delete(cabinet)
    db.commit()
    return {"status": "success", "message": "Cabinet supprime."}

# --- I. STATISTIQUES GLOBALES (Dashboard) ---
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

# --- J. LOGIN ADMIN SIMPLE (verifie le mot de passe et renvoie le token) ---
class AdminLogin(BaseModel):
    password: str

@app.post("/api/admin/login")
def admin_login(data: AdminLogin):
    if data.password != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Mot de passe incorrect.")
    return {"status": "success", "token": ADMIN_TOKEN}
