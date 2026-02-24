import os
import re
import json
import time
import hashlib
from typing import Dict, List, Tuple, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://hideoutgacha.com/games/seven-deadly-sins-origin"
URLS = {
    "characters": f"{BASE}/characters",
    "combat_guide": f"{BASE}/combat-guide",
    "general_info": f"{BASE}/general-info",
    "boss_guide": f"{BASE}/boss-guide",
}

STATE_PATH = "state/state.json"
UA = "Mozilla/5.0 (GitHubActions; 7DSOriginDiscordSync/1.0)"

# Boss keys -> webhook key (tu relies ça à tes salons via DISCORD_WEBHOOKS_JSON)
BOSS_WEBHOOK_KEYS = {
    "Information": "boss_information",
    "Guardian Golem": "boss_guardian_golem",
    "Drake": "boss_drake",
    "Red Demon": "boss_red_demon",
    "Grey Demon": "boss_grey_demon",
    "Albion": "boss_albion",
}

NAV_TRASH = {
    "HIDEOUT GUIDES", "Home", "Games", "About", "Discord", "Ko-fi",
    "Privacy Policy", "Terms of Service",
    "Bleach Soul Resonance", "Dragon Traveler", "Dragon Raja Rerise", "Star Sailors", "Seven Deadly Sins: Origin",
}

