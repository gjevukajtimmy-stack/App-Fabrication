# -*- coding: utf-8 -*-
"""
CLIMAIR - Commande de pièces rectangulaires (tôlerie / ventilation)
===================================================================
Lancer :  streamlit run app.py

Structure :
    climair-app/
    ├── app.py
    ├── requirements.txt
    ├── assets/    (logo.png, cid1.png, cid4.png, cid2.png, cid18.png, cid3.png,
    │               cid6.png, cid14.png, cid95.png, cid110.png, cid95-G.png, cid999.png)
    ├── uploads/   (croquis clients)
    └── exports/   (scripts CAMduct .txt)

NOTE : comptes, commandes et statuts sont conservés dans data/climair.db.
Sauvegarder ce fichier avec les dossiers uploads/ et exports/ en production.
"""
import hashlib
import hmac
import mimetypes
import smtplib
import threading
from email.message import EmailMessage
import os
import secrets as pysecrets
import shutil
import time
import uuid
from datetime import datetime

import streamlit as st
from ai_sketch import AnalyseErreur, analyser_croquis, analyser_listing

from domain import (
    UN_SEUL_CONNECTEUR,
    CATALOGUE,
    COEF_VENTE,
    CONNECTEURS,
    EPAISSEUR_DEFAUT,
    EPAISSEURS,
    PRIX_KG_REVIENT,
    calculer,
    fmt_qte,
    resume_piece,
    script_camduct,
)
from security import ITERATIONS_MDP, hash_mdp, mot_de_passe_robuste, verifier_mdp
from storage import Database
from services.maintenance import fichiers_orphelins
from services.media import enregistrer_image
from services.pdf import generer_recapitulatif

# --------------------------------------------------------------------------
# CONFIGURATION
# --------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOSSIER_ASSETS = os.path.join(BASE_DIR, "assets")
DOSSIER_UPLOADS = os.path.join(BASE_DIR, "uploads")
DOSSIER_EXPORTS = os.path.join(BASE_DIR, "exports")
DOSSIER_DONNEES = os.path.join(BASE_DIR, "data")
ICONE_PX = 48               # taille des icônes de pièces dans la grille (pixels)
CHEMIN_LOGO = os.path.join(DOSSIER_ASSETS, "logo.png")

for _d in (DOSSIER_ASSETS, DOSSIER_UPLOADS, DOSSIER_EXPORTS, DOSSIER_DONNEES):
    os.makedirs(_d, exist_ok=True)

