"""Lecture assistée de croquis de gaines. Aucune fabrication sans validation humaine."""

from __future__ import annotations

import base64
import io
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from PIL import Image, ImageOps
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

from domain import CATALOGUE


class AnalyseErreur(Exception):
    pass


def preparer_image(contenu: bytes) -> str:
    if len(contenu) > 15 * 1024 * 1024:
        raise AnalyseErreur("Image trop volumineuse (maximum 15 Mo).")
    try:
        with Image.open(io.BytesIO(contenu)) as originale:
            image = ImageOps.exif_transpose(originale).convert("RGB")
            image.thumbnail((1800, 1800))
            sortie = io.BytesIO()
            image.save(sortie, format="JPEG", quality=85)
    except Exception as exc:
        raise AnalyseErreur("Impossible de lire cette image. Essayez un JPG ou PNG.") from exc
    return "data:image/jpeg;base64," + base64.b64encode(sortie.getvalue()).decode("ascii")


def analyser_croquis(contenu: bytes, cle_api: str, modele: str = "gpt-4.1-mini") -> dict:
    """Extrait une seule pièce. Les dimensions incertaines restent nulles."""
    if not cle_api:
        raise AnalyseErreur("Clé OPENAI_API_KEY non configurée.")
    definitions = [
        {"cid": cid, "nom": cat["titre"],
         "champs": {k: lib for k, lib, *_ in cat["cotes"]}}
        for cid, cat in CATALOGUE.items() if cid != "CID 999"
    ]
    instruction = (
        "Tu analyses un croquis de tôlerie/ventilation rectangulaire CLIMAIR. "
        "Réponds UNIQUEMENT par un objet JSON avec cid, cotes, quantite, "
        "incertitudes (tableau de chaînes), explication (chaîne). "
        "Choisis un CID du catalogue ci-dessous ou CID 999 si non identifiable. "
        "Utilise les noms EXACTS des champs. Toutes les cotes sont en millimètres, "
        "angles en degrés. Ne devine JAMAIS une valeur absente ou illisible: null. "
        "Une cote écrite sur le dessin prime sur une estimation visuelle. "
        "Si plusieurs pièces apparaissent, indique de séparer les photos dans incertitudes. "
        "Ne confonds pas déport et départ. Catalogue: " + json.dumps(definitions, ensure_ascii=False)
    )
    corps = {
        "model": modele,
        "messages": [
            {"role": "system", "content": instruction},
            {"role": "user", "content": [
                {"type": "text", "text": "Lis cette pièce et ses cotes. Signale les ambiguïtés."},
                {"type": "image_url", "image_url": {"url": preparer_image(contenu), "detail": "high"}},
            ]},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }
    requete = Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(corps).encode("utf-8"),
        headers={"Authorization": "Bearer " + cle_api, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(requete, timeout=55) as reponse:
            resultat = json.load(reponse)
        brut = json.loads(resultat["choices"][0]["message"]["content"])
    except HTTPError as exc:
        raise AnalyseErreur(f"Service IA indisponible (HTTP {exc.code}). Vérifiez la clé et le quota.") from exc
    except (URLError, TimeoutError) as exc:
        raise AnalyseErreur("Connexion au service IA impossible. Réessayez.") from exc
    except (KeyError, ValueError, TypeError) as exc:
        raise AnalyseErreur("Réponse IA non exploitable. Réessayez avec une photo nette.") from exc
    if not isinstance(brut, dict):
        raise AnalyseErreur("Format de réponse IA incorrect.")
    cid = brut.get("cid")
    if cid not in CATALOGUE:
        cid = "CID 999"
    champs = {k: (mini, maxi) for k, _, _, mini, maxi in CATALOGUE[cid]["cotes"]}
    brutes = brut.get("cotes") if isinstance(brut.get("cotes"), dict) else {}
    cotes, manquantes = {}, []
    for cle, (mini, maxi) in champs.items():
        try:
            valeur = float(brutes[cle])
            if not mini <= valeur <= maxi:
                raise ValueError
            cotes[cle] = valeur
        except (KeyError, ValueError, TypeError):
            manquantes.append(cle)
    try:
        quantite = int(brut.get("quantite"))
        if not 1 <= quantite <= 10000:
            raise ValueError
    except (ValueError, TypeError):
        quantite = None
    incertitudes = brut.get("incertitudes", [])
    if not isinstance(incertitudes, list):
        incertitudes = []
    return {
        "cid": cid, "cotes": cotes, "manquantes": manquantes,
        "quantite": quantite,
        "incertitudes": [str(x)[:180] for x in incertitudes[:12]],
        "explication": str(brut.get("explication", ""))[:500],
    }


def normaliser_listing(brut: dict) -> dict:
    """Filtre les sorties IA selon le catalogue, sans inventer de dimensions."""
    if not isinstance(brut, dict) or not isinstance(brut.get("pieces"), list):
        raise AnalyseErreur("L'IA n'a pas retourné de liste de pièces exploitable.")
    pieces = []
    for i, item in enumerate(brut["pieces"][:30], 1):
        if not isinstance(item, dict):
            continue
        cid = item.get("cid") if item.get("cid") in CATALOGUE else "CID 999"
        cat = CATALOGUE[cid]
        source = item.get("cotes") if isinstance(item.get("cotes"), dict) else {}
        cotes, manquantes = {}, []
        for cle, _, _, minimum, maximum in cat["cotes"]:
            try:
                val = float(source[cle])
                if not minimum <= val <= maximum:
                    raise ValueError
                cotes[cle] = val
            except (KeyError, ValueError, TypeError):
                manquantes.append(cle)
        try:
            quantite = int(item.get("quantite"))
            if not 1 <= quantite <= 10000:
                raise ValueError
        except (ValueError, TypeError):
            quantite = None
        try:
            longueur_mm = float(item.get("longueur_mm"))
            if not 1 <= longueur_mm <= 10000:
                raise ValueError
        except (ValueError, TypeError):
            longueur_mm = None
        alertes = item.get("incertitudes", [])
        pieces.append({
            "repere": str(item.get("repere") or i)[:50].strip(),
            "cid": cid, "cotes": cotes, "manquantes": manquantes,
            "quantite": quantite, "longueur_mm": longueur_mm,
            "incertitudes": [str(x)[:180] for x in alertes[:8]] if isinstance(alertes, list) else [],
        })
    return {
        "pieces": pieces,
        "incertitudes_globales": [str(x)[:200] for x in brut.get("incertitudes_globales", [])[:15]]
        if isinstance(brut.get("incertitudes_globales"), list) else [],
        "complet": brut.get("complet") is True and len(brut["pieces"]) <= 30,
    }


def analyser_listing(contenu: bytes, cle_api: str, modele: str = "gpt-4.1-mini") -> dict:
    """Repère plusieurs dessins sur une feuille sans autoriser leur fabrication."""
    if not cle_api:
        raise AnalyseErreur("Clé OPENAI_API_KEY non configurée.")
    catalogue = [
        {"cid": cid, "nom": cat["titre"], "champs": {k: lib for k, lib, *_ in cat["cotes"]}}
        for cid, cat in CATALOGUE.items() if cid != "CID 999"
    ]
    instruction = (
        "Tu es un métreur expérimenté de gaines de ventilation. Une photo peut présenter "
        "PLUSIEURS dessins, organisés en zones et repères. Retourne strictement un JSON "
        "avec pieces (tableau), incertitudes_globales (tableau) et complet (booléen). "
        "Pour chaque dessin DISTINCT: repere, cid, cotes (objet), quantite (nombre entier "
        "ou null), longueur_mm (nombre ou null pour gaines droites CID 1), incertitudes (tableau). "
        "Sépare les pièces une par une, sans fusionner deux dessins. Conserve leur repère "
        "manuscrit et la zone dans le repère si possible. Une annotation ×12 est une quantité. "
        "Les cotes sont en mm et les angles en degrés. N'invente AUCUNE cote absente: null. "
        "Ne confonds pas dimensions d'entrée et sortie, diamètre Ø, rayon R et déport. "
        "Toute cote, quantité ou identification ambiguë doit figurer dans incertitudes. "
        "CID 999 si non classable. complet doit être false si un croquis n'est pas traité. "
        "Maximum 30 pièces par image; si davantage, complet=false et signaler la troncature. "
        "Catalogue: " + json.dumps(catalogue, ensure_ascii=False)
    )
    corps = {
        "model": modele,
        "messages": [
            {"role": "system", "content": instruction},
            {"role": "user", "content": [
                {"type": "text", "text": "Dénombre et lis TOUS les croquis indépendants de cette feuille."},
                {"type": "image_url", "image_url": {"url": preparer_image(contenu), "detail": "high"}},
            ]},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }
    requete = Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(corps).encode("utf-8"),
        headers={"Authorization": "Bearer " + cle_api, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(requete, timeout=90) as reponse:
            resultat = json.load(reponse)
        brut = json.loads(resultat["choices"][0]["message"]["content"])
        return normaliser_listing(brut)
    except HTTPError as exc:
        raise AnalyseErreur(f"Service IA indisponible (HTTP {exc.code}).") from exc
    except (URLError, TimeoutError) as exc:
        raise AnalyseErreur("Connexion IA impossible ou délai dépassé.") from exc
    except (KeyError, ValueError, TypeError) as exc:
        raise AnalyseErreur("Réponse IA incorrecte. Essayez une photo plus nette.") from exc