def load_state() -> Dict:
    if not os.path.exists(STATE_PATH):
        return {"messages": {}}
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(state: Dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def stable_hash(payload: Dict) -> str:
    s = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def http_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s

def discord_request(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    # Gestion rate limit Discord (429)
    while True:
        r = session.request(method, url, timeout=30, **kwargs)
        if r.status_code == 429:
            try:
                data = r.json()
                retry = float(data.get("retry_after", 1.0))
            except Exception:
                retry = 1.0
            time.sleep(retry + 0.25)
            continue
        r.raise_for_status()
        return r

def upsert_webhook_message(
    session: requests.Session,
    state: Dict,
    webhook_url: str,
    key: str,
    payload: Dict
) -> None:
    payload.setdefault("allowed_mentions", {"parse": []})

    h = stable_hash(payload)
    prev = state["messages"].get(key)

    if prev and prev.get("hash") == h:
        return  # inchangé

    if prev and prev.get("message_id"):
        edit_url = f"{webhook_url}/messages/{prev['message_id']}"
        discord_request(session, "PATCH", edit_url, json=payload)
        prev["hash"] = h
        state["messages"][key] = prev
    else:
        create_url = f"{webhook_url}?wait=true"
        resp = discord_request(session, "POST", create_url, json=payload).json()
        state["messages"][key] = {"message_id": resp["id"], "hash": h}

def fetch_html(session: requests.Session, url: str) -> str:
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return r.text

def soup_and_meta(html: str) -> Tuple[BeautifulSoup, Optional[str], Optional[str]]:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()

    og_title = None
    og_img = None
    mt = soup.find("meta", attrs={"property": "og:title"})
    if mt and mt.get("content"):
        og_title = mt["content"].strip()
    mi = soup.find("meta", attrs={"property": "og:image"})
    if mi and mi.get("content"):
        og_img = mi["content"].strip()
        if og_img.startswith("/"):
            og_img = urljoin("https://hideoutgacha.com", og_img)

    return soup, og_title, og_img

def clean_lines(soup: BeautifulSoup) -> List[str]:
    text = soup.get_text("\n")
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            continue
        if line in NAV_TRASH:
            continue
        if line.startswith("© "):
            continue
        if line.startswith("Disclaimer:"):
            continue
        lines.append(line)
    return lines

def find_section(lines: List[str], start_marker: str, end_markers: List[str]) -> List[str]:
    # renvoie les lignes après start_marker jusqu'à un des end_markers
    out = []
    try:
        start = lines.index(start_marker) + 1
    except ValueError:
        return out
    for i in range(start, len(lines)):
        if lines[i] in end_markers:
            break
        out.append(lines[i])
    return out

def is_value_token(s: str) -> bool:
    # nombres, 1,000, 10.0%, etc.
    return bool(re.fullmatch(r"[0-9][0-9,]*(\.[0-9]+)?%?", s))

def parse_pairs_from_block(block: List[str]) -> List[Tuple[str, str]]:
    # pattern Hideout courant: "Image: X" / "X" / "val"
    cleaned = [b for b in block if not b.startswith("Image:")]
    pairs = []
    i = 0
    while i + 1 < len(cleaned):
        k = cleaned[i]
        v = cleaned[i + 1]
        if (not is_value_token(k)) and is_value_token(v):
            pairs.append((k, v))
            i += 2
        else:
            i += 1
    return pairs

def parse_character_page(session: requests.Session, url: str) -> Dict:
    html = fetch_html(session, url)
    soup, og_title, og_img = soup_and_meta(html)
    lines = clean_lines(soup)

    # Nom
    name = None
    for l in lines:
        if l.startswith("# "):
            name = l[2:].strip()
            break
    if not name:
        # fallback: premier H1
        h1 = soup.find("h1")
        name = h1.get_text(strip=True) if h1 else url.rsplit("/", 1)[-1]

    # Overview / Base Stats / Weapons (si présents)
    overview_block = find_section(lines, "### Overview", ["### Base Stats", "### Weapons", "HIDEOUT GUIDES"])
    overview = " ".join(overview_block).strip()

    base_block = find_section(lines, "### Base Stats", ["### Weapons", "HIDEOUT GUIDES"])
    base_stats = parse_pairs_from_block(base_block)

    weapons_block = find_section(lines, "### Weapons", ["HIDEOUT GUIDES"])
    # Exemple: Longsword / Darkness / Axe / Darkness / Dual Swords / Darkness
    weapons_clean = [w for w in weapons_block if not w.startswith("Image:")]
    weapons = []
    i = 0
    while i + 1 < len(weapons_clean):
        wname = weapons_clean[i]
        elem = weapons_clean[i + 1]
        # évite de capturer des restes de texte
        if len(wname) <= 40 and len(elem) <= 40 and not is_value_token(wname) and not is_value_token(elem):
            weapons.append((wname, elem))
            i += 2
        else:
            i += 1

    return {
        "name": name,
        "url": url,
        "image": og_img,
        "overview": overview,
        "base_stats": base_stats,
        "weapons": weapons,
    }

def embed_for_character(ch: Dict) -> Dict:
    fields = []

    if ch["base_stats"]:
        # groupe en plusieurs fields pour rester lisible
        for k, v in ch["base_stats"][:25]:
            fields.append({"name": k, "value": v, "inline": True})

    if ch["weapons"]:
        wtxt = "\n".join([f"- **{w}** ({e})" for w, e in ch["weapons"]])
        fields.append({"name": "Weapons", "value": wtxt[:1024], "inline": False})

    desc = (ch["overview"] or "").strip()
    if len(desc) > 2048:
        desc = desc[:2045] + "..."

    embed = {
        "title": ch["name"],
        "url": ch["url"],
        "description": desc if desc else " ",
        "fields": fields[:25],
    }
    if ch.get("image"):
        embed["thumbnail"] = {"url": ch["image"]}
    return embed

def parse_character_roster(session: requests.Session) -> List[str]:
    html = fetch_html(session, URLS["characters"])
    soup, _, _ = soup_and_meta(html)
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "/games/seven-deadly-sins-origin/characters/" in href:
            full = href if href.startswith("http") else urljoin("https://hideoutgacha.com", href)
            links.append(full)
    # unique + stable
    uniq = sorted(set(links))
    return uniq

def parse_page_by_h2_sections(session: requests.Session, url: str) -> List[Tuple[str, str, str]]:
    """
    Retourne une liste de (section_title, section_text, og_image)
    Découpe sur '## ' (H2) via extraction text.
    """
    html = fetch_html(session, url)
    soup, og_title, og_img = soup_and_meta(html)
    lines = clean_lines(soup)

    # On garde tout après le premier H1 si présent
    start_idx = 0
    for i, l in enumerate(lines):
        if l.startswith("# "):
            start_idx = i
            break
    body = lines[start_idx:]

    sections: List[Tuple[str, List[str]]] = []
    cur_title = og_title or url
    cur_buf: List[str] = []
    for l in body:
        if l.startswith("## "):
            # flush
            if cur_buf:
                sections.append((cur_title, cur_buf))
            cur_title = l[3:].strip()
            cur_buf = []
        else:
            cur_buf.append(l)
    if cur_buf:
        sections.append((cur_title, cur_buf))

    out = []
    for title, buf in sections:
        txt = "\n".join([b for b in buf if not b.startswith("# ")])
        txt = txt.strip()
        if not txt:
            continue
        out.append((title, txt, og_img or ""))
    return out

def embed_for_section(page_title: str, section_title: str, section_text: str, url: str, image: str) -> Dict:
    # Discord embed description <= 2048 (safety doc: 2048/4096 selon sources; on reste conservateur)
    desc = section_text
    if len(desc) > 2048:
        desc = desc[:2045] + "..."

    embed = {
        "title": f"{page_title} — {section_title}"[:256],
        "url": url,
        "description": desc if desc else " ",
    }
    if image:
        embed["thumbnail"] = {"url": image}
    return embed

def main():
    webhooks = os.environ.get("DISCORD_WEBHOOKS_JSON", "").strip()
    if not webhooks:
        raise SystemExit("Missing env DISCORD_WEBHOOKS_JSON")
    webhook_map = json.loads(webhooks)

    state = load_state()
    session = http_session()

    # 1) Characters -> 1 message par perso (éditable)
    if "characters" in webhook_map:
        roster = parse_character_roster(session)
        for ch_url in roster:
            ch = parse_character_page(session, ch_url)
            payload = {
                "username": "7DS Origin DB",
                "embeds": [embed_for_character(ch)],
            }
            key = f"character::{ch_url}"
            upsert_webhook_message(session, state, webhook_map["characters"], key, payload)

    # 2) Combat Guide -> 1 message par section H2
    if "combat_guide" in webhook_map:
        sections = parse_page_by_h2_sections(session, URLS["combat_guide"])
        for title, txt, img in sections:
            payload = {
                "username": "7DS Origin DB",
                "embeds": [embed_for_section("Combat Guide", title, txt, URLS["combat_guide"], img)],
            }
            key = f"combat::{title}"
            upsert_webhook_message(session, state, webhook_map["combat_guide"], key, payload)

    # 3) General Info -> 1 message par section H2
    if "general_info" in webhook_map:
        sections = parse_page_by_h2_sections(session, URLS["general_info"])
        for title, txt, img in sections:
            payload = {
                "username": "7DS Origin DB",
                "embeds": [embed_for_section("General Info", title, txt, URLS["general_info"], img)],
            }
            key = f"general::{title}"
            upsert_webhook_message(session, state, webhook_map["general_info"], key, payload)

    # 4) Boss Guide -> on publie au minimum "Information" dans boss_information
    # Si Hideout ajoute des sections H2 par boss plus tard, elles seront automatiquement envoyées
    boss_sections = parse_page_by_h2_sections(session, URLS["boss_guide"])
    for title, txt, img in boss_sections:
        wh_key = BOSS_WEBHOOK_KEYS.get(title, None)
        if not wh_key:
            # fallback: tout ce qui n'est pas mappé va dans "boss_information" si présent
            wh_key = "boss_information"
        if wh_key not in webhook_map:
            continue
        payload = {
            "username": "7DS Origin DB",
            "embeds": [embed_for_section("Boss Guide", title, txt, URLS["boss_guide"], img)],
        }
        key = f"boss::{title}"
        upsert_webhook_message(session, state, webhook_map[wh_key], key, payload)

    save_state(state)

if __name__ == "__main__":
    main()
