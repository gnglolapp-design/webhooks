import json
import re
import hashlib
from typing import Any, Dict, List

NAV_TRASH = {
    "HIDEOUT GUIDES",
    "Hideout Guides - Gacha Game Guides & Tier Lists",
    "Privacy Policy",
    "Terms of Service",
    "©",
    "© 2026",
    "Back to Seven Deadly Sins: Origin",
    "Back to Seven Deadly Sin: Origin",
}

def stable_hash(obj: Any) -> str:
    s = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def norm_line(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def clean_lines(lines: List[str]) -> List[str]:
    out = []
    for raw in lines:
        t = norm_line(raw)
        if not t:
            continue
        if t in NAV_TRASH:
            continue
        if t.startswith("Disclaimer:"):
            continue
        if t.startswith("©"):
            continue
        # lignes parasites vues dans tes embeds
        if t in {".", "•", "·"}:
            continue
        # bullets vides "•"
        if re.fullmatch(r"[•.\-–—]+", t):
            continue
        out.append(t)
    return out

def chunk_by_chars(text: str, limit: int = 3200) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur.strip())
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur.strip():
        chunks.append(cur.strip())
    return chunks