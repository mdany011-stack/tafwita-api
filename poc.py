"""Tests de securite TAFWITA — auth cabinet.

Utilise une base SQLite jetable. Ne touche jamais la production.
Lancer depuis ce dossier : py poc.py
"""
import os
import sys
from datetime import datetime, timedelta

POC_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "poc_test.db")
os.environ["DATABASE_URL"] = "sqlite:///" + POC_DB.replace("\\", "/")
os.environ["ADMIN_TOKEN"] = "poc-admin-token-not-for-prod-123456"

if os.path.exists(POC_DB):
    os.remove(POC_DB)

from fastapi.testclient import TestClient
from main import (
    app,
    SessionLocal,
    Cabinet,
    PatientTicket,
    _pin_attempts,
    est_hash_bcrypt,
)

client = TestClient(app)
passed = 0
failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    if ok:
        passed += 1
        mark = "ok"
    else:
        failed += 1
        mark = "ECHEC"
    print(f"[{mark}] {label} (doit etre {expected}) -> {got}")
    return ok


def check_true(label, cond):
    return check(label, True if cond else False, True)


def register(nom, tel, password="Alpha1234"):
    return client.post("/api/cabinets/register", json={
        "nom_medecin": nom,
        "specialite": "Medecine generale",
        "telephone": tel,
        "wilaya": "Alger",
        "password": password,
    })


def login(slug, password):
    return client.post(f"/api/cabinets/{slug}/verify-pin", json={"password": password})


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def pin_en_base(slug):
    db = SessionLocal()
    try:
        cabinet = db.query(Cabinet).filter(Cabinet.slug == slug).first()
        return cabinet.code_pin if cabinet else None
    finally:
        db.close()


print("=== TAFWITA poc.py — authentification cabinet ===\n")

# --- Inscription + hash ---
r = register("Dr Alpha", "0555000001", "123456")
check("inscription refuse PIN faible", r.status_code, 400)
r = register("Dr Alpha", "0555000001", "Alpha1234")
check("inscription cabinet A", r.status_code, 200)
check_true("mot de passe cabinet A hache", est_hash_bcrypt(pin_en_base("dr-alpha")))

r = register("Dr Beta", "0555000002", "Beta12345")
check("inscription cabinet B", r.status_code, 200)
r = register("Dr Alpha", "0555000099", "Alpha9999")
check("slug collision suffixe", r.status_code, 200)
check("slug collision devient -2", r.json().get("cabinet_slug"), "dr-alpha-2")

# Cabinet legacy en clair (migration paresseuse)
db = SessionLocal()
db.add(Cabinet(
    slug="dr-legacy",
    nom_medecin="Dr Legacy",
    specialite="Pediatrie",
    telephone="0555000003",
    wilaya="Oran",
    code_pin="111111",
    day_closed=False,
    accept_tickets=True,
))
db.commit()
db.close()
check_true("PIN legacy encore en clair avant connexion", not est_hash_bcrypt(pin_en_base("dr-legacy")))

# --- Mot de passe faux + limiteur ---
r = login("dr-alpha", "Wrong999")
check("mot de passe faux", r.status_code, 403)

_pin_attempts.clear()
r = register("Dr Limite", "0555000004", "Limite123")
for _ in range(5):
    login("dr-limite", "Wrong999")
r = login("dr-limite", "Wrong999")
check("6e mot de passe faux (limiteur)", r.status_code, 403)
check_true(
    "message limiteur",
    "tentatives" in str(r.json().get("detail", "")).lower(),
)
_pin_attempts.clear()

# --- Connexion OK : jeton, pas de secret dans l'URL ---
r = login("dr-alpha", "Alpha1234")
check("verify-pin A OK", r.status_code, 200)
data_a = r.json()
token_a = data_a.get("token")
check_true("verify-pin rend un jeton", bool(token_a))
check_true("reponse conserve cabinet_nom", bool(data_a.get("cabinet_nom")))
check_true("secret absent de l'URL de connexion", "pin=" not in str(r.request.url).lower())

r = login("dr-legacy", "111111")
check("verify-pin legacy OK", r.status_code, 200)
check_true("PIN legacy rehashe a la connexion", est_hash_bcrypt(pin_en_base("dr-legacy")))
token_legacy = r.json().get("token")

r = login("dr-beta", "Beta12345")
token_b = r.json().get("token")
check("verify-pin B OK", r.status_code, 200)

# --- Routes cabinet sans jeton ---
r = client.post("/api/queue/dr-alpha/next")
check("next sans jeton", r.status_code, 401)

