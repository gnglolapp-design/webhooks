import os
import re
import json
import time
import hashlib
from typing import Dict, List, Tuple, Optional

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = "https://hideoutgacha.com/games/seven-deadly-sins-origin"
URLS = {
    "characters": f"{BASE}/characters",
    "combat_guide": f"{BASE}/combat-guide",
    "general_info": f"{BASE}/general-info",
    "boss_guide": f"{BASE}/boss-guide",
}

STATE_PATH = "state/state.json"
UA = "Mozilla/5.0 (GitHubActions; 7DSOriginDiscordSync/2.0)"

# Clés webhook -> onglets Boss
# Ces clés doivent exister dans ton secret DISCORD_WEBHOOKS_JSON
BOSS_WEBHOOK_KEYS = {
    "Information": "boss_information",
    "Guardian Golem": "boss_guardian_golem",
    "Drake": "boss_drake",
    "Red Demon": "boss_red_demon",
    "Grey Demon": "boss_grey_demon",
    "Albion": "boss_albion",
}

# Traduction UI (titres/sections) – le texte de fond reste celui du site
FR = {
    "Basic Info": "Infos",
    "Weapons": "Armes",
    "Armor": "Armure",
    "Potentials": "Potentiels",
    "Boss Guide": "Guide Boss",
    "Combat Guide": "Guide Combat",
    "General Info": "Infos générales",
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
    "HIDEOUT GUIDES", "Hideout Guides - Gacha Game Guides & Tier Lists",
    "Privacy Policy", "Terms of Service", "©", "© 2026",
    "Back to Seven Deadly Sins: Origin", "Back to Seven Deadly Sins: Origin",
}

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

