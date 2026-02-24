import os
import sys
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

# permettre imports scripts/lib
sys.path.append(str(Path(__file__).resolve().parent))

from lib.utils import stable_hash, chunk_by_chars, clean_lines
from lib.discord_api import http_session, upsert_message
from lib.translate_fr import TranslatorENFR
from lib.scrape_hideout import (
    goto_page, click_text_safe, try_next_data, main_inner_text,
    extract_largest_visible_image, parse_basic_info_from_text, parse_weapon_from_text,
    extract_weapon_buttons, extract_potentials_by_hover, extract_first_paragraph,
    split_sections_by_headings, stat_label_fr
)

import requests
from bs4 import BeautifulSoup

BASE = "https://hideoutgacha.com/games/seven-deadly-sins-origin"
URLS = {
    "characters": f"{BASE}/characters",
    "general_info": f"{BASE}/general-info",
    "combat_guide": f"{BASE}/combat-guide",
    "boss_guide": f"{BASE}/boss-guide",
}

STATE_PATH = "state/state.json"
EMBED_COLOR = int("C99700", 16)

WEBHOOK_ALIASES = {
    "characters": ["characters", "persos", "personnages"],
    "general": ["general", "general_info", "infos_generales", "info_generale"],
    "combat": ["combat", "combat_guide", "guide_combat"],
}

def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return {"v": 2, "items": {}, "webhook_fp": {}, "tcache": {}, "torder": []}
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        state = json.load(f)
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

def fetch_html(session: requests.Session, url: str) -> str:
    r = session.get(url, timeout=45)
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

