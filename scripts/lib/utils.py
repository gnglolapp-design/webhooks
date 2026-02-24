import json
import hashlib
from pathlib import Path
from typing import Any, Dict, List


def stable_hash(obj: Any) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def load_json(path: str, default: Any) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def save_json(path: str, data: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def clean_lines(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines()]
    out: List[str] = []
    for ln in lines:
        if not ln:
            if out and out[-1] == "":
                continue
            out.append("")
        else:
            out.append(ln)
    return "\n".join(out).strip()


def chunk_text(text: str, max_chars: int) -> List[str]:
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    chunks: List[str] = []
    cur: List[str] = []
    cur_len = 0
    for part in text.split("\n"):
        add = (part + "\n")
        if cur_len + len(add) > max_chars and cur:
            chunks.append("".join(cur).strip())
            cur = []
            cur_len = 0
        cur.append(add)
        cur_len += len(add)
    if cur:
        chunks.append("".join(cur).strip())
    return [c for c in chunks if c]
