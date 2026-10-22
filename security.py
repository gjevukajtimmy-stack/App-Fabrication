"""Fonctions de sécurité sans dépendance à Streamlit."""

import hashlib
import hmac


ITERATIONS_MDP = 600_000


def hash_mdp(mot_de_passe: str, sel: str, iterations: int = ITERATIONS_MDP) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", mot_de_passe.encode(), sel.encode(), iterations
    ).hex()


def verifier_mdp(
    mot_de_passe: str,
    sel: str,
    empreinte: str,
    iterations: int = ITERATIONS_MDP,
) -> bool:
    return hmac.compare_digest(empreinte, hash_mdp(mot_de_passe, sel, iterations))


def mot_de_passe_robuste(mot_de_passe: str) -> bool:
    """Exige 12 caractères et plusieurs familles de caractères."""
    return (
        len(mot_de_passe) >= 12
        and any(c.islower() for c in mot_de_passe)
        and any(c.isupper() for c in mot_de_passe)
        and any(c.isdigit() for c in mot_de_passe)
        and any(not c.isalnum() for c in mot_de_passe)
    )

