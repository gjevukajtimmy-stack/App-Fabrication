"""Création du récapitulatif PDF professionnel d'une commande."""

from __future__ import annotations

import math
import os
from html import escape
from io import BytesIO
from pathlib import Path

from PIL import Image as PilImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from domain import calculer


BLEU = colors.HexColor("#123C9A")
BLEU_FONCE = colors.HexColor("#092665")
BLEU_CLAIR = colors.HexColor("#F2F6FB")
GRIS = colors.HexColor("#53647D")
BORDURE = colors.HexColor("#CBD7E6")


def _image_proportionnelle(chemin, largeur_max, hauteur_max):
    """Crée une image ReportLab sans jamais déformer son ratio."""
    chemin = Path(chemin)
    if not chemin.exists():
        return None
    try:
        with PilImage.open(chemin) as source:
            largeur, hauteur = source.size
    except OSError:
        return None
    ratio = min(largeur_max / largeur, hauteur_max / hauteur)
    return Image(str(chemin), width=largeur * ratio, height=hauteur * ratio)


def _visuel_cid(ligne, dossier_assets):
    if dossier_assets:
        base = ligne["cid"].lower().replace(" ", "")
        for nom in (base.replace("-", "") + ".png", base + ".png"):
            visuel = _image_proportionnelle(
                Path(dossier_assets) / nom, 18 * mm, 15 * mm
            )
            if visuel:
                return visuel
    if ligne.get("photo") and os.path.exists(ligne["photo"]):
        return _image_proportionnelle(ligne["photo"], 18 * mm, 15 * mm)
    return "-"