def discord_request(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
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
        r.raise_for_status()
        return r

def upsert_webhook_message(session: requests.Session, state: Dict, webhook_url: str, key: str, payload: Dict) -> None:
    payload.setdefault("allowed_mentions", {"parse": []})

    h = stable_hash(payload)
    prev = state["messages"].get(key)

    if prev and prev.get("hash") == h:
        return

    if prev and prev.get("message_id"):
        edit_url = f"{webhook_url}/messages/{prev['message_id']}"
        discord_request(session, "PATCH", edit_url, json=payload)
        prev["hash"] = h
        state["messages"][key] = prev
    else:
        create_url = f"{webhook_url}?wait=true"
        resp = discord_request(session, "POST", create_url, json=payload).json()
        state["messages"][key] = {"message_id": resp["id"], "hash": h}

# =============== Helpers (text/embeds) ===============

def norm_line(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s

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

def make_embed(title: str, url: str, description: str = "", thumbnail: str = "", image: str = "", fields: Optional[List[Dict]] = None, footer: str = "") -> Dict:
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
    for i, ch in enumerate(chunks[:10]):  # max 10 embeds/message
        t = base_title if len(chunks) == 1 else f"{base_title} ({i+1}/{min(len(chunks),10)})"
        embeds.append(make_embed(t, url, description=ch, thumbnail=thumbnail, footer=footer))
    return embeds

# =============== Playwright extractors ===============

def goto_page(page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    # petit temps de stabilisation
    page.wait_for_timeout(400)

def click_text_safe(page, label: str) -> bool:
    # essaie bouton par rôle
    try:
        loc = page.get_by_role("button", name=label)
        if loc.count() > 0:
            loc.first.click(timeout=3000)
            return True
    except Exception:
        pass
    # texte exact
    try:
        loc = page.get_by_text(label, exact=True)
        if loc.count() > 0:
            loc.first.click(timeout=3000)
            return True
    except Exception:
        pass
    return False

def extract_best_block_text(page) -> str:
    # Renvoie le plus gros bloc de texte visible dans <main> (ou body)
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
      const candidates = Array.from(root.querySelectorAll('section, article, div'))
        .filter(isVisible);
      let best = root;
      let bestLen = (root.innerText || '').trim().length;
      for (const el of candidates) {
        const t = (el.innerText || '').trim();
        if (t.length > bestLen) {
          best = el;
          bestLen = t.length;
        }
      }
      return (best.innerText || '').trim();
    }
    """
    try:
        t = page.evaluate(js)
        return t or ""
    except Exception:
        return ""

def extract_largest_image(page) -> str:
    # Renvoie l'image visible la plus grande (par aire)
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
        if (score > bestScore) {
          best = src;
          bestScore = score;
        }
      }
      return best || '';
    }
    """
    try:
        return page.evaluate(js) or ""
    except Exception:
        return ""

# =============== Parsing (characters) ===============

def parse_basic_info(text: str) -> Tuple[str, List[Tuple[str, str]]]:
    lines = clean_lines(text.splitlines())

    # Overview: entre OVERVIEW et BASE STATS
    overview = ""
    base_stats: List[Tuple[str, str]] = []

    def find_idx(token: str) -> int:
        for i, l in enumerate(lines):
            if l.upper() == token:
                return i
        return -1

    i_over = find_idx("OVERVIEW")
    i_base = find_idx("BASE STATS")

    if i_over != -1:
        end = i_base if i_base != -1 else min(i_over + 40, len(lines))
        overview_lines = lines[i_over + 1:end]
        overview = " ".join(overview_lines).strip()

    # Base stats: paires Nom/Valeur après BASE STATS
    if i_base != -1:
        block = lines[i_base + 1:i_base + 80]
        # on prend les paires (nom, valeur) où la valeur ressemble à un nombre/%.
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
    """
    Retourne: (weapon_name, element, [(type, skill_name, desc), ...])
    Heuristique basée sur l'ordre du texte rendu (les cartes skills ont un label type).
    """
    lines = clean_lines(text.splitlines())

    weapon_name = lines[0] if lines else ""
    element = ""
    # L'élément est souvent juste après le nom (Darkness, etc.)
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
                # stop si on retombe sur le nom d'arme (quand on scrolle)
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

def weapon_field_name(typ: str, idx: int, total_same: int) -> str:
    base = FR.get(typ, typ)
    if total_same > 1:
        return f"{base} #{idx}"
    return base

def build_weapon_embed(char_name: str, url: str, thumb: str, weapon_name: str, element: str, skills: List[Tuple[str, str, str]], footer: str) -> Dict:
    title = f"{char_name} — {FR['Weapons']} : {weapon_name}"
    if element:
        title += f" ({element})"

    # Regroupe par type (au cas où il y en a plusieurs)
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
                "name": weapon_field_name(typ, idx, len(items))[:256],
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

# =============== Roster (requests) ===============

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
            if href.startswith("http"):
                links.append(href)
            else:
                links.append("https://hideoutgacha.com" + href)
    return sorted(set(links))

# =============== Scrapers ===============

def scrape_character(page, char_url: str, extra_footer: str) -> List[Dict]:
    goto_page(page, char_url)

    # Nom perso
    char_name = ""
    try:
        h1 = page.locator("h1")
        if h1.count() > 0:
            char_name = norm_line(h1.first.inner_text())
    except Exception:
        pass
    if not char_name:
        char_name = char_url.rstrip("/").split("/")[-1].replace("-", " ").title()

    # Image principale
    thumb = extract_largest_image(page)

    embeds: List[Dict] = []

    # ---------- Basic Info ----------
    click_text_safe(page, "Basic Info")
    page.wait_for_timeout(250)
    basic_text = extract_best_block_text(page)
    overview, base_stats = parse_basic_info(basic_text)

    fields = []
    for k, v in base_stats[:25]:
        fields.append({"name": k[:256], "value": v[:1024], "inline": True})

    basic_desc = ""
    if overview:
        basic_desc = f"**{FR['Overview']}**\n{overview}"

    embeds.append(make_embed(
        title=f"{char_name} — {FR['Basic Info']}",
        url=char_url,
        description=basic_desc if basic_desc else " ",
        thumbnail=thumb,
        fields=fields if fields else None,
        footer=extra_footer
    ))

    # ---------- Weapons ----------
    if click_text_safe(page, "Weapons"):
        page.wait_for_timeout(300)

        # Détection des types d'armes visibles (heuristique)
        # On récupère des candidats cliquables courts et uniques, puis on filtre.
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
                  if (t === 'Basic Info' || t === 'Weapons' || t === 'Armor' || t === 'Potentials') return false;
                  if (t === 'OVERVIEW' || t === 'BASE STATS') return false;
                  if (['Passive','Normal Attack','Special Attack','Normal Skill','Attack Skill','Ultimate Move'].includes(t)) return false;
                  // évite "Darkness" etc. (éléments)
                  if (/^[A-Za-z]+$/.test(t) && t.length <= 10) return false;
                  return true;
                })
                .map(el => (el.innerText || '').trim());

              // unique en conservant l'ordre
              const seen = new Set();
              const out = [];
              for (const t of candidates) {
                if (!seen.has(t)) { seen.add(t); out.push(t); }
              }
              return out;
            }
            """)
        except Exception:
            weapon_names = []

        # Filtre final : on garde 1..5 armes max pour éviter les erreurs
        weapon_names = [w for w in weapon_names if w and len(w) <= 25][:5]

        # Fallback si rien détecté: on tente les trois classiques
        if not weapon_names:
            weapon_names = ["Longsword", "Axe", "Dual Swords"]

        for w in weapon_names:
            # clique l'arme si possible
            clicked = False
            try:
                loc = page.get_by_text(w, exact=True)
                if loc.count() > 0:
                    loc.first.click(timeout=3000)
                    clicked = True
            except Exception:
                clicked = False

            page.wait_for_timeout(250)
            weapon_text = extract_best_block_text(page)
            wn, elem, skills = parse_weapon_skills(weapon_text)

            # si parse foire (wn vide), fallback sur le nom attendu
            if not wn:
                wn = w

            embeds.append(build_weapon_embed(
                char_name=char_name,
                url=char_url,
                thumb=thumb,
                weapon_name=wn,
                element=elem,
                skills=skills,
                footer=extra_footer
            ))

            # limite embeds/message (Discord: max 10)
            if len(embeds) >= 9:
                break

    # ---------- Armor ----------
    if len(embeds) < 10 and click_text_safe(page, "Armor"):
        page.wait_for_timeout(300)
        armor_text = extract_best_block_text(page)
        # on évite d'envoyer toute la page brute: on découpe proprement
        armor_lines = clean_lines(armor_text.splitlines())
        armor_body = "\n".join(armor_lines[:180])  # garde un max raisonnable
        armor_embeds = embeds_from_long_text(
            base_title=f"{char_name} — {FR['Armor']}",
            url=char_url,
            text=armor_body,
            thumbnail=thumb,
            footer=extra_footer
        )
        # on ne prend que ce qui rentre
        for e in armor_embeds:
            if len(embeds) >= 10:
                break
            embeds.append(e)

    # ---------- Potentials ----------
    if len(embeds) < 10 and click_text_safe(page, "Potentials"):
        page.wait_for_timeout(350)

        # On tente d'extraire les tooltips Tier 1..10 en hover
        tiers: List[str] = []

        for n in range(1, 11):
            try:
                # candidates: texte "n" visible dans main, petite bbox
                cand = page.locator("main").get_by_text(str(n), exact=True)
                chosen = None
                for i in range(min(cand.count(), 10)):
                    el = cand.nth(i)
                    try:
                        box = el.bounding_box()
                        if not box:
                            continue
                        if box["width"] <= 140 and box["height"] <= 140:
                            chosen = el
                            break
                    except Exception:
                        continue

                if chosen is None:
                    continue

                chosen.hover(timeout=3000)
                page.wait_for_timeout(150)

                # tooltip by role
                tip_text = ""
                tip = page.locator('[role="tooltip"]')
                if tip.count() > 0:
                    tip_text = norm_line(tip.first.inner_text())

                # fallback: chercher un bloc contenant "Tier"
                if not tip_text:
                    tip2 = page.locator("text=Tier").last
                    if tip2.count() > 0:
                        tip_text = norm_line(tip2.inner_text())

                if tip_text:
                    tiers.append(tip_text)
            except Exception:
                continue

        if tiers:
            pot_text = "\n".join([f"- {t}" for t in tiers])
        else:
            # fallback texte visible
            pot_text = extract_best_block_text(page)
            pot_lines = clean_lines(pot_text.splitlines())
            pot_text = "\n".join(pot_lines[:120])

        pot_embeds = embeds_from_long_text(
            base_title=f"{char_name} — {FR['Potentials']}",
            url=char_url,
            text=pot_text,
            thumbnail=thumb,
            footer=extra_footer
        )
        for e in pot_embeds:
            if len(embeds) >= 10:
                break
            embeds.append(e)

    # sécurité: au moins 1 embed
    if not embeds:
        embeds = [make_embed(f"{char_name}", char_url, description="Informations non détectées.", thumbnail=thumb, footer=extra_footer)]
    return embeds[:10]

def scrape_topics_page(page, url: str, page_title_fr: str, extra_footer: str) -> List[Tuple[str, List[Dict]]]:
    """
    Retourne [(topic, embeds)] pour Combat Guide / General Info
    Clique chaque topic (menu) et extrait le contenu.
    """
    goto_page(page, url)

    # Récupère les topics depuis la zone "Select a topic..."
    topics: List[str] = []
    try:
        topics = page.evaluate(r"""
        () => {
          const root = document.querySelector('main') || document.body;
          const marker = Array.from(root.querySelectorAll('*'))
            .find(el => (el.innerText || '').includes('Select a topic to view the guide'));
          let scope = root;
          if (marker) scope = marker.parentElement || root;

          const clickable = Array.from(scope.querySelectorAll('button, [role="button"], a, div'))
            .filter(el => {
              const t = (el.innerText || '').trim();
              if (!t) return false;
              if (t.length < 3 || t.length > 40) return false;
              if (t.includes('Select a topic')) return false;
              if (t.includes('Back to')) return false;
              return true;
            })
            .map(el => (el.innerText || '').trim());

          const seen = new Set();
          const out = [];
          for (const t of clickable) {
            if (!seen.has(t)) { seen.add(t); out.push(t); }
          }
          // On garde les 30 premiers max
          return out.slice(0, 30);
        }
        """)
    except Exception:
        topics = []

    # fallback si détection foire (ne rien casser)
    if not topics:
        return []

    results: List[Tuple[str, List[Dict]]] = []
    thumb = extract_largest_image(page)

    for topic in topics:
        # clique topic
        try:
            loc = page.get_by_text(topic, exact=True)
            if loc.count() > 0:
                loc.first.click(timeout=3000)
                page.wait_for_timeout(350)
        except Exception:
            continue

        raw = extract_best_block_text(page)
        lines = clean_lines(raw.splitlines())

        # nettoie: supprime la liste de topics si elle est présente au début
        # heuristique: coupe avant la dernière occurrence du titre
        last_idx = -1
        for i, l in enumerate(lines):
            if l == topic:
                last_idx = i
        if last_idx != -1:
            lines = lines[last_idx:]  # on garde à partir du topic

        body = "\n".join(lines[:260])  # limite raisonnable

        embeds = embeds_from_long_text(
            base_title=f"{page_title_fr} — {topic}",
            url=url,
            text=body,
            thumbnail=thumb,
            footer=extra_footer
        )
        results.append((topic, embeds[:10]))

    return results

def scrape_boss_tabs(page, url: str, extra_footer: str) -> Dict[str, List[Dict]]:
    """
    Retourne {tabName: embeds}
    """
    goto_page(page, url)
    thumb = extract_largest_image(page)

    tab_names = ["Information", "Guardian Golem", "Drake", "Red Demon", "Grey Demon", "Albion"]
    out: Dict[str, List[Dict]] = {}

    for tab in tab_names:
        # clique onglet
        ok = click_text_safe(page, tab)
        page.wait_for_timeout(350)

        # extrait texte
        raw = extract_best_block_text(page)
        lines = clean_lines(raw.splitlines())

        # coupe menu en gardant à partir du tab
        last_idx = -1
        for i, l in enumerate(lines):
            if l == tab:
                last_idx = i
        if last_idx != -1:
            lines = lines[last_idx:]

        body = "\n".join(lines[:320])

        embeds = embeds_from_long_text(
            base_title=f"{FR['Boss Guide']} — {tab}",
            url=url,
            text=body,
            thumbnail=thumb,
            footer=extra_footer
        )
        out[tab] = embeds[:10]

    return out

# =============== Main ===============

def main():
    webhooks_raw = os.environ.get("DISCORD_WEBHOOKS_JSON", "").strip()
    if not webhooks_raw:
        raise SystemExit("Missing env DISCORD_WEBHOOKS_JSON (GitHub Secret).")

    webhook_map = json.loads(webhooks_raw)

    extra_footer = os.environ.get("EXTRA_NOTE_FR", "").strip()
    # footer par défaut si rien
    if not extra_footer:
        extra_footer = "Source : hideoutgacha.com"

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

        # 1) Characters
        if "characters" in webhook_map:
            roster = parse_character_roster(session)
            for char_url in roster:
                embeds = scrape_character(page, char_url, extra_footer)
                payload = {
                    "username": "7DS Origin DB",
                    "embeds": embeds,
                }
                key = f"character::{char_url}"
                upsert_webhook_message(session, state, webhook_map["characters"], key, payload)
                time.sleep(0.35)  # pacing anti-rate-limit

        # 2) Combat Guide (topics)
        if "combat_guide" in webhook_map:
            topics = scrape_topics_page(page, URLS["combat_guide"], FR["Combat Guide"], extra_footer)
            for topic, embeds in topics:
                payload = {"username": "7DS Origin DB", "embeds": embeds}
                key = f"combat::{topic}"
                upsert_webhook_message(session, state, webhook_map["combat_guide"], key, payload)
                time.sleep(0.25)

        # 3) General Info (topics)
        if "general_info" in webhook_map:
            topics = scrape_topics_page(page, URLS["general_info"], FR["General Info"], extra_footer)
            for topic, embeds in topics:
                payload = {"username": "7DS Origin DB", "embeds": embeds}
                key = f"general::{topic}"
                upsert_webhook_message(session, state, webhook_map["general_info"], key, payload)
                time.sleep(0.25)

        # 4) Boss tabs (un webhook par boss)
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
