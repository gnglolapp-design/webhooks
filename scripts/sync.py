import os
import re
import json
import time
import hashlib
from typing import Dict, List, Tuple, Optional

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE = "https://hideoutgacha.com/games/seven-deadly-sins-origin"
URLS = {
    "characters": f"{BASE}/characters",
    "boss_guide": f"{BASE}/boss-guide",
}

STATE_PATH = "state/state.json"
UA = "Mozilla/5.0 (GitHubActions; 7DSOriginDiscordSync/2.2)"

BOSS_WEBHOOK_KEYS = {
    "Information": "boss_information",
    "Guardian Golem": "boss_guardian_golem",
    "Drake": "boss_drake",
    "Red Demon": "boss_red_demon",
    "Grey Demon": "boss_grey_demon",
    "Albion": "boss_albion",
}

# IMPORTANT: on met volontairement les DEUX clés "Potentials" et "Potentiels"
# pour éviter les KeyError si tu modifies une partie du code.
FR = {
    "Basic Info": "Infos",
    "Weapons": "Armes",
    "Armor": "Armure",
    "Potentials": "Potentiels",
    "Potentiels": "Potentiels",

    "Boss Guide": "Guide Boss",
    "Overview": "Aperçu",
    "Base Stats": "Stats de base",

    "Passive": "Passif",
    "Normal Attack": "Attaque normale",
    "Special Attack": "Attaque spéciale",
    "Normal Skill": "Compétence normale",
    "Attack Skill": "Compétence d’attaque",
    "Ultimate Move": "Ultime",
}

SKILL_TYPES = [
    "Passive",
    "Normal Attack",
    "Special Attack",
    "Normal Skill",
    "Attack Skill",
    "Ultimate Move",
]

NAV_TRASH = {
    "HIDEOUT GUIDES",
    "Hideout Guides - Gacha Game Guides & Tier Lists",
    "Privacy Policy",
    "Terms of Service",
    "©",
    "© 2026",
    "Back to Seven Deadly Sins: Origin",
}

def tr(key: str, fallback: Optional[str] = None) -> str:
    """Traduction robuste: jamais de KeyError."""
    if fallback is None:
        fallback = key
    return FR.get(key, fallback)

# =============== State ===============

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

# =============== Discord ===============

def http_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s

def redact_discord_webhook_url(url: str) -> str:
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

