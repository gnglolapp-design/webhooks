import json
import os
from typing import Any, Dict, List

import requests
from playwright.sync_api import sync_playwright

from lib.discord_api import replace_message
from lib.scrape_hideout import (
    scrape_character_urls,
    scrape_character,
    scrape_boss_tabs,
    scrape_topics,
    URL_GENERAL,
    URL_COMBAT,
)

STATE_PATH = os.getenv("STATE_PATH", "state/state.json")

# Webhooks (env)
WEBHOOK_CHARACTERS = os.getenv("WEBHOOK_CHARACTERS", "")
WEBHOOK_GENERAL = os.getenv("WEBHOOK_GENERAL", "")
WEBHOOK_COMBAT = os.getenv("WEBHOOK_COMBAT", "")

WEBHOOK_BOSS_INFO = os.getenv("WEBHOOK_BOSS_INFO", "")
WEBHOOK_GUARDIAN_GOLEM = os.getenv("WEBHOOK_GUARDIAN_GOLEM", "")
WEBHOOK_DRAKE = os.getenv("WEBHOOK_DRAKE", "")
WEBHOOK_RED_DEMON = os.getenv("WEBHOOK_RED_DEMON", "")
WEBHOOK_GREY_DEMON = os.getenv("WEBHOOK_GREY_DEMON", "")
WEBHOOK_ALBION = os.getenv("WEBHOOK_ALBION", "")


def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return {"messages": {}}
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def upsert_items(
    session: requests.Session,
    state: Dict[str, Any],
    webhook_url: str,
    items,
) -> None:
    if not webhook_url:
        return

    msg_state: Dict[str, Any] = state.setdefault("messages", {})
    for it in items:
        prev = msg_state.get(it.key, {})
        prev_hash = prev.get("hash")
        prev_id = prev.get("message_id")

        if prev_hash == it.content_hash and prev_id:
            # inchangé
            continue

        # Remplacement robuste: POST nouveau + DELETE ancien
        new_id = replace_message(
            session,
            webhook_url,
            prev_id,
            it.payload,
            files=it.files,
        )
        msg_state[it.key] = {"message_id": new_id, "hash": it.content_hash}


def boss_webhook_for_key(boss_key: str) -> str:
    # boss_key = "boss::guardian-golem" etc.
    slug = boss_key.split("::", 1)[-1]
    if slug == "information":
        return WEBHOOK_BOSS_INFO
    if "guardian" in slug:
        return WEBHOOK_GUARDIAN_GOLEM
    if "drake" in slug:
        return WEBHOOK_DRAKE
    if "red" in slug:
        return WEBHOOK_RED_DEMON
    if "grey" in slug or "gray" in slug:
        return WEBHOOK_GREY_DEMON
    if "albion" in slug:
        return WEBHOOK_ALBION
    # fallback: boss-infos
    return WEBHOOK_BOSS_INFO


def main() -> None:
    state = load_state()
    sess = requests.Session()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})

        # =========
        # CHARACTERS
        # =========
        if WEBHOOK_CHARACTERS:
            urls = scrape_character_urls(page)
            # dédoublonnage sécurité
            seen = set()
            items = []
            for u in urls:
                if u in seen:
                    continue
                seen.add(u)
                try:
                    items.append(scrape_character(page, u))
                except Exception:
                    # passe-outre: on continue
                    continue

            upsert_items(sess, state, WEBHOOK_CHARACTERS, items)

        # ==============
        # GENERAL TOPICS
        # ==============
        if WEBHOOK_GENERAL:
            try:
                general_items = scrape_topics(
                    page,
                    URL_GENERAL,
                    key_prefix="general",
                    title_prefix="Infos générales",
                )
                upsert_items(sess, state, WEBHOOK_GENERAL, general_items)
            except Exception:
                pass

        # ============
        # COMBAT GUIDE
        # ============
        if WEBHOOK_COMBAT:
            try:
                combat_items = scrape_topics(
                    page,
                    URL_COMBAT,
                    key_prefix="combat",
                    title_prefix="Combat",
                )
                upsert_items(sess, state, WEBHOOK_COMBAT, combat_items)
            except Exception:
                pass

        # =========
        # BOSSES
        # =========
        try:
            boss_items = scrape_boss_tabs(page)
            # 1 message par boss, envoyé dans son webhook dédié
            for it in boss_items:
                wh = boss_webhook_for_key(it.key)
                try:
                    upsert_items(sess, state, wh, [it])
                except Exception:
                    continue
        except Exception:
            pass

        browser.close()

    save_state(state)


if __name__ == "__main__":
    main()
