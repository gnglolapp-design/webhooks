import json
import time
from typing import Any, Dict, Optional, Tuple

import requests


DISCORD_API_TIMEOUT = 30
MAX_RETRIES = 6


class DiscordHTTPError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"Discord HTTP {status} sur {url} :: {body[:400]}")
        self.status = status
        self.url = url
        self.body = body


def _sleep_backoff(attempt: int) -> None:
    # backoff simple (1s, 2s, 4s, ...)
    time.sleep(min(2 ** attempt, 20))


def discord_request(
    session: requests.Session,
    method: str,
    url: str,
    json_payload: Optional[Dict[str, Any]] = None,
    timeout: int = DISCORD_API_TIMEOUT,
    allow_404: bool = False,
) -> Tuple[int, Optional[Dict[str, Any]], str]:
    """
    Retourne: (status_code, json|None, text)
    Gère 429 (rate limit) avec retries.
    """
    headers = {
        "User-Agent": "7DS-Origin-DB-Sync/1.0",
        "Content-Type": "application/json",
    }

    last_text = ""
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.request(
                method=method.upper(),
                url=url,
                headers=headers,
                json=json_payload,
                timeout=timeout,
            )
        except requests.RequestException as e:
            last_text = str(e)
            _sleep_backoff(attempt)
            continue

        last_text = resp.text or ""

        # Rate limit
        if resp.status_code == 429:
            try:
                data = resp.json()
                retry_after = float(data.get("retry_after", 1.0))
            except Exception:
                retry_after = 1.0
            time.sleep(min(retry_after, 10.0))
            continue

        # OK
        if 200 <= resp.status_code < 300:
            if resp.text:
                try:
                    return resp.status_code, resp.json(), last_text
                except Exception:
                    return resp.status_code, None, last_text
            return resp.status_code, None, last_text

        # 404 autorisé (utile pour savoir si le message existe)
        if resp.status_code == 404 and allow_404:
            return resp.status_code, None, last_text

        # Autres erreurs => pas de retry infini; petite tolérance sur 5xx
        if 500 <= resp.status_code < 600 and attempt < MAX_RETRIES - 1:
            _sleep_backoff(attempt)
            continue

        raise DiscordHTTPError(resp.status_code, url, last_text)

    raise DiscordHTTPError(0, url, f"Échec après retries: {last_text}")


def _webhook_base(webhook_url: str) -> str:
    # webhook_url attendu: https://discord.com/api/webhooks/{id}/{token}
    return webhook_url.rstrip("/")


def create_webhook_message(
    session: requests.Session,
    webhook_url: str,
    payload: Dict[str, Any],
) -> str:
    """
    POST -> retourne l'id du message créé.
    """
    url = f"{_webhook_base(webhook_url)}?wait=true"
    status, data, _ = discord_request(session, "POST", url, json_payload=payload)
    if not data or "id" not in data:
        raise RuntimeError(f"Réponse Discord inattendue à la création (status={status}).")
    return str(data["id"])


def edit_webhook_message(
    session: requests.Session,
    webhook_url: str,
    message_id: str,
    payload: Dict[str, Any],
) -> Tuple[bool, str]:
    """
    PATCH -> (existe, texte_erreur)
    """
    url = f"{_webhook_base(webhook_url)}/messages/{message_id}"
    status, _, text = discord_request(
        session, "PATCH", url, json_payload=payload, allow_404=True
    )
    if status == 404:
        return False, text
    return True, ""


def upsert_message(
    session: requests.Session,
    state: Dict[str, Any],
    section: str,
    webhook_url: str,
    item_key: str,
    payload: Dict[str, Any],
    src_hash: str,
    strict_no_duplicate: bool = True,
) -> bool:
    """
    - Si hash identique: ne fait rien.
    - Si message_id existe: tente PATCH.
        - Si PATCH 404: recrée automatiquement (POST) et met à jour le state.
    - Si pas de message_id: POST.
    Retourne True si une action réseau a eu lieu (post/patch).
    """
    if not webhook_url:
        raise RuntimeError(f"Webhook manquant pour section={section}")

    sec = state.setdefault(section, {})
    entry = sec.get(item_key, {}) if isinstance(sec, dict) else {}
    prev_hash = entry.get("hash")
    prev_mid = entry.get("message_id")

    # Anti-spam si inchangé
    if prev_hash and prev_hash == src_hash:
        print(f"[{section.upper()}] {item_key} inchangé -> skip")
        return False

    # Essayer d'éditer si on a un message_id
    if prev_mid:
        exists, _ = edit_webhook_message(session, webhook_url, str(prev_mid), payload)
        if exists:
            sec[item_key] = {"message_id": str(prev_mid), "hash": src_hash}
            print(f"[{section.upper()}] {item_key} -> message mis à jour")
            return True

        # 404: message supprimé / introuvable -> on recrée
        # IMPORTANT: même en strict, il n’y a PAS de doublon possible puisque le message n’existe plus.
        new_id = create_webhook_message(session, webhook_url, payload)
        sec[item_key] = {"message_id": new_id, "hash": src_hash}
        print(f"[{section.upper()}] {item_key} -> message recréé (ancien introuvable)")
        return True

    # Pas de message_id: création
    # En mode strict, on se base sur le hash (et pas sur un message_id) : si hash différent, on poste.
    # Le script global doit contrôler la pagination/limites.
    new_id = create_webhook_message(session, webhook_url, payload)
    sec[item_key] = {"message_id": new_id, "hash": src_hash}
    print(f"[{section.upper()}] {item_key} -> message créé")
    return True
