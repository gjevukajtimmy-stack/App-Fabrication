"""Validation et optimisation des photos envoyées à l'atelier."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from PIL import Image, ImageOps, UnidentifiedImageError

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:  # l'application reste compatible sans HEIC en développement
    pass


TAILLE_MAX = (2200, 2200)
TAILLE_FICHIER_MAX = 20 * 1024 * 1024
NOMBRE_PIXELS_MAX = 40_000_000


def enregistrer_image(source: BinaryIO, dossier: str | Path, prefixe: str = "photo") -> str:
    """Valide une image, corrige son orientation et l'enregistre en JPEG optimisé."""
    donnees = source.getvalue() if hasattr(source, "getvalue") else source.read()
    if len(donnees) > TAILLE_FICHIER_MAX:
        raise ValueError("L’image dépasse la taille maximale autorisée de 20 Mo.")
    try:
        image = Image.open(BytesIO(donnees))
        if image.width * image.height > NOMBRE_PIXELS_MAX:
            raise ValueError("L’image est trop grande (40 millions de pixels maximum).")
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("Le fichier sélectionné n'est pas une image valide.") from exc
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA"):
        fond = Image.new("RGB", image.size, "white")
        fond.paste(image, mask=image.getchannel("A"))
        image = fond
    else:
        image = image.convert("RGB")
    image.thumbnail(TAILLE_MAX, Image.Resampling.LANCZOS)
    chemin = Path(dossier) / f"{prefixe}_{uuid4().hex}.jpg"
    chemin.parent.mkdir(parents=True, exist_ok=True)
    image.save(chemin, "JPEG", quality=86, optimize=True)
    return str(chemin)
