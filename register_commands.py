#!/usr/bin/env python3
"""Enregistrement des commandes slash — utilisable en local (facultatif).

Tu n'as PAS besoin de ce script : la page `/setup.html` fait la même chose
depuis le navigateur. Il reste utile pour déboguer sans passer par Vercel.

    python register_commands.py            # serveur de test si DISCORD_GUILD_ID
    python register_commands.py --global   # force l'enregistrement global
    python register_commands.py --lister   # affiche les commandes existantes
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import config, discord_api  # noqa: E402


def main() -> int:
    parseur = argparse.ArgumentParser(description="Commandes slash Friedrich")
    parseur.add_argument("--global", dest="globales", action="store_true",
                         help="enregistrement global (ignore DISCORD_GUILD_ID)")
    parseur.add_argument("--lister", action="store_true",
                         help="liste les commandes déjà enregistrées")
    arguments = parseur.parse_args()

    try:
        application_id = config.require("DISCORD_APPLICATION_ID")
        jeton = config.require("DISCORD_TOKEN")
    except RuntimeError as exc:
        print(f"❌ {exc}")
        return 1

    try:
        identite = discord_api.bot_identity(jeton)
    except discord_api.DiscordError as exc:
        print(f"❌ Jeton refusé par Discord : {exc}")
        return 1
    print(f"🤖 Bot : {identite.get('nom')} ({identite.get('id')})")

    guild = None if arguments.globales else config.DISCORD_GUILD_ID()

    if arguments.lister:
        url = f"{config.DISCORD_API}/applications/{application_id}/commands"
        if guild:
            url = (f"{config.DISCORD_API}/applications/{application_id}"
                   f"/guilds/{guild}/commands")
        existantes = discord_api._request("GET", url, token=jeton)
        print(json.dumps(existantes, ensure_ascii=False, indent=2)[:4000])
        return 0

    resultat = discord_api.register_commands(application_id, jeton, guild)
    print(f"✅ {resultat['nombre']} commandes enregistrées — portée : {resultat['portee']}")
    for nom in resultat["commandes"]:
        print(f"   • /{nom}")
    if not guild:
        print("ℹ️  Enregistrement global : Discord peut mettre jusqu'à 1 h à propager.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
