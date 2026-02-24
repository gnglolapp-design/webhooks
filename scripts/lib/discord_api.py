import json
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests


@dataclass(frozen=True)
class Webhook:
    id: str
    token: str

    @property
    def base_url(self) -> str:
        return f"https://discord.com/api/webhooks/{self.id}/{self.token}"


def parse_webhook(url: str) -> Webhook:
    m = re.match(r"^https://discord\.com/api/webhooks/(\d+)/([^/]+)$", url.strip())
    if not m:
        raise ValueError("Webhook URL invalide")
    return Webhook(id=m.group(1), token=m.group(2))


def _sleep_rate_limit(resp: requests.Response) -> None:
    try:
        data = resp.json()
        retry_after = float(data.get("retry_after", 1.0))
    except Exception:
        retry_after = 1.0
    time.sleep(max(1.0, retry_after))


def discord_request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    json_payload: Optional[Dict[str, Any]] = None,
    files: Optional[List[Tuple[str, bytes, str]]] = None,
    timeout: int = 45,
    max_retries: int = 6,
) -> requests.Response:
    """
    - Gère 429 (rate limit)
    - Retries sur 5xx/timeout
    """
    for attempt in range(max_retries):
        try:
            if files:
                multipart = {}
                for i, (filename, content, mime) in enumerate(files):
                    multipart[f"files[{i}]"] = (filename, content, mime)

                data = {
                    "payload_json": json.dumps(json_payload or {}, ensure_ascii=False)
                }
                resp = session.request(
                    method,
                    url,
                    data=data,
                    files=multipart,
                    timeout=timeout,
                )
            else:
                resp = session.request(
                    method,
                    url,
                    json=json_payload,
                    timeout=timeout,
                )
        except requests.RequestException:
            time.sleep(1.5 * (attempt + 1))
            continue

        if resp.status_code == 429:
            _sleep_rate_limit(resp)
            continue

        if 500 <= resp.status_code < 600:
            time.sleep(1.5 * (attempt + 1))
            continue

        return resp

    return resp  # dernier


def post_message(
    session: requests.Session,
    webhook_url: str,
    payload: Dict[str, Any],
    files: Optional[List[Tuple[str, bytes, str]]] = None,
) -> Dict[str, Any]:
    wh = parse_webhook(webhook_url)
    url = f"{wh.base_url}?wait=true"
    resp = discord_request(session, "POST", url, json_payload=payload, files=files)
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"POST webhook échoué: {resp.status_code} {resp.text}")
    if resp.status_code == 204:
        return {}
    return resp.json()


def delete_message(
    session: requests.Session,
    webhook_url: str,
    message_id: str,
) -> None:
    wh = parse_webhook(webhook_url)
    url = f"{wh.base_url}/messages/{message_id}"
    resp = discord_request(session, "DELETE", url)
    # 404 => déjà supprimé, on ignore
    if resp.status_code not in (204, 200, 404):
        raise RuntimeError(f"DELETE échoué: {resp.status_code} {resp.text}")


def replace_message(
    session: requests.Session,
    webhook_url: str,
    old_message_id: Optional[str],
    payload: Dict[str, Any],
    files: Optional[List[Tuple[str, bytes, str]]] = None,
) -> str:
    """
    Remplacement robuste:
    1) POST nouveau message (avec screenshots)
    2) DELETE ancien message (si présent)
    """
    new_msg = post_message(session, webhook_url, payload, files=files)
    new_id = str(new_msg.get("id", "")) if new_msg else ""
    if not new_id:
        # si Discord ne renvoie pas l'id (rare), on laisse vide => l'état ne sera pas stable
        raise RuntimeError("Discord n'a pas renvoyé l'ID du message (wait=true requis).")

    if old_message_id:
        try:
            delete_message(session, webhook_url, old_message_id)
        except Exception:
            pass

    return new_id
