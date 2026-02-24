import os
import json
from datetime import datetime, timezone

from lib.discord_api import WebhookClient
from lib.scrape_hideout import (
    scrape_all_characters,
    scrape_general_information,
    scrape_combat_guide,
    scrape_bosses,
)
from lib.utils import load_json, save_json, stable_hash


STATE_PATH = "state/state.json"


def env(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"Variable manquante: {name}")
    return v


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def main():
    state = load_json(STATE_PATH, default={"messages": {}})

    webhooks = {
        "personnages": env("WEBHOOK_PERSONNAGES"),
        "combat": env("WEBHOOK_COMBAT"),
        "infos_generales": env("WEBHOOK_INFOS_GENERALES"),
        "boss_infos": env("WEBHOOK_BOSS_INFOS"),
        "guardian_golem": env("WEBHOOK_GUARDIAN_GOLEM"),
        "drake": env("WEBHOOK_DRAKE"),
        "red_demon": env("WEBHOOK_RED_DEMON"),
        "grey_demon": env("WEBHOOK_GREY_DEMON"),
        "albion": env("WEBHOOK_ALBION"),
    }

    username = os.getenv("DISCORD_USERNAME", "7DS Origin DB")
    color_hex = os.getenv("EMBED_COLOR", "#C99700")

    client = WebhookClient(username=username)

    # -------- PERSONNAGES --------
    chars = scrape_all_characters(color_hex=color_hex)
    print(f"[CHAR] roster={len(chars)}")
    for c in chars:
        key_base = f"character::{c['slug']}"
        for i, msg in enumerate(c["messages"], start=1):
            key = key_base if len(c["messages"]) == 1 else f"{key_base}::p{i}"
            client.replace_message(
                state=state["messages"],
                key=key,
                webhook_url=webhooks["personnages"],
                payload=msg["payload"],
                files=msg.get("files", {}),
            )

    # -------- INFOS GENERALES (6 sous-sections) --------
    general = scrape_general_information(color_hex=color_hex)
    print(f"[GEN] topics={len(general)}")
    for g in general:
        key = f"general::{g['slug']}"
        client.replace_message(
            state=state["messages"],
            key=key,
            webhook_url=webhooks["infos_generales"],
            payload=g["payload"],
            files=g.get("files", {}),
        )

    # -------- COMBAT GUIDE (sections) --------
    combat = scrape_combat_guide(color_hex=color_hex)
    print(f"[COMBAT] sections={len(combat)}")
    for s in combat:
        key = f"combat::{s['slug']}"
        client.replace_message(
            state=state["messages"],
            key=key,
            webhook_url=webhooks["combat"],
            payload=s["payload"],
            files=s.get("files", {}),
        )

    # -------- BOSS --------
    bosses = scrape_bosses(color_hex=color_hex)
    print(f"[BOSS] items={len(bosses)}")

    # boss infos (général)
    for item in bosses:
        slug = item["slug"]
        if slug == "information":
            webhook = webhooks["boss_infos"]
        else:
            # 1 salon par boss
            webhook = webhooks.get(slug)
            if not webhook:
                # si un boss nouveau apparaît sans salon dédié, on le route vers boss_infos
                webhook = webhooks["boss_infos"]

        key_base = f"boss::{slug}"
        for i, msg in enumerate(item["messages"], start=1):
            key = key_base if len(item["messages"]) == 1 else f"{key_base}::p{i}"
            client.replace_message(
                state=state["messages"],
                key=key,
                webhook_url=webhook,
                payload=msg["payload"],
                files=msg.get("files", {}),
            )

    state["meta"] = {"updated_at": now_iso(), "hash": stable_hash(state["messages"])}
    save_json(STATE_PATH, state)
    print("[OK] terminé")


if __name__ == "__main__":
    main()
