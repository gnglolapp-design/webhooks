import os
import re
import json
import time
import hashlib
from typing import Dict, List, Tuple, Optional, Any

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE = "https://hideoutgacha.com/games/seven-deadly-sins-origin"
URLS = {
    "characters": f"{BASE}/characters",
    "general_info": f"{BASE}/general-info",
    "combat_guide": f"{BASE}/combat-guide",
    "boss_guide": f"{BASE}/boss-guide",
}

STATE_PATH = "state/state.json"
UA = "Mozilla/5.0 (GitHubActions; 7DSOriginDiscordSync/3.0)"
EMBED_COLOR = int("C99700", 16)  # doré #C99700

SKILL_TYPES = ["Passive", "Normal Attack", "Special Attack", "Normal Skill", "Attack Skill", "Ultimate Move"]

# Clés webhooks attendues (alias acceptés pour éviter les erreurs de config)
WEBHOOK_ALIASES = {
    "characters": ["characters", "persos", "personnages"],
    "general": ["general", "general_info", "infos_generales", "info_generale"],
    "combat": ["combat", "combat_guide", "guide_combat"],
    # boss individuels : boss_<slug> ex: boss_guardian_golem
}

# Traductions forcées (post-traitement) + normalisation "0 anglais"
GLOSSARY_POST = [
    (r"\bTag System\b", "Système de relais"),
    (r"\bTag Gauge\b", "Jauge de relais"),
    (r"\bTag Points\b", "Points de relais"),
    (r"\bTag\b", "Relais"),

    (r"\bBurst System\b", "Système de déchaînement"),
    (r"\bBurst Gauge\b", "Jauge de déchaînement"),
    (r"\bBurst Effects\b", "Effets de déchaînement"),
    (r"\bBurst\b", "Déchaînement"),

    (r"\bCombat Basics\b", "Bases du combat"),
    (r"\bGeneral Information\b", "Informations générales"),
    (r"\bBoss Guide\b", "Guide des boss"),
    (r"\bStatus Effects Reference\b", "Référence des effets d’état"),
    (r"\bAdvanced Tips\b", "Conseils avancés"),
    (r"\bWorld Level\b", "Niveau du monde"),
    (r"\bCharacter Dupes\b", "Doublons de personnage"),

    (r"\bPassive\b", "Passif"),
    (r"\bNormal Attack\b", "Attaque normale"),
    (r"\bSpecial Attack\b", "Attaque spéciale"),
    (r"\bNormal Skill\b", "Compétence normale"),
    (r"\bAttack Skill\b", "Compétence d’attaque"),
    (r"\bUltimate Move\b", "Ultime"),

    # Statuts courants (évite de laisser des mots EN)
    (r"\bStun\b", "Étourdissement"),
    (r"\bFreeze\b", "Gel"),
    (r"\bParalysis\b", "Paralysie"),
    (r"\bPetrify\b", "Pétrification"),
    (r"\bBleed\b", "Saignement"),
    (r"\bBurn\b", "Brûlure"),
    (r"\bShock\b", "Choc"),
    (r"\bCurse\b", "Malédiction"),
    (r"\bChill\b", "Frisson"),
    (r"\bBind\b", "Entrave"),
]

STAT_LABELS_FR = {
    "Attack": "Attaque",
    "Defense": "Défense",
    "Max HP": "PV max",
    "Accuracy": "Précision",
    "Block": "Blocage",
    "Crit Rate": "Taux de critique",
    "Crit Damage": "Dégâts critiques",
    "Crit Res": "Résistance critique",
    "Crit Dmg Res": "Résistance dégâts crit.",
    "Block Dmg Res": "Résistance dégâts bloc.",
    "Move Speed": "Vitesse de déplacement",
    "PvP Dmg Inc": "Dégâts JcJ +",
    "PvP Dmg Dec": "Dégâts JcJ -",
}

NAV_TRASH = {
    "HIDEOUT GUIDES",
    "Hideout Guides - Gacha Game Guides & Tier Lists",
    "Privacy Policy",
    "Terms of Service",
    "©",
    "© 2026",
    "Back to Seven Deadly Sins: Origin",
}

# -------------------- State --------------------

