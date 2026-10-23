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
os.environ["SMTP_ENABLED"] = "0"
os.environ.pop("RESEND_API_KEY", None)
os.environ.pop("BREVO_API_KEY", None)

if os.path.exists(POC_DB):
    os.remove(POC_DB)

from fastapi.testclient import TestClient
from main import (
    app,
    SessionLocal,
    Cabinet,
    PatientTicket,
    SupportTicket,
    _pin_attempts,
    _signup_attempts,
    _contact_attempts,
    _take_attempts,
    _pii_ephemere,
    est_hash_bcrypt,
    email_configure,
    email_via,
    send_trial_welcome_email,
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
    r = client.post("/api/cabinets/register", json={
        "nom_medecin": nom,
        "specialite": "Medecine generale",
        "telephone": tel,
        "wilaya": "Alger",
        "password": password,
    })
    _signup_attempts.clear()
    return r


def login(slug, password):
    return client.post(f"/api/cabinets/{slug}/verify-pin", json={"password": password})


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def take(slug, nom, tel=None, accepte=True):
    body = {"cabinet_slug": slug, "nom_patient": nom, "accepte_politique": accepte}
    if tel is not None:
        body["telephone"] = tel
    return client.post("/api/tickets/take", json=body)


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

r = take("dr-alpha", "Farid Patient", "0555111222")
check("prise de ticket patient (public)", r.status_code, 200)
r_vide = take("dr-alpha", "   ")
check("prise sans nom refusee", r_vide.status_code, 400)
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
check_true("add-manual rend un jeton de suivi", bool(r.json().get("suivi_token")))

# --- Isolation inter-cabinets ---
r = client.post("/api/cabinets/dr-beta/start-day", json={}, headers=auth(token_b))
check("start-day B", r.status_code, 200)
r = take("dr-beta", "Patient Beta")
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

r = take("dr-legacy", "Patient Legacy")
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
check("suivi patient sans nom", r.json().get("ticket", {}).get("nom_patient"), None)
check("suivi patient sans telephone", r.json().get("ticket", {}).get("telephone"), None)

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
r = take("dr-urgent", "Farid")
tid_farid = r.json()["ticket"]["id"]
r = take("dr-urgent", "Karim")
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
r = take("dr-prio", "Farid")
tid_p_farid = r.json()["ticket"]["id"]
r = take("dr-prio", "Karim")
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
r = client.get(
    f"/api/tickets/{tid_p_karim}/events",
    headers={"X-Suivi-Token": suivi_p_karim},
)
blob_ev = str(r.json()).lower()
check_true(
    "events suivi sans motif medical",
    "valide par le medecin" not in blob_ev and "douleur" not in blob_ev,
)
r = client.post("/api/queue/dr-prio/next", headers=auth(token_p))
check("next priorise l urgence approuvee", r.json().get("ticket_id"), tid_p_karim)

# --- start-day annule les tickets ouverts ---
r = client.post("/api/cabinets/dr-alpha/start-day", json={}, headers=auth(token_a))
check("start-day reset", r.status_code, 200)
r = client.get(f"/api/tickets/{tid_a}/patient", headers={"X-Suivi-Token": suivi_a})
check("start-day annule l ancien ticket", r.json()["ticket"]["statut"], "cancelled")
r = take("dr-alpha", "Nouveau Jour")
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
r = take("dr-alpha", "Hors Horaire")
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
r = take("dr-alpha", "Essai Expire")
check("take abonnement expire", r.status_code, 403)
db = SessionLocal()
cab = db.query(Cabinet).filter(Cabinet.slug == "dr-alpha").first()
cab.subscription_end = datetime.utcnow() + timedelta(days=14)
db.commit()
db.close()

r = client.get("/tv.html")
check("page TV publique", r.status_code, 200)

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

# --- Inscription essai : honeypot et limite IP ---
def essai_corps(tel, website="", cabinet=None):
    return {
        "nom": "Essai",
        "prenom": "Test",
        "cabinet": cabinet or ("Cabinet " + tel),
        "telephone": tel,
        "email": "essai@example.com",
        "specialite": "Medecine generale",
        "wilaya": "Alger",
        "password": "Essai1234",
        "website": website,
        "accepte_politique": True,
    }

_signup_attempts.clear()
r = client.post("/api/trial-signup", json=essai_corps("0555111000", "http://spam.test"))
check("essai honeypot refuse", r.status_code, 400)
db = SessionLocal()
try:
    honeypot_cree = db.query(Cabinet).filter(Cabinet.telephone == "0555111000").first()
finally:
    db.close()
check("essai honeypot sans cabinet", honeypot_cree, None)

_signup_attempts.clear()
for i in range(3):
    r = client.post("/api/trial-signup", json=essai_corps("055511101%d" % i))
    check("essai %d accepte" % (i + 1), r.status_code, 200)
    if i == 0:
        check("essai sans promesse email si non configure", r.json().get("email_prevu"), False)
r = client.post("/api/trial-signup", json=essai_corps("0555111019"))
check("essai 4e refuse limite", r.status_code, 429)
db = SessionLocal()
try:
    insc = db.query(SupportTicket).filter(SupportTicket.sujet == "Inscription essai").first()
finally:
    db.close()
check_true("inscription visible dans le support", bool(insc and insc.email))
check_true(
    "support inscription sans mot de passe",
    insc is not None and "Essai1234" not in (insc.message or ""),
)
check("email non configure dans poc", email_configure(), False)
send_trial_welcome_email("poc@example.com", "dr-alpha", "Dr Alpha", "MG")
check_true("welcome email sans service ne plante pas", True)
r = client.get("/health")
check("health email_ok sans cle", r.json().get("email_ok"), False)
check("health email_via sans cle", r.json().get("email_via"), None)
check("email_via sans cle", email_via(), None)
db = SessionLocal()
try:
    echec_mail = db.query(SupportTicket).filter(SupportTicket.sujet == "Echec e-mail inscription").first()
finally:
    db.close()
check_true("echec email trace dans le support", bool(echec_mail))

_signup_attempts.clear()
for i in range(3):
    r = client.post("/api/cabinets/register", json={
        "nom_medecin": "Dr Limite %d" % i,
        "specialite": "Medecine generale",
        "telephone": "055511102%d" % i,
        "wilaya": "Alger",
        "password": "Limite123",
    })
    check("register limite %d accepte" % (i + 1), r.status_code, 200)
r = client.post("/api/cabinets/register", json={
    "nom_medecin": "Dr Limite 9",
    "specialite": "Medecine generale",
    "telephone": "0555111029",
    "wilaya": "Alger",
    "password": "Limite123",
})
check("register 4e refuse limite", r.status_code, 429)

r = client.get("/mentions.html")
check("page mentions", r.status_code, 200)
check_true("mentions listent Render", "Render" in r.text)
check_true("mentions listent Cloudflare", "Cloudflare" in r.text)
check_true("mentions listent Brevo", "Brevo" in r.text)
r = client.get("/confidentialite.html")
check("page confidentialite", r.status_code, 200)
check_true("confidentialite sans donnees de sante", "donnees de sante" not in r.text.lower())
r = client.get("/cgu.html")
check("page cgu", r.status_code, 200)
check_true("cgu sous-traitant", "sous-traitant" in r.text)
r = client.get("/sous-traitance.html")
check("page sous-traitance", r.status_code, 200)
r = client.get("/demarches.html")
check("page demarches", r.status_code, 200)
check_true("demarches ANAE", "ANAE" in r.text)
r = client.get("/anpdp-dossier.html")
check("page anpdp dossier", r.status_code, 200)

_contact_attempts.clear()
r = client.post("/api/contact", json={
    "nom": "Bot", "email": "bot@test.com", "message": "spam", "website": "http://spam.test",
})
check("contact honeypot refuse", r.status_code, 400)
r = client.post("/api/contact", json={
    "nom": "Amine", "email": "amine@example.com", "message": "Question sur l essai",
    "accepte_politique": True,
})
check("contact vitrine ok", r.status_code, 200)
db = SessionLocal()
try:
    ticket = db.query(SupportTicket).filter(SupportTicket.email == "amine@example.com").first()
finally:
    db.close()
check_true("contact enregistre", bool(ticket and ticket.message))
r = client.post("/api/contact", json={"nom": "", "email": "x", "message": ""})
check("contact champs vides", r.status_code, 400)

# --- X-Forwarded-For spoofe ne contourne pas les limites (hors proxy) ---
_signup_attempts.clear()
for i in range(3):
    r = client.post("/api/cabinets/register", json={
        "nom_medecin": "Dr Xff %d" % i,
        "specialite": "Medecine generale",
        "telephone": "055511200%d" % i,
        "wilaya": "Alger",
        "password": "Xff12345",
    }, headers={"X-Forwarded-For": "203.0.113.%d" % i})
    check("register XFF %d accepte" % (i + 1), r.status_code, 200)
r = client.post("/api/cabinets/register", json={
    "nom_medecin": "Dr Xff 9",
    "specialite": "Medecine generale",
    "telephone": "0555112009",
    "wilaya": "Alger",
    "password": "Xff12345",
}, headers={"X-Forwarded-For": "203.0.113.99"})
check("register XFF spoof refuse", r.status_code, 429)

# --- Limite prise de tickets par IP ---
_signup_attempts.clear()
r = register("Dr Prise", "0555113000", "Prise1234")
check("cabinet limite prise", r.status_code, 200)
r = login("dr-prise", "Prise1234")
token_prise = r.json().get("token")
r = client.post("/api/cabinets/dr-prise/start-day", json={}, headers=auth(token_prise))
check("start-day limite prise", r.status_code, 200)
_take_attempts.clear()
for i in range(30):
    r = take("dr-prise", "Patient %d" % i)
    if r.status_code != 200:
        check("prise limite %d acceptee" % (i + 1), r.status_code, 200)
        break
else:
    check("prises 30 acceptees", 200, 200)
r = take("dr-prise", "Patient trop")
check("prise 31e refusee limite", r.status_code, 429)

r = take("dr-prise", "Sans case", accepte=False)
check("prise sans consentement refusee", r.status_code, 400)
_signup_attempts.clear()
corps_sans = essai_corps("0555114001")
corps_sans["accepte_politique"] = False
r = client.post("/api/trial-signup", json=corps_sans)
check("essai sans consentement refuse", r.status_code, 400)
_contact_attempts.clear()
r = client.post("/api/contact", json={
    "nom": "Amine", "email": "amine2@example.com", "message": "Sans case",
})
check("contact sans consentement refuse", r.status_code, 400)
r = client.get("/health")
check("health email_ok present", "email_ok" in r.json(), True)
check("health sans mail_from", "mail_from" in r.json(), False)

_signup_attempts.clear()
r = register("Dr Droits", "0555114100", "Droits123")
check("cabinet droits", r.status_code, 200)
token_d = login("dr-droits", "Droits123").json()["token"]
client.post("/api/cabinets/dr-droits/start-day", json={}, headers=auth(token_d))
_take_attempts.clear()
r = take("dr-droits", "Patient Droits", "0555000111")
tid_d = r.json()["ticket"]["id"]
check_true("pii en ram avant cloture", tid_d in _pii_ephemere)
r = client.get("/api/cabinets/dr-droits/export", headers=auth(token_d))
check("export cabinet", r.status_code, 200)
noms_export = [t.get("nom_patient") for t in r.json().get("tickets", [])]
check_true("export sans noms patients", all(n is None for n in noms_export))
check_true("export a le telephone cabinet", r.json().get("cabinet", {}).get("telephone") == "0555114100")
r = client.post("/api/cabinets/dr-droits/close-day", json={"reason": "cabinet_closed"}, headers=auth(token_d))
check("close-day droits", r.status_code, 200)
check_true("pii purgee a la cloture", tid_d not in _pii_ephemere)

_signup_attempts.clear()
r = client.post("/api/trial-signup", json=essai_corps("0555114200", cabinet="Cabinet Efface"))
check("essai pour suppression", r.status_code, 200)
slug_eff = r.json().get("cabinet_slug")
token_e = login(slug_eff, "Essai1234").json()["token"]
r = client.get("/api/cabinets/" + slug_eff + "/export")
check("export sans jeton refuse", r.status_code, 401)
r = client.delete("/api/cabinets/" + slug_eff + "/account", headers=auth(token_e))
check("suppression compte cabinet", r.status_code, 200)
db = SessionLocal()
try:
    cab_eff = db.query(Cabinet).filter(Cabinet.slug == slug_eff).first()
    supp_eff = db.query(SupportTicket).filter(
        SupportTicket.message.contains("Identifiant : " + slug_eff)
    ).first()
finally:
    db.close()
check("cabinet efface absent", cab_eff, None)
check("traces inscription effacees", supp_eff, None)

_contact_attempts.clear()
r = client.post("/api/cabinets/reset-password", json={
    "slug": "dr-alpha", "telephone": "0555000001", "website": "http://spam.test",
})
check("reset honeypot refuse", r.status_code, 400)
r = client.post("/api/cabinets/reset-password", json={
    "slug": "dr-alpha", "telephone": "0000000000",
})
check("reset telephone faux reste 200", r.status_code, 200)
r = client.post("/api/cabinets/reset-password", json={
    "slug": "dr-alpha", "telephone": "0555000001",
})
check("reset telephone justes", r.status_code, 200)
db = SessionLocal()
try:
    reset_t = db.query(SupportTicket).filter(SupportTicket.sujet == "Mot de passe oublie").first()
    cab_a = db.query(Cabinet).filter(Cabinet.slug == "dr-alpha").first()
    id_a = cab_a.id if cab_a else None
finally:
    db.close()
check_true("reset visible dans le support", bool(reset_t))
r = client.post("/api/admin/cabinets/%s/password" % id_a, json={"password": "Nouveau123"})
check("reset admin sans jeton", r.status_code, 401)
r = client.post(
    "/api/admin/cabinets/%s/password" % id_a,
    json={"password": "123"},
    headers=admin_h,
)
check("reset admin mot de passe faible", r.status_code, 400)
r = client.post(
    "/api/admin/cabinets/%s/password" % id_a,
    json={"password": "Nouveau123"},
    headers=admin_h,
)
check("reset admin ok", r.status_code, 200)
r = login("dr-alpha", "Alpha1234")
check("ancien mot de passe refuse", r.status_code, 403)
r = login("dr-alpha", "Nouveau123")
check("nouveau mot de passe accepte", r.status_code, 200)

print(f"\n=== Resultat : {passed} ok, {failed} echec ===")
if os.path.exists(POC_DB):
    try:
        os.remove(POC_DB)
    except OSError:
        pass

sys.exit(1 if failed else 0)
