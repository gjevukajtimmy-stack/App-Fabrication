"""Persistance SQLite de CLIMAIR.

Le module conserve l'interface de données simple utilisée par l'application,
mais toutes les écritures métier sont désormais durables et transactionnelles.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


class Database:
    """Dépôt SQLite minimal et sûr pour les données métier de l'application."""

    def __init__(self, path: str = os.path.join("data", "climair.db")) -> None:
        self.path = path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._initialiser()
        self.sauvegarde_quotidienne()

    @contextmanager
    def _connexion(self):
        connexion = sqlite3.connect(self.path, timeout=15)
        connexion.row_factory = sqlite3.Row
        connexion.execute("PRAGMA foreign_keys = ON")
        connexion.execute("PRAGMA busy_timeout = 15000")
        try:
            yield connexion
            connexion.commit()
        except Exception:
            connexion.rollback()
            raise
        finally:
            connexion.close()

    def _initialiser(self) -> None:
        with self._lock, self._connexion() as connexion:
            connexion.execute("PRAGMA journal_mode = WAL")
            connexion.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    email TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL,
                    entreprise TEXT NOT NULL,
                    chantier TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS listings (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL,
                    entreprise TEXT NOT NULL,
                    chantier TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS company_prices (
                    entreprise TEXT PRIMARY KEY,
                    prix REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS processed (
                    entreprise TEXT NOT NULL,
                    chantier TEXT NOT NULL,
                    nombre INTEGER NOT NULL,
                    PRIMARY KEY (entreprise, chantier)
                );
                CREATE TABLE IF NOT EXISTS completed (
                    entreprise TEXT NOT NULL,
                    chantier TEXT NOT NULL,
                    date_fin TEXT NOT NULL,
                    PRIMARY KEY (entreprise, chantier)
                );
                CREATE TABLE IF NOT EXISTS mail_errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS drafts (
                    email TEXT PRIMARY KEY,
                    chantier TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS order_status (
                    reference TEXT PRIMARY KEY,
                    entreprise TEXT NOT NULL,
                    chantier TEXT NOT NULL,
                    type_envoi TEXT NOT NULL,
                    statut TEXT NOT NULL,
                    note_client TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS order_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL,
                    statut TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    date_evenement TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS order_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL,
                    auteur_type TEXT NOT NULL,
                    auteur TEXT NOT NULL,
                    message TEXT NOT NULL,
                    date_message TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    acteur TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '',
                    date_action TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS password_resets (
                    email TEXT PRIMARY KEY,
                    code_hash TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS email_verifications (
                    email TEXT PRIMARY KEY,
                    code_hash TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                """
            )
            self._migrer_statuts(connexion)

    def _migrer_statuts(self, connexion) -> None:
        """Crée les statuts par référence pour les anciennes commandes."""
        termines = {
            (r["entreprise"], r["chantier"]): r["date_fin"]
            for r in connexion.execute("SELECT * FROM completed")
        }
        traites = {
            (r["entreprise"], r["chantier"])
            for r in connexion.execute("SELECT entreprise, chantier FROM processed")
        }
        groupes = {}
        for row in connexion.execute("SELECT payload FROM orders ORDER BY rowid"):
            ligne = self._charger(row["payload"])
            reference = ligne.get("commande_id") or f"LEGACY-{ligne['id']}"
            groupes.setdefault(reference, ligne)
        for reference, ligne in groupes.items():
            cle = (ligne["entreprise"], ligne["chantier"])
            statut = "Terminée" if cle in termines else "En préparation" if cle in traites else "Reçue"
            date = termines.get(cle) or ligne.get("date") or datetime.now().strftime("%d/%m/%Y %H:%M")
            connexion.execute(
                """INSERT OR IGNORE INTO order_status
                   (reference, entreprise, chantier, type_envoi, statut, updated_at)
                   VALUES (?, ?, ?, 'commande', ?, ?)""",
                (reference, ligne["entreprise"], ligne["chantier"], statut, date),
            )
        for row in connexion.execute("SELECT payload FROM listings ORDER BY rowid"):
            listing = self._charger(row["payload"])
            reference = f"LIST-{listing['id'].upper()}"
            cle = (listing["entreprise"], listing["chantier"])
            statut = "Terminée" if cle in termines else "En préparation" if cle in traites else "Reçue"
            date = termines.get(cle) or listing.get("date") or datetime.now().strftime("%d/%m/%Y %H:%M")
            connexion.execute(
                """INSERT OR IGNORE INTO order_status
                   (reference, entreprise, chantier, type_envoi, statut, updated_at)
                   VALUES (?, ?, ?, 'listing', ?, ?)""",
                (reference, listing["entreprise"], listing["chantier"], statut, date),
            )

    @staticmethod
    def _json(valeur: Any) -> str:
        return json.dumps(valeur, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _charger(payload: str) -> Any:
        return json.loads(payload)

    def snapshot(self) -> dict[str, Any]:
        """Retourne un instantané compatible avec l'ancien dictionnaire STORE."""
        with self._lock, self._connexion() as connexion:
            users = {
                ligne["email"]: self._charger(ligne["payload"])
                for ligne in connexion.execute("SELECT email, payload FROM users")
            }
            orders = [
                self._charger(ligne["payload"])
                for ligne in connexion.execute("SELECT payload FROM orders ORDER BY rowid")
            ]
            listings = [
                self._charger(ligne["payload"])
                for ligne in connexion.execute("SELECT payload FROM listings ORDER BY rowid")
            ]
            prix_kg = {
                ligne["entreprise"]: float(ligne["prix"])
                for ligne in connexion.execute("SELECT entreprise, prix FROM company_prices")
            }
            traites = {
                (ligne["entreprise"], ligne["chantier"]): int(ligne["nombre"])
                for ligne in connexion.execute(
                    "SELECT entreprise, chantier, nombre FROM processed"
                )
            }
            termines = {
                (ligne["entreprise"], ligne["chantier"]): ligne["date_fin"]
                for ligne in connexion.execute(
                    "SELECT entreprise, chantier, date_fin FROM completed"
                )
            }
            mail_erreurs = [
                ligne["message"]
                for ligne in connexion.execute(
                    "SELECT message FROM mail_errors ORDER BY id DESC LIMIT 50"
                )
            ][::-1]
            statuts_commandes = {
                ligne["reference"]: dict(ligne)
                for ligne in connexion.execute("SELECT * FROM order_status")
            }
            evenements_commandes = {}
            for ligne in connexion.execute("SELECT * FROM order_events ORDER BY id"):
                evenements_commandes.setdefault(ligne["reference"], []).append(dict(ligne))
            messages_commandes = {}
            for ligne in connexion.execute("SELECT * FROM order_messages ORDER BY id"):
                messages_commandes.setdefault(ligne["reference"], []).append(dict(ligne))
        return {
            "users": users,
            "orders": orders,
            "listings": listings,
            "prix_kg": prix_kg,
            "traites": traites,
            "termines": termines,
            "mail_erreurs": mail_erreurs,
            "statuts_commandes": statuts_commandes,
            "evenements_commandes": evenements_commandes,
            "messages_commandes": messages_commandes,
        }

    def ajouter_utilisateur(self, utilisateur: dict[str, Any]) -> None:
        with self._lock, self._connexion() as connexion:
            connexion.execute(
                "INSERT INTO users(email, payload) VALUES (?, ?)",
                (utilisateur["email"], self._json(utilisateur)),
            )

    def definir_statut_utilisateur(self, email: str, statut: str) -> None:
        with self._lock, self._connexion() as connexion:
            ligne = connexion.execute(
                "SELECT payload FROM users WHERE email = ?", (email,)
            ).fetchone()
            if not ligne:
                return
            utilisateur = self._charger(ligne["payload"])
            utilisateur["statut"] = statut
            connexion.execute(
                "UPDATE users SET payload = ? WHERE email = ?",
                (self._json(utilisateur), email),
            )

    def mettre_a_jour_utilisateur(self, utilisateur: dict[str, Any]) -> None:
        with self._lock, self._connexion() as connexion:
            connexion.execute(
                "UPDATE users SET payload = ? WHERE email = ?",
                (self._json(utilisateur), utilisateur["email"]),
            )

    def ajouter_commandes(self, commandes: Iterable[dict[str, Any]]) -> None:
        lignes = list(commandes)
        with self._lock, self._connexion() as connexion:
            connexion.executemany(
                """INSERT INTO orders(id, email, entreprise, chantier, payload)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    (
                        ligne["id"], ligne["email"], ligne["entreprise"],
                        ligne["chantier"], self._json(ligne),
                    )
                    for ligne in lignes
                ],
            )
            if lignes:
                premiere = lignes[0]
                reference = premiere.get("commande_id") or f"LEGACY-{premiere['id']}"
                date = premiere.get("date") or datetime.now().strftime("%d/%m/%Y %H:%M")
                connexion.execute(
                    """INSERT OR REPLACE INTO order_status
                       (reference, entreprise, chantier, type_envoi, statut, note_client, updated_at)
                       VALUES (?, ?, ?, 'commande', 'Reçue', '', ?)""",
                    (reference, premiere["entreprise"], premiere["chantier"], date),
                )
                connexion.execute(
                    """INSERT INTO order_events(reference, statut, note, date_evenement)
                       VALUES (?, 'Reçue', '', ?)""",
                    (reference, date),
                )

    def mettre_a_jour_commande(self, commande: dict[str, Any]) -> None:
        with self._lock, self._connexion() as connexion:
            connexion.execute(
                "UPDATE orders SET payload = ? WHERE id = ?",
                (self._json(commande), commande["id"]),
            )

    def ajouter_listing(self, listing: dict[str, Any]) -> None:
        with self._lock, self._connexion() as connexion:
            connexion.execute(
                """INSERT INTO listings(id, email, entreprise, chantier, payload)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    listing["id"], listing["email"], listing["entreprise"],
                    listing["chantier"], self._json(listing),
                ),
            )
            reference = f"LIST-{listing['id'].upper()}"
            connexion.execute(
                """INSERT OR REPLACE INTO order_status
                   (reference, entreprise, chantier, type_envoi, statut, note_client, updated_at)
                   VALUES (?, ?, ?, 'listing', 'Reçue', '', ?)""",
                (reference, listing["entreprise"], listing["chantier"], listing["date"]),
            )
            connexion.execute(
                """INSERT INTO order_events(reference, statut, note, date_evenement)
                   VALUES (?, 'Reçue', '', ?)""",
                (reference, listing["date"]),
            )

    def definir_poids_listing(self, listing: dict[str, Any], poids: float) -> None:
        listing["poids_kg"] = float(poids)
        with self._lock, self._connexion() as connexion:
            connexion.execute(
                "UPDATE listings SET payload = ? WHERE id = ?",
                (self._json(listing), listing["id"]),
            )

    def definir_prix(self, entreprise: str, prix: float) -> None:
        with self._lock, self._connexion() as connexion:
            connexion.execute(
                """INSERT INTO company_prices(entreprise, prix) VALUES (?, ?)
                   ON CONFLICT(entreprise) DO UPDATE SET prix = excluded.prix""",
                (entreprise, float(prix)),
            )

    def ajouter_erreur_mail(self, message: str) -> None:
        with self._lock, self._connexion() as connexion:
            connexion.execute("INSERT INTO mail_errors(message) VALUES (?)", (message,))

    def enregistrer_brouillon(
        self, email: str, chantier: str, panier: list[dict[str, Any]]
    ) -> None:
        """Enregistre le panier courant afin de pouvoir le reprendre plus tard."""
        with self._lock, self._connexion() as connexion:
            connexion.execute(
                """INSERT INTO drafts(email, chantier, payload, updated_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(email) DO UPDATE SET
                       chantier = excluded.chantier,
                       payload = excluded.payload,
                       updated_at = CURRENT_TIMESTAMP""",
                (email, chantier, self._json(panier)),
            )

    def charger_brouillon(self, email: str) -> dict[str, Any] | None:
        with self._lock, self._connexion() as connexion:
            ligne = connexion.execute(
                "SELECT chantier, payload, updated_at FROM drafts WHERE email = ?",
                (email,),
            ).fetchone()
        if not ligne:
            return None
        return {
            "chantier": ligne["chantier"],
            "panier": self._charger(ligne["payload"]),
            "updated_at": ligne["updated_at"],
        }

    def photos_brouillons(self) -> list[str]:
        """Retourne les photos encore utilisées par les paniers sauvegardés."""
        photos = []
        with self._lock, self._connexion() as connexion:
            lignes = connexion.execute("SELECT payload FROM drafts").fetchall()
        for ligne in lignes:
            for piece in self._charger(ligne["payload"]):
                if piece.get("photo"):
                    photos.append(piece["photo"])
        return photos

    def supprimer_brouillon(self, email: str) -> None:
        with self._lock, self._connexion() as connexion:
            connexion.execute("DELETE FROM drafts WHERE email = ?", (email,))

    def definir_statut_commande(self, reference: str, statut: str, note: str = "") -> None:
        date = datetime.now().strftime("%d/%m/%Y %H:%M")
        with self._lock, self._connexion() as connexion:
            courant = connexion.execute(
                "SELECT statut, note_client FROM order_status WHERE reference = ?", (reference,)
            ).fetchone()
            if not courant:
                return
            connexion.execute(
                """UPDATE order_status SET statut = ?, note_client = ?, updated_at = ?
                   WHERE reference = ?""",
                (statut, note.strip(), date, reference),
            )
            if courant["statut"] != statut or courant["note_client"] != note.strip():
                connexion.execute(
                    """INSERT INTO order_events(reference, statut, note, date_evenement)
                       VALUES (?, ?, ?, ?)""",
                    (reference, statut, note.strip(), date),
                )

    def ajouter_message_commande(
        self, reference: str, auteur_type: str, auteur: str, message: str
    ) -> None:
        texte = message.strip()
        if not texte:
            return
        with self._lock, self._connexion() as connexion:
            connexion.execute(
                """INSERT INTO order_messages
                   (reference, auteur_type, auteur, message, date_message)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    reference,
                    auteur_type,
                    auteur,
                    texte,
                    datetime.now().strftime("%d/%m/%Y %H:%M"),
                ),
            )

    def ajouter_audit(self, acteur: str, action: str, details: str = "") -> None:
        with self._lock, self._connexion() as connexion:
            connexion.execute(
                """INSERT INTO audit_logs(acteur, action, details, date_action)
                   VALUES (?, ?, ?, ?)""",
                (acteur, action, details, datetime.now().strftime("%d/%m/%Y %H:%M")),
            )

    def lire_audit(self, acteur: str | None = None, limite: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connexion() as connexion:
            if acteur:
                lignes = connexion.execute(
                    """SELECT * FROM audit_logs WHERE acteur = ?
                       ORDER BY id DESC LIMIT ?""", (acteur, int(limite)),
                )
            else:
                lignes = connexion.execute(
                    "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (int(limite),)
                )
            return [dict(ligne) for ligne in lignes]

    def enregistrer_code_reset(self, email: str, code_hash: str, duree_minutes: int = 15) -> None:
        expiration = (datetime.now() + timedelta(minutes=duree_minutes)).isoformat()
        with self._lock, self._connexion() as connexion:
            connexion.execute(
                """INSERT INTO password_resets(email, code_hash, expires_at) VALUES (?, ?, ?)
                   ON CONFLICT(email) DO UPDATE SET
                   code_hash = excluded.code_hash, expires_at = excluded.expires_at""",
                (email, code_hash, expiration),
            )

    def verifier_code_reset(self, email: str, code_hash: str) -> bool:
        with self._lock, self._connexion() as connexion:
            ligne = connexion.execute(
                "SELECT code_hash, expires_at FROM password_resets WHERE email = ?", (email,)
            ).fetchone()
            valide = bool(
                ligne and ligne["code_hash"] == code_hash
                and datetime.fromisoformat(ligne["expires_at"]) >= datetime.now()
            )
            if valide:
                connexion.execute("DELETE FROM password_resets WHERE email = ?", (email,))
            return valide

    def enregistrer_code_verification(
        self, email: str, code_hash: str, duree_minutes: int = 30
    ) -> None:
        expiration = (datetime.now() + timedelta(minutes=duree_minutes)).isoformat()
        with self._lock, self._connexion() as connexion:
            connexion.execute(
                """INSERT INTO email_verifications(email, code_hash, expires_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(email) DO UPDATE SET
                   code_hash = excluded.code_hash, expires_at = excluded.expires_at""",
                (email, code_hash, expiration),
            )

    def verifier_code_email(self, email: str, code_hash: str) -> bool:
        with self._lock, self._connexion() as connexion:
            ligne = connexion.execute(
                "SELECT code_hash, expires_at FROM email_verifications WHERE email = ?",
                (email,),
            ).fetchone()
            valide = bool(
                ligne and ligne["code_hash"] == code_hash
                and datetime.fromisoformat(ligne["expires_at"]) >= datetime.now()
            )
            if valide:
                connexion.execute("DELETE FROM email_verifications WHERE email = ?", (email,))
            return valide

    def sauvegarde_quotidienne(self) -> str:
        dossier = Path(self.path).resolve().parent / "backups"
        dossier.mkdir(parents=True, exist_ok=True)
        destination = dossier / f"climair-{datetime.now():%Y-%m-%d}.db"
        if destination.exists():
            return str(destination)
        with self._lock:
            source = sqlite3.connect(self.path)
            cible = sqlite3.connect(destination)
            try:
                source.backup(cible)
            finally:
                cible.close()
                source.close()
        return str(destination)

    def definir_traitee(self, entreprise: str, chantier: str, nombre: int) -> None:
        with self._lock, self._connexion() as connexion:
            connexion.execute(
                """INSERT INTO processed(entreprise, chantier, nombre) VALUES (?, ?, ?)
                   ON CONFLICT(entreprise, chantier)
                   DO UPDATE SET nombre = excluded.nombre""",
                (entreprise, chantier, int(nombre)),
            )

    def terminer(self, entreprise: str, chantier: str, date_fin: str, nombre: int) -> None:
        with self._lock, self._connexion() as connexion:
            connexion.execute(
                """INSERT INTO completed(entreprise, chantier, date_fin) VALUES (?, ?, ?)
                   ON CONFLICT(entreprise, chantier)
                   DO UPDATE SET date_fin = excluded.date_fin""",
                (entreprise, chantier, date_fin),
            )
            connexion.execute(
                """INSERT INTO processed(entreprise, chantier, nombre) VALUES (?, ?, ?)
                   ON CONFLICT(entreprise, chantier)
                   DO UPDATE SET nombre = excluded.nombre""",
                (entreprise, chantier, int(nombre)),
            )

    def rouvrir(self, entreprise: str, chantier: str) -> None:
        with self._lock, self._connexion() as connexion:
            connexion.execute(
                "DELETE FROM completed WHERE entreprise = ? AND chantier = ?",
                (entreprise, chantier),
            )