def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return {"v": 2, "items": {}, "webhook_fp": {}, "tcache": {}, "torder": []}
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        state = json.load(f)

    # migration ancienne version
    if "messages" in state and "items" not in state:
        items = {}
        for k, v in state.get("messages", {}).items():
            items[k] = {"message_id": v.get("message_id"), "payload_hash": v.get("hash")}
        state = {"v": 2, "items": items, "webhook_fp": {}, "tcache": {}, "torder": []}
    state.setdefault("v", 2)
    state.setdefault("items", {})
    state.setdefault("webhook_fp", {})
    state.setdefault("tcache", {})
    state.setdefault("torder", [])
    return state

def save_state(state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def stable_hash(obj: Any) -> str:
    s = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

# -------------------- Discord --------------------

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

def ensure_webhook_fingerprint(state: Dict[str, Any], webhook_key: str, webhook_url: str) -> None:
    fp = sha(webhook_url)
    old = state["webhook_fp"].get(webhook_key)
    if old and old != fp:
        raise RuntimeError(
            f"Webhook '{webhook_key}' a changé (empreinte différente). "
            f"Pour éviter les doublons, le script s'arrête. "
            f"Supprime les anciens messages ou reset volontairement state/state.json."
        )
    state["webhook_fp"][webhook_key] = fp

def upsert_message_strict(
    session: requests.Session,
    state: Dict[str, Any],
    webhook_key: str,
    webhook_url: str,
    item_key: str,
    payload: Dict[str, Any],
    source_hash: str,
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
            # message supprimé manuellement : on reposte (empreinte webhook identique => pas de doublon "fantôme")
            create_url = f"{webhook_url}?wait=true"
            resp = discord_request_raw(session, "POST", create_url, json=payload)
            if not (200 <= resp.status_code < 300):
                raise RuntimeError(
                    f"Discord error {resp.status_code} POST {redact_discord_webhook_url(create_url)} "
                    f"(body: {resp.text[:300]!r})"
                )
            data = resp.json()
            state["items"][item_key] = {"message_id": data["id"], "payload_hash": payload_hash, "source_hash": source_hash}
            return

        if not (200 <= r.status_code < 300):
            raise RuntimeError(
                f"Discord error {r.status_code} PATCH {redact_discord_webhook_url(edit_url)} "
                f"(body: {r.text[:300]!r})"
            )

        state["items"][item_key] = {"message_id": message_id, "payload_hash": payload_hash, "source_hash": source_hash}
        return

    # nouveau
    create_url = f"{webhook_url}?wait=true"
    resp = discord_request_raw(session, "POST", create_url, json=payload)
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(
            f"Discord error {resp.status_code} POST {redact_discord_webhook_url(create_url)} "
            f"(body: {resp.text[:300]!r})"
        )
    data = resp.json()
    state["items"][item_key] = {"message_id": data["id"], "payload_hash": payload_hash, "source_hash": source_hash}

# -------------------- Text / Embeds --------------------

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

def make_embed(
    title: str,
    url: str,
    description: str = "",
    thumbnail: str = "",
    image: str = "",
    fields: Optional[List[Dict[str, Any]]] = None,
    footer: str = "",
) -> Dict[str, Any]:
    emb: Dict[str, Any] = {
        "title": title[:256],
        "url": url,
        "description": (description or " ")[:4096],
        "color": EMBED_COLOR,
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

def embeds_from_long_text(base_title: str, url: str, text: str, thumbnail: str = "", image: str = "", footer: str = "") -> List[Dict[str, Any]]:
    chunks = chunk_by_chars(text, limit=3200)
    if not chunks:
        return [make_embed(base_title, url, " ", thumbnail=thumbnail, image=image, footer=footer)]
    out = []
    for i, ch in enumerate(chunks[:10]):
        t = base_title if len(chunks) == 1 else f"{base_title} ({i+1}/{min(len(chunks),10)})"
        out.append(make_embed(t, url, ch, thumbnail=thumbnail, image=image if i == 0 else "", footer=footer))
    return out

def slugify(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s

# -------------------- Translation (Argos) --------------------

class TranslatorENFR:
    def __init__(self, state: Dict[str, Any]):
        self.state = state
        self._translation = None

    def ensure(self) -> None:
        if self._translation is not None:
            return

        from argostranslate import translate, package

        langs = translate.get_installed_languages()
        en = next((l for l in langs if l.code == "en"), None)
        fr = next((l for l in langs if l.code == "fr"), None)

        if not en or not fr or not en.get_translation(fr):
            package.update_package_index()
            available = package.get_available_packages()
            pkg = next((p for p in available if p.from_code == "en" and p.to_code == "fr"), None)
            if not pkg:
                raise RuntimeError("Impossible de trouver le modèle Argos EN->FR.")
            path = pkg.download()
            package.install_from_path(path)

            langs = translate.get_installed_languages()
            en = next((l for l in langs if l.code == "en"), None)
            fr = next((l for l in langs if l.code == "fr"), None)

        if not en or not fr:
            raise RuntimeError("Langues Argos EN/FR non disponibles après installation.")
        tr = en.get_translation(fr)
        if not tr:
            raise RuntimeError("Traduction Argos EN->FR indisponible.")
        self._translation = tr

    def _postprocess(self, text: str) -> str:
        out = text
        for pat, repl in GLOSSARY_POST:
            out = re.sub(pat, repl, out, flags=re.IGNORECASE)
        out = re.sub(r"[ \t]+", " ", out)
        out = re.sub(r"\n{3,}", "\n\n", out).strip()
        return out

    def translate(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""

        # évite de traduire des chaînes "quasi numériques"
        if re.fullmatch(r"[\d\s\.,:%+\-–/()]+", text):
            return text

        key = sha(text)
        cache = self.state["tcache"]
        if key in cache:
            return cache[key]

        self.ensure()
        fr = self._translation.translate(text)
        fr = self._postprocess(fr)

        # cache (LRU simple)
        order = self.state["torder"]
        cache[key] = fr
        order.append(key)
        if len(order) > 2500:
            # purge oldest
            for _ in range(400):
                if not order:
                    break
                k = order.pop(0)
                cache.pop(k, None)

        return fr

# -------------------- Webhook map helpers --------------------

def load_webhooks() -> Dict[str, str]:
    raw = os.environ.get("DISCORD_WEBHOOKS_JSON", "").strip()
    if not raw:
        raise SystemExit("Missing DISCORD_WEBHOOKS_JSON (GitHub Secret).")
    m = json.loads(raw)

    # normalise: expose canonical keys via alias list
    out = dict(m)
    for canon, aliases in WEBHOOK_ALIASES.items():
        for a in aliases:
            if a in m:
                out[canon] = m[a]
                break
    return out

def get_webhook(m: Dict[str, str], key: str) -> Optional[str]:
    return m.get(key)

# -------------------- Playwright DOM extractors --------------------

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

def extract_largest_image(page) -> str:
    js = r"""
    () => {
      const main = document.querySelector('main') || document.body;
      const imgs = Array.from(main.querySelectorAll('img'));
      const isVisible = (el) => {
        const r = el.getBoundingClientRect();
        if (!r || r.width < 20 || r.height < 20) return false;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
        return true;
      };
      let best = '';
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
      return best;
    }
    """
    try:
        return page.evaluate(js) or ""
    except Exception:
        return ""

def extract_min_container_text(page, required_tokens: List[str], max_chars: int = 25000) -> str:
    js = r"""
    (requiredTokens, maxChars) => {
      const main = document.querySelector('main') || document.body;
      const isVisible = (el) => {
        const r = el.getBoundingClientRect();
        if (!r || r.width < 5 || r.height < 5) return false;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
        return true;
      };
      const tokens = requiredTokens.map(t => t.toLowerCase());
      let best = null;
      let bestLen = Infinity;
      const candidates = Array.from(main.querySelectorAll('section, article, div')).filter(isVisible);
      for (const el of candidates) {
        const t = (el.innerText || '').trim();
        if (!t) continue;
        const low = t.toLowerCase();
        let ok = true;
        for (const tok of tokens) {
          if (!low.includes(tok)) { ok = false; break; }
        }
        if (!ok) continue;
        if (t.length < bestLen && t.length <= maxChars) {
          best = t;
          bestLen = t.length;
        }
      }
      return best || '';
    }
    """
    try:
        return page.evaluate(js, required_tokens, max_chars) or ""
    except Exception:
        return ""

def extract_buttons_in_main(page) -> List[str]:
    js = r"""
    () => {
      const main = document.querySelector('main') || document.body;
      const buttons = Array.from(main.querySelectorAll('button'))
        .map(b => (b.innerText || '').trim())
        .filter(t => t && t.length >= 3 && t.length <= 40);
      const ban = new Set(['Discord','Ko-fi','Home','Games','About']);
      const out = [];
      const seen = new Set();
      for (const t of buttons) {
        if (ban.has(t)) continue;
        if (!seen.has(t)) { seen.add(t); out.push(t); }
      }
      return out;
    }
    """
    try:
        return page.evaluate(js) or []
    except Exception:
        return []

def extract_cards_in_active_panel(page) -> Dict[str, Any]:
    js = r"""
    () => {
      const main = document.querySelector('main') || document.body;

      const isVisible = (el) => {
        const r = el.getBoundingClientRect();
        if (!r || r.width < 5 || r.height < 5) return false;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
        return true;
      };

      // titre principal du panneau
      const h = Array.from(main.querySelectorAll('h2,h1')).find(el => isVisible(el) && (el.innerText||'').trim().length>0);
      const title = h ? (h.innerText||'').trim() : '';

      // cartes: bloc avec h3 + p
      const cardEls = Array.from(main.querySelectorAll('div,section,article'))
        .filter(isVisible)
        .filter(el => el.querySelector('h3') && el.querySelector('p'));

      const cards = [];
      for (const el of cardEls) {
        const h3 = el.querySelector('h3');
        const p = el.querySelector('p');
        const ct = (h3?.innerText||'').trim();
        const cd = (p?.innerText||'').trim();
        if (!ct || !cd) continue;
        if (ct.length > 80) continue;
        if (cd.length > 500) {
          cards.push({title: ct, desc: cd});
        } else {
          cards.push({title: ct, desc: cd});
        }
      }

      // dédoublonnage (titre+desc)
      const seen = new Set();
      const uniq = [];
      for (const c of cards) {
        const k = c.title + '||' + c.desc;
        if (seen.has(k)) continue;
        seen.add(k);
        uniq.push(c);
      }

      return {title, cards: uniq.slice(0, 60)};
    }
    """
    try:
        return page.evaluate(js) or {"title": "", "cards": []}
    except Exception:
        return {"title": "", "cards": []}

# -------------------- Requests roster --------------------

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

# -------------------- Character parsing (tab text -> structured) --------------------

def parse_basic_info_block(text: str) -> Tuple[str, List[Tuple[str, str]]]:
    lines = clean_lines(text.splitlines())
    overview = ""
    stats: List[Tuple[str, str]] = []

    def idx(token: str) -> int:
        token = token.upper()
        for i, l in enumerate(lines):
            if l.upper() == token:
                return i
        return -1

    io = idx("OVERVIEW")
    ib = idx("BASE STATS")
    if io != -1:
        end = ib if ib != -1 else min(io + 40, len(lines))
        overview = " ".join(lines[io + 1:end]).strip()

    if ib != -1:
        block = lines[ib + 1:ib + 130]
        j = 0
        while j + 1 < len(block):
            k = block[j]
            v = block[j + 1]
            if re.fullmatch(r"[0-9][0-9,]*(\.[0-9]+)?%?", v):
                stats.append((k, v))
                j += 2
            else:
                j += 1
    return overview, stats

def parse_weapon_block(text: str) -> Tuple[str, str, List[Tuple[str, str, str]]]:
    lines = clean_lines(text.splitlines())
    weapon_name = lines[0] if lines else ""
    element = ""
    for l in lines[1:12]:
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
            skills.append((typ, name, " ".join(desc_parts).strip()))
            i = k
        else:
            i += 1

    return weapon_name, element, skills

# -------------------- Builders (FR) --------------------

def stat_label_fr(label: str) -> str:
    return STAT_LABELS_FR.get(label, label)

def build_character_embeds_fr(tr: TranslatorENFR, url: str, char_name: str, portrait: str,
                             overview_en: str, stats_en: List[Tuple[str, str]],
                             weapons_en: List[Tuple[str, str, List[Tuple[str, str, str]]]],
                             armor_en: str, potentials_en: str,
                             footer: str) -> List[Dict[str, Any]]:

    embeds: List[Dict[str, Any]] = []

    # Couverture
    cover_desc = tr.translate(overview_en) if overview_en else ""
    if cover_desc:
        cover_desc = "🎯 **Aperçu**\n" + cover_desc
    else:
        cover_desc = "🎯 **Aperçu**\nInformations indisponibles."

    embeds.append(make_embed(
        title=f"{char_name} — Guide personnage",
        url=url,
        description=cover_desc[:4096],
        image=portrait,
        footer=footer
    ))

    # Stats
    fields = []
    for k, v in stats_en[:24]:
        k_fr = tr.translate(stat_label_fr(k))
        fields.append({"name": k_fr[:256], "value": v[:1024], "inline": True})
    if not fields:
        fields = [{"name": "Statistiques", "value": "Informations indisponibles.", "inline": False}]

    embeds.append(make_embed(
        title=f"{char_name} — Statistiques",
        url=url,
        description=" ",
        thumbnail=portrait,
        fields=fields,
        footer=footer
    ))

    # Armes (1 embed par arme)
    for wn, elem, skills in weapons_en[:3]:
        wn_fr = tr.translate(wn) if wn else "Arme"
        elem_fr = tr.translate(elem) if elem else ""
        title = f"{char_name} — Arme : {wn_fr}"
        if elem_fr:
            title += f" ({elem_fr})"

        w_fields: List[Dict[str, Any]] = []
        for typ, name, desc in skills[:20]:
            typ_fr = tr.translate(typ)
            name_fr = tr.translate(name)
            desc_fr = tr.translate(desc)
            value = f"**{name_fr}**\n{desc_fr}".strip()
            if len(value) > 1024:
                value = value[:1021] + "..."
            w_fields.append({"name": typ_fr[:256], "value": value if value else " ", "inline": False})
            if len(w_fields) >= 25:
                break

        if not w_fields:
            w_fields = [{"name": "Détails", "value": "Informations indisponibles.", "inline": False}]

        embeds.append(make_embed(
            title=title,
            url=url,
            description=" ",
            thumbnail=portrait,
            fields=w_fields,
            footer=footer
        ))

        if len(embeds) >= 9:
            break

    # Build (Armure/Accessoires) - compact
    if len(embeds) < 10:
        armor_fr = tr.translate(armor_en) if armor_en else "Informations indisponibles."
        armor_fr = armor_fr.strip()
        armor_fr = "🛡️ **Armure & accessoires (recommandé)**\n" + armor_fr
        for e in embeds_from_long_text(f"{char_name} — Build", url, armor_fr, thumbnail=portrait, footer=footer)[:2]:
            if len(embeds) >= 10:
                break
            embeds.append(e)

    # Potentiels - compact
    if len(embeds) < 10:
        pot_fr = tr.translate(potentials_en) if potentials_en else "Informations indisponibles."
        pot_fr = pot_fr.strip()
        pot_fr = "⭐ **Potentiels**\n" + pot_fr
        for e in embeds_from_long_text(f"{char_name} — Potentiels", url, pot_fr, thumbnail=portrait, footer=footer)[:2]:
            if len(embeds) >= 10:
                break
            embeds.append(e)

    return embeds[:10]

def build_topic_message_fr(tr: TranslatorENFR, title_en: str, url: str, cards: List[Dict[str, str]], footer: str) -> List[Dict[str, Any]]:
    title_fr = tr.translate(title_en) if title_en else "Guide"
    fields = []
    for c in cards[:25]:
        name_fr = tr.translate(c.get("title", ""))
        desc_fr = tr.translate(c.get("desc", ""))
        if not name_fr:
            continue
        if not desc_fr:
            desc_fr = " "
        if len(desc_fr) > 1024:
            desc_fr = desc_fr[:1021] + "..."
        fields.append({"name": name_fr[:256], "value": desc_fr, "inline": False})

    if not fields:
        fields = [{"name": "Contenu", "value": "Informations indisponibles.", "inline": False}]

    return [make_embed(
        title=title_fr,
        url=url,
        description=" ",
        fields=fields,
        footer=footer
    )]

def build_section_messages_fr(tr: TranslatorENFR, base_title_fr: str, url: str, sections: List[Dict[str, Any]], footer: str) -> Dict[str, List[Dict[str, Any]]]:
    """
    Retourne: { section_slug : [embeds...] }
    Chaque section -> 1 message (avec 1..n embeds si nécessaire).
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    for s in sections:
        title_en = s.get("title", "").strip()
        slug = slugify(title_en) or sha(title_en)[:10]

        title_fr = tr.translate(title_en) if title_en else "Section"
        full_title = f"{base_title_fr} — {title_fr}"

        # Corps: cartes + bullets
        parts = []
        cards = s.get("cards", [])
        bullets = s.get("bullets", [])

        if cards:
            parts.append("📌 **Points clés**")
            for c in cards[:12]:
                ct = tr.translate(c.get("title", ""))
                cd = tr.translate(c.get("desc", ""))
                parts.append(f"• **{ct}** — {cd}")
        if bullets:
            parts.append("\n🧠 **À retenir**")
            for b in bullets[:18]:
                parts.append(f"• {tr.translate(b)}")

        body = "\n".join(parts).strip() if parts else "Informations indisponibles."
        embeds = embeds_from_long_text(full_title, url, body, footer=footer)
        out[slug] = embeds[:10]
    return out

# -------------------- Combat/General extraction --------------------

def extract_sections_from_h2(page) -> List[Dict[str, Any]]:
    js = r"""
    () => {
      const main = document.querySelector('main') || document.body;

      const isVisible = (el) => {
        const r = el.getBoundingClientRect();
        if (!r || r.width < 5 || r.height < 5) return false;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
        return true;
      };

      const h2s = Array.from(main.querySelectorAll('h2')).filter(isVisible);
      const sections = [];

      for (const h2 of h2s) {
        const title = (h2.innerText||'').trim();
        if (!title) continue;

        const container = h2.closest('section') || h2.parentElement;
        if (!container) continue;

        // cards: h3+p
        const cardEls = Array.from(container.querySelectorAll('div,article,section'))
          .filter(isVisible)
          .filter(el => el.querySelector('h3') && el.querySelector('p'));

        const cards = [];
        for (const el of cardEls) {
          const h3 = el.querySelector('h3');
          const p = el.querySelector('p');
          const ct = (h3?.innerText||'').trim();
          const cd = (p?.innerText||'').trim();
          if (!ct || !cd) continue;
          if (ct.length > 100) continue;
          cards.push({title: ct, desc: cd});
        }

        // bullets (li)
        const bullets = Array.from(container.querySelectorAll('li'))
          .filter(isVisible)
          .map(li => (li.innerText||'').trim())
          .filter(t => t && t.length <= 240);

        // dédoublonnage
        const uniqCards = [];
        const seen = new Set();
        for (const c of cards) {
          const k = c.title + '||' + c.desc;
          if (seen.has(k)) continue;
          seen.add(k);
          uniqCards.push(c);
        }

        const uniqBullets = [];
        const seenB = new Set();
        for (const b of bullets) {
          if (seenB.has(b)) continue;
          seenB.add(b);
          uniqBullets.push(b);
        }

        sections.push({title, cards: uniqCards.slice(0, 30), bullets: uniqBullets.slice(0, 40)});
      }

      return sections.slice(0, 20);
    }
    """
    try:
        return page.evaluate(js) or []
    except Exception:
        return []

# -------------------- Boss extraction --------------------

def extract_boss_tab_names(page) -> List[str]:
    # onglets visibles en haut du boss guide
    names = extract_buttons_in_main(page)
    # filtre grossier: onglets connus contiennent souvent un espace ou une majuscule, et pas "Discord/Ko-fi"
    # on garde surtout ceux qui sont sur la ligne des onglets (Information + bosses)
    # Heuristique: on conserve ceux qui apparaissent aussi dans le bloc texte principal au début
    keep = []
    for n in names:
        if n.lower() in {"basic info", "weapons", "armor", "potentials"}:
            continue
        if n.lower() in {"key game systems", "elemental types", "world level", "character dupes", "costumes", "swimming"}:
            continue
        keep.append(n)
    # en pratique, ça retourne déjà la bonne liste sur cette page
    # on force "Information" en premier si présent
    if "Information" in keep:
        keep = ["Information"] + [x for x in keep if x != "Information"]
    return keep[:10]

def extract_boss_sections_from_page(page) -> List[Dict[str, Any]]:
    # utilise les h2 du panneau actif
    return extract_sections_from_h2(page)

# -------------------- Main orchestration --------------------

def main():
    state = load_state()
    session = http_session()
    webhooks = load_webhooks()
    footer = (os.environ.get("EXTRA_NOTE_FR", "").strip() or "Source : hideoutgacha.com").strip()

    tr = TranslatorENFR(state)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=UA,
            locale="fr-FR",
            extra_http_headers={"Accept-Language": "fr-FR,fr;q=0.9"},
            viewport={"width": 1400, "height": 900},
        )
        page = context.new_page()

        # ---------------- Characters (Solution A) ----------------
        wh_char = get_webhook(webhooks, "characters")
        if wh_char:
            roster = parse_character_roster(session)
            for char_url in roster:
                goto_page(page, char_url)

                # Nom
                char_name = ""
                try:
                    h1 = page.locator("h1")
                    if h1.count() > 0:
                        char_name = norm_line(h1.first.inner_text())
                except Exception:
                    pass
                if not char_name:
                    char_name = char_url.rstrip("/").split("/")[-1].replace("-", " ").title()

                portrait = extract_largest_image(page)

                # Basic Info
                click_text_safe(page, "Basic Info")
                page.wait_for_timeout(300)
                basic_text = extract_min_container_text(page, ["OVERVIEW", "BASE STATS"], max_chars=30000)
                overview_en, stats_en = parse_basic_info_block(basic_text)

                # Weapons
                weapons_en: List[Tuple[str, str, List[Tuple[str, str, str]]]] = []
                if click_text_safe(page, "Weapons"):
                    page.wait_for_timeout(300)

                    # essaie de détecter les noms d’armes sur la colonne gauche
                    weapon_candidates = []
                    try:
                        weapon_candidates = page.evaluate(r"""
                        () => {
                          const main = document.querySelector('main') || document.body;
                          const btns = Array.from(main.querySelectorAll('button, [role="button"]'))
                            .map(b => (b.innerText||'').trim())
                            .filter(t => t && t.length <= 20);
                          const ban = new Set(['Basic Info','Weapons','Armor','Potentials']);
                          const out = [];
                          const seen = new Set();
                          for (const t of btns) {
                            if (ban.has(t)) continue;
                            if (/^(Passive|Normal Attack|Special Attack|Normal Skill|Attack Skill|Ultimate Move)$/i.test(t)) continue;
                            if (!seen.has(t)) { seen.add(t); out.push(t); }
                          }
                          return out.slice(0, 6);
                        }
                        """) or []
                    except Exception:
                        weapon_candidates = []

                    # fallback si rien détecté
                    if not weapon_candidates:
                        weapon_candidates = ["Longsword", "Axe", "Dual Swords", "Book", "Spear", "Bow"]

                    for w in weapon_candidates:
                        # clic + extraction
                        click_text_safe(page, w)
                        page.wait_for_timeout(250)
                        w_text = extract_min_container_text(page, ["Passive", "Normal Attack"], max_chars=40000)
                        wn, elem, skills = parse_weapon_block(w_text)
                        if wn or skills:
                            weapons_en.append((wn or w, elem, skills))
                        if len(weapons_en) >= 3:
                            break

                # Armor
                armor_en = ""
                if click_text_safe(page, "Armor"):
                    page.wait_for_timeout(300)
                    armor_text = extract_min_container_text(page, ["Armor"], max_chars=35000) or extract_min_container_text(page, ["ARMOR"], max_chars=35000)
                    armor_lines = clean_lines((armor_text or "").splitlines())
                    armor_en = "\n".join(armor_lines[:220])

                # Potentials
                potentials_en = ""
                if click_text_safe(page, "Potentials"):
                    page.wait_for_timeout(300)
                    pot_text = extract_min_container_text(page, ["Tier"], max_chars=35000) or extract_min_container_text(page, ["Potential"], max_chars=35000)
                    pot_lines = clean_lines((pot_text or "").splitlines())
                    potentials_en = "\n".join(pot_lines[:220])

                source_obj = {
                    "name": char_name,
                    "overview": overview_en,
                    "stats": stats_en,
                    "weapons": weapons_en,
                    "armor": armor_en,
                    "potentials": potentials_en,
                }
                src_hash = stable_hash(source_obj)

                embeds = build_character_embeds_fr(
                    tr=tr,
                    url=char_url,
                    char_name=char_name,
                    portrait=portrait,
                    overview_en=overview_en,
                    stats_en=stats_en,
                    weapons_en=weapons_en,
                    armor_en=armor_en,
                    potentials_en=potentials_en,
                    footer=footer,
                )
                payload = {"username": "7DS Origin DB", "embeds": embeds}

                item_key = f"character::{char_url}"
                upsert_message_strict(session, state, "characters", wh_char, item_key, payload, src_hash)
                time.sleep(0.25)

        # ---------------- General Information (topics) ----------------
        wh_gen = get_webhook(webhooks, "general")
        if wh_gen:
            goto_page(page, URLS["general_info"])
            page.wait_for_timeout(500)

            topics = extract_buttons_in_main(page)
            # filtre: sur cette page, les topics sont des boutons courts
            # on enlève les boutons non pertinents
            banned = set(["Back", "← Back", "Back to Seven Deadly Sin: Origin", "Back to Seven Deadly Sins: Origin",
                          "Basic Info", "Weapons", "Armor", "Potentials", "Information", "Guardian Golem", "Drake", "Red Demon", "Grey Demon", "Albion"])
            topics = [t for t in topics if t not in banned]
            # si le site renvoie trop, on garde ceux qu'on attend + tout nouveau en plus
            preferred = ["Key Game Systems", "Elemental Types", "World Level", "Character Dupes", "Costumes", "Swimming"]
            ordered = [t for t in preferred if t in topics] + [t for t in topics if t not in preferred]
            topics = ordered[:12]

            for tname in topics:
                click_text_safe(page, tname)
                page.wait_for_timeout(350)
                data = extract_cards_in_active_panel(page)
                title_en = data.get("title") or tname
                cards = data.get("cards", [])

                src_hash = stable_hash({"topic": title_en, "cards": cards})
                embeds = build_topic_message_fr(tr, title_en, URLS["general_info"], cards, footer)
                payload = {"username": "7DS Origin DB", "embeds": embeds}

                item_key = f"general::{slugify(title_en)}"
                upsert_message_strict(session, state, "general", wh_gen, item_key, payload, src_hash)
                time.sleep(0.25)

        # ---------------- Combat Guide (topics from H2) ----------------
        wh_combat = get_webhook(webhooks, "combat")
        if wh_combat:
            goto_page(page, URLS["combat_guide"])
            page.wait_for_timeout(500)

            sections = extract_sections_from_h2(page)
            # construit 1 message par section
            sections_map = build_section_messages_fr(tr, "Guide de combat", URLS["combat_guide"], sections, footer)

            for sec_slug, embeds in sections_map.items():
                src_hash = stable_hash({"sec": sec_slug, "embeds_src": sections})  # simplifié (hash global section list)
                payload = {"username": "7DS Origin DB", "embeds": embeds}
                item_key = f"combat::{sec_slug}"
                upsert_message_strict(session, state, "combat", wh_combat, item_key, payload, src_hash)
                time.sleep(0.25)

        # ---------------- Boss Guide (tabs -> boss_<slug>) ----------------
        goto_page(page, URLS["boss_guide"])
        page.wait_for_timeout(500)

        boss_tabs = extract_boss_tab_names(page)
        for tab in boss_tabs:
            click_text_safe(page, tab)
            page.wait_for_timeout(400)

            boss_img = extract_largest_image(page)
            sections = extract_boss_sections_from_page(page)

            # on veut un message digeste : on prend les 4-6 sections les plus pertinentes
            # et on force un format "anti pavé"
            base_title_fr = "Guide boss"
            sec_map = build_section_messages_fr(tr, base_title_fr, URLS["boss_guide"], sections[:8], footer)

            # cover embed : image + résumé
            tab_fr = tr.translate(tab) if tab else "Boss"
            cover = make_embed(
                title=f"{tab_fr} — Guide boss",
                url=URLS["boss_guide"],
                description="✅ **Résumé**\n" + (tr.translate(sections[0]["bullets"][0]) if sections and sections[0].get("bullets") else "Stratégie, mécaniques et conseils."),
                image=boss_img,
                footer=footer,
            )

            embeds = [cover]
            # ajoute 3-5 "pages" max
            for k in list(sec_map.keys())[:5]:
                embeds.extend(sec_map[k][:1])
                if len(embeds) >= 10:
                    break
            embeds = embeds[:10]

            boss_slug = slugify(tab)
            boss_key = f"boss_{boss_slug}"
            wh_boss = get_webhook(webhooks, boss_key)
            if not wh_boss:
                # si pas de webhook dédié, on ne poste pas (pas de spam dans un mauvais salon)
                continue

            src_hash = stable_hash({"tab": tab, "sections": sections})
            payload = {"username": "7DS Origin DB", "embeds": embeds}
            item_key = f"boss::{boss_slug}"
            upsert_message_strict(session, state, boss_key, wh_boss, item_key, payload, src_hash)
            time.sleep(0.25)

        context.close()
        browser.close()

    save_state(state)

if __name__ == "__main__":
    main()
