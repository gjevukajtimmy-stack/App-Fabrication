"""Sauvegardes et inventaire contrôlé des fichiers envoyés."""

from __future__ import annotations

from pathlib import Path


def fichiers_orphelins(dossier, chemins_references):
    racine = Path(dossier).resolve()
    references = {Path(p).resolve() for p in chemins_references if p}
    return [
        str(p) for p in racine.rglob("*")
        if p.is_file() and p.resolve() not in references
    ]
