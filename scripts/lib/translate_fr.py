import re
from typing import Any, Dict, List, Tuple
from .utils import sha

# Doré géré dans sync.py (embed color), ici on gère uniquement la langue.

GLOSSARY_POST: List[Tuple[str, str]] = [
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

EN_STOPWORDS = {
    "the","and","with","when","use","gain","deals","damage","increases","decreases",
    "target","enemies","enemy","seconds","sec","cooldown","stack","stacks","reset",
    "your","you","while","into","from","during","before","after","based","max",
    "effect","effects","skill","skills","attack","defense","crit","rate","movespeed"
}

class TranslatorENFR:
    """
    Argos offline EN->FR + cache + post-traitement + détection basique d'anglais restant.
    """
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
                raise RuntimeError("Modèle Argos EN->FR introuvable.")
            path = pkg.download()
            package.install_from_path(path)

            langs = translate.get_installed_languages()
            en = next((l for l in langs if l.code == "en"), None)
            fr = next((l for l in langs if l.code == "fr"), None)

        if not en or not fr:
            raise RuntimeError("Langues Argos EN/FR indisponibles.")
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

    def _has_english_left(self, text: str) -> bool:
        tokens = re.findall(r"[A-Za-z]{3,}", text.lower())
        if not tokens:
            return False
        # si une part non négligeable de stopwords EN reste, on considère qu'il reste de l'anglais
        hits = sum(1 for t in tokens if t in EN_STOPWORDS)
        return hits >= 2

    def translate(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        # numérique / symboles uniquement -> pas de traduction
        if re.fullmatch(r"[\d\s\.,:%+\-–/()]+", text):
            return text

        key = sha(text)
        cache = self.state["tcache"]
        if key in cache:
            return cache[key]

        self.ensure()
        fr = self._translation.translate(text)
        fr = self._postprocess(fr)

        # tentative de “zéro anglais” : si des stopwords anglais restent, on retraduit segmenté
        if self._has_english_left(fr):
            # retraduit phrase par phrase (meilleur que rien)
            parts = re.split(r"(\n+)", text)
            out_parts = []
            for p in parts:
                if p.startswith("\n"):
                    out_parts.append(p)
                else:
                    tp = p.strip()
                    if not tp:
                        out_parts.append(p)
                    else:
                        out_parts.append(self._postprocess(self._translation.translate(tp)))
            fr = "".join(out_parts).strip()

        # cache LRU simple
        order = self.state["torder"]
        cache[key] = fr
        order.append(key)
        if len(order) > 2500:
            for _ in range(400):
                if not order:
                    break
                k = order.pop(0)
                cache.pop(k, None)

        return fr