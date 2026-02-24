import re
import io
from typing import Dict, Any, List, Tuple
import requests
from PIL import Image

from playwright.sync_api import sync_playwright

from lib.utils import clean_lines, chunk_text
from lib.translate_fr import tr


BASE = "https://hideoutgacha.com/games/seven-deadly-sins-origin"


# Routes attendues (si Hideout change ses URLs, adapte ici uniquement)
URLS = {
    "characters": f"{BASE}/characters",
    "general": f"{BASE}/general-information",
    "combat": f"{BASE}/combat-guide",
    "boss": f"{BASE}/boss-guides",
}

KNOWN_BOSS_SLUGS = ["information", "guardian_golem", "drake", "red_demon", "grey_demon", "albion"]


def hex_to_int(hex_color: str) -> int:
    h = hex_color.strip().lstrip("#")
    return int(h, 16)


def abs_url(href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return "https://hideoutgacha.com" + href
    return BASE.rstrip("/") + "/" + href


def dl_bytes(url: str) -> bytes:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content


def to_png_bytes(img_bytes: bytes) -> bytes:
    im = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    out = io.BytesIO()
    im.save(out, format="PNG", optimize=True)
    return out.getvalue()


def make_grid_png(urls: List[str], cell: int = 96, cols: int = 6, pad: int = 10) -> bytes:
    # Télécharge N icônes, fabrique une grille PNG
    icons: List[Image.Image] = []
    for u in urls[:24]:
        try:
            b = dl_bytes(u)
            im = Image.open(io.BytesIO(b)).convert("RGBA")
            im.thumbnail((cell, cell))
            icons.append(im)
        except Exception:
            continue

    if not icons:
        return b""

    rows = (len(icons) + cols - 1) // cols
    w = pad + cols * (cell + pad)
    h = pad + rows * (cell + pad)
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))

    for i, im in enumerate(icons):
        r = i // cols
        c = i % cols
        x = pad + c * (cell + pad) + (cell - im.width) // 2
        y = pad + r * (cell + pad) + (cell - im.height) // 2
        canvas.paste(im, (x, y), im)

    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()


