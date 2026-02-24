import os
import re
import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

# ==========
# Réglages
# ==========
COLOR_GOLD = int("C99700", 16)

BASE = os.getenv("HIDEOUT_BASE", "https://hideoutgacha.com/games/seven-deadly-sins-origin")
URL_CHAR_LIST = os.getenv("HIDEOUT_CHAR_LIST", f"{BASE}/characters")
URL_BOSS_GUIDES = os.getenv("HIDEOUT_BOSS_GUIDES", f"{BASE}/boss-guides")
URL_GENERAL = os.getenv("HIDEOUT_GENERAL", f"{BASE}/general-information")
URL_COMBAT = os.getenv("HIDEOUT_COMBAT", f"{BASE}/combat-guide")

MAX_BULLETS_PER_SECTION = 4
MAX_SECTIONS = 6

EN_STOP = {
    "the", "and", "or", "when", "use", "cancel", "knock", "damage", "phase",
    "overview", "strategy", "core", "mechanics", "fight", "tips", "guide",
    "attack", "defense", "critical", "window", "boss"
}

# Sections connus (on force des titres FR)
SECTION_FR = {
    "Fight Overview": "Aperçu du combat",
    "Core Mechanics": "Mécaniques clés",
    "Strategy": "Stratégie",
    "When Underpowered": "Si sous-équipé",
    "When Overpowered": "Si suréquipé",
    "Damage Windows and Burst Strategy": "Fenêtres de DPS et burst",
    "Dodging and Avoidance": "Esquive et placement",
    "Most Important Mechanic": "Mécanique la plus importante",
    "The Blue Outline Mechanic — Critical to Learn": "Mécanique lueur bleue — prioritaire",
    "Blue Outline Phase — Critical": "Phase lueur bleue — prioritaire",
    "Advanced Tips": "Astuces avancées",
    "Combat Basics": "Bases du combat",
    "Burst System": "Système de déchaînement",
    "Tag System": "Système de relais",
    "Status Effects Reference": "Effets de statut",
}

# Glossaire de puces fréquemment rencontrées
BULLET_FR = {
    "Cancel the incoming ability": "Annuler la compétence entrante",
    "Knock the boss down": "Mettre le boss à terre",
    "Open a large damage window": "Ouvrir une grosse fenêtre de DPS",
    "Play patiently — avoid committing all cooldowns at once": "Jouer patient : ne pas claquer tous les CD d’un coup",
    "Save high-damage abilities for knockdown phases": "Garder les gros dégâts pour les phases de mise à terre",
    "Coordinate interrupts in multiplayer so they are not wasted": "Coordonner les interruptions en multi pour ne pas les gaspiller",
    "Focus on burst rotations": "Prioriser les rotations burst",
    "Interrupt early to maximise damage windows": "Interrompre tôt pour maximiser les fenêtres de DPS",
    "Use multiplayer to trivialise the fight": "En multi, la gestion est plus simple : répartir les rôles",
    "Expect quick clears": "Clear rapide attendu",
    "Mechanics still exist but can often be brute-forced": "Les mécaniques restent là, mais peuvent souvent être forcées",
}

STAT_FR = {
    "Attack": "Attaque",
    "Defense": "Défense",
    "Accuracy": "Précision",
    "Block": "Blocage",
    "Crit Rate": "Taux critique",
    "Crit Damage": "Dégâts critiques",
    "Move Speed": "Vitesse de déplacement",
    "PvP Dmg Inc": "Bonus dégâts JcJ",
    "PvP Dmg Dec": "Réduction dégâts JcJ",
    "Block Dmg Res": "Résistance aux dégâts bloqués",
    "Crit Res": "Résistance critique",
}


def stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slugify(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-") or "item"


def clean_text(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s


def looks_english(s: str) -> bool:
    words = re.findall(r"[a-zA-Z']+", (s or "").lower())
    if not words:
        return False
    hits = sum(1 for w in words if w in EN_STOP)
    return (hits / max(1, len(words))) >= 0.25


def fr_section_title(raw: str) -> str:
    raw = clean_text(raw)
    return SECTION_FR.get(raw, raw)


def fr_bullet(raw: str) -> Optional[str]:
    raw = clean_text(raw)
    if not raw:
        return None
    if raw in BULLET_FR:
        return BULLET_FR[raw]
    # si c'est clairement anglais => on ne le sort pas
    if looks_english(raw):
        return None
    return raw


def pick_main_soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "lxml")
    main = soup.find("main") or soup.body or soup
    return main


def extract_first_image_url(main: BeautifulSoup) -> Optional[str]:
    img = main.find("img")
    if not img:
        return None
    src = img.get("src")
    if not src:
        return None
    if src.startswith("//"):
        src = "https:" + src
    if src.startswith("/"):
        src = "https://hideoutgacha.com" + src
    return src


def extract_sections(main: BeautifulSoup) -> List[Tuple[str, List[str]]]:
    """
    Extraction tolérante:
    - on parcourt h2/h3/h4
    - on récupère les <li> et quelques <p> courts
    """
    headers = main.find_all(["h2", "h3", "h4"])
    out: List[Tuple[str, List[str]]] = []

    for h in headers:
        title = clean_text(h.get_text(" ", strip=True))
        if not title:
            continue

        bullets: List[str] = []
        # Parcours des siblings jusqu'au prochain header
        for sib in h.find_all_next():
            if sib == h:
                continue
            if sib.name in ("h2", "h3", "h4"):
                break

            if sib.name in ("ul", "ol"):
                for li in sib.find_all("li"):
                    t = clean_text(li.get_text(" ", strip=True))
                    if t:
                        bullets.append(t)

            if sib.name == "p":
                t = clean_text(sib.get_text(" ", strip=True))
                # on ne prend pas des pavés
                if t and len(t) <= 200:
                    bullets.append(t)

            if len(bullets) >= 12:
                break

        if bullets:
            out.append((title, bullets))

        if len(out) >= MAX_SECTIONS:
            break

    return out


def extract_stat_pairs(main: BeautifulSoup) -> List[Tuple[str, str]]:
    """
    Essaie d'extraire des paires label->valeur visibles.
    Si non trouvé, renvoie vide (et on s'appuie sur la capture).
    """
    text = main.get_text("\n", strip=True)
    # Heuristique: repérer des lignes "Attack 200" etc.
    pairs: List[Tuple[str, str]] = []
    for line in text.splitlines():
        line = clean_text(line)
        m = re.match(r"^(Attack|Defense|Accuracy|Block|Crit Rate|Crit Damage|Move Speed|PvP Dmg Inc|PvP Dmg Dec|Block Dmg Res|Crit Res)\s+([0-9.]+%?)$", line)
        if m:
            k = STAT_FR.get(m.group(1), m.group(1))
            v = m.group(2)
            pairs.append((k, v))
    # dédoublonnage
    seen = set()
    uniq = []
    for k, v in pairs:
        if k in seen:
            continue
        seen.add(k)
        uniq.append((k, v))
    return uniq[:10]


@dataclass
class RenderedDiscordMessage:
    key: str
    payload: Dict[str, Any]
    files: List[Tuple[str, bytes, str]]
    content_hash: str


def build_embed_payload(
    *,
    title: str,
    description_lines: List[str],
    image_attachment_name: str,
    thumbnail_url: Optional[str],
    footer: str,
    fields: Optional[List[Tuple[str, str]]] = None,
) -> Dict[str, Any]:
    desc = "\n".join([f"• {clean_text(x)}" for x in description_lines if clean_text(x)])
    embed: Dict[str, Any] = {
        "title": title[:256],
        "color": COLOR_GOLD,
        "description": desc[:4096] if desc else "• Détails : voir la capture ci-dessous.",
        "image": {"url": f"attachment://{image_attachment_name}"},
        "footer": {"text": footer[:2048]},
    }
    if thumbnail_url:
        embed["thumbnail"] = {"url": thumbnail_url}

    if fields:
        ef = []
        for name, value in fields:
            name = clean_text(name)[:256]
            value = clean_text(value)[:1024]
            if name and value:
                ef.append({"name": name, "value": value, "inline": False})
        if ef:
            embed["fields"] = ef[:25]

    payload = {
        "content": "",
        "username": "7DS Origin DB",
        "allowed_mentions": {"parse": []},
        "embeds": [embed],
    }
    return payload


# =========================
# Playwright: screenshots
# =========================
def screenshot_main_jpeg(page, *, quality: int = 70, full_page: bool = True) -> bytes:
    # Essai sur <main>, sinon page complète
    try:
        loc = page.locator("main")
        if loc.count() > 0:
            return loc.first.screenshot(type="jpeg", quality=quality)
    except Exception:
        pass
    return page.screenshot(type="jpeg", quality=quality, full_page=full_page)


# =========================
# SCRAPE: Characters
# =========================
def scrape_character_urls(page) -> List[str]:
    page.goto(URL_CHAR_LIST, wait_until="domcontentloaded")
    page.wait_for_timeout(500)
    page.wait_for_load_state("networkidle")

    html = page.content()
    soup = pick_main_soup(html)

    urls = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/characters/" in href:
            if href.startswith("/"):
                href = "https://hideoutgacha.com" + href
            urls.add(href.split("?")[0].split("#")[0])

    return sorted(urls)


def scrape_character(page, url: str) -> RenderedDiscordMessage:
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(300)

    html = page.content()
    main = pick_main_soup(html)

    name = None
    h1 = main.find("h1")
    if h1:
        name = clean_text(h1.get_text(" ", strip=True))
    if not name:
        # fallback: dernier segment d'URL
        name = url.rstrip("/").split("/")[-1].replace("-", " ").title()

    portrait = extract_first_image_url(main)

    # stats (si dispo)
    stats = extract_stat_pairs(main)
    stat_lines = [f"{k} : {v}" for k, v in stats]

    # sections (bullets)
    sections = extract_sections(main)
    recap: List[str] = []
    for sec_title, bullets in sections[:3]:
        # Pas d'anglais : si le titre est anglais, on le force si connu sinon on le supprime
        tfr = fr_section_title(sec_title)
        # si titre encore anglais (heuristique), on remplace par un libellé neutre
        if looks_english(tfr):
            tfr = "Infos"

        recap.append(f"**{tfr}**")
        kept = 0
        for b in bullets:
            fb = fr_bullet(b)
            if not fb:
                continue
            recap.append(fb)
            kept += 1
            if kept >= 3:
                break

    if not recap:
        recap = ["Résumé : voir la capture."]

    # screenshot
    img_bytes = screenshot_main_jpeg(page, quality=70, full_page=True)
    img_name = f"perso_{slugify(name)}.jpg"

    fields = []
    if stat_lines:
        fields.append(("Statistiques", "\n".join([f"• {x}" for x in stat_lines[:10]])))

    payload = build_embed_payload(
        title=f"{name} — Guide personnage",
        description_lines=recap[:12],
        image_attachment_name=img_name,
        thumbnail_url=portrait,
        footer="Source : hideoutgacha.com",
        fields=fields if fields else None,
    )

    key = f"character::{url}"
    content_hash = stable_hash(name + "|" + "|".join(recap) + "|" + "|".join(stat_lines))

    return RenderedDiscordMessage(
        key=key,
        payload=payload,
        files=[(img_name, img_bytes, "image/jpeg")],
        content_hash=content_hash,
    )


# =========================
# SCRAPE: Topics pages (General/Combat)
# =========================
def _find_tabs(page):
    # Priorité aux vrais tabs ARIA
    loc = page.locator('[role="tab"]')
    if loc.count() > 0:
        return loc
    # fallback
    for sel in [".tabs button", "nav button", "button"]:
        loc = page.locator(sel)
        if loc.count() > 2:
            return loc
    return page.locator("button")


def _safe_click(locator):
    try:
        locator.click(timeout=4000)
        return True
    except Exception:
        return False


def scrape_topics(page, url: str, key_prefix: str, title_prefix: str) -> List[RenderedDiscordMessage]:
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(400)

    tabs = _find_tabs(page)
    n = min(tabs.count(), 12)  # sécurité

    results: List[RenderedDiscordMessage] = []
    seen_titles = set()

    for i in range(n):
        tab = tabs.nth(i)
        raw_title = clean_text(tab.inner_text() or "")
        if not raw_title or len(raw_title) > 60:
            continue

        # évite d'envoyer 2 fois le même
        if raw_title in seen_titles:
            continue
        seen_titles.add(raw_title)

        _safe_click(tab)
        page.wait_for_timeout(300)
        page.wait_for_load_state("networkidle")

        html = page.content()
        main = pick_main_soup(html)
        thumb = extract_first_image_url(main)

        sections = extract_sections(main)
        recap: List[str] = []

        # On ne poste pas le texte brut anglais: on fabrique un récap FR court
        # Si rien de propre, on laisse "voir capture"
        for sec_title, bullets in sections[:3]:
            tfr = fr_section_title(sec_title)
            if looks_english(tfr):
                tfr = "Infos"
            recap.append(f"**{tfr}**")
            kept = 0
            for b in bullets:
                fb = fr_bullet(b)
                if not fb:
                    continue
                recap.append(fb)
                kept += 1
                if kept >= 3:
                    break

        if not recap:
            recap = ["Détails : voir la capture ci-dessous."]

        img_bytes = screenshot_main_jpeg(page, quality=70, full_page=True)
        img_name = f"{key_prefix}_{slugify(raw_title)}.jpg"

        payload = build_embed_payload(
            title=f"{title_prefix} — {raw_title}",
            description_lines=recap[:14],
            image_attachment_name=img_name,
            thumbnail_url=thumb,
            footer="Source : hideoutgacha.com",
        )

        key = f"{key_prefix}::{slugify(raw_title)}"
        content_hash = stable_hash(raw_title + "|" + "|".join(recap))

        results.append(RenderedDiscordMessage(
            key=key,
            payload=payload,
            files=[(img_name, img_bytes, "image/jpeg")],
            content_hash=content_hash,
        ))

    return results


# =========================
# SCRAPE: Boss guides (tabs bosses)
# =========================
def scrape_boss_tabs(page) -> List[RenderedDiscordMessage]:
    page.goto(URL_BOSS_GUIDES, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(400)

    tabs = _find_tabs(page)
    n = min(tabs.count(), 12)

    results: List[RenderedDiscordMessage] = []
    seen_titles = set()

    for i in range(n):
        tab = tabs.nth(i)
        raw_title = clean_text(tab.inner_text() or "")
        if not raw_title or len(raw_title) > 60:
            continue
        if raw_title in seen_titles:
            continue
        seen_titles.add(raw_title)

        _safe_click(tab)
        page.wait_for_timeout(350)
        page.wait_for_load_state("networkidle")

        html = page.content()
        main = pick_main_soup(html)
        boss_img = extract_first_image_url(main)

        sections = extract_sections(main)

        # Récap FR ultra digeste (pas de pavé)
        recap: List[str] = []
        # 1) Un “Résumé” maison
        recap.append("**Résumé**")
        # Si on arrive à récupérer 2–4 puces “traduisibles” via glossaire => on les prend
        picked = 0
        for _, bullets in sections[:4]:
            for b in bullets:
                fb = fr_bullet(b)
                if not fb:
                    continue
                recap.append(fb)
                picked += 1
                if picked >= 4:
                    break
            if picked >= 4:
                break
        if picked == 0:
            recap.append("Les points clés sont visibles sur la capture ci-dessous.")

        # 2) Mini sections (si on a des titres connus)
        for sec_title, bullets in sections[:4]:
            tfr = fr_section_title(sec_title)
            if looks_english(tfr):
                continue
            recap.append(f"**{tfr}**")
            kept = 0
            for b in bullets:
                fb = fr_bullet(b)
                if not fb:
                    continue
                recap.append(fb)
                kept += 1
                if kept >= 2:
                    break

        # Screenshot principal
        img_bytes = screenshot_main_jpeg(page, quality=70, full_page=True)
        img_name = f"boss_{slugify(raw_title)}.jpg"

        # Titre embed (FR)
        title = "Informations — Guide boss" if raw_title.lower() == "information" else f"{raw_title} — Guide boss"

        payload = build_embed_payload(
            title=title,
            description_lines=recap[:16],
            image_attachment_name=img_name,
            thumbnail_url=boss_img,
            footer="Source : hideoutgacha.com",
        )

        key = f"boss::{slugify(raw_title)}"
        content_hash = stable_hash(raw_title + "|" + "|".join(recap))

        results.append(RenderedDiscordMessage(
            key=key,
            payload=payload,
            files=[(img_name, img_bytes, "image/jpeg")],
            content_hash=content_hash,
        ))

    return results