def discord_request(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    r = discord_request_raw(session, method, url, **kwargs)
    if not (200 <= r.status_code < 300):
        raise RuntimeError(
            f"Discord API error {r.status_code} on {method} {redact_discord_webhook_url(url)} "
            f"(body: {r.text[:300]!r})"
        )
    return r

def upsert_webhook_message(session: requests.Session, state: Dict, webhook_url: str, key: str, payload: Dict) -> None:
    payload.setdefault("allowed_mentions", {"parse": []})

    h = stable_hash(payload)
    prev = state["messages"].get(key)

    if prev and prev.get("hash") == h:
        return

    if prev and prev.get("message_id"):
        edit_url = f"{webhook_url}/messages/{prev['message_id']}"
        r = discord_request_raw(session, "PATCH", edit_url, json=payload)
        if r.status_code == 404:
            create_url = f"{webhook_url}?wait=true"
            resp = discord_request(session, "POST", create_url, json=payload).json()
            state["messages"][key] = {"message_id": resp["id"], "hash": h}
            return
        if not (200 <= r.status_code < 300):
            raise RuntimeError(
                f"Discord API error {r.status_code} on PATCH {redact_discord_webhook_url(edit_url)} "
                f"(body: {r.text[:300]!r})"
            )
        prev["hash"] = h
        state["messages"][key] = prev
        return

    create_url = f"{webhook_url}?wait=true"
    resp = discord_request(session, "POST", create_url, json=payload).json()
    state["messages"][key] = {"message_id": resp["id"], "hash": h}

# =============== Embeds / text ===============

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
        out.append(t)
    return out

def chunk_by_chars(text: str, limit: int = 3500) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks = []
    cur = ""
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

def make_embed(
    title: str,
    url: str,
    description: str = "",
    thumbnail: str = "",
    image: str = "",
    fields: Optional[List[Dict]] = None,
    footer: str = "",
) -> Dict:
    emb: Dict = {
        "title": title[:256],
        "url": url,
        "description": (description or " ")[:4096],
    }
    if thumbnail:
        emb["thumbnail"] = {"url": thumbnail}
    if image:
        emb["image"] = {"url": image}
    if fields:
        emb["fields"] = fields[:25]
    if footer:
        emb["footer"] = {"text": footer[:2048]}
    return emb

def embeds_from_long_text(base_title: str, url: str, text: str, thumbnail: str = "", footer: str = "") -> List[Dict]:
    chunks = chunk_by_chars(text, limit=3500)
    if not chunks:
        return [make_embed(base_title, url, description=" ", thumbnail=thumbnail, footer=footer)]
    embeds = []
    for i, ch in enumerate(chunks[:10]):
        t = base_title if len(chunks) == 1 else f"{base_title} ({i+1}/{min(len(chunks),10)})"
        embeds.append(make_embed(t, url, description=ch, thumbnail=thumbnail, footer=footer))
    return embeds

# =============== Playwright helpers ===============

def goto_page(page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(400)

def click_text_safe(page, label: str) -> bool:
    try:
        loc = page.get_by_role("button", name=label)
        if loc.count() > 0:
            loc.first.click(timeout=3000)
            return True
    except Exception:
        pass
    try:
        loc = page.get_by_text(label, exact=True)
        if loc.count() > 0:
            loc.first.click(timeout=3000)
            return True
    except Exception:
        pass
    return False

def extract_best_block_text(page) -> str:
    js = r"""
    () => {
      const root = document.querySelector('main') || document.body;
      const isVisible = (el) => {
        const r = el.getBoundingClientRect();
        if (!r || r.width < 5 || r.height < 5) return false;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
        return true;
      };
      const candidates = Array.from(root.querySelectorAll('section, article, div')).filter(isVisible);
      let best = root;
      let bestLen = (root.innerText || '').trim().length;
      for (const el of candidates) {
        const t = (el.innerText || '').trim();
        if (t.length > bestLen) { best = el; bestLen = t.length; }
      }
      return (best.innerText || '').trim();
    }
    """
    try:
        return page.evaluate(js) or ""
    except Exception:
        return ""

def extract_largest_image(page) -> str:
    js = r"""
    () => {
      const imgs = Array.from(document.querySelectorAll('img'));
      const isVisible = (el) => {
        const r = el.getBoundingClientRect();
        if (!r || r.width < 10 || r.height < 10) return false;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
        return true;
      };
      let best = null;
      let bestScore = 0;
      for (const img of imgs) {
        if (!isVisible(img)) continue;
        const w = img.naturalWidth || img.width || 0;
        const h = img.naturalHeight || img.height || 0;
        const score = w * h;
        const src = img.currentSrc || img.src || '';
        if (!src || src.startsWith('data:')) continue;
        if (score > bestScore) { best = src; bestScore = score; }
      }
      return best || '';
    }
    """
    try:
        return page.evaluate(js) or ""
    except Exception:
        return ""

# =============== Requests: roster ===============

def fetch_html(session: requests.Session, url: str) -> str:
    r = session.get(url, timeout=45, headers={"User-Agent": UA})
    r.raise_for_status()
    return r.text

def parse_character_roster(session: requests.Session) -> List[str]:
    html = fetch_html(session, URLS["characters"])
    soup = BeautifulSoup(html, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "/games/seven-deadly-sins-origin/characters/" in href:
            links.append(href if href.startswith("http") else "https://hideoutgacha.com" + href)
    return sorted(set(links))

# =============== Parsing content ===============

def parse_basic_info(text: str) -> Tuple[str, List[Tuple[str, str]]]:
    lines = clean_lines(text.splitlines())
    overview = ""
    base_stats: List[Tuple[str, str]] = []

    def find_idx(token: str) -> int:
        token = token.upper()
        for i, l in enumerate(lines):
            if l.upper() == token:
                return i
        return -1

    i_over = find_idx("OVERVIEW")
    i_base = find_idx("BASE STATS")

    if i_over != -1:
        end = i_base if i_base != -1 else min(i_over + 40, len(lines))
        overview = " ".join(lines[i_over + 1:end]).strip()

    if i_base != -1:
        block = lines[i_base + 1:i_base + 120]
        j = 0
        while j + 1 < len(block):
            k = block[j]
            v = block[j + 1]
            if re.fullmatch(r"[0-9][0-9,]*(\.[0-9]+)?%?", v):
                base_stats.append((k, v))
                j += 2
            else:
                j += 1

    return overview, base_stats

def parse_weapon_skills(text: str) -> Tuple[str, str, List[Tuple[str, str, str]]]:
    lines = clean_lines(text.splitlines())
    weapon_name = lines[0] if lines else ""
    element = ""
    for l in lines[1:10]:
        if len(l) <= 20 and re.fullmatch(r"[A-Za-z]+", l):
            element = l
            break

    skills: List[Tuple[str, str, str]] = []
    i = 0
    while i < len(lines):
        if lines[i] in SKILL_TYPES:
            typ = lines[i]
            name = lines[i + 1] if i + 1 < len(lines) else ""
            desc_parts = []
            k = i + 2
            while k < len(lines) and lines[k] not in SKILL_TYPES:
                if weapon_name and lines[k] == weapon_name:
                    break
                desc_parts.append(lines[k])
                k += 1
            desc = " ".join(desc_parts).strip()
            skills.append((typ, name, desc))
            i = k
        else:
            i += 1

    return weapon_name, element, skills

def build_weapon_embed(char_name: str, url: str, thumb: str, weapon_name: str, element: str, skills: List[Tuple[str, str, str]], footer: str) -> Dict:
    title = f"{char_name} — {tr('Weapons')} : {weapon_name}"
    if element:
        title += f" ({element})"

    by_type: Dict[str, List[Tuple[str, str]]] = {}
    for typ, name, desc in skills:
        by_type.setdefault(typ, []).append((name, desc))

    fields: List[Dict] = []
    for typ in SKILL_TYPES:
        if typ not in by_type:
            continue
        items = by_type[typ]
        for idx, (name, desc) in enumerate(items, start=1):
            value = f"**{name}**\n{desc}".strip()
            if len(value) > 1024:
                value = value[:1021] + "..."
            fields.append({
                "name": tr(typ)[:256],
                "value": value if value else " ",
                "inline": False
            })
            if len(fields) >= 25:
                break
        if len(fields) >= 25:
            break

    if not fields:
        fields = [{"name": "Détails", "value": "Informations non détectées.", "inline": False}]

    return make_embed(title, url, description=" ", thumbnail=thumb, fields=fields, footer=footer)

# =============== Scrapers ===============

def scrape_character(page, char_url: str, extra_footer: str) -> List[Dict]:
    goto_page(page, char_url)

    char_name = ""
    try:
        h1 = page.locator("h1")
        if h1.count() > 0:
            char_name = norm_line(h1.first.inner_text())
    except Exception:
        pass
    if not char_name:
        char_name = char_url.rstrip("/").split("/")[-1].replace("-", " ").title()

    thumb = extract_largest_image(page)
    embeds: List[Dict] = []

    # Basic Info
    click_text_safe(page, "Basic Info")
    page.wait_for_timeout(250)
    basic_text = extract_best_block_text(page)
    overview, base_stats = parse_basic_info(basic_text)
    fields = [{"name": k[:256], "value": v[:1024], "inline": True} for k, v in base_stats[:25]]
    desc = f"**{tr('Overview')}**\n{overview}" if overview else " "
    embeds.append(make_embed(
        title=f"{char_name} — {tr('Basic Info')}",
        url=char_url,
        description=desc,
        thumbnail=thumb,
        fields=fields if fields else None,
        footer=extra_footer
    ))

    # Weapons
    if click_text_safe(page, "Weapons"):
        page.wait_for_timeout(300)
        weapon_names = []
        try:
            weapon_names = page.evaluate(r"""
            () => {
              const root = document.querySelector('main') || document.body;
              const candidates = Array.from(root.querySelectorAll('button, [role="button"], a, div'))
                .filter(el => {
                  const r = el.getBoundingClientRect();
                  if (!r || r.width < 20 || r.height < 20) return false;
                  const t = (el.innerText || '').trim();
                  if (!t) return false;
                  if (t.length > 25) return false;
                  if (['Basic Info','Weapons','Armor','Potentials'].includes(t)) return false;
                  if (['Passive','Normal Attack','Special Attack','Normal Skill','Attack Skill','Ultimate Move'].includes(t)) return false;
                  if (/^[A-Za-z]+$/.test(t) && t.length <= 10) return false;
                  return true;
                })
                .map(el => (el.innerText || '').trim());
              const seen = new Set();
              const out = [];
              for (const t of candidates) { if (!seen.has(t)) { seen.add(t); out.push(t); } }
              return out.slice(0, 5);
            }
            """)
        except Exception:
            weapon_names = []

        if not weapon_names:
            weapon_names = ["Longsword", "Axe", "Dual Swords"]

        for w in weapon_names:
            try:
                loc = page.get_by_text(w, exact=True)
                if loc.count() > 0:
                    loc.first.click(timeout=3000)
            except Exception:
                pass

            page.wait_for_timeout(250)
            weapon_text = extract_best_block_text(page)
            wn, elem, skills = parse_weapon_skills(weapon_text)
            if not wn:
                wn = w

            embeds.append(build_weapon_embed(char_name, char_url, thumb, wn, elem, skills, extra_footer))
            if len(embeds) >= 9:
                break

    # Armor
    if len(embeds) < 10 and click_text_safe(page, "Armor"):
        page.wait_for_timeout(300)
        armor_text = extract_best_block_text(page)
        armor_lines = clean_lines(armor_text.splitlines())
        armor_body = "\n".join(armor_lines[:260])
        for e in embeds_from_long_text(f"{char_name} — {tr('Armor')}", char_url, armor_body, thumb, extra_footer):
            if len(embeds) >= 10:
                break
            embeds.append(e)

    # Potentials (corrigé : tr() + pas de KeyError)
    if len(embeds) < 10 and click_text_safe(page, "Potentials"):
        page.wait_for_timeout(350)
        pot_text = extract_best_block_text(page)
        pot_lines = clean_lines(pot_text.splitlines())
        pot_body = "\n".join(pot_lines[:260])
        for e in embeds_from_long_text(f"{char_name} — {tr('Potentials', 'Potentiels')}", char_url, pot_body, thumb, extra_footer):
            if len(embeds) >= 10:
                break
            embeds.append(e)

    return embeds[:10] if embeds else [make_embed(char_name, char_url, "Informations non détectées.", thumbnail=thumb, footer=extra_footer)]

def scrape_boss_tabs(page, url: str, extra_footer: str) -> Dict[str, List[Dict]]:
    goto_page(page, url)
    thumb = extract_largest_image(page)

    tab_names = ["Information", "Guardian Golem", "Drake", "Red Demon", "Grey Demon", "Albion"]
    out: Dict[str, List[Dict]] = {}

    for tab in tab_names:
        click_text_safe(page, tab)
        page.wait_for_timeout(350)

        raw = extract_best_block_text(page)
        lines = clean_lines(raw.splitlines())

        last_idx = -1
        for i, l in enumerate(lines):
            if l == tab:
                last_idx = i
        if last_idx != -1:
            lines = lines[last_idx:]

        body = "\n".join(lines[:320])
        out[tab] = embeds_from_long_text(f"{tr('Boss Guide')} — {tab}", url, body, thumb, extra_footer)[:10]

    return out

# =============== Main ===============

def main():
    webhooks_raw = os.environ.get("DISCORD_WEBHOOKS_JSON", "").strip()
    if not webhooks_raw:
        raise SystemExit("Missing DISCORD_WEBHOOKS_JSON (GitHub Secret).")

    webhook_map = json.loads(webhooks_raw)

    extra_footer = os.environ.get("EXTRA_NOTE_FR", "").strip() or "Source : hideoutgacha.com"
    state = load_state()
    session = http_session()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=UA,
            locale="fr-FR",
            extra_http_headers={"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"},
            viewport={"width": 1400, "height": 900},
        )
        page = context.new_page()

        # Characters
        if "characters" in webhook_map:
            roster = parse_character_roster(session)
            for char_url in roster:
                embeds = scrape_character(page, char_url, extra_footer)
                payload = {"username": "7DS Origin DB", "embeds": embeds}
                key = f"character::{char_url}"
                upsert_webhook_message(session, state, webhook_map["characters"], key, payload)
                time.sleep(0.35)

        # Boss tabs -> un webhook par boss si présent dans le secret
        boss_tabs = scrape_boss_tabs(page, URLS["boss_guide"], extra_footer)
        for tab, embeds in boss_tabs.items():
            wh_key = BOSS_WEBHOOK_KEYS.get(tab, "boss_information")
            if wh_key not in webhook_map:
                continue
            payload = {"username": "7DS Origin DB", "embeds": embeds}
            key = f"boss::{tab}"
            upsert_webhook_message(session, state, webhook_map[wh_key], key, payload)
            time.sleep(0.25)

        context.close()
        browser.close()

    save_state(state)

if __name__ == "__main__":
    main()
