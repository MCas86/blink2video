"""Entry point: verb dispatch only.

Since step 8 of the remediation plan (AUDIT-2026-08-13.md, section 20),
business logic lives in five separate modules:

- blink_auth.py     : Blink session, credentials, 2FA (8.1) ;
- blink_models.py   : USB/cloud clips, identity, paths (8.2) ;
- blink_registre.py : incremental registry, correlation, repair (8.3) ;
- blink_engine.py   : one download pass, looping or once (8.4) ;
- blink_cli.py       : grammar, delegated verbs, supervision (8.5).

This file only routes (8.6). `runtime.bootstrap()` is no longer
called here: blink_cli.py does it, just before importing modules that
actually need aiohttp or blinkpy (main, the "start" branch of
executer, accueillir) — never for --help, --version, stop, open or update
(O-06/8.7/8.8)."""

import sys

# asyncio/datetime/time are unused here: they are shared stdlib modules
# (Python caches them once), kept imported so that tests which patch
# `blink2video.time`/`.asyncio`/`.dt` still reach the same object as
# blink_engine.py/blink_cli.py, without knowing those internal modules.
import asyncio  # noqa: F401
import datetime as dt  # noqa: F401
import time  # noqa: F401

import runtime

if __name__ == "__main__" and sys.argv[1:2] == ["--self-test-version"]:
    # The shared updater (nico579_commons.maj_install) launches the downloaded
    # version this way, from its staging folder, before installing it: 0 if it
    # recognizes itself, 1 otherwise. Right at the top, before preparer_etat():
    # this launch must not touch any state or output folders.
    attendue = (sys.argv[2] if len(sys.argv) > 2 else "").strip().lstrip("vV")
    raise SystemExit(0 if attendue == runtime.VERSION else 1)

if __name__ == "__main__":
    # Before blink_cli: its modules pull constants from the state folder
    # and the outputs folder as soon as they are imported. Once, migrates
    # state from a version ≤ 0.13. At launch only: a test that imports this
    # module must never touch the real state folder.
    runtime.preparer_etat()

from blink_cli import msg, route  # noqa: E402


if __name__ == "__main__":
    try:
        raise SystemExit(route(sys.argv[1:]))
    except ValueError as erreur:
        print(f"{erreur}. " + msg("liste_verbes", liste=", ".join(runtime.VERBES)))
        raise SystemExit(2)
    except (KeyboardInterrupt, EOFError):
        print(msg("connexion_annulee"))
        raise SystemExit(130)
