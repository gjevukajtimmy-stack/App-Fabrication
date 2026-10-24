"""Conversion entre boîtes CLIMAIR normalisées et rectangles du canvas Streamlit."""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageOps

from ai_sketch import normaliser_regions


def preparer_fond(contenu: bytes, max_largeur=760, max_hauteur=1300):
    """Image affichée sans déformation, en pixels de canvas."""
    with Image.open(BytesIO(contenu)) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
    image.thumbnail((max_largeur, max_hauteur))
    return image


def regions_vers_boites(regions, largeur, hauteur):
    return [
        {"label": str(region["repere"]),
         "left": region["bbox"][0] * largeur / 1000,
         "top": region["bbox"][1] * hauteur / 1000,
         "width": (region["bbox"][2] - region["bbox"][0]) * largeur / 1000,
         "height": (region["bbox"][3] - region["bbox"][1]) * hauteur / 1000}
        for region in regions
    ]


def boites_vers_regions(boites, largeur, hauteur):
    """Inverse des coordonnées de canvas ; rejette boîtes hors image et trop petites."""
    if largeur <= 0 or hauteur <= 0:
        raise ValueError("Taille de canvas invalide")
    regions = []
    for index, boite in enumerate(boites, 1):
        try:
            gauche = float(boite["left"])
            haut = float(boite["top"])
            droite = gauche + float(boite["width"])
            bas = haut + float(boite["height"])
        except (ValueError, TypeError, KeyError):
            continue
        if not (0 <= gauche < droite <= largeur and 0 <= haut < bas <= hauteur):
            continue
        regions.append({"repere": str(boite.get("label") or index).strip()[:50],
                        "bbox": [gauche * 1000 / largeur, haut * 1000 / hauteur,
                                 droite * 1000 / largeur, bas * 1000 / hauteur]})
    return normaliser_regions({"regions": regions})
