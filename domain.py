"""Règles métier CLIMAIR indépendantes de l'interface Streamlit."""

from __future__ import annotations

from datetime import datetime
from typing import Any


PRIX_KG_REVIENT = 3.50
COEF_VENTE = 1.5
FORFAIT_VENTE = 15.0


def _ab(l="Largeur A (mm)", h="Hauteur B (mm)"):
    return [("A", l, 300, 1, 5000), ("B", h, 200, 1, 5000)]


def _le_coude(cotes):
    angle = cotes["ANGLE"]
    return 1.5 if angle >= 50 else (1.0 if angle >= 15 else 0.5)


CATALOGUE = {
    "CID 1": dict(emoji="⏹️", titre="Gaine Droite", dim=("A", "B"),
                  cotes=_ab(), le=lambda c: 1.0),
    "CID 4": dict(emoji="↩️", titre="Coude", dim=("A", "B"),
                  cotes=_ab() + [("ANGLE", "Angle libre (° de 1 à 90)", 90, 1, 90),
                                 ("D", "Extension Haute D (mm)", 25, 0, 5000),
                                 ("F", "Extension Basse F (mm)", 25, 0, 5000)],
                  le=_le_coude),
    "CID 2": dict(emoji="📐", titre="Réduction", dim=("A1", "B1"),
                  cotes=[("A1", "Largeur Entrée A1 (mm)", 300, 1, 5000),
                         ("B1", "Hauteur Entrée B1 (mm)", 200, 1, 5000),
                         ("A2", "Largeur Sortie A2 (mm)", 200, 1, 5000),
                         ("B2", "Hauteur Sortie B2 (mm)", 150, 1, 5000),
                         ("L", "Longueur L (mm)", 500, 1, 10000)],
                  le=lambda c: 1.5),
    "CID 18": dict(emoji="🔄", titre="Carré vers rond", dim=("A", "B"),
                   cotes=_ab("Base Largeur A (mm)", "Base Hauteur B (mm)") +
                         [("DIAM", "Sortie Diamètre Ø (mm)", 200, 1, 5000),
                          ("L", "Longueur L (mm)", 400, 1, 10000)],
                   le=lambda c: 2.0),
    "CID 3": dict(emoji="📌", titre="Piquage rectangulaire", dim=("A", "B"),
                  cotes=_ab("Largeur Piquage A (mm)", "Hauteur Piquage B (mm)") +
                        [("L", "Longueur L (mm)", 300, 1, 10000),
                         ("C", "Talon C (mm)", 30, 0, 1000)],
                  le=lambda c: 1.0),
    "CID 6": dict(emoji="🔀", titre="Dévoiement rectangulaire", dim=("A", "B"),
                  cotes=_ab() + [("L", "Longueur L (mm)", 500, 1, 10000),
                                 ("DEPORT", "Valeur du Déport (mm)", 100, 0, 5000)],
                  le=lambda c: 1.5),
    "CID 14": dict(emoji="🌿", titre="Té rectangulaire", dim=("A", "B"),
                   cotes=_ab("Corps Largeur A (mm)", "Corps Hauteur B (mm)") +
                         [("C", "Branche Largeur C (mm)", 200, 1, 5000),
                          ("L", "Longueur L (mm)", 600, 1, 10000)],
                   le=lambda c: 1.5),
    "CID 95": dict(emoji="🛑", titre="Bouchon", dim=("A", "B"), cotes=_ab(),
                   le=lambda c: 0.5),
    "CID 110": dict(emoji="📐🕸️", titre="Sifflet grillagé", dim=("A", "B"),
                    cotes=_ab(), le=lambda c: 2.5),
    "CID 95-G": dict(emoji="🕸️", titre="Bouchon grillagé", dim=("A", "B"),
                     cotes=_ab(), le=lambda c: 1.5),
    "CID 999": dict(emoji="✏️", titre="Pièce sur mesure", dim=None,
                    cotes=[], le=lambda c: 1.5),
}

CONNECTEURS = (["METU 20 FIXE", "METU 20 NF", "METU 30 FIXE", "METU 30 NF"] +
               [f"BT {mm}" for mm in range(10, 55, 5)] + ["Snap-Climair"])

ITM_CAMDUCT = {
    "CID 1": "./jouanel/Gaine.ITM",
    "CID 4": "./jouanel/Coude 90 R120.ITM",
    "CID 2": "./jouanel/Réduction.ITM",
    "CID 18": "./jouanel/Round to Sq.ITM",
    "CID 3": "./jouanel/Piquage.ITM",
    "CID 6": "./jouanel/ESSE.ITM",
    "CID 95": "./jouanel/Bouchon.ITM",
    "CID 14": "./jouanel/Té.ITM",
    "CID 110": "./jouanel/Sifflet Grillagé.ITM",
    "CID 95-G": "./jouanel/Bouchon Grillagé.ITM",
}