r = client.post("/api/tickets/add-manual", json={
    "cabinet_slug": "dr-alpha",
    "nom_patient": "Patient Papier",
})
check("add-manual sans jeton", r.status_code, 401)

r = client.get("/api/cabinets/dr-alpha/tickets")
check("historique sans jeton", r.status_code, 401)

# --- Token : journee + ticket patient ---
r = client.post("/api/cabinets/dr-alpha/start-day", json={}, headers=auth(token_a))
check("start-day avec jeton", r.status_code, 200)

r = client.post("/api/tickets/take", json={
    "cabinet_slug": "dr-alpha",
    "nom_patient": "Farid Patient",
    "telephone": "0555111222",
})
check("prise de ticket patient (public)", r.status_code, 200)
ticket_a = r.json()["ticket"]
tid_a = ticket_a["id"]
suivi_a = r.json().get("suivi_token")
check_true("prise rend un jeton de suivi", bool(suivi_a))
check("echo take avec nom", ticket_a.get("nom_patient"), "Farid Patient")
db = SessionLocal()
row_sql = db.query(PatientTicket).filter(PatientTicket.id == tid_a).first()
check("nom patient absent de SQL", row_sql.nom_patient, None)
check("telephone patient absent de SQL", row_sql.telephone, None)
db.close()

r = client.post(f"/api/tickets/{tid_a}/patient-suspend", json={
    "reason_code": "patient_late",
    "reason_note": "retard",
})
check("action patient-suspend sans jeton suivi", r.status_code, 404)

r = client.post(f"/api/tickets/{tid_a}/patient-suspend", json={
    "reason_code": "patient_late",
    "reason_note": "retard",
}, headers={"X-Suivi-Token": suivi_a})
check("action patient-suspend avec jeton suivi", r.status_code, 200)

r = client.post(f"/api/tickets/{tid_a}/patient-present", json={
    "reason_note": "present",
}, headers={"X-Suivi-Token": suivi_a})
check("action patient-present avec jeton suivi", r.status_code, 200)

r = client.post("/api/queue/dr-alpha/next", headers=auth(token_a))
check("next avec jeton", r.status_code, 200)

r = client.post("/api/tickets/add-manual", json={
    "cabinet_slug": "dr-alpha",
    "nom_patient": "Ticket Papier",
}, headers=auth(token_a))
check("add-manual avec jeton", r.status_code, 200)

# --- Isolation inter-cabinets ---
r = client.post("/api/cabinets/dr-beta/start-day", json={}, headers=auth(token_b))
check("start-day B", r.status_code, 200)
r = client.post("/api/tickets/take", json={
    "cabinet_slug": "dr-beta",
    "nom_patient": "Patient Beta",
})
tid_b = r.json()["ticket"]["id"]

r = client.post(f"/api/tickets/{tid_b}/notify", headers=auth(token_a))
check("jeton A sur ticket B (notify)", r.status_code, 404)

r = client.post(f"/api/tickets/{tid_b}/suspend", json={}, headers=auth(token_a))
check("jeton A sur ticket B (suspend)", r.status_code, 404)

r = client.post("/api/cabinets/dr-beta/start-day", json={}, headers=auth(token_a))
check("jeton A sur slug B", r.status_code, 403)

# --- Compat mot de passe dans le body, refuse dans l'URL ---
r = client.post("/api/cabinets/dr-legacy/start-day", json={"password": "111111"})
check("start-day compat mot de passe body", r.status_code, 200)

r = client.post("/api/tickets/take", json={
    "cabinet_slug": "dr-legacy",
    "nom_patient": "Patient Legacy",
})
tid_l = r.json()["ticket"]["id"]
suivi_l = r.json().get("suivi_token")

r = client.post("/api/queue/dr-legacy/next?pin=111111")
check("next refuse secret dans l'URL", r.status_code, 401)

r = client.post(f"/api/tickets/{tid_l}/notify?pin=111111")
check("notify refuse secret dans l'URL", r.status_code, 401)
r = client.post(f"/api/tickets/{tid_l}/notify", headers=auth(token_legacy))
check("notify avec jeton", r.status_code, 200)

# --- File publique sans PII + suivi patient ---
r = client.get("/api/queue/dr-alpha")
check("file publique sans jeton", r.status_code, 200)
noms_publics = [t.get("nom_patient") for t in r.json().get("tickets", [])]
tels_publics = [t.get("telephone") for t in r.json().get("tickets", [])]
check_true("file publique sans noms", all(n is None for n in noms_publics))
check_true("file publique sans telephones", all(t is None for t in tels_publics))