def generer_recapitulatif(
    reference, lignes, utilisateur, statut, logo=None, dossier_assets=None
) -> bytes:
    """Retourne un PDF en mémoire, prêt à être téléchargé."""
    flux = BytesIO()
    document = SimpleDocTemplate(
        flux,
        pagesize=A4,
        rightMargin=12 * mm,
        leftMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=16 * mm,
        title=f"Commande {reference}",
        author="CLIMAIR",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="TitreClimair", parent=styles["Title"], textColor=BLEU,
        fontName="Helvetica-Bold", fontSize=19, leading=22,
    ))
    styles.add(ParagraphStyle(
        name="Meta", parent=styles["BodyText"], textColor=GRIS,
        fontSize=8.4, leading=11,
    ))
    styles.add(ParagraphStyle(
        name="Cellule", parent=styles["BodyText"], textColor=BLEU_FONCE,
        fontSize=7.2, leading=9,
    ))
    styles.add(ParagraphStyle(
        name="CelluleCentre", parent=styles["Cellule"], alignment=TA_CENTER,
    ))
    styles.add(ParagraphStyle(
        name="Droite", parent=styles["Meta"], alignment=TA_RIGHT,
    ))

    elements = []
    logo_pdf = _image_proportionnelle(logo, 54 * mm, 13 * mm) if logo else None
    entete_gauche = logo_pdf or Paragraph("<b>CLIMAIR</b>", styles["TitreClimair"])
    entete = Table(
        [[
            entete_gauche,
            Paragraph(
                f"<b>{escape(reference)}</b><br/><font color='#53647D'>{escape(statut)}</font>",
                styles["Droite"],
            ),
        ]],
        colWidths=[120 * mm, 54 * mm],
    )
    entete.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
    ]))
    elements.extend([entete, Spacer(1, 4 * mm)])

    premiere = lignes[0] if lignes else {}
    meta = Table([
        [Paragraph("<b>Entreprise</b>", styles["Meta"]),
         Paragraph(escape(utilisateur.get("entreprise", "")), styles["Meta"]),
         Paragraph("<b>Chantier</b>", styles["Meta"]),
         Paragraph(escape(premiere.get("chantier", "")), styles["Meta"])],
        [Paragraph("<b>Contact</b>", styles["Meta"]),
         Paragraph(escape(f"{utilisateur.get('prenom', '')} {utilisateur.get('nom', '')}".strip()), styles["Meta"]),
         Paragraph("<b>Date</b>", styles["Meta"]),
         Paragraph(escape(premiere.get("date", "")), styles["Meta"])],
        [Paragraph("<b>Email</b>", styles["Meta"]),
         Paragraph(escape(utilisateur.get("email", "")), styles["Meta"]),
         Paragraph("<b>Statut</b>", styles["Meta"]),
         Paragraph(escape(statut), styles["Meta"])],
    ], colWidths=[25 * mm, 64 * mm, 22 * mm, 63 * mm])
    meta.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BLEU_CLAIR),
        ("BOX", (0, 0), (-1, -1), 0.6, BORDURE),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, BORDURE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    elements.extend([
        meta,
        Spacer(1, 5 * mm),
        Paragraph("Détail des pièces", styles["Heading2"]),
        Spacer(1, 2 * mm),
    ])

    donnees = [["Visuel", "Repère", "CID", "Dimensions", "Configuration", "Quantité", "Poids"]]
    poids_total = 0.0
    total_unites = 0
    total_troncons = 0
    for ligne in lignes:
        resultat = calculer(ligne)
        poids = resultat["poids_kg"]
        poids_total += poids
        dimensions = ", ".join(
            f"{k}={v:g}" for k, v in ligne["cotes"].items()
        ) or "Sur-mesure"
        connecteurs = ligne["con1"] + (
            f" / {ligne['con2']}" if ligne["con2"] else ""
        )
        if ligne["cid"] == "CID 1":
            troncons = math.ceil(float(ligne["qte"]) / 1.5)
            total_troncons += troncons
            quantite = Paragraph(
                f"{ligne['qte']:g} ml<br/><b>{troncons} tronçon{'s' if troncons > 1 else ''}</b>",
                styles["CelluleCentre"],
            )
        else:
            total_unites += int(ligne["qte"])
            quantite = Paragraph(f"x{int(ligne['qte'])}", styles["CelluleCentre"])
        donnees.append([
            _visuel_cid(ligne, dossier_assets),
            Paragraph(escape(str(ligne["repere"])), styles["CelluleCentre"]),
            Paragraph(escape(ligne["cid"]), styles["CelluleCentre"]),
            Paragraph(escape(dimensions), styles["Cellule"]),
            Paragraph(
                f"{escape(ligne['epaisseur'])}<br/>{escape(connecteurs)}",
                styles["Cellule"],
            ),
            quantite,
            Paragraph(f"<b>{poids:.2f} kg</b>", styles["CelluleCentre"]),
        ])

    tableau = Table(
        donnees,
        repeatRows=1,
        colWidths=[20 * mm, 15 * mm, 17 * mm, 36 * mm, 41 * mm, 30 * mm, 25 * mm],
    )
    tableau.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BLEU),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 7.2),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.35, BORDURE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BLEU_CLAIR]),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    elements.extend([tableau, Spacer(1, 5 * mm)])

    resume = Table(
        [[
            Paragraph(f"<b>{total_unites}</b><br/>pièce(s) hors gaine", styles["CelluleCentre"]),
            Paragraph(f"<b>{total_troncons}</b><br/>tronçon(s) de gaine", styles["CelluleCentre"]),
            Paragraph(f"<b>{poids_total:.2f} kg</b><br/>poids total", styles["CelluleCentre"]),
        ]],
        colWidths=[58 * mm, 58 * mm, 58 * mm],
    )
    resume.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BLEU_CLAIR),
        ("BOX", (0, 0), (-1, -1), 0.6, BORDURE),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, BORDURE),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    elements.extend([
        resume,
        Spacer(1, 2 * mm),
        Paragraph(
            "* Pour les gaines CID 1 : nombre de tronçons = longueur en ml ÷ 1,5, "
            "arrondi au tronçon supérieur.",
            styles["Meta"],
        ),
    ])

    def pied_page(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(BORDURE)
        canvas.line(12 * mm, 12 * mm, 198 * mm, 12 * mm)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(GRIS)
        canvas.drawString(12 * mm, 7 * mm, "CLIMAIR - Récapitulatif de commande")
        canvas.drawRightString(198 * mm, 7 * mm, f"Page {doc.page}")
        canvas.restoreState()

    document.build(elements, onFirstPage=pied_page, onLaterPages=pied_page)
    return flux.getvalue()
