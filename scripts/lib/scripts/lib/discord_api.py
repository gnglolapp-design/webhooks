import re
import time
import requests
from typing import Any, Dict
from .utils import sha, stable_hash

UA = "Mozilla/5.0 (GitHubActions; 7DSOriginDiscordSync/4.0)"

def http_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s

def redact_webhook_url(url: str) -> str:
    return re.sub(r"(https?://discord\.com/api/webhooks/\d+/)[^/?]+", r"\1[REDACTED]", url)

def discord_request_raw(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    while True:
        r = session.request(method, url, timeout=60, **kwargs)
        if r.status_code == 429:
            try:
                data = r.json()
                retry = float(data.get("retry_after", 1.0))
            except Exception:
                retry = 1.0
            time.sleep(retry + 0.25)
            continue
        return r

def ensure_webhook_fingerprint(state: Dict[str, Any], webhook_key: str, webhook_url: str) -> None:
    fp = sha(webhook_url)
    old = state["webhook_fp"].get(webhook_key)
    if old and old != fp:
        raise RuntimeError(
            f"Webhook '{webhook_key}' a changé (empreinte différente). "
            f"Pour éviter les doublons, arrêt. "
            f"Supprime les anciens messages OU reset volontairement state/state.json."
        )
    state["webhook_fp"][webhook_key] = fp

def upsert_message(
    session: requests.Session,
    state: Dict[str, Any],
    webhook_key: str,
    webhook_url: str,
    item_key: str,
    payload: Dict[str, Any],
    source_hash: str,
    strict_no_duplicate: bool = True,
) -> None:
    payload.setdefault("allowed_mentions", {"parse": []})
    ensure_webhook_fingerprint(state, webhook_key, webhook_url)

    item = state["items"].get(item_key, {})
    prev_source = item.get("source_hash")
    if prev_source == source_hash:
        return

    payload_hash = stable_hash(payload)
    message_id = item.get("message_id")

    if message_id:
        edit_url = f"{webhook_url}/messages/{message_id}"
        r = discord_request_raw(session, "PATCH", edit_url, json=payload)

        if r.status_code == 404:
            # message supprimé à la main : soit on reposte, soit on bloque (anti-doublon strict)
            if strict_no_duplicate:
                raise RuntimeError(
                    f"Message introuvable (404) pour {item_key}. "
                    f"Anti-doublons strict activé : je ne reposte pas. "
                    f"Supprime/clean le state pour repartir propre."
                )
            create_url = f"{webhook_url}?wait=true"
            resp = discord_request_raw(session, "POST", create_url, json=payload)
            if not (200 <= resp.status_code < 300):
                raise RuntimeError(f"Discord {resp.status_code} POST {redact_webhook_url(create_url)}: {resp.text[:300]!r}")
            data = resp.json()
            state["items"][item_key] = {"message_id": data["id"], "payload_hash": payload_hash, "source_hash": source_hash}
            return

        if not (200 <= r.status_code < 300):
            raise RuntimeError(f"Discord {r.status_code} PATCH {redact_webhook_url(edit_url)}: {r.text[:300]!r}")

        state["items"][item_key] = {"message_id": message_id, "payload_hash": payload_hash, "source_hash": source_hash}
        return

    create_url = f"{webhook_url}?wait=true"
    resp = discord_request_raw(session, "POST", create_url, json=payload)
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(f"Discord {resp.status_code} POST {redact_webhook_url(create_url)}: {resp.text[:300]!r}")
    data = resp.json()
    state["items"][item_key] = {"message_id": data["id"], "payload_hash": payload_hash, "source_hash": source_hash}