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
 <!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TAFWITA — Admin Dashboard</title>
<style>
:root{
  --bg:#f4f9f8;--surface:#ffffff;--surface-mint:#f1fbf8;--border:#dcefeb;--border-strong:#bfeee3;
  --turquoise:#16b6b0;--turquoise-dark:#128f8a;--text:#24343d;--text-soft:#50646b;--text-muted:#91a4a8;
  --success:#39c996;--danger:#e0554f;--warning:#f2b96b;--radius:16px;--ff:'Segoe UI',Arial,sans-serif;
}
*{box-sizing:border-box}
body{margin:0;font-family:var(--ff);background:var(--bg);color:var(--text)}
a{color:inherit;text-decoration:none}

/* LOGIN */
#loginScreen{min-height:100vh;display:flex;align-items:center;justify-content:center;background:linear-gradient(145deg,#edf7f5,#f4f9f8)}
.login-box{background:var(--surface);padding:40px;border-radius:24px;border:1px solid var(--border-strong);box-shadow:0 20px 50px rgba(45,121,115,.12);width:min(380px,90%);text-align:center}
.login-box h1{margin:0 0 6px;font-size:22px}
.login-box h1 span{color:var(--turquoise)}
.login-box p{color:var(--text-muted);font-size:13px;margin:0 0 22px}
.login-box input{width:100%;min-height:48px;padding:0 16px;border:1px solid var(--border-strong);border-radius:12px;font-size:14px;margin-bottom:14px;outline:none}
.login-box input:focus{border-color:var(--turquoise);box-shadow:0 0 0 4px rgba(22,182,176,.12)}
.login-box button{width:100%;min-height:48px;border:none;border-radius:12px;background:linear-gradient(90deg,var(--turquoise),#43cdb7);color:#fff;font-weight:700;font-size:14px;cursor:pointer}
.login-error{color:var(--danger);font-size:12px;min-height:16px;margin-bottom:6px}

/* LAYOUT */
#app{display:none;min-height:100vh;grid-template-columns:250px 1fr}
#app.active{display:grid}
.sidebar{background:#0e2a2c;color:#dff7f1;padding:24px 14px;display:flex;flex-direction:column;gap:4px}
.sidebar .brand{font-size:19px;font-weight:800;margin:0 10px 26px;letter-spacing:.5px}
.sidebar .brand span{color:#5be6d8}
.nav-item{display:flex;align-items:center;gap:10px;padding:12px 14px;border-radius:12px;cursor:pointer;font-size:14px;color:#bcdcd8;transition:.15s}
.nav-item:hover{background:rgba(255,255,255,.06);color:#fff}
.nav-item.active{background:var(--turquoise);color:#fff;font-weight:700}
.nav-icon{font-size:16px;width:20px;text-align:center}
.sidebar .logout{margin-top:auto;padding:12px 14px;border-radius:12px;color:#ff9a94;cursor:pointer;font-size:13px}

main{padding:28px 34px;overflow-y:auto;max-height:100vh}
.topbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:26px}
.topbar h2{margin:0;font-size:24px}
.topbar .sub{color:var(--text-muted);font-size:13px;margin-top:4px}

.kpi-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:26px}
.kpi-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:18px;box-shadow:0 6px 16px rgba(45,121,115,.05)}
.kpi-card .label{color:var(--text-muted);font-size:12px;font-weight:700;text-transform:uppercase}
.kpi-card .value{font-size:28px;font-weight:800;margin-top:6px}
.kpi-card .value.success{color:var(--success)}
.kpi-card .value.warning{color:var(--warning)}
.kpi-card .value.turquoise{color:var(--turquoise-dark)}

.panel{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:22px;margin-bottom:22px;box-shadow:0 6px 16px rgba(45,121,115,.05)}
.panel h3{margin:0 0 16px;font-size:16px}

table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:10px 12px;background:var(--surface-mint);color:var(--text-soft);font-weight:700;font-size:12px}
td{padding:10px 12px;border-top:1px solid var(--border);color:var(--text-soft)}
tr:hover td{background:#fafefe}
.badge{padding:4px 10px;border-radius:99px;font-size:11px;font-weight:700}
.badge.active{background:#e3f9ee;color:var(--success)}
.badge.inactive{background:#fdeceb;color:var(--danger)}
.badge.trial{background:#fff3e2;color:var(--warning)}
.badge.monthly, .badge.annual{background:#e6f7f3;color:var(--turquoise-dark)}
.btn-mini{border:1px solid var(--border-strong);background:#fff;padding:6px 10px;border-radius:8px;font-size:12px;cursor:pointer;margin-right:6px}
.btn-mini:hover{background:var(--surface-mint)}
.btn-mini.danger{color:var(--danger);border-color:#f3c6c3}

.search-row{display:flex;gap:10px;margin-bottom:16px}
.search-row input{flex:1;min-height:40px;padding:0 14px;border:1px solid var(--border-strong);border-radius:10px;font-size:13px;outline:none}

.empty-state{color:var(--text-muted);font-size:13px;text-align:center;padding:30px}
.placeholder-note{background:var(--surface-mint);border:1px solid var(--border-strong);border-radius:12px;padding:14px;font-size:13px;color:var(--text-soft);margin-bottom:16px}

.section{display:none}
.section.active{display:block}

.wilaya-bar{display:flex;flex-direction:column;gap:8px}
.wilaya-row{display:flex;align-items:center;gap:10px;font-size:12px}
.wilaya-row .name{width:110px;color:var(--text-soft)}
.wilaya-row .track{flex:1;height:10px;background:var(--surface-mint);border-radius:99px;overflow:hidden}
.wilaya-row .fill{height:100%;background:linear-gradient(90deg,var(--turquoise),#43cdb7)}
.wilaya-row .count{width:26px;text-align:right;font-weight:700;color:var(--text)}

@media(max-width:900px){
  #app.active{grid-template-columns:1fr}
  .sidebar{display:none}
  .kpi-grid{grid-template-columns:1fr 1fr}
}
</style>
</head>
<body>

<div id="loginScreen">
  <div class="login-box">
    <h1>TAF<span>WITA</span></h1>
    <p>Panneau d'administration</p>
    <div class="login-error" id="loginError"></div>
    <input type="password" id="adminPassword" placeholder="Mot de passe admin">
    <button id="loginBtn">Se connecter</button>
  </div>
</div>

<div id="app">
  <div class="sidebar">
    <div class="brand">TAF<span>WITA</span> ADMIN</div>
    <div class="nav-item active" data-section="utilisateurs"><span class="nav-icon">👥</span> Utilisateurs</div>
    <div class="nav-item" data-section="praticiens"><span class="nav-icon">🏥</span> Praticiens</div>
    <div class="nav-item" data-section="abonnements"><span class="nav-icon">💳</span> Abonnements</div>
    <div class="nav-item" data-section="paiements"><span class="nav-icon">💰</span> Paiements</div>
    <div class="nav-item" data-section="chiffre"><span class="nav-icon">📈</span> Chiffre d'affaires</div>
    <div class="nav-item" data-section="support"><span class="nav-icon">🎫</span> Support</div>
    <div class="nav-item" data-section="securite"><span class="nav-icon">🔒</span> Sécurité</div>
    <div class="nav-item" data-section="statistiques"><span class="nav-icon">📊</span> Statistiques</div>
    <div class="nav-item" data-section="parametres"><span class="nav-icon">⚙️</span> Paramètres</div>
    <div class="logout" id="logoutBtn">↩ Déconnexion</div>
  </div>

  <main>
    <div class="topbar">
      <div>
        <h2 id="pageTitle">Utilisateurs</h2>
        <div class="sub" id="pageSub">Vue d'ensemble des patients ayant pris un ticket</div>
      </div>
    </div>

    <div class="kpi-grid">
      <div class="kpi-card"><div class="label">Cabinets total</div><div class="value turquoise" id="kpiTotal">–</div></div>
      <div class="kpi-card"><div class="label">Cabinets actifs</div><div class="value success" id="kpiActifs">–</div></div>
      <div class="kpi-card"><div class="label">En essai gratuit</div><div class="value warning" id="kpiEssai">–</div></div>
      <div class="kpi-card"><div class="label">CA mensuel estimé</div><div class="value" id="kpiCA">–</div></div>
    </div>

    <!-- UTILISATEURS -->
    <section class="section active" id="sec-utilisateurs">
      <div class="panel">
        <h3>Tickets émis au total</h3>
        <div class="kpi-grid" style="grid-template-columns:repeat(2,1fr)">
          <div class="kpi-card"><div class="label">Total tickets émis</div><div class="value turquoise" id="kpiTickets">–</div></div>
          <div class="kpi-card"><div class="label">Cabinets suivis</div><div class="value" id="kpiCabinetsSuivis">–</div></div>
        </div>
        <div class="placeholder-note">Le suivi détaillé par patient (compte App patient) sera ajouté quand l'authentification patient sera activée.</div>
      </div>
    </section>

    <!-- PRATICIENS -->
    <section class="section" id="sec-praticiens">
      <div class="panel">
        <div class="search-row">
          <input type="text" id="searchCabinet" placeholder="Rechercher un praticien, une wilaya, une spécialité...">
        </div>
        <table>
          <thead><tr>
            <th>Médecin</th><th>Spécialité</th><th>Wilaya</th><th>Téléphone</th><th>Statut</th><th>Abonnement</th><th>Actions</th>
          </tr></thead>
          <tbody id="tblCabinets"></tbody>
        </table>
        <div class="empty-state" id="emptyCabinets" style="display:none">Aucun cabinet trouvé.</div>
      </div>
    </section>

    <!-- ABONNEMENTS -->
    <section class="section" id="sec-abonnements">
      <div class="panel">
        <h3>Répartition des abonnements</h3>
        <table>
          <thead><tr><th>Médecin</th><th>Type</th><th>Fin d'abonnement</th><th>Jours restants</th><th>Actions</th></tr></thead>
          <tbody id="tblAbonnements"></tbody>
        </table>
      </div>
    </section>

    <!-- PAIEMENTS -->
    <section class="section" id="sec-paiements">
      <div class="panel">
        <div class="placeholder-note">Le module de paiement en ligne (Chargily / CIB / Edahabia) n'est pas encore activé. Cette section affichera l'historique des transactions une fois le RC validé et l'API de paiement intégrée.</div>
        <table>
          <thead><tr><th>Date</th><th>Cabinet</th><th>Montant</th><th>Méthode</th><th>Statut</th></tr></thead>
          <tbody><tr><td colspan="5" class="empty-state">Aucune transaction pour le moment.</td></tr></tbody>
        </table>
      </div>
    </section>

    <!-- CHIFFRE D'AFFAIRES -->
    <section class="section" id="sec-chiffre">
      <div class="panel">
        <h3>Chiffre d'affaires estimé</h3>
        <div class="placeholder-note">Estimation basée sur le nombre de cabinets payants (2 500 DA/mois ou 36 000 DA/an), en attendant l'intégration du paiement réel.</div>
        <div class="kpi-grid" style="grid-template-columns:repeat(3,1fr)">
          <div class="kpi-card"><div class="label">Cabinets mensuels</div><div class="value turquoise" id="kpiMensuels">–</div></div>
          <div class="kpi-card"><div class="label">Cabinets annuels</div><div class="value turquoise" id="kpiAnnuels">–</div></div>
          <div class="kpi-card"><div class="label">CA mensuel estimé</div><div class="value success" id="kpiCA2">–</div></div>
        </div>
      </div>
    </section>

    <!-- SUPPORT -->
    <section class="section" id="sec-support">
      <div class="panel">
        <div class="placeholder-note">Aucun ticket support pour le moment. Ce module affichera les demandes envoyées via contact@tafwita ou le futur formulaire in-app.</div>
        <table>
          <thead><tr><th>Date</th><th>Cabinet</th><th>Sujet</th><th>Statut</th></tr></thead>
          <tbody><tr><td colspan="4" class="empty-state">Aucun ticket support.</td></tr></tbody>
        </table>
      </div>
    </section>

    <!-- SECURITE -->
    <section class="section" id="sec-securite">
      <div class="panel">
        <h3>Accès admin</h3>
        <div class="placeholder-note">Le token admin est actuellement stocké en variable d'environnement (ADMIN_TOKEN) côté serveur Render. Changez-le régulièrement.</div>
        <table>
          <thead><tr><th>Événement</th><th>Détail</th></tr></thead>
          <tbody>
            <tr><td>Connexion admin</td><td id="secLoginTime">–</td></tr>
            <tr><td>Cabinets désactivés manuellement</td><td id="secInactive">–</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- STATISTIQUES -->
    <section class="section" id="sec-statistiques">
      <div class="panel">
        <h3>Répartition des cabinets par wilaya</h3>
        <div class="wilaya-bar" id="wilayaBars"></div>
      </div>
    </section>

    <!-- PARAMETRES -->
    <section class="section" id="sec-parametres">
      <div class="panel">
        <h3>Paramètres généraux</h3>
        <table>
          <tbody>
            <tr><td>URL API</td><td id="paramApiUrl">–</td></tr>
            <tr><td>Prix mensuel</td><td>2 500 DA / mois</td></tr>
            <tr><td>Prix annuel</td><td>36 000 DA / an</td></tr>
            <tr><td>Durée essai gratuit</td><td>14 jours</td></tr>
          </tbody>
        </table>
      </div>
    </section>

  </main>
</div>

<script>
const API_URL = "https://tafwita-api.onrender.com";
let ADMIN_TOKEN = sessionStorage.getItem("tafwita_admin_token") || "";

const loginScreen = document.getElementById('loginScreen');
const app = document.getElementById('app');
const loginError = document.getElementById('loginError');

document.getElementById('paramApiUrl').textContent = API_URL;

function showApp(){
  loginScreen.style.display = 'none';
  app.classList.add('active');
  document.getElementById('secLoginTime').textContent = new Date().toLocaleString('fr-FR');
  loadAll();
}

if(ADMIN_TOKEN){ showApp(); }

document.getElementById('loginBtn').addEventListener('click', async () => {
  const pwd = document.getElementById('adminPassword').value.trim();
  if(!pwd){ loginError.textContent = "Entrez le mot de passe."; return; }
  loginError.textContent = "Connexion...";
  try{
    const r = await fetch(`${API_URL}/api/admin/login`, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({password: pwd})
    });
    const d = await r.json();
    if(!r.ok) throw new Error(d.detail || "Mot de passe incorrect.");
    ADMIN_TOKEN = d.token;
    sessionStorage.setItem("tafwita_admin_token", ADMIN_TOKEN);
    loginError.textContent = "";
    showApp();
  }catch(e){
    loginError.textContent = e.message;
  }
});

document.getElementById('logoutBtn').addEventListener('click', () => {
  sessionStorage.removeItem("tafwita_admin_token");
  location.reload();
});

// NAVIGATION
document.querySelectorAll('.nav-item[data-section]').forEach(item => {
  item.addEventListener('click', () => {
    document.querySelectorAll('.nav-item').forEach(i => i.classList.remove('active'));
    item.classList.add('active');
    const sec = item.dataset.section;
    document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
    document.getElementById('sec-' + sec).classList.add('active');
    const titles = {
      utilisateurs: ["Utilisateurs", "Vue d'ensemble des patients ayant pris un ticket"],
      praticiens: ["Praticiens", "Liste des cabinets médicaux inscrits"],
      abonnements: ["Abonnements", "Gestion des essais gratuits, mensuels et annuels"],
      paiements: ["Paiements", "Historique des transactions (à venir)"],
      chiffre: ["Chiffre d'affaires", "Estimation du revenu récurrent"],
      support: ["Support", "Demandes d'assistance des praticiens"],
      securite: ["Sécurité", "Journal d'accès et paramètres de sécurité"],
      statistiques: ["Statistiques", "Répartition géographique et usage"],
      parametres: ["Paramètres", "Configuration générale de la plateforme"]
    };
    document.getElementById('pageTitle').textContent = titles[sec][0];
    document.getElementById('pageSub').textContent = titles[sec][1];
  });
});

let cabinetsCache = [];

async function apiGet(path){
  const r = await fetch(`${API_URL}${path}`, { headers: {'X-Admin-Token': ADMIN_TOKEN} });
  if(r.status === 401){ sessionStorage.removeItem("tafwita_admin_token"); location.reload(); }
  if(!r.ok) throw new Error("Erreur API " + path);
  return r.json();
}
async function apiPost(path, body){
  const r = await fetch(`${API_URL}${path}`, {
    method:'POST',
    headers:{'Content-Type':'application/json','X-Admin-Token': ADMIN_TOKEN},
    body: body ? JSON.stringify(body) : undefined
  });
  return r.json();
}
async function apiDelete(path){
  const r = await fetch(`${API_URL}${path}`, { method:'DELETE', headers:{'X-Admin-Token': ADMIN_TOKEN} });
  return r.json();
}

async function loadAll(){
  try{
    const stats = await apiGet('/api/admin/stats');
    document.getElementById('kpiTotal').textContent = stats.total_cabinets;
    document.getElementById('kpiActifs').textContent = stats.cabinets_actifs;
    document.getElementById('kpiEssai').textContent = stats.cabinets_en_essai;
    document.getElementById('kpiCA').textContent = stats.ca_estime_mensuel_da.toLocaleString('fr-FR') + ' DA';
    document.getElementById('kpiTickets').textContent = stats.total_tickets_emis;
    document.getElementById('kpiCabinetsSuivis').textContent = stats.total_cabinets;
    document.getElementById('kpiCA2').textContent = stats.ca_estime_mensuel_da.toLocaleString('fr-FR') + ' DA';

    renderWilaya(stats.repartition_wilaya);
  }catch(e){ console.error(e); }

  try{
    cabinetsCache = await apiGet('/api/admin/cabinets');
    renderCabinets(cabinetsCache);
    renderAbonnements(cabinetsCache);

    const inactifs = cabinetsCache.filter(c => !c.is_active).length;
    document.getElementById('secInactive').textContent = inactifs;

    const mensuels = cabinetsCache.filter(c => c.subscription_type === 'monthly').length;
    const annuels = cabinetsCache.filter(c => c.subscription_type === 'annual').length;
    document.getElementById('kpiMensuels').textContent = mensuels;
    document.getElementById('kpiAnnuels').textContent = annuels;
  }catch(e){ console.error(e); }
}

function badgeStatut(c){
  return c.is_active ? '<span class="badge active">Actif</span>' : '<span class="badge inactive">Inactif</span>';
}
function badgeAbo(type){
  if(type === 'trial_14d') return '<span class="badge trial">Essai</span>';
  if(type === 'monthly') return '<span class="badge monthly">Mensuel</span>';
  if(type === 'annual') return '<span class="badge annual">Annuel</span>';
  return type;
}

function renderCabinets(list){
  const tbl = document.getElementById('tblCabinets');
  const empty = document.getElementById('emptyCabinets');
  if(!list.length){ tbl.innerHTML=''; empty.style.display='block'; return; }
  empty.style.display='none';
  tbl.innerHTML = list.map(c => `
    <tr>
      <td>${c.nom_medecin}</td>
      <td>${c.specialite || '-'}</td>
      <td>${c.wilaya || '-'}</td>
      <td>${c.telephone}</td>
      <td>${badgeStatut(c)}</td>
      <td>${badgeAbo(c.subscription_type)}</td>
      <td>
        <button class="btn-mini" onclick="toggleCabinet(${c.id})">${c.is_active ? 'Désactiver' : 'Activer'}</button>
        <button class="btn-mini danger" onclick="deleteCabinet(${c.id})">Supprimer</button>
      </td>
    </tr>`).join('');
}

function renderAbonnements(list){
  document.getElementById('tblAbonnements').innerHTML = list.map(c => `
    <tr>
      <td>${c.nom_medecin}</td>
      <td>${badgeAbo(c.subscription_type)}</td>
      <td>${c.subscription_end || '-'}</td>
      <td>${c.jours_restants !== null ? c.jours_restants + ' j' : '-'}</td>
      <td>
        <button class="btn-mini" onclick="extendSub(${c.id})">+30 jours</button>
        <button class="btn-mini" onclick="setMonthly(${c.id})">Passer mensuel</button>
      </td>
    </tr>`).join('');
}

function renderWilaya(data){
  const entries = Object.entries(data || {}).sort((a,b)=>b[1]-a[1]);
  const max = entries.length ? entries[0][1] : 1;
  document.getElementById('wilayaBars').innerHTML = entries.map(([wilaya,count]) => `
    <div class="wilaya-row">
      <div class="name">${wilaya}</div>
      <div class="track"><div class="fill" style="width:${(count/max*100)}%"></div></div>
      <div class="count">${count}</div>
    </div>`).join('') || '<div class="empty-state">Aucune donnée.</div>';
}

async function toggleCabinet(id){
  await apiPost(`/api/admin/cabinets/${id}/toggle`);
  loadAll();
}
async function deleteCabinet(id){
  if(!confirm('Supprimer ce cabinet définitivement ?')) return;
  await apiDelete(`/api/admin/cabinets/${id}`);
  loadAll();
}
async function extendSub(id){
  await apiPost(`/api/admin/cabinets/${id}/subscription`, {subscription_type:"monthly", extend_days:30});
  loadAll();
}
async function setMonthly(id){
  await apiPost(`/api/admin/cabinets/${id}/subscription`, {subscription_type:"monthly"});
  loadAll();
}

document.getElementById('searchCabinet').addEventListener('input', (e) => {
  const q = e.target.value.toLowerCase();
  const filtered = cabinetsCache.filter(c =>
    (c.nom_medecin||'').toLowerCase().includes(q) ||
    (c.wilaya||'').toLowerCase().includes(q) ||
    (c.specialite||'').toLowerCase().includes(q)
  );
  renderCabinets(filtered);
});
</script>
</body>
</html>