BOUCHONS = ("CID 95", "CID 95-G")
# Pièces avec un seul champ connecteur : les bouchons et le carré vers rond (CID 18)
UN_SEUL_CONNECTEUR = BOUCHONS + ("CID 18",)
EPAISSEURS = {"8/10ème": 8, "10/10ème": 10}
# Le client ne choisit plus l'épaisseur : l'atelier peut la modifier par pièce.
EPAISSEUR_DEFAUT = "8/10ème"


def fmt_qte(ligne: dict[str, Any]) -> str:
    """Formate une quantité avec son unité métier."""
    return f"{ligne['qte']:g} ml" if ligne["cid"] == "CID 1" else f"x{int(ligne['qte'])}"


def resume_piece(ligne: dict[str, Any]) -> str:
    """Construit une ligne de résumé pour les exports et e-mails."""
    dimensions = ", ".join(f"{k}={v:g}" for k, v in ligne["cotes"].items()) or "sur-mesure"
    connecteurs = ligne["con1"] + (f" / {ligne['con2']}" if ligne["con2"] else "")
    return (
        f"{ligne['repere']} | {ligne['cid']} {CATALOGUE[ligne['cid']]['titre']} | "
        f"{dimensions} | {connecteurs} | {ligne['epaisseur']} | {fmt_qte(ligne)}"
    )


def calculer(ligne: dict[str, Any]) -> dict[str, float]:
    """Calcule périmètre, longueur équivalente, poids et prix internes."""
    categorie = CATALOGUE[ligne["cid"]]
    if categorie["dim"]:
        largeur, hauteur = (ligne["cotes"][cle] for cle in categorie["dim"])
        perimetre = (largeur + hauteur) * 2 / 1000
    else:
        perimetre = 0.0
    longueur = categorie["le"](ligne["cotes"])
    quantite = ligne["qte"]
    poids = perimetre * longueur * EPAISSEURS[ligne["epaisseur"]] * quantite
    revient = poids * PRIX_KG_REVIENT
    return {
        "perimetre_m": perimetre,
        "longueur_eq_ml": longueur * quantite,
        "poids_kg": poids,
        "prix_revient": revient,
        "prix_vente": revient * COEF_VENTE + FORFAIT_VENTE,
    }


def script_camduct(
    chantier: str,
    entreprise: str,
    lignes: list[dict[str, Any]],
    date: datetime | None = None,
) -> tuple[str, list[str]]:
    """Construit le script ASCII CAMduct et retourne les repères ignorés."""
    date = date or datetime.now()
    contenu = [
        "JOBHEADER_START", "JOB_NAME", f"  {chantier}", "JOB_DATE",
        f"  {date:%d/%m/%Y}", "COMPANYADDRESS_START", "  CLIMAIR",
        "COMPANYADDRESS_END", "CUSTOMERADDRESS_START", f"  {entreprise}",
        "CUSTOMERADDRESS_END", "JOBHEADER_END", "",
    ]
    ignorees = []
    for ligne in lignes:
        if ligne["cid"] not in ITM_CAMDUCT:
            ignorees.append(ligne["repere"])
            continue
        contenu += [
            "ITEM_START", "ITEMHEADER_START", "ITEMFILE", f"  {ITM_CAMDUCT[ligne['cid']]}",
            "ITEM_NUMBER", f"  {ligne['repere']}", "MATERIAL", "  GALVANISEE", "GAUGE",
            f"  {EPAISSEURS[ligne['epaisseur']] / 10:.1f}", "QUANTITY",
            f"  {1 if ligne['cid'] == 'CID 1' else int(ligne['qte'])}",
            "ITEMHEADER_END", "DIMS_START",
        ]
        contenu += [f"  {cle}={valeur:g}" for cle, valeur in ligne["cotes"].items()]
        if ligne["cid"] == "CID 1":
            contenu.append(f"  L={ligne['qte'] * 1000:g}")
        contenu += ["DIMS_END", "CONNS_START", f"  {ligne['con1']}"]
        if ligne["con2"]:
            contenu.append(f"  {ligne['con2']}")
        contenu += [
            "CONNS_END", "SEAMS_START", "  Snap-Climair", "SEAMS_END",
            "ITEM_END", "",
        ]
    return "\r\n".join(contenu), ignorees