r = client.get("/api/queue/dr-alpha", headers=auth(token_a))
check("file authentifiee", r.status_code, 200)
check_true(
    "file authentifiee avec noms",
    any(t.get("nom_patient") for t in r.json().get("tickets", [])),
)

r = client.get(f"/api/tickets/{tid_a}/patient")
check("suivi patient sans jeton suivi", r.status_code, 404)

r = client.get(f"/api/tickets/{tid_a}/patient", headers={"X-Suivi-Token": suivi_a})
check("suivi patient avec jeton suivi", r.status_code, 200)

r = client.get("/api/cabinets")
check("liste cabinets publique", r.status_code, 200)

# Ancien ticket sans hash : suivi ferme
db = SessionLocal()
ancien = PatientTicket(
    cabinet_slug="dr-legacy",
    ticket_num=99,
    nom_patient=None,
    statut="waiting",
)
db.add(ancien)
db.commit()
ancien_id = ancien.id
db.close()
r = client.get(f"/api/tickets/{ancien_id}/patient")
check("suivi ancien ticket sans hash refuse", r.status_code, 404)
r = client.get(f"/api/tickets/{tid_a}/events")
check("events sans jeton refuse", r.status_code, 404)
r = client.get(f"/api/tickets/{tid_a}/events", headers={"X-Suivi-Token": suivi_a})
check("events avec jeton suivi", r.status_code, 200)
r = client.get("/api/test-email")
check("route test-email supprimee", r.status_code, 404)

# --- Urgence : pas de saut sans avis medecin, puis priorite apres approbation ---
r = register("Dr Urgent", "0555000005", "Urgent123")
check("inscription cabinet urgent", r.status_code, 200)
token_u = login("dr-urgent", "Urgent123").json()["token"]
client.post("/api/cabinets/dr-urgent/start-day", json={}, headers=auth(token_u))
r = client.post("/api/tickets/take", json={"cabinet_slug": "dr-urgent", "nom_patient": "Farid"})
tid_farid = r.json()["ticket"]["id"]
r = client.post("/api/tickets/take", json={"cabinet_slug": "dr-urgent", "nom_patient": "Karim"})
tid_karim = r.json()["ticket"]["id"]
suivi_karim = r.json()["suivi_token"]

r = client.post(
    f"/api/tickets/{tid_karim}/request-urgent",
    json={"reason_note": "douleur"},
    headers={"X-Suivi-Token": suivi_karim},
)
check("request-urgent", r.status_code, 200)
check("request-urgent reste en file", r.json()["ticket"]["statut"], "urgent_requested")

r = client.post("/api/queue/dr-urgent/next", headers=auth(token_u))
check("next sans approbation appelle Farid", r.status_code, 200)
check("next sans approbation ticket", r.json().get("ticket_id"), tid_farid)

# Seconde file : Karim approuve saute Farid
r = register("Dr Prio", "0555000006", "Prio12345")
token_p = login("dr-prio", "Prio12345").json()["token"]
client.post("/api/cabinets/dr-prio/start-day", json={}, headers=auth(token_p))
r = client.post("/api/tickets/take", json={"cabinet_slug": "dr-prio", "nom_patient": "Farid"})
tid_p_farid = r.json()["ticket"]["id"]
r = client.post("/api/tickets/take", json={"cabinet_slug": "dr-prio", "nom_patient": "Karim"})
tid_p_karim = r.json()["ticket"]["id"]
suivi_p_karim = r.json()["suivi_token"]
client.post(
    f"/api/tickets/{tid_p_karim}/request-urgent",
    json={"reason_note": "douleur"},
    headers={"X-Suivi-Token": suivi_p_karim},
)
r = client.post(
    f"/api/tickets/{tid_p_karim}/approve-urgent",
    json={"urgent_reason": "valide par le medecin"},
    headers=auth(token_p),
)
check("approve-urgent", r.status_code, 200)
check("approve-urgent statut", r.json()["ticket"]["statut"], "urgent")
r = client.post("/api/queue/dr-prio/next", headers=auth(token_p))
check("next priorise l urgence approuvee", r.json().get("ticket_id"), tid_p_karim)

# --- start-day annule les tickets ouverts ---
r = client.post("/api/cabinets/dr-alpha/start-day", json={}, headers=auth(token_a))
check("start-day reset", r.status_code, 200)
r = client.get(f"/api/tickets/{tid_a}/patient", headers={"X-Suivi-Token": suivi_a})
check("start-day annule l ancien ticket", r.json()["ticket"]["statut"], "cancelled")
r = client.post("/api/tickets/take", json={
    "cabinet_slug": "dr-alpha",
    "nom_patient": "Nouveau Jour",
})
check("nouveau ticket apres start-day", r.status_code, 200)
check("nouveau ticket est P-01", r.json()["ticket"]["ticket_num"], 1)
suivi_nouveau = r.json().get("suivi_token")

