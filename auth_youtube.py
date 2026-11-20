"""Einmalig auf dem Laptop (Windows/Mac/Linux mit Browser) ausführen, um ein YouTube-Token zu erzeugen.

Voraussetzungen:
  1. Google Cloud Console: Projekt anlegen, "YouTube Data API v3" aktivieren.
  2. OAuth-Zustimmungsbildschirm: Typ "Extern", Status auf "In Produktion" setzen
     (im Status "Testing" verfällt das Refresh-Token nach 7 Tagen).
  3. Anmeldedaten → OAuth-Client-ID → Anwendungstyp "Desktop-App" → JSON herunterladen
     und als client_secret.json neben dieses Skript legen.

Ausführen (PowerShell):
  py -m pip install google-auth-oauthlib
  py auth_youtube.py --channel de

Im Browser mit dem Google-Konto anmelden und den richtigen YouTube-Kanal (ggf. Brand-Account)
auswählen. Ergebnis: token_de.json – diese Datei wie ein Passwort behandeln.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def main() -> int:
    p = argparse.ArgumentParser(description="YouTube-OAuth-Token für die Shorts-Pipeline erzeugen")
    p.add_argument("--channel", default="de", help="Kanal-Key aus channels.yaml (Dateiname token_<key>.json)")
    p.add_argument("--client-secret", default="client_secret.json", type=Path)
    args = p.parse_args()

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Bitte zuerst installieren:  py -m pip install google-auth-oauthlib")
        return 1
    if not args.client_secret.exists():
        print(f"{args.client_secret} nicht gefunden (OAuth-Client vom Typ 'Desktop-App' herunterladen).")
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(str(args.client_secret), SCOPES)
    # prompt=consent erzwingt ein refresh_token, auch wenn schon einmal zugestimmt wurde
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline",
                                  authorization_prompt_message="Browser öffnet sich: {url}",
                                  success_message="Fertig – du kannst dieses Fenster schließen.")
    if not creds.refresh_token:
        print("Kein refresh_token erhalten. App-Zugriff unter myaccount.google.com/permissions "
              "entfernen und erneut ausführen.")
        return 1

    out = Path(f"token_{args.channel}.json")
    out.write_text(creds.to_json(), encoding="utf-8")
    scopes = json.loads(out.read_text(encoding="utf-8")).get("scopes")
    print(f"Gespeichert: {out.resolve()}  (Scopes: {scopes})")
    print("Jetzt per scp auf den Server nach ~/shorts-pipeline/credentials/ kopieren (siehe README).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