def sectionize_by_headings(text: str, headings: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """
    headings: [("Overview","Aperçu"), ...]
    Retourne [(titreFR, contenuFR), ...] en gardant un maximum de contenu.
    """
    raw = clean_lines(text)
    if not raw:
        return []

    # Repère les headings EN dans le texte (ligne seule)
    lines = raw.splitlines()
    idx_map = {}
    for i, ln in enumerate(lines):
        for en, fr in headings:
            if ln.strip().lower() == en.strip().lower():
                idx_map.setdefault(i, fr)

    if not idx_map:
        # fallback: tout dans "Détails"
        return [("Détails", tr(raw))]

    sorted_idx = sorted(idx_map.keys())
    sections: List[Tuple[str, str]] = []
    for n, start in enumerate(sorted_idx):
        title_fr = idx_map[start]
        end = sorted_idx[n + 1] if n + 1 < len(sorted_idx) else len(lines)
        body = "\n".join(lines[start + 1 : end]).strip()
        if body:
            sections.append((title_fr, tr(body)))
    return sections


def build_embeds(title: str, sections: List[Tuple[str, str]], color_hex: str) -> List[Dict[str, Any]]:
    """
    Transforme sections -> liste d'embeds (chunking Discord-safe).
    """
    color = hex_to_int(color_hex)
    embeds: List[Dict[str, Any]] = []

    current = {
        "title": title,
        "color": color,
        "fields": [],
        "footer": {"text": "Source : hideoutgacha.com"},
    }
    field_count = 0

    for sec_title, sec_body in sections:
        for chunk in chunk_text(sec_body, 900):  # marge sous 1024
            if field_count >= 24:
                embeds.append(current)
                current = {
                    "title": title,
                    "color": color,
                    "fields": [],
                    "footer": {"text": "Source : hideoutgacha.com"},
                }
                field_count = 0
            current["fields"].append(
                {"name": f"🟨 {sec_title}", "value": chunk or "—", "inline": False}
            )
            field_count += 1

    embeds.append(current)
    return embeds


def chunk_embeds_into_messages(embeds: List[Dict[str, Any]], base_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Discord: max 10 embeds / message.
    """
    out = []
    for i in range(0, len(embeds), 10):
        payload = dict(base_payload)
        payload["embeds"] = embeds[i : i + 10]
        out.append(payload)
    return out


def extract_icon_urls(page, limit: int = 24) -> List[str]:
    # Prend les petites images (icônes) visibles dans le contenu
    urls = []
    imgs = page.locator("main img")
    count = min(imgs.count(), 200)
    for i in range(count):
        el = imgs.nth(i)
        src = el.get_attribute("src") or ""
        if not src.startswith("http"):
            continue
        try:
            box = el.bounding_box()
        except Exception:
            box = None
        if not box:
            continue
        area = box["width"] * box["height"]
        # icônes: petites
        if 400 <= area <= 20000:
            urls.append(src)
        if len(urls) >= limit:
            break
    # unique
    seen = set()
    uniq = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        uniq.append(u)
    return uniq


def best_main_image_url(page) -> str:
    # Image principale (perso/boss) : la plus grande dans main
    best = ("", 0.0)
    imgs = page.locator("main img")
    count = min(imgs.count(), 50)
    for i in range(count):
        el = imgs.nth(i)
        src = el.get_attribute("src") or ""
        if not src.startswith("http"):
            continue
        try:
            box = el.bounding_box()
        except Exception:
            box = None
        if not box:
            continue
        area = box["width"] * box["height"]
        if area > best[1]:
            best = (src, area)
    return best[0]


def collect_tab_buttons(page) -> List[str]:
    # Essaie d’attraper une UI "tabs" (armes, boss tabs, topics)
    buttons = page.locator('main button[role="tab"]')
    if buttons.count() == 0:
        buttons = page.locator('main [role="tablist"] button')
    names = []
    for i in range(min(buttons.count(), 30)):
        t = (buttons.nth(i).inner_text() or "").strip()
        if t and t not in names:
            names.append(t)
    return names


def click_tab_by_text(page, txt: str) -> None:
    # Clique un tab si possible
    loc = page.locator('main button[role="tab"]', has_text=txt)
    if loc.count() == 0:
        loc = page.locator('main [role="tablist"] button', has_text=txt)
    if loc.count() > 0:
        loc.first.click()
        page.wait_for_timeout(350)


def scrape_all_characters(color_hex: str) -> List[Dict[str, Any]]:
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        page = ctx.new_page()

        page.goto(URLS["characters"], wait_until="networkidle")
        page.wait_for_timeout(600)

        links = page.locator('a[href*="/games/seven-deadly-sins-origin/characters/"]')
        urls = []
        for i in range(min(links.count(), 400)):
            href = links.nth(i).get_attribute("href") or ""
            if "/characters/" in href:
                u = abs_url(href)
                urls.append(u)

        # dedup
        seen = set()
        char_urls = []
        for u in urls:
            if u in seen:
                continue
            seen.add(u)
            char_urls.append(u)

        for u in char_urls:
            slug = u.rstrip("/").split("/")[-1].strip()
            page.goto(u, wait_until="networkidle")
            page.wait_for_timeout(450)

            # Texte complet -> sections (FR)
            main_text = page.locator("main").inner_text() or ""
            headings = [
                ("Overview", "🎯 Aperçu"),
                ("Stats", "📊 Stats"),
                ("Potentials", "🧩 Potentiels"),
                ("Weapons", "⚔️ Armes"),
                ("Skills", "🌀 Compétences"),
            ]
            sections = sectionize_by_headings(main_text, headings)
            if not sections:
                sections = [("Détails", tr(clean_lines(main_text)))]

            title = f"{slug.title()} — Guide personnage"
            embeds = build_embeds(title, sections, color_hex=color_hex)

            # Images: perso + icônes (armes/skills)
            files = {}
            hero_url = best_main_image_url(page)
            if hero_url:
                try:
                    files[f"{slug}_perso.png"] = to_png_bytes(dl_bytes(hero_url))
                    # thumbnail sur le 1er embed
                    embeds[0]["thumbnail"] = {"url": f"attachment://{slug}_perso.png"}
                except Exception:
                    pass

            icon_urls = extract_icon_urls(page, limit=24)
            grid = make_grid_png(icon_urls)
            if grid:
                files[f"{slug}_icons.png"] = grid
                embeds[0]["image"] = {"url": f"attachment://{slug}_icons.png"}

            base_payload = {"content": "", "embeds": []}
            payloads = chunk_embeds_into_messages(embeds, base_payload)

            messages = []
            for pay in payloads:
                messages.append({"payload": pay, "files": files})

            results.append({"slug": slug, "messages": messages})

        ctx.close()
        browser.close()

    return results


def scrape_general_information(color_hex: str) -> List[Dict[str, Any]]:
    out = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        page = ctx.new_page()

        page.goto(URLS["general"], wait_until="networkidle")
        page.wait_for_timeout(600)

        topics = collect_tab_buttons(page)
        # Fallback si Hideout change: on garde les 6 attendus
        if not topics:
            topics = ["Key Game Systems", "Elemental Types", "World Level", "Character Dupes", "Costumes", "Swimming"]

        for t in topics[:12]:
            click_tab_by_text(page, t)
            txt = clean_lines(page.locator("main").inner_text() or "")
            sections = sectionize_by_headings(
                txt,
                [
                    ("General Information", "🧭 Infos générales"),
                    (t, f"🟨 {tr(t)}"),
                ],
            )
            if not sections:
                sections = [(f"🟨 {tr(t)}", tr(txt))]

            title = f"{tr(t)} — Infos générales"
            embeds = build_embeds(title, sections, color_hex=color_hex)
            payload = {"content": "", "embeds": embeds[:10]}
            out.append({"slug": slugify(t), "payload": payload, "files": {}})

        ctx.close()
        browser.close()
    return out


def scrape_combat_guide(color_hex: str) -> List[Dict[str, Any]]:
    out = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        page = ctx.new_page()

        page.goto(URLS["combat"], wait_until="networkidle")
        page.wait_for_timeout(600)

        sections_tabs = collect_tab_buttons(page)
        if not sections_tabs:
            sections_tabs = ["Combat Basics", "Burst System", "Tag System", "Status Effects Reference", "Advanced Tips"]

        for t in sections_tabs[:20]:
            click_tab_by_text(page, t)
            txt = clean_lines(page.locator("main").inner_text() or "")
            sections = sectionize_by_headings(
                txt,
                [
                    ("Combat Basics", "⚔️ Bases du combat"),
                    ("Burst System", "💥 Système de déchaînement"),
                    ("Tag System", "🔁 Système de relais"),
                    ("Status Effects Reference", "🧷 Effets de statut"),
                    ("Advanced Tips", "🧠 Conseils avancés"),
                ],
            )
            if not sections:
                sections = [("Détails", tr(txt))]

            title = f"{tr(t)} — Guide combat"
            embeds = build_embeds(title, sections, color_hex=color_hex)
            payload = {"content": "", "embeds": embeds[:10]}
            out.append({"slug": slugify(t), "payload": payload, "files": {}})

        ctx.close()
        browser.close()
    return out


def scrape_bosses(color_hex: str) -> List[Dict[str, Any]]:
    out = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        page = ctx.new_page()

        page.goto(URLS["boss"], wait_until="networkidle")
        page.wait_for_timeout(800)

        tabs = collect_tab_buttons(page)
        if not tabs:
            tabs = ["Information", "Guardian Golem", "Drake", "Red Demon", "Grey Demon", "Albion"]

        # Map tab -> slug salon
        def boss_slug(tab: str) -> str:
            low = tab.strip().lower()
            if "information" in low:
                return "information"
            if "guardian" in low or "golem" in low:
                return "guardian_golem"
            if "drake" in low:
                return "drake"
            if "red" in low:
                return "red_demon"
            if "grey" in low or "gray" in low:
                return "grey_demon"
            if "albion" in low:
                return "albion"
            return slugify(tab)

        for t in tabs[:20]:
            click_tab_by_text(page, t)
            txt = clean_lines(page.locator("main").inner_text() or "")
            sections = sectionize_by_headings(
                txt,
                [
                    ("Fight Overview", "🧱 Vue d’ensemble"),
                    ("Core Mechanics", "🧠 Mécaniques clés"),
                    ("Strategy", "🎮 Stratégie"),
                    ("When Underpowered", "🛡️ Quand tu es en dessous"),
                    ("When Overpowered", "⚡ Quand tu surclasses"),
                ],
            )
            if not sections:
                sections = [("Détails", tr(txt))]

            slug = boss_slug(t)
            title = f"{tr(t)} — Guide boss"
            embeds = build_embeds(title, sections, color_hex=color_hex)

            files = {}
            img_url = best_main_image_url(page)
            if img_url:
                try:
                    files[f"{slug}_boss.png"] = to_png_bytes(dl_bytes(img_url))
                    embeds[0]["image"] = {"url": f"attachment://{slug}_boss.png"}
                except Exception:
                    pass

            payloads = chunk_embeds_into_messages(embeds, {"content": "", "embeds": []})
            messages = [{"payload": pay, "files": files} for pay in payloads]
            out.append({"slug": slug, "messages": messages})

        ctx.close()
        browser.close()
    return out


def slugify(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "item"