_ICONE = os.path.join(DOSSIER_ASSETS, "icone.png")   # emblème du logo, carré
st.set_page_config(
    page_title="CLIMAIR",
    page_icon=_ICONE if os.path.exists(_ICONE) else ":material/air:",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# Icône de raccourci (écran d'accueil du téléphone) : on déclare l'emblème du logo
# dans l'en-tête de la page. Fichiers servis depuis static/ (enableStaticServing
# dans .streamlit/config.toml). Il faut ré-ajouter le raccourci après modification.
st.html("""
<script>
const d = window.parent.document;
function lien(rel, href, extra) {
  let l = d.querySelector('link[rel="' + rel + '"]');
  if (!l) { l = d.createElement('link'); l.rel = rel; d.head.appendChild(l); }
  l.href = href; if (extra) l.sizes = extra;
}
lien('apple-touch-icon', '/app/static/apple-touch-icon.png', '180x180');
lien('icon', '/app/static/icon-192.png');
lien('manifest', '/app/static/manifest.json');
</script>
""", unsafe_allow_javascript=True)

# Style smartphone : gros boutons tactiles, et la grille de pièces reste en
# colonnes (Streamlit empile normalement les colonnes sur écran étroit).
st.html("""
<style>
.stButton > button {min-height: 3rem; font-size: 1rem;}
.st-key-grille [data-testid="stHorizontalBlock"] {flex-direction: row !important; flex-wrap: nowrap !important; gap: 0.5rem;}
.st-key-grille [data-testid="stColumn"] {min-width: 0 !important; flex: 1 1 0 !important; width: auto !important;}
/* Onglet de commande terminée : vert */
[class*="st-key-termine_"] [data-testid="stExpander"] {background: #e6f4ea; border: 2px solid #2e9e4f; border-radius: 0.5rem;}
[class*="st-key-termine_"] [data-testid="stExpander"] summary {background: #cfebd7; font-weight: 600;}
/* Cellule de la grille : vue 3D de la pièce au-dessus du bouton (CID + nom) */
.st-key-grille [data-testid="stImage"] {margin: 0;}
.st-key-grille [data-testid="stImage"] img {height: 4.5rem; width: 100%; object-fit: contain;}
.st-key-grille .stButton > button {min-height: 3.2rem; padding: 0.2rem; font-size: 0.8rem; line-height: 1.15; white-space: normal;}
</style>
""")

# --------------------------------------------------------------------------
# DONNÉES PERSISTANTES
# --------------------------------------------------------------------------
# Incrémenter cette version lorsque l'API ou le schéma de Database évolue.
# Elle empêche Streamlit de réutiliser une ancienne instance cache_resource
# après un rechargement à chaud du code.
DATABASE_RESOURCE_VERSION = 4


@st.cache_resource
def get_database(chemin, resource_version):
    del resource_version  # utilisé uniquement pour invalider proprement le cache
    return Database(chemin)


CHEMIN_BASE = os.environ.get(
    "CLIMAIR_DB_PATH", os.path.join(DOSSIER_DONNEES, "climair.db")
)
DB = get_database(CHEMIN_BASE, DATABASE_RESOURCE_VERSION)
STORE = DB.snapshot()


# --------------------------------------------------------------------------
# ALERTES E-MAIL (nouvelle commande / nouveau listing)
# --------------------------------------------------------------------------
# Réglages SMTP : variables d'environnement ou fichier .streamlit/secrets.toml :
#   SMTP_HOST, SMTP_PORT (465 = SSL, sinon STARTTLS), SMTP_USER, SMTP_PASSWORD,
#   MAIL_TO (destinataire des alertes ; défaut ci-dessous)
MAIL_TO_DEFAUT = "gclim@orange.fr"


def _reglage(nom, defaut=""):
    if os.environ.get(nom):
        return os.environ[nom]
    try:
        return str(st.secrets.get(nom, defaut))
    except Exception:
        return defaut


def interface_analyse_croquis(fichier, identifiant, ajouter_au_panier=False, ligne_atelier=None):
    """Analyse, correction humaine puis insertion dans le panier ou modification atelier."""
    if fichier is None:
        st.info("Ajoutez une photo nette du croquis pour commencer.")
        return
    if hasattr(fichier, "getvalue"):
        contenu = fichier.getvalue()
    else:
        with open(fichier, "rb") as entree:
            contenu = entree.read()
    signature = hashlib.sha256(contenu).hexdigest()
    cle_resultat = f"resultat_ia_{identifiant}"
    etat = st.session_state.get(cle_resultat)
    if etat and etat["signature"] != signature:
        st.session_state.pop(cle_resultat, None)
        etat = None
    st.image(contenu, caption="Croquis soumis à l'analyse", width=320)
    st.caption("La photo sera transmise au service d'analyse IA uniquement si vous cliquez sur Analyser.")
    cle_api = _reglage("OPENAI_API_KEY")
    if not cle_api:
        st.warning("Analyse indisponible : configurez OPENAI_API_KEY dans les secrets Streamlit.")
        return
    if st.button("Analyser le croquis par IA", key=f"lancer_ia_{identifiant}"):
        with st.spinner("Lecture des cotes et identification de la pièce…"):
            try:
                resultat = analyser_croquis(contenu, cle_api, _reglage("OPENAI_VISION_MODEL", "gpt-4.1-mini"))
            except AnalyseErreur as erreur:
                st.error(str(erreur))
            else:
                st.session_state[cle_resultat] = {"signature": signature, "resultat": resultat}
                st.rerun()
    if not etat:
        return
    proposition = etat["resultat"]
    st.info(proposition["explication"] or "Résultat à vérifier manuellement.")
    for incertitude in proposition["incertitudes"]:
        st.warning(incertitude)
    with st.form(f"validation_ia_{identifiant}"):
        cids = list(CATALOGUE)
        cid = st.selectbox("Type de pièce proposé", cids, index=cids.index(proposition["cid"]),
                           format_func=lambda v: f"{v} · {CATALOGUE[v]['titre']}")
        cotes = {}
        for cle, lib, valeur_defaut, mini, maxi in CATALOGUE[cid]["cotes"]:
            proposition_valeur = proposition["cotes"].get(cle)
            if proposition_valeur is None:
                st.warning(f"{lib} : non lu — saisie manuelle obligatoire.")
            cotes[cle] = st.number_input(
                lib, min_value=float(mini), max_value=float(maxi),
                value=float(proposition_valeur if proposition_valeur is not None else valeur_defaut),
                step=1.0, key=f"ia_{identifiant}_{cid}_{cle}",
            )
        if ajouter_au_panier:
            repere = st.text_input("Repère *", value="1")
            qte = st.number_input("Longueur en ml" if cid == "CID 1" else "Quantité",
                                  min_value=0.1 if cid == "CID 1" else 1,
                                  value=1.0 if cid == "CID 1" else (proposition["quantite"] or 1),
                                  step=0.5 if cid == "CID 1" else 1)
            con1 = st.selectbox("Connecteur côté 1", CONNECTEURS)
            con2 = "" if cid in UN_SEUL_CONNECTEUR else st.selectbox("Connecteur côté 2", CONNECTEURS)
        validation = st.checkbox("J'ai contrôlé les cotes sur le croquis et corrigé les erreurs.")
        confirmer = st.form_submit_button(
            "Ajouter au panier" if ajouter_au_panier else "Enregistrer la correction atelier",
            disabled=not validation, type="primary",
        )
    if not confirmer:
        return
    if cid == "CID 999":
        st.error("Pièce non identifiée : utilisez la saisie manuelle sur-mesure.")
        return
    if ajouter_au_panier:
        if not repere.strip() or any(l["repere"].casefold() == repere.strip().casefold()
                                     for l in st.session_state.panier):
            st.error("Repère vide ou déjà présent dans le panier.")
            return
        from services.media import enregistrer_image
        photo = enregistrer_image(fichier, DOSSIER_UPLOADS, "croquis_ia")
        st.session_state.panier.append(dict(
            repere=repere.strip(), cid=cid, cotes=cotes, epaisseur=EPAISSEUR_DEFAUT,
            con1=con1, con2=con2, qte=float(qte) if cid == "CID 1" else int(qte), photo=photo,
        ))
        st.session_state.panier_chantier = st.session_state.get("chantier", "").strip()
        enregistrer_brouillon_courant()
        st.session_state.flash = "Pièce analysée et validée ajoutée au panier."
    else:
        ligne_atelier["cid"], ligne_atelier["cotes"] = cid, cotes
        DB.mettre_a_jour_commande(ligne_atelier)
        DB.ajouter_audit("atelier", "correction_croquis_ia", ligne_atelier["id"])
    st.session_state.pop(cle_resultat, None)
    st.rerun()


def interface_analyse_listing(fichier, identifiant, user=None, chantier=None):
    """Un listing photo -> plusieurs pièces revues -> panier client ou commande atelier."""
    if fichier is None:
        st.info("Ajoutez une photo contenant vos différents croquis.")
        return
    if hasattr(fichier, "getvalue"):
        contenu = fichier.getvalue()
    else:
        with open(fichier, "rb") as source:
            contenu = source.read()
    signature = hashlib.sha256(contenu).hexdigest()
    cle = f"listing_ia_{identifiant}"
    etat = st.session_state.get(cle)
    if etat and etat["signature"] != signature:
        st.session_state.pop(cle, None)
        etat = None
    st.image(contenu, caption="Listing à analyser", width=500)
    st.caption("La photo est envoyée au service IA uniquement après clic sur Analyser.")
    cle_api = _reglage("OPENAI_API_KEY")
    if not cle_api:
        st.warning("Configurez OPENAI_API_KEY dans les secrets Streamlit pour activer l'analyse.")
        return
    if st.button("Analyser toutes les pièces", key=f"analyser_listing_{identifiant}"):
        with st.spinner("Repérage et lecture des différents dessins…"):
            try:
                resultat = analyser_listing(contenu, cle_api, _reglage("OPENAI_VISION_MODEL", "gpt-4.1-mini"))
            except AnalyseErreur as erreur:
                st.error(str(erreur))
            else:
                st.session_state[cle] = {"signature": signature, "resultat": resultat}
                st.rerun()
    if not etat:
        return
    resultat = etat["resultat"]
    propositions = resultat["pieces"]
    if not propositions:
        st.error("Aucune pièce identifiée. Essayez une photo plus nette.")
        return
    st.metric("Dessins détectés", len(propositions))
    if not resultat["complet"]:
        st.error("L'IA ne garantit pas avoir lu toutes les pièces. Comparez avec la photo.")
    for alerte in resultat["incertitudes_globales"]:
        st.warning(alerte)
    st.caption("Vérifiez chaque ligne ; les valeurs proposées pour les cotes non lues ne sont que des exemples à corriger.")
    revues = []
    with st.form(f"form_listing_ia_{identifiant}"):
        for i, proposition in enumerate(propositions):
            avec = st.container(border=True)
            with avec:
                st.markdown(f"**Pièce {i + 1} · {proposition['repere']}**")
                inclure = st.checkbox("Inclure cette pièce", value=True, key=f"inc_{identifiant}_{i}")
                cids = list(CATALOGUE)
                cid = st.selectbox("Type de pièce", cids, index=cids.index(proposition["cid"]),
                                   format_func=lambda v: f"{v} — {CATALOGUE[v]['titre']}",
                                   key=f"cid_{identifiant}_{i}")
                repere = st.text_input("Repère", value=proposition["repere"], key=f"rep_{identifiant}_{i}")
                cotes = {}
                for nom, titre, defaut, mini, maxi in CATALOGUE[cid]["cotes"]:
                    if nom not in proposition["cotes"]:
                        st.warning(f"{titre} non reconnu : mesure à compléter manuellement.")
                    cotes[nom] = st.number_input(
                        titre, min_value=float(mini), max_value=float(maxi),
                        value=float(proposition["cotes"].get(nom, defaut)), step=1.0,
                        key=f"cot_{identifiant}_{i}_{cid}_{nom}",
                    )
                if cid == "CID 1":
                    if proposition["longueur_mm"] is None:
                        st.warning("Longueur d'une gaine non reconnue : à compléter.")
                    longueur = st.number_input("Longueur unitaire (mm)", min_value=1.0,
                                              value=float(proposition["longueur_mm"] or 1500),
                                              step=10.0, key=f"long_{identifiant}_{i}")
                else:
                    longueur = None
                if proposition["quantite"] is None:
                    st.warning("Quantité non reconnue : à vérifier.")
                nombre = st.number_input("Quantité de pièces", min_value=1, max_value=10000,
                                         value=proposition["quantite"] or 1,
                                         key=f"nb_{identifiant}_{i}")
                con1 = st.selectbox("Connecteur côté 1", CONNECTEURS, key=f"co1_{identifiant}_{i}")
                con2 = "" if cid in UN_SEUL_CONNECTEUR else st.selectbox(
                    "Connecteur côté 2", CONNECTEURS, key=f"co2_{identifiant}_{i}")
                for incertitude in proposition["incertitudes"]:
                    st.warning(incertitude)
                revues.append({"inclure": inclure, "repere": repere.strip(), "cid": cid,
                               "cotes": cotes, "con1": con1, "con2": con2,
                               "qte": (nombre * longueur / 1000 if cid == "CID 1" else nombre)})
        valide = st.checkbox("J'ai vérifié chaque dessin, les pièces oubliées, les dimensions et les quantités.",
                            key=f"tout_verifie_{identifiant}")
        ajouter = st.form_submit_button("Valider les pièces sélectionnées", type="primary", disabled=not valide)
    if not ajouter:
        return
    retenues = [x for x in revues if x["inclure"]]
    reperes = [x["repere"].casefold() for x in retenues]
    existants = [l["repere"].casefold() for l in st.session_state.panier] if user else []
    if not retenues or any(not x for x in reperes) or len(set(reperes + existants)) != len(reperes + existants):
        st.error("Sélection vide, repère manquant ou repères dupliqués. Corrigez les repères.")
        return
    if any(x["cid"] == "CID 999" for x in retenues):
        st.error("Les pièces non identifiées (CID 999) doivent être traitées manuellement.")
        return
    # Fichiers distincts : l'envoi standard déplace chaque croquis dans le dossier de commande.
    if isinstance(fichier, (str, os.PathLike)):
        extension = os.path.splitext(str(fichier))[1].lower()
        photo_initiale = os.path.join(DOSSIER_UPLOADS, f"listing_ia_{uuid.uuid4().hex}{extension}")
        shutil.copy2(fichier, photo_initiale)
    else:
        photo_initiale = enregistrer_image(fichier, DOSSIER_UPLOADS, "listing_ia")
    lignes = []
    for numero, item in enumerate(retenues):
        photo = photo_initiale
        if numero:
            ext = os.path.splitext(photo_initiale)[1]
            photo = os.path.join(DOSSIER_UPLOADS, f"listing_ia_{uuid.uuid4().hex}{ext}")
            shutil.copy2(photo_initiale, photo)
        lignes.append({"repere": item["repere"], "cid": item["cid"], "cotes": item["cotes"],
                       "epaisseur": EPAISSEUR_DEFAUT, "con1": item["con1"], "con2": item["con2"],
                       "qte": item["qte"], "photo": photo})
    if user:
        st.session_state.panier.extend(lignes)
        st.session_state.panier_chantier = chantier
        enregistrer_brouillon_courant()
        st.session_state.flash = f"{len(lignes)} pièces vérifiées ajoutées au panier."
    else:
        # Conversion atelier en une nouvelle commande, sans écraser le listing photo original.
        reference = f"CMD-IA-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
        # L'appelant atelier fournit l'identité propriétaire via chantier (dictionnaire).
        donnees = chantier
        commandes = [dict(id=uuid.uuid4().hex[:8], commande_id=reference,
                          entreprise=donnees["entreprise"], email=donnees["email"],
                          chantier=donnees["chantier"], date=datetime.now().strftime("%d/%m/%Y %H:%M"),
                          **ligne) for ligne in lignes]
        DB.ajouter_commandes(commandes)
        DB.ajouter_audit("atelier", "listing_ia_converti", reference)
        st.toast(f"Commande {reference} créée après validation.")
    st.session_state.pop(cle, None)
    st.rerun()


ADMIN_PASSWORD = _reglage("ADMIN_PASSWORD")


def envoyer_alerte(sujet, corps, pieces_jointes=(), destinataire=None):
    """Envoie un e-mail à l'atelier en arrière-plan. Une erreur n'est JAMAIS visible
    du client : elle est seulement signalée dans l'espace atelier."""
    cfg = {k: _reglage(k) for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD")}
    destinataire = destinataire or _reglage("MAIL_TO", MAIL_TO_DEFAUT)

    def _envoi():
        try:
            if not (cfg["SMTP_HOST"] and cfg["SMTP_USER"] and cfg["SMTP_PASSWORD"]):
                raise RuntimeError("SMTP non configuré (voir réglages SMTP_* dans app.py)")
            msg = EmailMessage()
            msg["Subject"], msg["From"], msg["To"] = sujet, cfg["SMTP_USER"], destinataire
            msg.set_content(corps)
            for chemin in pieces_jointes:
                if os.path.exists(chemin):
                    type_mime = mimetypes.guess_type(chemin)[0] or "application/octet-stream"
                    principal, secondaire = type_mime.split("/")
                    with open(chemin, "rb") as f:
                        msg.add_attachment(f.read(), maintype=principal, subtype=secondaire,
                                           filename=os.path.basename(chemin))
            port = int(cfg["SMTP_PORT"] or 465)
            if port == 465:
                srv = smtplib.SMTP_SSL(cfg["SMTP_HOST"], port, timeout=20)
            else:
                srv = smtplib.SMTP(cfg["SMTP_HOST"], port, timeout=20)
                srv.starttls()
            with srv:
                srv.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
                srv.send_message(msg)
        except Exception as e:   # noqa: BLE001 - on mémorise l'erreur pour l'atelier
            DB.ajouter_erreur_mail(f"{datetime.now():%d/%m %H:%M} - {sujet} : {e}")

    threading.Thread(target=_envoi, daemon=True).start()


def tester_connexion_smtp():
    """Teste la connexion et l'authentification sans envoyer de message."""
    cfg = {k: _reglage(k) for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD")}
    if not (cfg["SMTP_HOST"] and cfg["SMTP_USER"] and cfg["SMTP_PASSWORD"]):
        raise RuntimeError("Configuration SMTP incomplète")
    port = int(cfg["SMTP_PORT"] or 465)
    if port == 465:
        serveur = smtplib.SMTP_SSL(cfg["SMTP_HOST"], port, timeout=20)
    else:
        serveur = smtplib.SMTP(cfg["SMTP_HOST"], port, timeout=20)
        serveur.starttls()
    with serveur:
        serveur.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
        serveur.noop()


def init_session():
    for k, v in dict(user_email=None, admin=False, panier=[], form_n=0,
                     panier_chantier=None, piece=None, ia={}, listing_n=0,
                     commande_envoyee=None, echecs_client=0,
                     client_blocage_jusqua=0.0, echecs_admin=0,
                     admin_blocage_jusqua=0.0, navigation_client="Commander",
                     mode_commande="Saisie guidée", revue_commande=False,
                     commande_verifiee=False).items():
        st.session_state.setdefault(k, v)


def fichier_asset(nom):
    chemin = os.path.join(DOSSIER_ASSETS, nom)
    return chemin if os.path.exists(chemin) else None


def image_piece(cid):
    """Vue 3D d'une pièce : CID 95-G -> cid95g.png (ou cid95-g.png), sinon None."""
    base = cid.lower().replace(" ", "")
    return fichier_asset(base.replace("-", "") + ".png") or fichier_asset(base + ".png")


# Couleur des badges de statut (atelier et client)
COULEURS_STATUT = {
    "Reçue": "orange",
    "En préparation": "blue",
    "En fabrication": "violet",
    "Prête": "green",
    "Terminée": "green",
}


def afficher_logo(sidebar=False):
    cible = st.sidebar if sidebar else st
    logo = fichier_asset("logo.png")
    if logo:
        cible.image(logo, width=180 if sidebar else 260, alt="Logo CLIMAIR")
    else:
        cible.markdown("# CLIMAIR" if not sidebar else "## CLIMAIR")


def afficher_page_sans_erreur(fonction):
    """Affiche une page sans révéler une trace technique aux clients."""
    try:
        fonction()
    except Exception as erreur:  # noqa: BLE001 - frontière UI volontaire
        acteur = "atelier" if st.session_state.get("admin") else (
            st.session_state.get("user_email") or "visiteur"
        )
        DB.ajouter_audit(acteur, "erreur_application", type(erreur).__name__)
        st.error(
            "Une erreur inattendue est survenue. Vos données déjà enregistrées sont "
            "conservées. Vous pouvez actualiser la page ou contacter l’atelier.",
            icon=":material/error:",
        )
        if st.session_state.get("admin"):
            with st.expander("Détail technique"):
                st.code(f"{type(erreur).__name__}: {erreur}")


# --------------------------------------------------------------------------
# CLIENT : CONNEXION / INSCRIPTION
# --------------------------------------------------------------------------
def page_auth():
    st.header("Votre espace client", icon=":material/account_circle:")
    st.caption(
        "Préparez vos pièces de ventilation, envoyez vos croquis et suivez vos "
        "commandes auprès de l’atelier."
    )
    tab_in, tab_new, tab_verify, tab_reset = st.tabs(
        [
            "Se connecter",
            "Créer un compte",
            "Vérifier mon email",
            "Mot de passe oublié",
        ]
    )

    with tab_in:
        st.caption("Connectez-vous pour reprendre vos commandes et consulter leur suivi.")
        secondes_restantes = max(
            0, int(st.session_state.client_blocage_jusqua - time.time())
        )
        with st.form("login"):
            email = st.text_input("Adresse email").strip().lower()
            mdp = st.text_input("Mot de passe", type="password")
            ok = st.form_submit_button(
                "Se connecter", icon=":material/login:", width="stretch",
                disabled=secondes_restantes > 0,
            )
        if secondes_restantes:
            st.warning(
                f"Trop de tentatives. Réessayez dans {secondes_restantes} seconde(s)."
            )
        if ok:
            u = STORE["users"].get(email)
            iterations = int(u.get("iterations", 100_000)) if u else ITERATIONS_MDP
            hash_valide = u and verifier_mdp(
                mdp, u["sel"], u["hash"], iterations
            )
            if not hash_valide:
                DB.ajouter_audit(email or "inconnu", "connexion_refusee")
                st.session_state.echecs_client += 1
                if st.session_state.echecs_client >= 5:
                    st.session_state.client_blocage_jusqua = time.time() + 30
                    st.session_state.echecs_client = 0
                st.error("Email ou mot de passe incorrect.")
            elif not u.get("email_verifie", True):
                st.warning(
                    "Votre adresse email n’est pas encore vérifiée. Utilisez l’onglet "
                    "« Vérifier mon email ».",
                    icon=":material/mark_email_unread:",
                )
            elif u["statut"] == "En attente":
                st.warning("Votre compte CLIMAIR a bien été créé mais il est en cours de "
                           "validation par notre atelier. Vous pourrez commander dès qu'un "
                           "administrateur l'aura activé.", icon=":material/schedule:")
            elif u["statut"] != "Actif":
                st.error("Votre compte est suspendu. Contactez l'atelier.")
            else:
                DB.ajouter_audit(email, "connexion_reussie")
                st.session_state.echecs_client = 0
                st.session_state.client_blocage_jusqua = 0.0
                if iterations < ITERATIONS_MDP:
                    u["hash"] = hash_mdp(mdp, u["sel"])
                    u["iterations"] = ITERATIONS_MDP
                    DB.mettre_a_jour_utilisateur(u)
                brouillon = DB.charger_brouillon(email)
                if brouillon:
                    st.session_state.panier = brouillon["panier"]
                    st.session_state.panier_chantier = brouillon["chantier"]
                    st.session_state.flash = "Votre panier sauvegardé a été restauré."
                st.session_state.user_email = email
                st.session_state.navigation_client = "Commander"
                st.session_state.revue_commande = False
                st.rerun()

    with tab_new:
        st.caption("Votre accès sera disponible après validation de l’entreprise par l’atelier.")
        with st.form("signup"):
            c1, c2 = st.columns(2)
            email = c1.text_input("Adresse email *").strip().lower()
            mdp = c2.text_input(
                "Mot de passe *",
                type="password",
                help=(
                    "12 caractères minimum avec minuscule, majuscule, chiffre "
                    "et caractère spécial."
                ),
            )
            nom = c1.text_input("Nom *")
            prenom = c2.text_input("Prénom *")
            entreprise = c1.text_input("Entreprise *")
            siret = c2.text_input("Numéro de SIRET *")
            adresse = st.text_area("Adresse postale *")
            ok = st.form_submit_button(
                "Créer mon compte", icon=":material/person_add:", width="stretch"
            )
        if ok:
            siret_net = siret.replace(" ", "")
            if not all(x.strip() for x in (email, mdp, nom, prenom, entreprise, siret, adresse)):
                st.error("Merci de remplir tous les champs obligatoires (*).")
            elif "@" not in email:
                st.error("Adresse email invalide.")
            elif not mot_de_passe_robuste(mdp):
                st.error(
                    "Le mot de passe doit contenir au moins 12 caractères, avec une "
                    "minuscule, une majuscule, un chiffre et un caractère spécial."
                )
            elif not (siret_net.isdigit() and len(siret_net) == 14):
                st.error("Le SIRET doit comporter 14 chiffres.")
            elif email in STORE["users"]:
                st.error("Un compte existe déjà avec cet email.")
            else:
                sel = uuid.uuid4().hex
                utilisateur = dict(
                    email=email, sel=sel, hash=hash_mdp(mdp, sel), nom=nom, prenom=prenom,
                    entreprise=entreprise, siret=siret_net, adresse=adresse,
                    statut="En attente", email_verifie=False, iterations=ITERATIONS_MDP,
                    cree_le=datetime.now().strftime("%d/%m/%Y %H:%M"))
                DB.ajouter_utilisateur(utilisateur)
                code = f"{pysecrets.randbelow(900000) + 100000}"
                code_hash = hashlib.sha256(f"{email}:{code}".encode()).hexdigest()
                DB.enregistrer_code_verification(email, code_hash)
                envoyer_alerte(
                    "Vérifiez votre adresse email CLIMAIR",
                    f"Votre code de vérification est : {code}\nIl est valable 30 minutes.",
                    destinataire=email,
                )
                st.success(
                    "Compte créé. Un code de vérification vient de vous être envoyé, "
                    "puis l’atelier devra valider votre entreprise.",
                    icon=":material/check_circle:",
                )

    with tab_verify:
        st.caption("Saisissez le code reçu par email. Il reste valable 30 minutes.")
        with st.form("verification_email"):
            email_verification = st.text_input(
                "Adresse email", type="email", key="email_verification"
            )
            code_verification = st.text_input("Code de vérification", max_chars=6)
            verifier = st.form_submit_button(
                "Vérifier mon adresse", icon=":material/verified:", width="stretch"
            )
        if verifier:
            email_verification = email_verification.strip().lower()
            empreinte = hashlib.sha256(
                f"{email_verification}:{code_verification.strip()}".encode()
            ).hexdigest()
            utilisateur = DB.snapshot()["users"].get(email_verification)
            if not utilisateur or not DB.verifier_code_email(email_verification, empreinte):
                st.error("Code invalide ou expiré.")
            else:
                utilisateur["email_verifie"] = True
                DB.mettre_a_jour_utilisateur(utilisateur)
                DB.ajouter_audit(email_verification, "email_verifie")
                st.success(
                    "Adresse vérifiée. Votre accès sera ouvert après validation par l’atelier.",
                    icon=":material/check_circle:",
                )
        with st.form("renvoi_verification"):
            email_renvoi = st.text_input(
                "Adresse email", type="email", key="email_renvoi_verification"
            )
            renvoyer = st.form_submit_button(
                "Renvoyer un code", icon=":material/forward_to_inbox:", width="stretch"
            )
        if renvoyer:
            email_renvoi = email_renvoi.strip().lower()
            utilisateur = DB.snapshot()["users"].get(email_renvoi)
            if utilisateur and not utilisateur.get("email_verifie", True):
                code = f"{pysecrets.randbelow(900000) + 100000}"
                empreinte = hashlib.sha256(f"{email_renvoi}:{code}".encode()).hexdigest()
                DB.enregistrer_code_verification(email_renvoi, empreinte)
                envoyer_alerte(
                    "Vérifiez votre adresse email CLIMAIR",
                    f"Votre code de vérification est : {code}\nIl est valable 30 minutes.",
                    destinataire=email_renvoi,
                )
            st.success("Si nécessaire, un nouveau code vient d’être envoyé.")

    with tab_reset:
        st.caption("Un code valable 15 minutes vous sera envoyé par email.")
        with st.form("demande_reset"):
            email_reset = st.text_input("Adresse email", type="email", key="email_reset")
            demander = st.form_submit_button(
                "Envoyer un code", icon=":material/mail:", width="stretch"
            )
        if demander:
            email_reset = email_reset.strip().lower()
            utilisateur = STORE["users"].get(email_reset)
            if utilisateur:
                code = f"{pysecrets.randbelow(900000) + 100000}"
                code_hash = hashlib.sha256(f"{email_reset}:{code}".encode()).hexdigest()
                DB.enregistrer_code_reset(email_reset, code_hash)
                envoyer_alerte(
                    "Votre code de réinitialisation CLIMAIR",
                    f"Votre code est : {code}\nIl est valable 15 minutes.",
                    destinataire=email_reset,
                )
            st.success(
                "Si cette adresse est enregistrée, un code vient d’être envoyé.",
                icon=":material/check_circle:",
            )
        with st.form("validation_reset"):
            email_code = st.text_input("Adresse email", type="email", key="email_code")
            code = st.text_input("Code reçu", max_chars=6)
            nouveau = st.text_input("Nouveau mot de passe", type="password")
            confirmation = st.text_input("Confirmer le mot de passe", type="password")
            reinitialiser = st.form_submit_button(
                "Changer le mot de passe", icon=":material/password:", width="stretch"
            )
        if reinitialiser:
            email_code = email_code.strip().lower()
            code_hash = hashlib.sha256(f"{email_code}:{code.strip()}".encode()).hexdigest()
            utilisateur = STORE["users"].get(email_code)
            if not utilisateur or not DB.verifier_code_reset(email_code, code_hash):
                st.error("Code invalide ou expiré.")
            elif nouveau != confirmation:
                st.error("Les deux mots de passe sont différents.")
            elif not mot_de_passe_robuste(nouveau):
                st.error(
                    "Utilisez au moins 12 caractères avec minuscule, majuscule, "
                    "chiffre et caractère spécial."
                )
            else:
                sel = uuid.uuid4().hex
                utilisateur.update(
                    sel=sel, hash=hash_mdp(nouveau, sel), iterations=ITERATIONS_MDP
                )
                DB.mettre_a_jour_utilisateur(utilisateur)
                DB.ajouter_audit(email_code, "mot_de_passe_reinitialise")
                st.success("Mot de passe modifié. Vous pouvez maintenant vous connecter.")


# --------------------------------------------------------------------------
# CLIENT : CATALOGUE EN GRILLE
# --------------------------------------------------------------------------
def grille_pieces(cids=None):
    """Grille visuelle : vue 3D de la pièce (ou emoji si image absente) + bouton."""
    st.subheader("Choisissez une pièce", icon=":material/category:")
    cids = list(CATALOGUE) if cids is None else list(cids)
    par_ligne = 3   # 3 cases par ligne, comme la maquette smartphone
    with st.container(key="grille"):
        for debut in range(0, len(cids), par_ligne):
            cols = st.columns(par_ligne)
            for col, cid in zip(cols, cids[debut:debut + par_ligne]):
                cat = CATALOGUE[cid]
                with col, st.container(border=True):
                    img = image_piece(cid)
                    choisi = st.session_state.piece == cid
                    if img:
                        st.image(img, width="stretch", alt=f"Vue 3D {cat['titre']}")
                        libelle = f"{cid} {cat['titre']}"
                    else:   # sans image : emoji dans le bouton
                        libelle = f"{cid} {cat['titre']} {cat['emoji']}"
                    if st.button(libelle, key=f"pc_{cid}", help=cat["titre"],
                                 type="primary" if choisi else "secondary",
                                 width="stretch"):
                        st.session_state.piece = cid
                        st.rerun()


def _nom_sur(texte):
    """Nom utilisable dans un nom de dossier/fichier."""
    return "".join(c if c.isalnum() else "_" for c in texte.strip()).strip("_") or "x"


def creer_dossier_envoi(user, chantier):
    """Crée uploads/<client>_<chantier>_<date d'envoi>/ (un dossier par envoi)."""
    base = f"{_nom_sur(user['entreprise'])}_{_nom_sur(chantier)}_{datetime.now():%Y-%m-%d_%Hh%M}"
    dossier, i = os.path.join(DOSSIER_UPLOADS, base), 2
    while os.path.exists(dossier):          # deux envois dans la même minute
        dossier = os.path.join(DOSSIER_UPLOADS, f"{base}_{i}")
        i += 1
    os.makedirs(dossier)
    return dossier


def depot_listing(user, chantier):
    """Dépôt global : le client photographie son listing au lieu de saisir pièce par pièce."""
    st.subheader("Déposer un listing", icon=":material/upload_file:")
    st.caption(
        "Photographiez votre listing : l’atelier le traitera sans saisie pièce par pièce."
    )
    n = st.session_state.listing_n
    photos = st.file_uploader(
        "Photo(s) du listing (appareil photo ou photothèque)",
        type=["png", "jpg", "jpeg", "heic", "heif"],
        accept_multiple_files=True,
        key=f"lst{n}",
        help="Sur iPhone, touchez Parcourir puis Prendre une photo.",
    )
    commentaire = st.text_area("Commentaire (optionnel)", key=f"lstc{n}")
    if st.button(
        "Envoyer le listing à l’atelier",
        key=f"lsto{n}",
        icon=":material/send:",
        type="primary",
        width="stretch",
    ):
        if not photos:
            st.error("Ajoutez au moins une photo.")
            return
        dossier = creer_dossier_envoi(user, chantier)
        chemins = []
        for i, ph in enumerate(photos, 1):
            try:
                chemins.append(enregistrer_image(ph, dossier, f"listing_{i}"))
            except ValueError as exc:
                st.error(str(exc))
                return
        with open(os.path.join(dossier, "listing.txt"), "w", encoding="utf-8") as f:
            f.write(f"Client : {user['entreprise']} ({user['email']})\n"
                    f"Chantier : {chantier}\n"
                    f"Date d'envoi : {datetime.now():%d/%m/%Y %H:%M}\n"
                    f"Commentaire : {commentaire.strip() or '-'}\n"
                    f"Photos : {', '.join(os.path.basename(c) for c in chemins)}\n")
        listing = dict(
            id=uuid.uuid4().hex[:8], entreprise=user["entreprise"], email=user["email"],
            chantier=chantier, date=datetime.now().strftime("%d/%m/%Y %H:%M"),
            photos=chemins, commentaire=commentaire.strip(), poids_kg=0.0)
        DB.ajouter_listing(listing)
        envoyer_alerte(
            f"[CLIMAIR] Nouveau listing - {user['entreprise']} - {chantier}",
            f"Chantier : {chantier}\nEntreprise : {user['entreprise']}\n"
            f"Date : {datetime.now():%d/%m/%Y %H:%M}\n"
            f"Commentaire : {commentaire.strip() or '-'}\n"
            f"Photos jointes : {len(chemins)}\nPoids estimé : à saisir par l'atelier",
            chemins)
        st.session_state.listing_n += 1
        st.session_state.commande_envoyee = (
            f"Listing {listing['id'].upper()} envoyé à l'atelier."
        )
        st.rerun()


def afficher_progression_commande(chantier):
    """Les quatre étapes de la maquette : Chantier, Pièces, Vérification, Envoi."""
    if not chantier.strip():
        courante = 0
    elif st.session_state.revue_commande:
        courante = 2
    else:
        courante = 1
    with st.container(horizontal=True, wrap=False, gap="xsmall"):
        for i, nom in enumerate(["Chantier", "Pièces", "Vérification", "Envoi"]):
            # Barre pleine pour les étapes faites ou en cours, vide pour les suivantes
            st.progress(
                1.0 if i <= courante else 0.0,
                text=f"**{nom}**" if i == courante else nom,
            )


def resume_quantites_panier(panier):
    unites = sum(l["qte"] for l in panier if l["cid"] != "CID 1")
    ml_gaine = sum(l["qte"] for l in panier if l["cid"] == "CID 1")
    parties = [f"{int(unites)} unité(s)"] if unites else []
    if ml_gaine:
        parties.append(f"{ml_gaine:g} ml de gaine")
    return " · ".join(parties) or "aucune quantité"


def enregistrer_brouillon_courant():
    if st.session_state.panier and st.session_state.panier_chantier:
        DB.enregistrer_brouillon(
            st.session_state.user_email,
            st.session_state.panier_chantier,
            st.session_state.panier,
        )
    elif st.session_state.user_email:
        DB.supprimer_brouillon(st.session_state.user_email)


def barre_panier():
    panier = st.session_state.panier
    if not panier:
        return
    with st.bottom:
        with st.container(horizontal=True, vertical_alignment="center"):
            st.markdown(
                f"**Panier · {len(panier)} ligne(s)**  \n{resume_quantites_panier(panier)}"
            )
            if st.button(
                "Vérifier la commande",
                type="primary",
                icon=":material/shopping_cart_checkout:",
                key="ouvrir_verification",
            ):
                st.session_state.revue_commande = True
                st.session_state.piece = None
                st.rerun()


def configurer_piece(user, chantier, cid):
    n = st.session_state.form_n
    cat = CATALOGUE[cid]
    if st.button("Retour au catalogue", icon=":material/arrow_back:"):
        st.session_state.piece = None
        st.rerun()
    st.subheader(f"{cat['titre']}", icon=":material/tune:")
    st.badge(cid, color="blue")
    img = image_piece(cid)
    if img:
        with st.container(border=True, horizontal_alignment="center"):
            st.image(img, width=280, alt=f"Vue 3D {cat['titre']}")

    pris = {l["repere"] for l in st.session_state.panier}
    suivant = 1
    while str(suivant) in pris:
        suivant += 1
    with st.form(f"configuration_{cid}_{n}"):
        repere = st.text_input(
            "Numéro de repère *",
            value=str(suivant),
            icon=":material/tag:",
            help="Identifiant visible sur le plan ou le listing.",
            key=f"rep{n}_{len(st.session_state.panier)}",
        )
        cotes = {}
        if cat["cotes"]:
            cols = st.columns(min(len(cat["cotes"]), 2))
            for i, (cle, lib, defaut, mini, maxi) in enumerate(cat["cotes"]):
                cotes[cle] = cols[i % len(cols)].number_input(
                    lib, min_value=float(mini), max_value=float(maxi),
                    value=float(defaut), step=1.0, key=f"{cid}{cle}{n}")
        else:
            st.info("Pièce sur-mesure : joignez obligatoirement un croquis.")

        st.subheader("Connecteurs et configuration", icon=":material/settings:")
        if cid in UN_SEUL_CONNECTEUR:
            con1 = st.selectbox("Connecteur (côté unique)", CONNECTEURS, key=f"c1{n}")
            con2 = ""
        else:
            con1 = st.selectbox("Connecteur côté 1 (entrée)", CONNECTEURS, key=f"c1{n}")
            con2 = st.selectbox("Connecteur côté 2 (sortie)", CONNECTEURS, key=f"c2{n}")
        if cid == "CID 1":
            qte = st.number_input(
                "Longueur souhaitée (ml)", min_value=0.1, value=1.0,
                step=0.5, key=f"q{n}{cid}")
        else:
            qte = st.number_input(
                "Quantité souhaitée (unités)", min_value=1, value=1,
                step=1, key=f"q{n}{cid}")
        photo = st.file_uploader(
            "Photo ou croquis" + (" *" if cid == "CID 999" else " (optionnel)"),
            type=["png", "jpg", "jpeg", "heic", "heif"], key=f"ph{n}",
            help="Sur iPhone, touchez Parcourir puis Prendre une photo ou Photothèque.",
        )
        # Plus de caméra intégrée : la photo passe par le bouton ci-dessus
        # (sur téléphone il propose déjà « Prendre une photo »).
        photo_camera = None
        ajouter = st.form_submit_button(
            "Ajouter au panier",
            type="primary",
            icon=":material/add_shopping_cart:",
            width="stretch",
        )

    if not ajouter:
        barre_panier()
        return
    if not repere.strip():
        st.error("Le numéro de repère est obligatoire.")
    elif any(
        ligne["repere"].casefold() == repere.strip().casefold()
        for ligne in st.session_state.panier
    ):
        st.error("Ce numéro de repère est déjà utilisé dans le panier.")
    elif cid == "CID 999" and not (photo or photo_camera):
        st.error("Pour une pièce sur-mesure, le croquis est obligatoire.")
    else:
        chemin = None
        source_photo = photo or photo_camera
        if source_photo:
            try:
                chemin = enregistrer_image(source_photo, DOSSIER_UPLOADS, "croquis")
            except ValueError as exc:
                st.error(str(exc))
                barre_panier()
                return
        st.session_state.panier.append(dict(
            repere=repere.strip(), cid=cid, cotes=cotes, epaisseur=EPAISSEUR_DEFAUT,
            con1=con1, con2=con2,
            qte=float(qte) if cid == "CID 1" else int(qte), photo=chemin))
        st.session_state.panier_chantier = chantier.strip()
        st.session_state.commande_verifiee = False
        st.session_state.form_n += 1
        st.session_state.piece = None
        st.session_state.flash = f"Pièce {repere.strip()} ({cid}) ajoutée au panier."
        enregistrer_brouillon_courant()
        st.rerun()
    barre_panier()


@st.dialog("Modifier la pièce", icon=":material/edit:")
def modifier_piece_panier(index):
    ligne = st.session_state.panier[index]
    cat = CATALOGUE[ligne["cid"]]
    st.caption(f"{ligne['cid']} · {cat['titre']}")
    if ligne.get("photo") and os.path.exists(ligne["photo"]):
        st.image(ligne["photo"], width=220, alt=f"Croquis du repère {ligne['repere']}")
    with st.form(f"edition_piece_{index}"):
        repere = st.text_input("Numéro de repère", value=ligne["repere"])
        cotes = {}
        if cat["cotes"]:
            colonnes = st.columns(min(2, len(cat["cotes"])))
            for i, (cle, libelle, defaut, mini, maxi) in enumerate(cat["cotes"]):
                cotes[cle] = colonnes[i % len(colonnes)].number_input(
                    libelle, min_value=float(mini), max_value=float(maxi),
                    value=float(ligne["cotes"].get(cle, defaut)), step=1.0,
                )
        if ligne["cid"] in UN_SEUL_CONNECTEUR:
            con1 = st.selectbox(
                "Connecteur", CONNECTEURS,
                index=CONNECTEURS.index(ligne["con1"]) if ligne["con1"] in CONNECTEURS else 0,
            )
            con2 = ""
        else:
            con1 = st.selectbox(
                "Connecteur côté 1", CONNECTEURS,
                index=CONNECTEURS.index(ligne["con1"]) if ligne["con1"] in CONNECTEURS else 0,
            )
            con2 = st.selectbox(
                "Connecteur côté 2", CONNECTEURS,
                index=CONNECTEURS.index(ligne["con2"]) if ligne["con2"] in CONNECTEURS else 0,
            )
        if ligne["cid"] == "CID 1":
            qte = st.number_input(
                "Longueur (ml)", min_value=0.1, value=float(ligne["qte"]), step=0.5
            )
        else:
            qte = st.number_input(
                "Quantité", min_value=1, value=int(ligne["qte"]), step=1
            )
        nouvelle_photo = st.file_uploader(
            "Remplacer le croquis", type=["png", "jpg", "jpeg", "heic", "heif"],
            key=f"edition_photo_{index}",
            help="Sur iPhone, touchez Parcourir puis Prendre une photo ou Photothèque.",
        )
        nouvelle_camera = None
        supprimer_photo = st.checkbox("Supprimer le croquis actuel")
        enregistrer = st.form_submit_button(
            "Enregistrer les modifications", type="primary",
            icon=":material/save:", width="stretch",
        )
    if not enregistrer:
        return
    if not repere.strip():
        st.error("Le numéro de repère est obligatoire.")
        return
    if any(
        i != index and autre["repere"].casefold() == repere.strip().casefold()
        for i, autre in enumerate(st.session_state.panier)
    ):
        st.error("Ce numéro de repère est déjà utilisé.")
        return
    photo = None if supprimer_photo else ligne.get("photo")
    source = nouvelle_photo or nouvelle_camera
    if source:
        try:
            photo = enregistrer_image(source, DOSSIER_UPLOADS, "croquis")
        except ValueError as exc:
            st.error(str(exc))
            return
    if ligne["cid"] == "CID 999" and not photo:
        st.error("Le croquis reste obligatoire pour une pièce sur-mesure.")
        return
    ligne.update(
        repere=repere.strip(), cotes=cotes, con1=con1, con2=con2,
        qte=float(qte) if ligne["cid"] == "CID 1" else int(qte), photo=photo,
    )
    st.session_state.commande_verifiee = False
    enregistrer_brouillon_courant()
    st.rerun()


def afficher_lignes_panier():
    panier = st.session_state.panier
    for i, ligne in enumerate(panier):
        titre = CATALOGUE[ligne["cid"]]["titre"]
        with st.container(border=True):
            # Carte : vue 3D à gauche, repère / pièce / cotes / connecteurs à droite
            with st.container(horizontal=True, wrap=False, vertical_alignment="top"):
                img = image_piece(ligne["cid"])
                if img:
                    st.image(img, width=72, alt=f"Vue 3D {titre}")
                with st.container(gap="xxsmall"):
                    with st.container(horizontal=True, wrap=False):
                        st.markdown(f"**{ligne['repere']} · {titre}**")
                        st.markdown(f"**{fmt_qte(ligne)}**", width="content")
                    dims = " · ".join(f"{k} {v:g}" for k, v in ligne["cotes"].items()) or "sur-mesure"
                    st.caption(
                        f"{ligne['cid']} · {dims}"
                        f"{' · croquis joint' if ligne['photo'] else ''}"
                    )
                    st.caption(
                        ligne["con1"] + (f" / {ligne['con2']}" if ligne["con2"] else "")
                    )
            with st.container(horizontal=True):
                modifier = st.button(
                    "Modifier", key=f"edit{i}", icon=":material/edit:",
                    help=f"Modifier le repère {ligne['repere']}",
                )
                supprimer = st.button(
                    "Supprimer", key=f"del{i}", icon=":material/delete:",
                    help=f"Retirer le repère {ligne['repere']}", type="tertiary",
                )
            if modifier:
                modifier_piece_panier(i)
            if supprimer:
                panier.pop(i)
                st.session_state.commande_verifiee = False
                if not panier:
                    st.session_state.panier_chantier = None
                    st.session_state.revue_commande = False
                enregistrer_brouillon_courant()
                st.rerun()


def envoyer_commande(user, chantier):
    panier = st.session_state.panier
    dossier = creer_dossier_envoi(user, chantier)
    for ligne in panier:
        if ligne["photo"] and os.path.exists(ligne["photo"]):
            cible = os.path.join(
                dossier, f"{_nom_sur(ligne['repere'])}_{os.path.basename(ligne['photo'])}"
            )
            shutil.move(ligne["photo"], cible)
            ligne["photo"] = cible
    with open(os.path.join(dossier, "listing_pieces.txt"), "w", encoding="utf-8") as f:
        f.write(f"Client : {user['entreprise']} ({user['email']})\n"
                f"Chantier : {chantier}\n"
                f"Date d'envoi : {datetime.now():%d/%m/%Y %H:%M}\n\n"
                + "\n".join(resume_piece(ligne) for ligne in panier) + "\n")
    texte_cam, _ = script_camduct(chantier, user["entreprise"], panier)
    with open(os.path.join(dossier, "export_camduct.txt"), "wb") as f:
        f.write(texte_cam.encode("cp1252", errors="replace"))
    commande_id = f"CMD-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
    commandes = [dict(
        id=uuid.uuid4().hex[:8], entreprise=user["entreprise"], email=user["email"],
        chantier=chantier, commande_id=commande_id,
        date=datetime.now().strftime("%d/%m/%Y %H:%M"), **ligne,
    ) for ligne in panier]
    DB.ajouter_commandes(commandes)
    envoyer_alerte(
        f"[CLIMAIR] {commande_id} - {user['entreprise']} - {chantier}",
        f"NOUVELLE COMMANDE {commande_id}\n\n"
        f"Société  : {user['entreprise']}\n"
        f"Client   : {user['prenom']} {user['nom']} ({user['email']})\n"
        f"Chantier : {chantier}\nDate : {datetime.now():%d/%m/%Y %H:%M}\n\n"
        f"Pièces ({len(panier)} ligne(s)) :\n"
        + "\n".join(f" - {resume_piece(ligne)}" for ligne in panier),
        [ligne["photo"] for ligne in panier if ligne["photo"]],
    )
    DB.supprimer_brouillon(user["email"])
    st.session_state.panier = []
    st.session_state.panier_chantier = None
    st.session_state.revue_commande = False
    st.session_state.commande_envoyee = f"Commande {commande_id} envoyée à l’atelier."
    st.session_state.reinitialiser_commande = True
    st.rerun()


def page_verification(user, chantier):
    if st.button("Continuer à ajouter des pièces", icon=":material/arrow_back:"):
        st.session_state.revue_commande = False
        st.rerun()
    st.header("Vérification de la commande", icon=":material/fact_check:")
    panier = st.session_state.panier
    if not panier:
        st.info("Le panier est vide.", icon=":material/shopping_cart:")
        return
    with st.container(border=True):
        st.caption(":material/apartment: Chantier")
        st.markdown(f"**{chantier}**")
    with st.container(horizontal=True, vertical_alignment="bottom"):
        st.markdown(f"**{len(panier)} ligne(s)**")
        st.caption(resume_quantites_panier(panier), width="content")
    afficher_lignes_panier()
    if st.button("Ajouter une pièce", icon=":material/add:", width="stretch",
                 key="ajouter_piece_verification"):
        st.session_state.revue_commande = False
        st.rerun()
    with st.container(border=True):
        confirmer = st.checkbox(
            "J’ai vérifié le chantier, les repères, les dimensions et les quantités.",
            key="commande_verifiee",
        )
        if st.button(
            "Envoyer la commande", type="primary", icon=":material/send:",
            width="stretch", disabled=not confirmer,
        ):
            envoyer_commande(user, chantier)
        st.caption(
            "L’atelier reçoit la commande et vos croquis immédiatement.",
            text_alignment="center",
        )


def page_catalogue():
    user = STORE["users"][st.session_state.user_email]
    if st.session_state.pop("reinitialiser_commande", False):
        st.session_state.pop("chantier", None)
        st.session_state.pop("chantier_verrouille", None)
    confirmation = st.session_state.pop("commande_envoyee", None)
    if confirmation:
        st.success(confirmation, icon=":material/check_circle:")
    flash = st.session_state.pop("flash", None)
    if flash:
        st.toast(flash, icon=":material/check_circle:")
    st.caption(f"{user['prenom']} {user['nom']} · {user['entreprise']}")

    if st.session_state.panier and st.session_state.panier_chantier:
        chantier = st.text_input(
            "Nom du chantier / projet *", value=st.session_state.panier_chantier,
            disabled=True, key="chantier_verrouille", icon=":material/apartment:",
            help="Le chantier reste verrouillé tant que le panier contient des pièces.",
        )
        st.caption("Le chantier est verrouillé pendant la préparation de cette commande.")
    else:
        chantier = st.text_input(
            "Nom du chantier / projet *", key="chantier", icon=":material/apartment:",
            persist_state="session",
        )
    afficher_progression_commande(chantier)
    if not chantier.strip():
        st.info(
            "Commencez par saisir le nom du chantier pour afficher les modes de commande.",
            icon=":material/info:",
        )
        return
    if st.session_state.revue_commande:
        page_verification(user, chantier.strip())
        return

    cid = st.session_state.piece
    if cid:
        configurer_piece(user, chantier, cid)
        return

    mode = st.segmented_control(
        "Comment souhaitez-vous commander ?",
        ["Saisie guidée", "Analyser un croquis IA", "Analyser un listing IA", "Déposer un listing"],
        key="mode_commande",
        required=True,
        width="stretch",
        persist_state="session",
    )
    if mode == "Déposer un listing":
        depot_listing(user, chantier.strip())
        barre_panier()
        return

    if mode == "Analyser un croquis IA":
        st.subheader("Reconnaissance de croquis", icon=":material/document_scanner:")
        st.caption("Une pièce par photo. Les cotes reconnues doivent être vérifiées avant l'ajout.")
        photo_ia = st.file_uploader("Photo du croquis", type=["jpg", "jpeg", "png", "heic", "heif"],
                                    key="photo_ia_client")
        interface_analyse_croquis(photo_ia, "client", ajouter_au_panier=True)
        barre_panier()
        return
    if mode == "Analyser un listing IA":
        st.subheader("Lecture d'un listing complet", icon=":material/document_scanner:")
        photo_liste = st.file_uploader("Photo de la feuille de croquis",
                                       type=["jpg", "jpeg", "png", "heic", "heif"],
                                       key="photo_listing_ia_client")
        interface_analyse_listing(photo_liste, "client_liste", user=user, chantier=chantier.strip())
        barre_panier()
        return

    grille_pieces()
    barre_panier()


def statut_commande(reference):
    info = STORE["statuts_commandes"].get(reference, {})
    statut = info.get("statut", "Reçue")
    note = info.get("note_client", "")
    if statut == "Terminée":
        return "Terminées", statut, "green", ":material/check_circle:", note
    if statut in ("En préparation", "En fabrication", "Prête"):
        icone = ":material/factory:" if statut == "En fabrication" else ":material/build:"
        return "En préparation", statut, "blue", icone, note
    return "Reçues", "Reçue par l’atelier", "orange", ":material/inbox:", note


def dupliquer_commande(lignes):
    copies = []
    for ligne in lignes:
        photo = None
        if ligne.get("photo") and os.path.exists(ligne["photo"]):
            ext = os.path.splitext(ligne["photo"])[1].lower()
            photo = os.path.join(DOSSIER_UPLOADS, f"{uuid.uuid4().hex}{ext}")
            shutil.copy2(ligne["photo"], photo)
        copies.append({
            "repere": ligne["repere"], "cid": ligne["cid"],
            "cotes": dict(ligne["cotes"]), "epaisseur": ligne["epaisseur"],
            "con1": ligne["con1"], "con2": ligne["con2"],
            "qte": ligne["qte"], "photo": photo,
        })
    st.session_state.panier = copies
    st.session_state.panier_chantier = lignes[0]["chantier"]
    st.session_state.mode_commande = "Saisie guidée"
    st.session_state.revue_commande = True
    st.session_state.commande_verifiee = False
    st.session_state.navigation_client_cible = "Commander"
    enregistrer_brouillon_courant()
    st.rerun()


def afficher_conversation_commande(reference):
    messages = STORE["messages_commandes"].get(reference, [])
    if not messages:
        st.caption("Aucun message pour cette commande.")
        return
    for message in messages:
        atelier = message["auteur_type"] == "atelier"
        with st.container(border=True):
            st.markdown(
                f":material/{'factory' if atelier else 'person'}: "
                f"**{'Atelier CLIMAIR' if atelier else message['auteur']}**"
            )
            st.write(message["message"])
            st.caption(message["date_message"])


def formulaire_reponse_client(reference, user):
    with st.expander("Échanger avec l’atelier", icon=":material/forum:"):
        afficher_conversation_commande(reference)
        with st.form(f"reponse_client_{reference}", border=False):
            reponse = st.text_area(
                "Votre réponse",
                placeholder="Écrivez votre question ou votre réponse à l’atelier…",
                max_chars=2000,
            )
            envoyer = st.form_submit_button(
                "Envoyer à l’atelier", type="primary", icon=":material/send:",
                width="stretch",
            )
        if envoyer:
            if not reponse.strip():
                st.error("Écrivez un message avant de l’envoyer.")
            else:
                auteur = f"{user['prenom']} {user['nom']}"
                DB.ajouter_message_commande(reference, "client", auteur, reponse)
                DB.ajouter_audit(user["email"], "message_commande_envoye", reference)
                envoyer_alerte(
                    f"[CLIMAIR] Réponse client - {reference}",
                    f"Référence : {reference}\n"
                    f"Client : {auteur} ({user['entreprise']})\n"
                    f"Email : {user['email']}\n\n{reponse.strip()}",
                )
                st.toast("Votre réponse a été envoyée à l’atelier.", icon=":material/check:")
                st.rerun()


def page_mes_commandes():
    user = STORE["users"][st.session_state.user_email]
    commandes = [ligne for ligne in STORE["orders"] if ligne["email"] == user["email"]]
    listings = [ligne for ligne in STORE["listings"] if ligne["email"] == user["email"]]
    groupes = {}
    for ligne in reversed(commandes):
        reference = ligne.get("commande_id") or f"{ligne['chantier']} · {ligne['date']}"
        groupes.setdefault(reference, []).append(ligne)

    st.header("Mes commandes", icon=":material/history:")
    if not groupes and not listings:
        st.info("Vous n’avez encore envoyé aucune commande.", icon=":material/inbox:")
        return
    compteurs = {nom: 0 for nom in ("Reçues", "En préparation", "Terminées")}
    for reference in groupes:
        compteurs[statut_commande(reference)[0]] += 1
    for listing in listings:
        compteurs[statut_commande(f"LIST-{listing['id'].upper()}")[0]] += 1
    filtre = st.segmented_control(
        "Filtrer par statut", ["Toutes", "Reçues", "En préparation", "Terminées"],
        default="Toutes", required=True, key="filtre_historique", width="stretch",
        format_func=lambda valeur: (
            f"{valeur} ({len(groupes) + len(listings)})" if valeur == "Toutes"
            else f"{valeur} ({compteurs[valeur]})"
        ),
    )
    affiche = 0
    for reference, lignes in groupes.items():
        categorie, libelle, couleur, icone, note = statut_commande(reference)
        if filtre != "Toutes" and filtre != categorie:
            continue
        affiche += 1
        with st.container(border=True):
            st.markdown(f"**{reference}**")
            st.badge(libelle, color=couleur, icon=icone)
            st.caption(
                f"{lignes[0]['chantier']} · {lignes[0]['date']} · "
                f"{len(lignes)} ligne(s)"
            )
            if note:
                st.info(note, icon=":material/chat:")
            evenements = STORE["evenements_commandes"].get(reference, [])
            if evenements:
                with st.expander("Suivi de la commande", icon=":material/timeline:"):
                    for evenement in reversed(evenements):
                        st.markdown(
                            f":material/check_circle: **{evenement['statut']}** · "
                            f"{evenement['date_evenement']}"
                        )
                        if evenement["note"]:
                            st.caption(evenement["note"])
            with st.expander("Voir les pièces", icon=":material/receipt_long:"):
                for ligne in lignes:
                    st.markdown(f"**Repère {ligne['repere']}** · {ligne['cid']} · {fmt_qte(ligne)}")
                    st.caption(resume_piece(ligne))
                recap = generer_recapitulatif(
                    reference, lignes, user, libelle, CHEMIN_LOGO, DOSSIER_ASSETS
                )
                st.download_button(
                    "Télécharger le récapitulatif", recap,
                    file_name=f"{_nom_sur(reference)}.pdf", mime="application/pdf",
                    icon=":material/download:", key=f"recap_{reference}",
                )
                if st.button(
                    "Commander à nouveau", icon=":material/replay:",
                    key=f"dup_{reference}", disabled=bool(st.session_state.panier),
                    help=("Terminez ou videz le panier actuel avant de dupliquer."
                          if st.session_state.panier else None),
                ):
                    dupliquer_commande(lignes)
            formulaire_reponse_client(reference, user)
    for listing in reversed(listings):
        reference = f"LIST-{listing['id'].upper()}"
        categorie, libelle, couleur, icone, note = statut_commande(reference)
        if filtre != "Toutes" and filtre != categorie:
            continue
        affiche += 1
        with st.container(border=True):
            st.markdown(f"**Listing {listing['id'].upper()}**")
            st.badge(libelle, color=couleur, icon=icone)
            st.caption(
                f"{listing['chantier']} · {listing['date']} · "
                f"{len(listing['photos'])} photo(s)"
            )
            if note:
                st.info(note, icon=":material/chat:")
            if listing.get("commentaire"):
                st.write(listing["commentaire"])
            formulaire_reponse_client(reference, user)
    if not affiche:
        st.info("Aucune commande avec ce statut.", icon=":material/filter_alt_off:")


def deconnecter_client():
    st.session_state.update(
        user_email=None, panier=[], panier_chantier=None, piece=None,
        revue_commande=False,
    )
    st.rerun()


def page_mon_compte():
    user = STORE["users"][st.session_state.user_email]
    references = {
        ligne.get("commande_id") or f"{ligne['chantier']} · {ligne['date']}"
        for ligne in STORE["orders"] if ligne["email"] == user["email"]
    }
    st.header("Mon compte", icon=":material/account_circle:")
    c1, c2 = st.columns(2)
    c1.metric("Commandes envoyées", len(references), border=True)
    c2.metric("Pièces dans le panier", len(st.session_state.panier), border=True)
    with st.container(border=True):
        st.subheader("Entreprise", icon=":material/business:")
        st.write(f"**{user['entreprise']}**")
        st.caption(f"SIRET · {user['siret']}")
        st.write(user["adresse"])
    with st.container(border=True):
        st.subheader("Contact", icon=":material/person:")
        st.write(f"{user['prenom']} {user['nom']}")
        st.caption(user["email"])
        st.badge("Compte actif", color="green", icon=":material/verified_user:")
    with st.expander("Modifier mes coordonnées", icon=":material/edit:"):
        with st.form("modifier_coordonnees"):
            c1, c2 = st.columns(2)
            prenom = c1.text_input("Prénom", value=user["prenom"])
            nom = c2.text_input("Nom", value=user["nom"])
            adresse = st.text_area("Adresse postale", value=user["adresse"])
            modifier = st.form_submit_button(
                "Enregistrer", type="primary", icon=":material/save:", width="stretch"
            )
        if modifier:
            if not all((prenom.strip(), nom.strip(), adresse.strip())):
                st.error("Tous les champs sont obligatoires.")
            else:
                user.update(prenom=prenom.strip(), nom=nom.strip(), adresse=adresse.strip())
                DB.mettre_a_jour_utilisateur(user)
                DB.ajouter_audit(user["email"], "coordonnees_modifiees")
                st.success("Coordonnées mises à jour.")
                st.rerun()
    with st.expander("Changer mon mot de passe", icon=":material/password:"):
        with st.form("changer_mot_de_passe"):
            actuel = st.text_input("Mot de passe actuel", type="password")
            nouveau = st.text_input("Nouveau mot de passe", type="password")
            confirmer = st.text_input("Confirmer le nouveau mot de passe", type="password")
            changer = st.form_submit_button(
                "Changer le mot de passe", type="primary",
                icon=":material/security:", width="stretch",
            )
        if changer:
            if not verifier_mdp(actuel, user["sel"], user["hash"], int(user["iterations"])):
                st.error("Le mot de passe actuel est incorrect.")
            elif nouveau != confirmer:
                st.error("Les deux nouveaux mots de passe sont différents.")
            elif not mot_de_passe_robuste(nouveau):
                st.error(
                    "Utilisez au moins 12 caractères avec minuscule, majuscule, "
                    "chiffre et caractère spécial."
                )
            else:
                sel = uuid.uuid4().hex
                user.update(sel=sel, hash=hash_mdp(nouveau, sel), iterations=ITERATIONS_MDP)
                DB.mettre_a_jour_utilisateur(user)
                DB.ajouter_audit(user["email"], "mot_de_passe_modifie")
                st.success("Mot de passe modifié.")
    connexions = [
        entree for entree in DB.lire_audit(user["email"], 20)
        if entree["action"] == "connexion_reussie"
    ][:5]
    if connexions:
        with st.expander("Connexions récentes", icon=":material/history:"):
            for entree in connexions:
                st.caption(entree["date_action"])
    if st.button(
        "Se déconnecter", icon=":material/logout:", width="stretch",
        key="deconnexion_compte",
    ):
        deconnecter_client()


# --------------------------------------------------------------------------
# ATELIER : VALIDATION DES COMPTES
# --------------------------------------------------------------------------
def page_validation():
    st.header("Validation des comptes", icon=":material/group:")
    users = list(STORE["users"].values())
    attente = [u for u in users if u["statut"] == "En attente"]
    actifs = [u for u in users if u["statut"] == "Actif"]
    suspendus = [u for u in users if u["statut"] == "Suspendu"]

    m1, m2, m3 = st.columns(3)
    m1.metric("À valider", len(attente), border=True)
    m2.metric("Actifs", len(actifs), border=True)
    m3.metric("Suspendus", len(suspendus), border=True)

    st.subheader(f"En attente ({len(attente)})", icon=":material/pending_actions:")
    if not attente:
        st.caption("Aucune demande en attente.")
    for u in attente:
        with st.container(border=True):
            a, b = st.columns([5, 1])
            a.markdown(f"**{u['entreprise']}** - {u['prenom']} {u['nom']}  \n"
                       f"SIRET : {u['siret']} · {u['email']}  \n{u['adresse']}  \n"
                       f"*Demande du {u['cree_le']}*")
            a.badge("En attente", color="orange", icon=":material/schedule:")
            email_verifie = u.get("email_verifie", True)
            a.badge(
                "Email vérifié" if email_verifie else "Email non vérifié",
                color="green" if email_verifie else "red",
                icon=":material/mark_email_read:" if email_verifie else ":material/mark_email_unread:",
            )
            if not email_verifie and b.button(
                "Confirmer l’email",
                key=f"mail{u['email']}",
                icon=":material/mark_email_read:",
                help="À utiliser seulement après une vérification manuelle avec le client.",
                width="stretch",
            ):
                u["email_verifie"] = True
                DB.mettre_a_jour_utilisateur(u)
                DB.ajouter_audit("atelier", "email_verifie_manuellement", u["email"])
                st.rerun()
            if b.button(
                "Activer",
                key=f"act{u['email']}",
                icon=":material/check_circle:",
                type="primary",
                width="stretch",
                disabled=not email_verifie,
                help=None if email_verifie else "Le client doit d’abord vérifier son email.",
            ):
                DB.definir_statut_utilisateur(u["email"], "Actif")
                DB.ajouter_audit("atelier", "compte_active", u["email"])
                st.rerun()

    st.subheader(f"Comptes actifs ({len(actifs)})", icon=":material/verified_user:")
    for u in actifs:
        with st.container(border=True):
            a, b = st.columns([5, 1], vertical_alignment="center")
            a.write(f"**{u['entreprise']}** · {u['prenom']} {u['nom']} · {u['email']}")
            a.badge("Actif", color="green", icon=":material/check:")
            if b.button(
                "Suspendre",
                key=f"sus{u['email']}",
                icon=":material/pause:",
                type="tertiary",
                width="stretch",
            ):
                DB.definir_statut_utilisateur(u["email"], "Suspendu")
                DB.ajouter_audit("atelier", "compte_suspendu", u["email"])
                st.rerun()

    if suspendus:
        st.subheader(f"Comptes suspendus ({len(suspendus)})", icon=":material/person_off:")
    for u in suspendus:
        with st.container(border=True):
            a, b = st.columns([5, 1], vertical_alignment="center")
            a.write(f"**{u['entreprise']}** · {u['email']}")
            a.badge("Suspendu", color="red", icon=":material/block:")
            if b.button(
                "Réactiver",
                key=f"re{u['email']}",
                icon=":material/refresh:",
                width="stretch",
            ):
                DB.definir_statut_utilisateur(u["email"], "Actif")
                DB.ajouter_audit("atelier", "compte_reactive", u["email"])
                st.rerun()


# --------------------------------------------------------------------------
# ATELIER : MAINTENANCE ET PRODUCTION
# --------------------------------------------------------------------------
def page_maintenance():
    st.header("Maintenance", icon=":material/health_and_safety:")
    st.caption(
        "Contrôlez les sauvegardes, la messagerie et le stockage sans exposer "
        "les réglages sensibles."
    )

    sauvegarde = DB.sauvegarde_quotidienne()
    sauvegarde_ok = os.path.exists(sauvegarde)
    c1, c2, c3 = st.columns(3)
    c1.metric("Base de données", "Opérationnelle", border=True)
    c2.metric("Sauvegarde du jour", "Disponible" if sauvegarde_ok else "Absente", border=True)
    smtp_configure = all(
        _reglage(nom) for nom in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD")
    )
    c3.metric("Messagerie", "Configurée" if smtp_configure else "À configurer", border=True)

    with st.container(border=True):
        st.subheader("Sauvegarde quotidienne", icon=":material/backup:")
        st.write(f"Dernière sauvegarde : **{os.path.basename(sauvegarde)}**")
        if sauvegarde_ok:
            with open(sauvegarde, "rb") as fichier:
                st.download_button(
                    "Télécharger la sauvegarde",
                    fichier.read(),
                    file_name=os.path.basename(sauvegarde),
                    mime="application/vnd.sqlite3",
                    icon=":material/download:",
                )

    with st.container(border=True):
        st.subheader("Messagerie SMTP", icon=":material/mail:")
        if not smtp_configure:
            st.warning(
                "Ajoutez SMTP_HOST, SMTP_PORT, SMTP_USER et SMTP_PASSWORD dans "
                "les secrets Streamlit.",
                icon=":material/settings_alert:",
            )
        if st.button(
            "Tester la connexion SMTP",
            disabled=not smtp_configure,
            icon=":material/network_check:",
        ):
            with st.spinner("Connexion au serveur de messagerie…"):
                try:
                    tester_connexion_smtp()
                except Exception as erreur:  # noqa: BLE001 - résultat du diagnostic
                    DB.ajouter_audit("atelier", "test_smtp_echec", type(erreur).__name__)
                    st.error(f"Échec de la connexion SMTP : {erreur}")
                else:
                    DB.ajouter_audit("atelier", "test_smtp_reussi")
                    st.success("Connexion SMTP et authentification réussies.")

    references = []
    references.extend(
        commande.get("photo") for commande in STORE["orders"] if commande.get("photo")
    )
    for listing in STORE["listings"]:
        references.extend(listing.get("photos", []))
    references.extend(DB.photos_brouillons())
    orphelins = fichiers_orphelins(DOSSIER_UPLOADS, references)
    with st.container(border=True):
        st.subheader("Nettoyage des photos", icon=":material/cleaning_services:")
        if not orphelins:
            st.success("Aucun fichier orphelin détecté.")
        else:
            st.warning(f"{len(orphelins)} fichier(s) non rattaché(s) ont été détectés.")
            with st.expander("Voir les fichiers concernés"):
                for chemin in orphelins:
                    st.code(os.path.relpath(chemin, BASE_DIR))
            confirmation = st.checkbox(
                "Je confirme la suppression définitive de ces fichiers non utilisés."
            )
            if st.button(
                "Supprimer les fichiers orphelins",
                disabled=not confirmation,
                type="primary",
                icon=":material/delete_sweep:",
            ):
                racine = os.path.realpath(DOSSIER_UPLOADS)
                supprimes = 0
                for chemin in orphelins:
                    cible = os.path.realpath(chemin)
                    if os.path.commonpath((racine, cible)) == racine and os.path.isfile(cible):
                        os.remove(cible)
                        supprimes += 1
                DB.ajouter_audit(
                    "atelier", "nettoyage_photos", f"{supprimes} fichier(s) supprimé(s)"
                )
                st.success(f"{supprimes} fichier(s) supprimé(s).")
                st.rerun()

    st.subheader("Journal d’activité", icon=":material/history:")
    journal = DB.lire_audit(limite=100)
    if not journal:
        st.caption("Aucune activité enregistrée.")
    else:
        st.dataframe(
            [
                {
                    "Date": entree["date_action"],
                    "Acteur": entree["acteur"],
                    "Action": entree["action"],
                    "Détails": entree["details"],
                }
                for entree in journal
            ],
            hide_index=True,
            width="stretch",
        )


# --------------------------------------------------------------------------
# ATELIER : COMMANDES ET IA (DÉMO)
# --------------------------------------------------------------------------
def bloc_ia(l):
    """Reconnaissance réelle, avant validation explicite de l'atelier."""
    if l.get("photo") and os.path.isfile(l["photo"]):
        interface_analyse_croquis(l["photo"], f"atelier_{l['id']}", ligne_atelier=l)


def page_commandes():
    # Titre et recherche sur la même ligne (la recherche passe dessous sur téléphone)
    with st.container(horizontal=True, vertical_alignment="bottom"):
        st.header("Commandes", icon=":material/inventory_2:", width="content")
        recherche = st.text_input(
            "Rechercher une commande",
            placeholder="Référence, entreprise ou chantier",
            icon=":material/search:",
            type="search",
            live="300ms",
            label_visibility="collapsed",
            width=340,
        ).strip().casefold()
    if STORE["mail_erreurs"]:
        st.warning(
            "Alerte e-mail non envoyée : " + STORE["mail_erreurs"][-1],
            icon=":material/mail_lock:",
        )
    # Une référence = un envoi. Deux commandes du même chantier restent donc
    # totalement indépendantes dans le suivi atelier.
    groupes = {}
    for commande in STORE["orders"]:
        reference = commande.get("commande_id") or f"LEGACY-{commande['id']}"
        groupe = groupes.setdefault(
            reference,
            {
                "entreprise": commande["entreprise"],
                "chantier": commande["chantier"],
                "pieces": [],
                "listings": [],
            },
        )
        groupe["pieces"].append(commande)
    for listing in STORE["listings"]:
        reference = f"LIST-{listing['id'].upper()}"
        groupes[reference] = {
            "entreprise": listing["entreprise"],
            "chantier": listing["chantier"],
            "pieces": [],
            "listings": [listing],
        }

    def statut_groupe(reference):
        return STORE["statuts_commandes"].get(reference, {}).get("statut", "Reçue")

    nouvelles = sum(statut_groupe(reference) == "Reçue" for reference in groupes)
    terminees = sum(statut_groupe(reference) == "Terminée" for reference in groupes)
    en_preparation = len(groupes) - nouvelles - terminees

    if not groupes:
        st.info("Aucune commande pour le moment.", icon=":material/inbox:")
        return

    # Compteurs cliquables (Toutes / Nouvelles / En cours / Terminées) = filtre
    filtre_statut = st.segmented_control(
        "Filtrer les commandes",
        ["Toutes", "Nouvelles", "En cours", "Terminées"],
        default="Toutes",
        required=True,
        key="filtre_commandes_atelier",
        width="stretch",
        label_visibility="collapsed",
        format_func=lambda valeur: {
            "Toutes": f"Toutes · {len(groupes)}",
            "Nouvelles": f"Nouvelles · {nouvelles}",
            "En cours": f"En cours · {en_preparation}",
            "Terminées": f"Terminées · {terminees}",
        }[valeur],
    )
    groupes_affiches = {
        reference: contenu for reference, contenu in groupes.items()
        if (
            not recherche
            or recherche in " ".join(
                (reference, contenu["entreprise"], contenu["chantier"])
            ).casefold()
        )
        and (
            filtre_statut == "Toutes"
            or (filtre_statut == "Nouvelles" and statut_groupe(reference) == "Reçue")
            or (
                filtre_statut == "En cours"
                and statut_groupe(reference) not in ("Reçue", "Terminée")
            )
            or (filtre_statut == "Terminées" and statut_groupe(reference) == "Terminée")
        )
    }
    if not groupes_affiches:
        st.info("Aucune commande ne correspond à cette recherche.", icon=":material/search_off:")
        return

    # Commandes en cours d'abord, commandes terminées en bas de liste.
    ordre = sorted(
        groupes_affiches.items(),
        key=lambda element: (
            statut_groupe(element[0]) == "Terminée",
            element[0],
        ),
    )
    references = [reference for reference, _ in ordre]
    if st.session_state.get("commande_atelier") not in references:
        st.session_state.commande_atelier = references[0]
    choisie = st.session_state.commande_atelier

    def resume_groupe(contenu):
        """Calculs, date de commande et tonnage d'un envoi."""
        calc = [calculer(l) for l in contenu["pieces"]]
        dates = [
            datetime.strptime(x["date"], "%d/%m/%Y %H:%M")
            for x in contenu["pieces"] + contenu["listings"]
        ]
        kg = sum(c["poids_kg"] for c in calc) + sum(ls["poids_kg"] for ls in contenu["listings"])
        return calc, min(dates), kg

    def choisir_commande(reference):
        st.session_state.commande_atelier = reference

    # Liste à gauche (défilante), détail de la commande choisie à droite.
    # Sur téléphone, les deux colonnes s'empilent : liste puis détail.
    liste, detail = st.columns([1, 2], gap="medium")
    with liste, st.container(height=720, border=False):
        titre_terminees = False
        for reference, contenu in ordre:
            statut = statut_groupe(reference)
            if statut == "Terminée" and not titre_terminees:
                st.caption("**Terminées**")
                titre_terminees = True
            _, date_min, kg = resume_groupe(contenu)
            selection = reference == choisie
            cle = hashlib.md5(reference.encode()).hexdigest()[:10]
            with st.container(border=True):
                with st.container(horizontal=True, vertical_alignment="center"):
                    st.caption(reference)
                    st.badge(
                        "Nouvelle" if statut == "Reçue" else statut,
                        color=COULEURS_STATUT.get(statut, "gray"),
                    )
                st.markdown(f"**{contenu['chantier']}**")
                if contenu["listings"]:
                    nb_photos = sum(len(ls["photos"]) for ls in contenu["listings"])
                    details = f"Listing photo · {nb_photos} photo(s) · {kg:.1f} kg"
                else:
                    details = f"{len(contenu['pieces'])} ligne(s) · {kg:.1f} kg"
                st.caption(
                    f"{contenu['entreprise']} · {date_min:%d/%m à %H:%M}  \n{details}"
                )
                st.button(
                    "Affichée" if selection else "Ouvrir",
                    key=f"ouvrir_{cle}",
                    type="primary" if selection else "secondary",
                    icon=":material/visibility:" if selection else ":material/open_in_new:",
                    disabled=selection,
                    width="stretch",
                    on_click=choisir_commande,
                    args=(reference,),
                )

    for reference, contenu in ordre:
        if reference != choisie:
            continue
        entreprise, chantier = contenu["entreprise"], contenu["chantier"]
        lignes, listings = contenu["pieces"], contenu["listings"]
        calc, date_min, kg_total = resume_groupe(contenu)
        date_cmd = date_min.strftime("%d/%m/%Y à %H:%M")
        cle_ch = hashlib.md5(reference.encode()).hexdigest()[:10]
        statut = statut_groupe(reference)
        client = STORE["users"].get((lignes + listings)[0]["email"], {})
        with detail:
            with st.container(border=True):
                with st.container(horizontal=True, vertical_alignment="center"):
                    st.caption(reference)
                    st.badge(
                        "Nouvelle" if statut == "Reçue" else statut,
                        color=COULEURS_STATUT.get(statut, "gray"),
                        icon=":material/manufacturing:",
                    )
                st.subheader(chantier)
                contact = f"{client.get('prenom', '')} {client.get('nom', '')}".strip()
                st.caption(
                    " · ".join(x for x in (entreprise, contact, f"reçue le {date_cmd}") if x)
                )
                # Boutons d'action (export CAMduct, PDF) remplis plus bas
                actions = st.container(horizontal=True)
                if lignes and client:
                    actions.download_button(
                        "Récapitulatif PDF",
                        generer_recapitulatif(reference, lignes, client, statut, CHEMIN_LOGO),
                        file_name=f"{_nom_sur(reference)}.pdf",
                        mime="application/pdf",
                        key=f"pdf_{cle_ch}",
                        icon=":material/picture_as_pdf:",
                    )
            for l, r in zip(lignes, calc):
                titre = CATALOGUE[l["cid"]]["titre"]
                with st.container(border=True):
                    # Vue 3D + repère / pièce / poids, puis cotes et connecteurs
                    with st.container(horizontal=True, wrap=False, vertical_alignment="top"):
                        img = image_piece(l["cid"])
                        if img:
                            st.image(img, width=64, alt=f"Vue 3D {titre}")
                        with st.container(gap="xxsmall"):
                            with st.container(horizontal=True, wrap=False):
                                st.markdown(f"**Repère {l['repere']} · {titre}**")
                                # Périmètre et longueur équivalente restent calculés mais masqués
                                st.markdown(f"**{r['poids_kg']:.2f} kg**", width="content")
                            dims = " · ".join(f"{k} {v:g}" for k, v in l["cotes"].items()) or "sur-mesure"
                            st.caption(f"{l['cid']} · {dims} · {fmt_qte(l)}")
                            st.caption(
                                f"Côté 1 : {l['con1']} · Côté 2 : {l['con2']}" if l["con2"]
                                else f"Connecteur : {l['con1']}"
                            )
                    # Épaisseur fixée par l'atelier (8/10ème par défaut côté client)
                    epaisseurs = list(EPAISSEURS)
                    ep = st.segmented_control(
                        "Épaisseur", epaisseurs,
                        default=l["epaisseur"] if l["epaisseur"] in epaisseurs else EPAISSEUR_DEFAUT,
                        required=True, key=f"ep_{l['id']}",
                    )
                    if ep != l["epaisseur"]:
                        l["epaisseur"] = ep
                        DB.mettre_a_jour_commande(l)
                        DB.ajouter_audit(
                            "atelier", "epaisseur_modifiee",
                            f"{reference} repère {l['repere']} → {ep}",
                        )
                        st.rerun()
                    if l["photo"] and os.path.exists(l["photo"]):
                        st.image(
                            l["photo"], width=250,
                            alt=f"Croquis de la pièce {l['repere']}",
                        )
                        if l["cid"] == "CID 999":
                            bloc_ia(l)
            # Listings photo déposés par le client : l'atelier saisit le poids lu dessus
            poids_listings = 0.0
            for ls in listings:
                with st.container(border=True):
                    st.markdown(f"**Listing du {ls['date']}** · {ls['email']}")
                    if ls["commentaire"]:
                        st.caption(ls["commentaire"])
                    for p in ls["photos"]:
                        if os.path.exists(p):
                            st.image(p, width="stretch", alt="Photo du listing client")
                            with st.expander("Analyser ce listing par IA", icon=":material/document_scanner:"):
                                interface_analyse_listing(
                                    p, f"atelier_liste_{ls['id']}_{hashlib.md5(p.encode()).hexdigest()[:8]}",
                                    chantier={"entreprise": ls["entreprise"],
                                              "email": ls["email"], "chantier": ls["chantier"]},
                                )
                    poids_saisi = st.number_input(
                        "Poids estimé de ce listing (kg)", min_value=0.0,
                        value=float(ls["poids_kg"]), step=1.0, key=f"pl{ls['id']}")
                    if float(poids_saisi) != float(ls["poids_kg"]):
                        DB.definir_poids_listing(ls, poids_saisi)
                    ls["poids_kg"] = float(poids_saisi)
                    poids_listings += ls["poids_kg"]

            poids_total = sum(c["poids_kg"] for c in calc) + poids_listings

            # Totaux : pièces, tonnage, prix au kg (mémorisé par entreprise), Total Général
            unites = sum(l["qte"] for l in lignes if l["cid"] != "CID 1")
            ml_gaine = sum(l["qte"] for l in lignes if l["cid"] == "CID 1")
            t1, t2 = st.columns(2)
            t1.metric("Pièces", f"{int(unites)}" + (f" + {ml_gaine:g} ml" if ml_gaine else ""),
                      border=True)
            t2.metric("Tonnage", f"{poids_total:.2f} kg", border=True)
            t3, t4 = st.columns(2, vertical_alignment="bottom")
            pkg_defaut = STORE["prix_kg"].get(entreprise, round(PRIX_KG_REVIENT * COEF_VENTE, 2))
            pkg = t3.number_input(f"Prix au kg · {entreprise} (€/kg)", min_value=0.0,
                                  value=float(pkg_defaut), step=0.1, key=f"pkg_{cle_ch}")
            if float(pkg) != float(pkg_defaut):
                DB.definir_prix(entreprise, pkg)
            total = poids_total * pkg    # Total Général = tonnage x prix au kg
            t4.metric("Total général", f"{total:.2f} €", border=True)
            if listings:
                st.caption(f"{len(listings)} listing(s) photo (poids saisi inclus dans le tonnage)")

            if lignes:
                texte, ignorees = script_camduct(chantier, entreprise, lignes)
                if ignorees:
                    st.warning(
                        "Pièces non exportées (sur-mesure à convertir) : "
                        + ", ".join(ignorees)
                    )
                nom = "".join(c if c.isalnum() else "_" for c in f"{reference}_{chantier}")
                donnees = texte.encode("cp1252", errors="replace")
                with open(os.path.join(DOSSIER_EXPORTS, f"{nom}.txt"), "wb") as fichier:
                    fichier.write(donnees)
                actions.download_button(
                    "Exporter pour CAMduct",
                    donnees,
                    file_name=f"{nom}.txt",
                    mime="text/plain",
                    key=f"exp{cle_ch}",
                    icon=":material/download:",
                    type="primary",
                )

            st.subheader("Suivi visible par le client", icon=":material/local_shipping:")
            messages = STORE["messages_commandes"].get(reference, [])
            if messages:
                with st.expander(
                    f"Conversation avec le client ({len(messages)})",
                    icon=":material/forum:",
                ):
                    afficher_conversation_commande(reference)
            etapes_statut = ["Reçue", "En préparation", "En fabrication", "Prête", "Terminée"]
            statut_choisi = st.segmented_control(
                "Statut atelier",
                etapes_statut,
                format_func=lambda s: f"{etapes_statut.index(s) + 1} · {s}",
                default=statut,
                required=True,
                key=f"statut_{cle_ch}",
                width="stretch",
            )
            note_actuelle = STORE["statuts_commandes"].get(reference, {}).get(
                "note_client", ""
            )
            note = st.text_area(
                "Note pour le client",
                value=note_actuelle,
                placeholder="Ex. : fabrication prévue mardi matin.",
                key=f"note_{cle_ch}",
            )
            if st.button(
                "Enregistrer le suivi",
                type="primary",
                icon=":material/save:",
                key=f"suivi_{cle_ch}",
                width="stretch",
            ):
                statut_modifie = statut_choisi != statut
                note_modifiee = note.strip() != note_actuelle.strip()
                DB.definir_statut_commande(reference, statut_choisi, note)
                DB.ajouter_audit(
                    "atelier", "statut_commande_modifie", f"{reference} → {statut_choisi}"
                )
                if note_modifiee and note.strip():
                    DB.ajouter_message_commande(
                        reference, "atelier", "Atelier CLIMAIR", note
                    )
                if statut_modifie or note_modifiee:
                    destinataire = (lignes + listings)[0]["email"]
                    texte_note = f"\n\nMessage de l’atelier :\n{note.strip()}" if note.strip() else ""
                    envoyer_alerte(
                        f"[CLIMAIR] Mise à jour de votre commande {reference}",
                        f"Votre commande {reference} est maintenant : {statut_choisi}."
                        f"{texte_note}\n\nConnectez-vous à votre espace CLIMAIR pour répondre.",
                        destinataire=destinataire,
                    )
                st.toast("Suivi enregistré", icon=":material/check_circle:")
                st.rerun()

            evenements = STORE["evenements_commandes"].get(reference, [])
            if evenements:
                st.caption(
                    "Historique : "
                    + " · ".join(
                        f"{evt['date_evenement']} — {evt['statut']}" for evt in evenements
                    )
                )


# --------------------------------------------------------------------------
# PROGRAMME PRINCIPAL
# --------------------------------------------------------------------------
init_session()

# Porte dérobée : l'espace atelier n'existe que si l'URL contient ?page=admin.
# Sans ce paramètre, aucun code ni élément d'interface d'administration n'est rendu.
mode_admin = st.query_params.get("page") == "admin"

if not mode_admin:
    # ---------------- PARCOURS CLIENT UNIQUEMENT ----------------
    st.session_state.admin = False      # une session admin ne "fuit" jamais côté client
    with st.sidebar:
        afficher_logo(sidebar=True)
        if st.session_state.user_email:
            user_sidebar = STORE["users"][st.session_state.user_email]
            st.caption(user_sidebar["entreprise"])
            if st.button(
                "Se déconnecter", icon=":material/logout:", width="stretch",
                key="deconnexion_sidebar",
            ):
                deconnecter_client()
    afficher_logo()
    if st.session_state.user_email:
        cible = st.session_state.pop("navigation_client_cible", None)
        if cible:
            st.session_state.navigation_client = cible
        vue_client = st.segmented_control(
            "Navigation",
            ["Commander", "Mes commandes", "Mon compte"],
            key="navigation_client",
            required=True,
            width="stretch",
            persist_state="session",
            label_visibility="collapsed",
        )
        if vue_client == "Mes commandes":
            afficher_page_sans_erreur(page_mes_commandes)
        elif vue_client == "Mon compte":
            afficher_page_sans_erreur(page_mon_compte)
        else:
            afficher_page_sans_erreur(page_catalogue)
    else:
        afficher_page_sans_erreur(page_auth)
else:
    # ---------------- ESPACE ATELIER (?page=admin) ----------------
    vue = None
    with st.sidebar:
        afficher_logo(sidebar=True)
        if st.session_state.admin:
            a_valider = sum(u["statut"] == "En attente" for u in STORE["users"].values())
            vue = st.segmented_control(
                "Espace atelier",
                ["Commandes", "Comptes", "Maintenance"],
                default="Commandes",
                required=True,
                width="stretch",
                # Nombre de comptes à valider à côté de « Comptes », comme la maquette
                format_func=lambda v: f"Comptes · {a_valider}" if v == "Comptes" and a_valider else v,
            )
            if st.button("Quitter l'atelier", icon=":material/logout:", width="stretch"):
                st.session_state.admin = False
                st.rerun()
    afficher_logo()
    if not st.session_state.admin:
        st.subheader("Accès atelier", icon=":material/lock:")
        if not ADMIN_PASSWORD:
            st.error(
                "L'accès atelier n'est pas configuré. Ajoutez ADMIN_PASSWORD dans "
                ".streamlit/secrets.toml.",
                icon=":material/admin_panel_settings:",
            )
            st.stop()
        if not mot_de_passe_robuste(ADMIN_PASSWORD):
            st.warning(
                "Le mot de passe atelier devrait contenir au moins 12 caractères, "
                "avec une minuscule, une majuscule, un chiffre et un caractère spécial.",
                icon=":material/security:",
            )
        secondes_restantes = max(
            0, int(st.session_state.admin_blocage_jusqua - time.time())
        )
        with st.form("admin_login"):
            mdp = st.text_input("Mot de passe", type="password")
            ok = st.form_submit_button(
                "Entrer",
                icon=":material/login:",
                width="stretch",
                disabled=secondes_restantes > 0,
            )
        if secondes_restantes:
            st.warning(
                f"Trop de tentatives. Réessayez dans {secondes_restantes} seconde(s)."
            )
        if ok:
            if hmac.compare_digest(mdp.encode(), ADMIN_PASSWORD.encode()):
                DB.ajouter_audit("atelier", "connexion_reussie")
                st.session_state.admin = True
                st.session_state.echecs_admin = 0
                st.session_state.admin_blocage_jusqua = 0.0
                st.rerun()
            else:
                DB.ajouter_audit("atelier", "connexion_refusee")
                st.session_state.echecs_admin += 1
                if st.session_state.echecs_admin >= 5:
                    st.session_state.admin_blocage_jusqua = time.time() + 30
                    st.session_state.echecs_admin = 0
                st.error("Mot de passe incorrect.")
    elif vue == "Comptes":
        afficher_page_sans_erreur(page_validation)
    elif vue == "Maintenance":
        afficher_page_sans_erreur(page_maintenance)
    else:
        afficher_page_sans_erreur(page_commandes)



