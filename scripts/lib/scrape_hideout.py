import re
from typing import Any, Dict, List, Tuple, Optional
from playwright.sync_api import Page
from .utils import clean_lines, norm_line

SKILL_TYPES = ["Passive", "Normal Attack", "Special Attack", "Normal Skill", "Attack Skill", "Ultimate Move"]

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

def goto_page(page: Page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(500)

def click_text_safe(page: Page, label: str) -> bool:
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

def try_next_data(page: Page) -> Optional[Dict[str, Any]]:
    # si c'est du Next.js, il peut y avoir __NEXT_DATA__
    js = r"""
    () => {
      const s = document.querySelector('script#__NEXT_DATA__');
      if (!s) return null;
      try { return JSON.parse(s.textContent || ''); } catch(e) { return null; }
    }
    """
    try:
        return page.evaluate(js)
    except Exception:
        return None

def main_inner_text(page: Page, max_chars: int = 90000) -> str:
    js = r"""
    (maxChars) => {
      const main = document.querySelector('main') || document.body;
      const t = (main.innerText || '').trim();
      return t.length > maxChars ? t.slice(0, maxChars) : t;
    }
    """
    try:
        return page.evaluate(js, max_chars) or ""
    except Exception:
        return ""

def extract_largest_visible_image(page: Page) -> str:
    js = r"""
    () => {
      const main = document.querySelector('main') || document.body;
      const imgs = Array.from(main.querySelectorAll('img'));
      const isVisible = (el) => {
        const r = el.getBoundingClientRect();
        if (!r || r.width < 40 || r.height < 40) return false;
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

def parse_basic_info_from_text(text: str) -> Tuple[str, List[Tuple[str, str]]]:
    lines = clean_lines(text.splitlines())
    overview = ""
    stats: List[Tuple[str, str]] = []

    def find_idx(token: str) -> int:
        token = token.upper()
        for i, l in enumerate(lines):
            if l.upper() == token:
                return i
        return -1

    io = find_idx("OVERVIEW")
    ib = find_idx("BASE STATS")

    if io != -1:
        end = ib if ib != -1 else min(io + 60, len(lines))
        overview = " ".join(lines[io + 1:end]).strip()

    if ib != -1:
        block = lines[ib + 1:ib + 180]
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

def parse_weapon_from_text(text: str) -> Tuple[str, str, List[Tuple[str, str, str]]]:
    lines = clean_lines(text.splitlines())
    weapon_name = lines[0] if lines else ""
    element = ""

    for l in lines[1:15]:
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
                desc_parts.append(lines[k])
                k += 1
            desc = " ".join(desc_parts).strip()
            skills.append((typ, name, desc))
            i = k
        else:
            i += 1

    return weapon_name, element, skills

def extract_weapon_buttons(page: Page) -> List[str]:
    # Sur l'onglet Weapons : il y a une colonne de boutons avec noms d'armes
    js = r"""
    () => {
      const main = document.querySelector('main') || document.body;
      const texts = [];
      const ban = new Set(['Basic Info','Weapons','Armor','Potentials']);
      const btns = Array.from(main.querySelectorAll('button,[role="button"]'));
      for (const b of btns) {
        const t = (b.innerText || '').trim();
        if (!t || t.length > 24) continue;
        if (ban.has(t)) continue;
        if (/^(Passive|Normal Attack|Special Attack|Normal Skill|Attack Skill|Ultimate Move)$/i.test(t)) continue;
        texts.push(t);
      }
      const out = [];
      const seen = new Set();
      for (const t of texts) {
        if (!seen.has(t)) { seen.add(t); out.push(t); }
      }
      return out.slice(0, 8);
    }
    """
    try:
        return page.evaluate(js) or []
    except Exception:
        return []

def extract_potentials_by_hover(page: Page) -> List[str]:
    """
    Essaye d'extraire les tooltips Tier 1..10 en hover.
    Si échec, fallback : lignes contenant 'Tier'.
    """
    tiers: List[str] = []

    # 1) fallback rapide si le texte contient déjà Tier
    t = main_inner_text(page, max_chars=120000)
    if "Tier" in t:
        for line in clean_lines(t.splitlines()):
            if re.search(r"\bTier\s*\d+\b", line):
                tiers.append(line)
        if tiers:
            return tiers[:12]

    # 2) hover sur éléments cliquables "1..10" (souvent des boutons)
    # On limite la recherche au main
    for n in range(1, 11):
        try:
            # essaie : role=button name="n"
            loc = page.get_by_role("button", name=str(n))
            if loc.count() == 0:
                # fallback : texte exact "n"
                loc = page.get_by_text(str(n), exact=True)
            if loc.count() == 0:
                continue

            loc.first.hover(timeout=2500)
            page.wait_for_timeout(200)

            # tooltip générique
            tip = ""
            try:
                tt = page.locator("[role='tooltip']")
                if tt.count() > 0:
                    tip = (tt.first.inner_text() or "").strip()
            except Exception:
                tip = ""

            if not tip:
                # fallback : un bloc qui contient "Tier n"
                cand = page.locator(f"text=/Tier\\s*{n}\\b/i")
                if cand.count() > 0:
                    tip = (cand.first.inner_text() or "").strip()

            tip = norm_line(tip)
            if tip and "Tier" in tip:
                tiers.append(tip)

        except Exception:
            continue

    # dédoublonnage
    uniq = []
    seen = set()
    for x in tiers:
        if x in seen:
            continue
        seen.add(x)
        uniq.append(x)
    return uniq[:12]

def extract_first_paragraph(page: Page) -> str:
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
      const ps = Array.from(main.querySelectorAll('p')).filter(isVisible);
      for (const p of ps) {
        const t = (p.innerText||'').trim();
        if (t && t.length >= 40) return t;
      }
      // fallback : première grosse ligne
      const t = (main.innerText||'').trim().split('\n').map(x=>x.trim()).filter(Boolean);
      for (const line of t) {
        if (line.length >= 60) return line;
      }
      return '';
    }
    """
    try:
        return page.evaluate(js) or ""
    except Exception:
        return ""

def split_sections_by_headings(text: str, heading_candidates: List[str]) -> List[Tuple[str, str]]:
    """
    Découpe un gros texte par titres connus (Fight Overview, Core Mechanics, etc.).
    Retourne [(heading, body)] dans l'ordre d'apparition.
    """
    lines = clean_lines(text.splitlines())
    if not lines:
        return []

    # index des headings repérés
    idxs = []
    upper_map = {h.upper(): h for h in heading_candidates}
    for i, l in enumerate(lines):
        lu = l.upper()
        if lu in upper_map:
            idxs.append((i, upper_map[lu]))

    if not idxs:
        return []

    out = []
    for k, (i, h) in enumerate(idxs):
        start = i + 1
        end = idxs[k + 1][0] if k + 1 < len(idxs) else len(lines)
        body = "\n".join(lines[start:end]).strip()
        if body:
            out.append((h, body))
    return out

def stat_label_fr(label: str) -> str:
    return STAT_LABELS_FR.get(label, label)