# --- Horaires fixed (UTC+1) ---
r = client.put("/api/cabinets/dr-alpha/settings", json={
    "ticket_mode": "fixed",
    "ticket_start_time": "23:59",
    "ticket_end_time": "23:59",
}, headers=auth(token_a))
check("reglage horaire fixed", r.status_code, 200)
r = client.post("/api/tickets/take", json={
    "cabinet_slug": "dr-alpha",
    "nom_patient": "Hors Horaire",
})
check("take hors horaire fixed", r.status_code, 400)
client.put("/api/cabinets/dr-alpha/settings", json={
    "ticket_mode": "manual",
    "ticket_start_time": "06:00",
    "ticket_end_time": None,
}, headers=auth(token_a))

# --- Abonnement expire ---
db = SessionLocal()
cab = db.query(Cabinet).filter(Cabinet.slug == "dr-alpha").first()
cab.subscription_end = datetime.utcnow() - timedelta(days=1)
db.commit()
db.close()
r = client.post("/api/tickets/take", json={
    "cabinet_slug": "dr-alpha",
    "nom_patient": "Essai Expire",
})
check("take abonnement expire", r.status_code, 403)
db = SessionLocal()
cab = db.query(Cabinet).filter(Cabinet.slug == "dr-alpha").first()
cab.subscription_end = datetime.utcnow() + timedelta(days=14)
db.commit()
db.close()

r = client.get("/patient.html")
check("page patient publique", r.status_code, 200)
r = client.get("/p/dr-alpha")
check("page prise QR", r.status_code, 200)
check_true("page prise QR contient un formulaire", "Prendre mon ticket" in r.text)
r = client.get("/p/cabinet-inconnu")
check("page prise QR cabinet inconnu", r.status_code, 404)
r = client.get("/main.py")
check("code source non servi", r.status_code, 404)

# --- Admin dashboard ---
admin_h = {"X-Admin-Token": "poc-admin-token-not-for-prod-123456"}
r = client.get("/admin.html")
check("page admin", r.status_code, 200)
r = client.get("/api/admin/stats")
check("stats admin sans jeton", r.status_code, 401)
r = client.get("/api/admin/patients")
check("patients admin sans jeton", r.status_code, 401)
r = client.get("/api/admin/stats", headers=admin_h)
check("stats admin avec jeton", r.status_code, 200)
r = client.get("/api/admin/patients", headers=admin_h)
check("patients admin avec jeton", r.status_code, 200)
check_true(
    "patients admin sans noms",
    all(not p.get("nom_patient") for p in r.json()),
)
check_true(
    "patients admin sans telephones",
    all(not p.get("telephone") for p in r.json()),
)
r = client.post("/api/admin/login", json={"password": "mauvais"})
check("login admin mauvais mot de passe", r.status_code, 401)
r = client.post("/api/admin/login", json={"password": "poc-admin-token-not-for-prod-123456"})
check("login admin ok", r.status_code, 200)
r = client.get("/api/admin/security", headers=admin_h)
check("securite admin", r.status_code, 200)
r = client.post(
    "/api/admin/support",
    json={"nom": "Test", "sujet": "Aide", "message": "Bonjour"},
    headers=admin_h,
)
check("creer ticket support", r.status_code, 200)

r = client.post("/api/cabinets/dr-alpha/close-day", json={"reason": "cabinet_closed"}, headers=auth(token_a))
check("close-day", r.status_code, 200)
r = client.post("/api/tickets/add-manual", json={
    "cabinet_slug": "dr-alpha",
    "nom_patient": "Papier apres cloture",
}, headers=auth(token_a))
check("add-manual journee cloturee", r.status_code, 400)
r = client.get("/api/tickets/" + str(tid_a) + "/events", headers=auth(token_a))
check("events avec jeton cabinet", r.status_code, 200)

# --- Logout invalide le jeton ---
r = client.post("/api/cabinets/logout", headers=auth(token_a))
check("logout", r.status_code, 200)
r = client.post("/api/queue/dr-alpha/next", headers=auth(token_a))
check("next apres logout", r.status_code, 401)

print(f"\n=== Resultat : {passed} ok, {failed} echec ===")
if os.path.exists(POC_DB):
    try:
        os.remove(POC_DB)
    except OSError:
        pass

sys.exit(1 if failed else 0)
