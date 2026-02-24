import json
import time
import requests
from typing import Dict, Any, Optional

from lib.utils import stable_hash, sha256_bytes


class WebhookClient:
    def __init__(self, username: str):
        self.username = username
        self.session = requests.Session()

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        for attempt in range(1, 8):
            r = self.session.request(method, url, timeout=60, **kwargs)
            if r.status_code == 429:
                try:
                    data = r.json()
                    wait = float(data.get("retry_after", 2.0))
                except Exception:
                    wait = 2.0
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                time.sleep(min(2 ** attempt, 15))
                continue
            return r
        return r

    def delete_message(self, webhook_url: str, message_id: str) -> None:
        url = f"{webhook_url}/messages/{message_id}"
        r = self._request("DELETE", url)
        if r.status_code in (200, 204, 404):
            return
        raise RuntimeError(f"Delete failed {r.status_code}: {r.text[:2000]}")

    def post_message(self, webhook_url: str, payload: Dict[str, Any], files: Dict[str, bytes]) -> str:
        url = f"{webhook_url}?wait=true"

        payload = dict(payload)
        payload["username"] = self.username
        payload.setdefault("allowed_mentions", {"parse": []})

        multipart_files = {}
        for idx, (fname, content) in enumerate(files.items()):
            multipart_files[f"files[{idx}]"] = (fname, content, "image/png")

        data = {"payload_json": json.dumps(payload, ensure_ascii=False)}

        r = self._request("POST", url, data=data, files=multipart_files if multipart_files else None)
        if r.status_code not in (200, 204):
            raise RuntimeError(f"POST failed {r.status_code}: {r.text[:2000]}")
        j = r.json()
        return str(j["id"])

    def replace_message(
        self,
        state: Dict[str, Any],
        key: str,
        webhook_url: str,
        payload: Dict[str, Any],
        files: Optional[Dict[str, bytes]] = None,
    ) -> None:
        files = files or {}

        # Hash = payload + hash fichiers
        file_hashes = {name: sha256_bytes(content) for name, content in files.items()}
        new_hash = stable_hash({"payload": payload, "files": file_hashes})

        prev = state.get(key, {})
        if prev.get("hash") == new_hash:
            print(f"[SKIP] {key} inchangé")
            return

        # Remplacement strict: delete + repost (évite les soucis d’edit + attachments)
        old_id = prev.get("message_id")
        if old_id:
            try:
                self.delete_message(webhook_url, old_id)
            except Exception as e:
                # On continue quand même: on veut "passer outre"
                print(f"[WARN] delete {key} failed: {e}")

        new_id = self.post_message(webhook_url, payload, files)
        state[key] = {"message_id": new_id, "hash": new_hash}
        print(f"[OK] {key} -> message {new_id}")