def make_embed(title: str, url: str, description: str = " ", thumbnail: str = "", image: str = "",
               fields: List[Dict[str, Any]] = None, footer: str = "") -> Dict[str, Any]:
    emb: Dict[str, Any] = {
        "title": (title or " ").strip()[:256],
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

def embeds_from_long_text(title: str, url: str, text: str, thumbnail: str = "", image: str = "", footer: str = "") -> List[Dict[str, Any]]:
    chunks = chunk_by_chars(text, limit=3200)
    if not chunks:
        return [make_embed(title, url, " ", thumbnail=thumbnail, image=image, footer=footer)]
    out = []
    for i, ch in enumerate(chunks[:10]):
        t = title if len(chunks) == 1 else f"{title} ({i+1}/{min(len(chunks),10)})"
        out.append(make_embed(t, url, ch, thumbnail=thumbnail, image=image if i == 0 else "", footer=footer))
    return out

def load_webhooks() -> Dict[str, str]:
    raw = os.environ.get("DISCORD_WEBHOOKS_JSON", "").strip()
    if not raw:
        raise SystemExit("Missing DISCORD_WEBHOOKS_JSON (GitHub Secret).")
    m = json.loads(raw)

    out = dict(m)
    for canon, aliases in WEBHOOK_ALIASES.items():
        for a in aliases:
            if a in m:
                out[canon] = m[a]
                break
    return out

def get_webhook(m: Dict[str, str], key: str) -> str:
    return m.get(key, "")

def slugify(s: str) -> str:
    s = (s or "").lower().strip()
    s = __import__("re").sub(r"[^a-z0-9]+", "_", s)
    s = __import__("re").sub(r"_+", "_", s).strip("_")
    return s

def build_character_embeds(tr: TranslatorENFR, url: str, name: str, portrait: str,
                          overview_en: str, stats_en: List[Tuple[str, str]],
                          weapons_en: List[Dict[str, Any]],
                          armor_en: str, potentials_list: List[str],
                          footer: str) -> List[Dict[str, Any]]:

    embeds: List[Dict[str, Any]] = []

    # Couverture
    overview_fr = tr.translate(overview_en) if overview_en else "Informations indisponibles."
    cover_desc = f"🎯 **Aperçu**\n{overview_fr}".strip()
    embeds.append(make_embed(
        title=f"{name} — Guide personnage",
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
        title=f"{name} — Statistiques",
        url=url,
        description=" ",
        thumbnail=portrait,
        fields=fields,
        footer=footer
    ))

    # Armes (max 3)
    for w in weapons_en[:3]:
        wn_fr = tr.translate(w.get("name","")) or "Arme"
        elem_fr = tr.translate(w.get("element","")) if w.get("element") else ""
        w_icon = w.get("icon","")

        title = f"{name} — Arme : {wn_fr}" + (f" ({elem_fr})" if elem_fr else "")
        w_fields = []
        for s in w.get("skills", [])[:20]:
            typ_fr = tr.translate(s.get("type",""))
            sn_fr = tr.translate(s.get("name",""))
            sd_fr = tr.translate(s.get("desc",""))
            value = f"**{sn_fr}**\n{sd_fr}".strip()
            if len(value) > 1024:
                value = value[:1021] + "..."
            w_fields.append({"name": typ_fr[:256] or " ", "value": value if value else " ", "inline": False})
            if len(w_fields) >= 25:
                break
        if not w_fields:
            w_fields = [{"name": "Détails", "value": "Informations indisponibles.", "inline": False}]

        embeds.append(make_embed(
            title=title,
            url=url,
            description=" ",
            thumbnail=w_icon or portrait,
            fields=w_fields,
            footer=footer
        ))
        if len(embeds) >= 9:
            break

    # Build (Armor)
    if len(embeds) < 10:
        armor_fr = tr.translate(armor_en) if armor_en else "Informations indisponibles."
        body = "🛡️ **Armure & accessoires (recommandé)**\n" + armor_fr
        for e in embeds_from_long_text(f"{name} — Build", url, body, thumbnail=portrait, footer=footer)[:2]:
            if len(embeds) >= 10:
                break
            embeds.append(e)

    # Potentials
    if len(embeds) < 10:
        if potentials_list:
            pot_fr_lines = [tr.translate(x) for x in potentials_list]
            pot_fr = "\n".join([f"• {x}" for x in pot_fr_lines if x.strip()])
        else:
            pot_fr = "Informations indisponibles."
        body = "⭐ **Potentiels**\n" + pot_fr
        for e in embeds_from_long_text(f"{name} — Potentiels", url, body, thumbnail=portrait, footer=footer)[:2]:
            if len(embeds) >= 10:
                break
            embeds.append(e)

    return embeds[:10]

def main():
    state = load_state()
    session = http_session()
    webhooks = load_webhooks()
    footer = (os.environ.get("EXTRA_NOTE_FR", "").strip() or "Source : hideoutgacha.com").strip()

    tr = TranslatorENFR(state)

    # anti-doublons strict : ne reposte pas si message_id perdu
    STRICT = True

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="fr-FR",
            extra_http_headers={"Accept-Language": "fr-FR,fr;q=0.9"},
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()

        # ---------------- Characters ----------------
        wh_char = get_webhook(webhooks, "characters")
        if wh_char:
            roster = parse_character_roster(session)
            print(f"[CHAR] roster={len(roster)}")

            for char_url in roster:
                goto_page(page, char_url)

                # nom
                name = ""
                try:
                    h1 = page.locator("h1")
                    if h1.count() > 0:
                        name = h1.first.inner_text().strip()
                except Exception:
                    pass
                if not name:
                    name = char_url.rstrip("/").split("/")[-1].replace("-", " ").title()

                portrait = extract_largest_visible_image(page)

                # Basic Info
                click_text_safe(page, "Basic Info")
                page.wait_for_timeout(250)

                txt = main_inner_text(page)
                overview_en, stats_en = parse_basic_info_from_text(txt)

                # Weapons
                weapons_en: List[Dict[str, Any]] = []
                if click_text_safe(page, "Weapons"):
                    page.wait_for_timeout(300)
                    btns = extract_weapon_buttons(page)
                    # fallback hard si détection ratée
                    if not btns:
                        btns = ["Longsword", "Axe", "Dual Swords", "Book", "Spear", "Bow"]

                    for b in btns:
                        click_text_safe(page, b)
                        page.wait_for_timeout(200)
                        wtxt = main_inner_text(page)
                        wn, elem, skills = parse_weapon_from_text(wtxt)
                        weapons_en.append({
                            "name": wn or b,
                            "element": elem,
                            "icon": "",  # (optionnel) difficile à stabiliser sans selector dédié
                            "skills": [{"type": t, "name": n, "desc": d} for (t, n, d) in skills],
                        })
                        if len(weapons_en) >= 3:
                            break

                # Armor
                armor_en = ""
                if click_text_safe(page, "Armor"):
                    page.wait_for_timeout(250)
                    armor_en = "\n".join(clean_lines(main_inner_text(page).splitlines())[:260])

                # Potentials (hover)
                potentials_list: List[str] = []
                if click_text_safe(page, "Potentials"):
                    page.wait_for_timeout(350)
                    potentials_list = extract_potentials_by_hover(page)

                # debug
                print(f"[CHAR] {name} overview={len(overview_en)} stats={len(stats_en)} weapons={len(weapons_en)} armor={len(armor_en)} pot={len(potentials_list)}")

                src_hash = stable_hash({
                    "name": name,
                    "overview": overview_en,
                    "stats": stats_en,
                    "weapons": weapons_en,
                    "armor": armor_en,
                    "potentials": potentials_list
                })

                embeds = build_character_embeds(tr, char_url, name, portrait, overview_en, stats_en, weapons_en, armor_en, potentials_list, footer)
                payload = {"username": "7DS Origin DB", "embeds": embeds}

                item_key = f"character::{char_url}"
                upsert_message(session, state, "characters", wh_char, item_key, payload, src_hash, strict_no_duplicate=STRICT)
                time.sleep(0.25)

        # ---------------- General Info (topics -> 1 message par topic, même salon) ----------------
        wh_gen = get_webhook(webhooks, "general")
        if wh_gen:
            goto_page(page, URLS["general_info"])

            # topics : on lit les boutons visibles (texte court)
            js = r"""
            () => {
              const main = document.querySelector('main') || document.body;
              const btns = Array.from(main.querySelectorAll('button'))
                .map(b => (b.innerText||'').trim())
                .filter(t => t && t.length >= 3 && t.length <= 40);
              const ban = new Set(['Discord','Ko-fi','Home','Games','About']);
              const out = [];
              const seen = new Set();
              for (const t of btns) {
                if (ban.has(t)) continue;
                if (!seen.has(t)) { seen.add(t); out.push(t); }
              }
              return out;
            }
            """
            topics = page.evaluate(js) or []
            preferred = ["Key Game Systems", "Elemental Types", "World Level", "Character Dupes", "Costumes", "Swimming"]
            ordered = [t for t in preferred if t in topics] + [t for t in topics if t not in preferred]
            topics = ordered[:12]
            print(f"[GEN] topics={topics}")

            for tname in topics:
                click_text_safe(page, tname)
                page.wait_for_timeout(250)

                # extraction cards (h3+p) sur le panneau actif
                data = page.evaluate(r"""
                () => {
                  const main = document.querySelector('main') || document.body;
                  const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    if (!r || r.width < 5 || r.height < 5) return false;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                    return true;
                  };
                  const h = Array.from(main.querySelectorAll('h2,h1')).find(el => isVisible(el) && (el.innerText||'').trim().length>0);
                  const title = h ? (h.innerText||'').trim() : '';
                  const cardEls = Array.from(main.querySelectorAll('div,section,article'))
                    .filter(isVisible)
                    .filter(el => el.querySelector('h3') && el.querySelector('p'));
                  const cards = [];
                  for (const el of cardEls) {
                    const ct = (el.querySelector('h3')?.innerText||'').trim();
                    const cd = (el.querySelector('p')?.innerText||'').trim();
                    if (!ct || !cd) continue;
                    if (ct.length > 90) continue;
                    cards.push({title: ct, desc: cd});
                  }
                  const seen = new Set();
                  const uniq = [];
                  for (const c of cards) {
                    const k = c.title + '||' + c.desc;
                    if (seen.has(k)) continue;
                    seen.add(k);
                    uniq.push(c);
                  }
                  return {title, cards: uniq.slice(0, 40)};
                }
                """) or {"title": "", "cards": []}

                title_en = data.get("title") or tname
                cards = data.get("cards", [])

                title_fr = tr.translate(title_en) or tr.translate(tname) or "Informations générales"
                fields = []
                for c in cards[:25]:
                    n_fr = tr.translate(c.get("title",""))
                    d_fr = tr.translate(c.get("desc",""))
                    if not n_fr:
                        continue
                    if len(d_fr) > 1024:
                        d_fr = d_fr[:1021] + "..."
                    fields.append({"name": n_fr[:256], "value": d_fr or " ", "inline": False})
                if not fields:
                    fields = [{"name": "Contenu", "value": "Informations indisponibles.", "inline": False}]

                embeds = [make_embed(title_fr, URLS["general_info"], " ", fields=fields, footer=footer)]
                payload = {"username": "7DS Origin DB", "embeds": embeds}

                src_hash = stable_hash({"topic": title_en, "cards": cards})
                item_key = f"general::{slugify(title_en)}"
                upsert_message(session, state, "general", wh_gen, item_key, payload, src_hash, strict_no_duplicate=STRICT)
                time.sleep(0.25)

        # ---------------- Combat Guide (sections -> 1 message par heading) ----------------
        wh_combat = get_webhook(webhooks, "combat")
        if wh_combat:
            goto_page(page, URLS["combat_guide"])
            full = main_inner_text(page)

            headings = [
                "Combat Basics",
                "Burst System",
                "Tag System",
                "Status Effects Reference",
                "Advanced Tips",
            ]
            sections = split_sections_by_headings(full, headings)
            print(f"[COMBAT] sections={len(sections)}")

            # si découpe ratée, fallback: 1 seul gros message (toujours sans pavé grâce au split embed)
            if not sections:
                body = "\n".join(clean_lines(full.splitlines())[:500])
                embeds = embeds_from_long_text("Guide de combat", URLS["combat_guide"], tr.translate(body), footer=footer)[:10]
                payload = {"username": "7DS Origin DB", "embeds": embeds}
                src_hash = stable_hash({"fallback": body[:6000]})
                upsert_message(session, state, "combat", wh_combat, "combat::fallback", payload, src_hash, strict_no_duplicate=STRICT)
            else:
                for h, body_en in sections:
                    title_fr = tr.translate(h) or "Guide de combat"
                    body_fr = tr.translate(body_en)
                    embeds = embeds_from_long_text(f"Guide de combat — {title_fr}", URLS["combat_guide"], body_fr, footer=footer)[:6]
                    payload = {"username": "7DS Origin DB", "embeds": embeds}
                    src_hash = stable_hash({"h": h, "body": body_en})
                    item_key = f"combat::{slugify(h)}"
                    upsert_message(session, state, "combat", wh_combat, item_key, payload, src_hash, strict_no_duplicate=STRICT)
                    time.sleep(0.25)

        # ---------------- Boss Guide (tabs -> boss_<slug> webhook) ----------------
        goto_page(page, URLS["boss_guide"])

        # onglets boss
        tabs = page.evaluate(r"""
        () => {
          const main = document.querySelector('main') || document.body;
          const btns = Array.from(main.querySelectorAll('button'))
            .map(b => (b.innerText||'').trim())
            .filter(t => t && t.length >= 3 && t.length <= 40);
          // heuristique : sur cette page on a "Information" + noms de boss
          const ban = new Set(['Discord','Ko-fi','Home','Games','About','Basic Info','Weapons','Armor','Potentials']);
          const out = [];
          const seen = new Set();
          for (const t of btns) {
            if (ban.has(t)) continue;
            if (!seen.has(t)) { seen.add(t); out.push(t); }
          }
          if (out.includes('Information')) {
            return ['Information', ...out.filter(x => x !== 'Information')].slice(0, 12);
          }
          return out.slice(0, 12);
        }
        """) or []

        print(f"[BOSS] tabs={tabs}")

        boss_headings = [
            "Fight Overview",
            "Core Mechanics",
            "Strategy",
            "Damage Windows and Burst Strategy",
            "Dodging and Avoidance",
            "When Underpowered",
            "When Overpowered",
        ]

        for tab in tabs:
            boss_slug = slugify(tab)
            boss_key = f"boss_{boss_slug}"
            wh_boss = get_webhook(webhooks, boss_key)
            if not wh_boss:
                continue

            click_text_safe(page, tab)
            page.wait_for_timeout(350)

            img = extract_largest_visible_image(page)
            intro_en = extract_first_paragraph(page)
            full = main_inner_text(page)
            sections = split_sections_by_headings(full, boss_headings)

            tab_fr = tr.translate(tab)
            intro_fr = tr.translate(intro_en) if intro_en else "Informations indisponibles."

            cover = make_embed(
                title=f"{tab_fr} — Guide boss",
                url=URLS["boss_guide"],
                description=("✅ **Résumé**\n" + intro_fr)[:4096],
                image=img,
                footer=footer
            )

            embeds = [cover]

            # Ajoute sections (anti-pavé)
            added = 0
            for h, body_en in sections:
                if added >= 5 or len(embeds) >= 10:
                    break
                h_fr = tr.translate(h)
                body_fr = tr.translate(body_en)
                body_fr = "\n".join(body_fr.splitlines()[:60]).strip()
                embeds.append(make_embed(
                    title=f"{tab_fr} — {h_fr}",
                    url=URLS["boss_guide"],
                    description=body_fr[:4096] if body_fr else " ",
                    footer=footer
                ))
                added += 1

            # fallback si aucune section détectée
            if len(embeds) == 1:
                body = "\n".join(clean_lines(full.splitlines())[:300])
                body_fr = tr.translate(body)
                embeds.extend(embeds_from_long_text(f"{tab_fr} — Détails", URLS["boss_guide"], body_fr, footer=footer)[:3])

            payload = {"username": "7DS Origin DB", "embeds": embeds[:10]}
            src_hash = stable_hash({"tab": tab, "intro": intro_en, "sections": sections, "img": img})
            item_key = f"boss::{boss_slug}"
            upsert_message(session, state, boss_key, wh_boss, item_key, payload, src_hash, strict_no_duplicate=STRICT)
            time.sleep(0.25)

        context.close()
        browser.close()

    save_state(state)

if __name__ == "__main__":
    main()