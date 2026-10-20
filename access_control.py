"""Per-user camera access: Cloudflare Access identity + server-side allowlists.

When Cloudflare Access is configured (team domain + application AUD), every
browser request must carry a valid Cf-Access-Jwt-Assertion whose email maps
to either an admin or a guest allowlist in blink_access.json.

When Cloudflare is not configured, the process behaves as before: anyone
with the page CSRF token is treated as admin (local single-user mode).
"""

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
import runtime

FICHIER_ACCES = "blink_access.json"
ADMIN_DEFAUT = ""
PERMISSIONS_INVITE = ("view_live", "view_clips", "view_pictures")

_JWKS_CACHE: dict[str, Any] = {"team": "", "fetched": 0.0, "keys": {}}
_JWKS_TTL_SECONDS = 3600
_JWKS_LOCK = threading.Lock()
_ACCES_LOCK = threading.Lock()


def _chemin() -> Path:
    return runtime.app_dir() / FICHIER_ACCES


def _normaliser_email(valeur: str) -> str:
    return str(valeur or "").strip().casefold()


def _normaliser_team_domain(valeur: str) -> str:
    """Accept team host, https URL, or full JWKS URL; store bare hostname only."""
    texte = str(valeur or "").strip()
    if not texte:
        return ""
    for prefixe in ("https://", "http://"):
        if texte.lower().startswith(prefixe):
            texte = texte[len(prefixe):]
            break
    texte = texte.strip().rstrip("/")
    if "/" in texte:
        texte = texte.split("/", 1)[0]
    return texte.strip()


def _defaut() -> dict:
    return {
        "admin_emails": [ADMIN_DEFAUT] if ADMIN_DEFAUT else [],
        "cloudflare": {"team_domain": "", "aud": ""},
        "users": [],
    }


def lire_acces() -> dict:
    """Return the access config, always with the expected shape."""
    brut = _defaut()
    try:
        # utf-8-sig: Notepad / some editors write a BOM; plain utf-8 then
        # raises JSONDecodeError and we would silently fall back to empty
        # defaults (no CF, no admins) → "Connected as ." with no cameras.
        valeurs = json.loads(_chemin().read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return brut
    if not isinstance(valeurs, dict):
        return brut

    admins = valeurs.get("admin_emails")
    if isinstance(admins, list) and admins:
        brut["admin_emails"] = sorted({
            _normaliser_email(e) for e in admins if str(e).strip()
        }) or ([ADMIN_DEFAUT] if ADMIN_DEFAUT else [])
    else:
        brut["admin_emails"] = [ADMIN_DEFAUT] if ADMIN_DEFAUT else []

    cf = valeurs.get("cloudflare") if isinstance(valeurs.get("cloudflare"), dict) else {}
    brut["cloudflare"] = {
        "team_domain": _normaliser_team_domain(cf.get("team_domain") or ""),
        "aud": str(cf.get("aud") or "").strip(),
    }

    users = []
    for entree in valeurs.get("users") or []:
        if not isinstance(entree, dict):
            continue
        email = _normaliser_email(entree.get("email") or "")
        if not email:
            continue
        cameras = [
            str(c).strip() for c in (entree.get("cameras") or [])
            if str(c).strip()
        ]
        perms = [
            str(p).strip() for p in (entree.get("permissions") or PERMISSIONS_INVITE)
            if str(p).strip()
        ] or list(PERMISSIONS_INVITE)
        users.append({"email": email, "cameras": cameras, "permissions": perms})
    brut["users"] = users
    return brut


def ecrire_acces(donnees: dict) -> dict:
    """Validate and write blink_access.json; return the normalized config."""
    actuel = lire_acces()
    if not isinstance(donnees, dict):
        raise ValueError("Access config must be a JSON object.")

    admins_src = donnees.get("admin_emails", actuel["admin_emails"])
    if not isinstance(admins_src, list) or not admins_src:
        raise ValueError("At least one admin email is required.")
    admins = sorted({_normaliser_email(e) for e in admins_src if str(e).strip()})
    if not admins:
        raise ValueError("At least one admin email is required.")

    cf_src = donnees.get("cloudflare", actuel["cloudflare"])
    if not isinstance(cf_src, dict):
        raise ValueError("cloudflare must be an object.")
    cloudflare = {
        "team_domain": _normaliser_team_domain(cf_src.get("team_domain") or ""),
        "aud": str(cf_src.get("aud") or "").strip(),
    }

    users = []
    for entree in donnees.get("users") or []:
        if not isinstance(entree, dict):
            continue
        email = _normaliser_email(entree.get("email") or "")
        if not email or email in admins:
            continue
        cameras = [
            str(c).strip() for c in (entree.get("cameras") or [])
            if str(c).strip()
        ]
        perms = [
            str(p).strip() for p in (entree.get("permissions") or PERMISSIONS_INVITE)
            if str(p).strip() in PERMISSIONS_INVITE
        ] or list(PERMISSIONS_INVITE)
        users.append({"email": email, "cameras": cameras, "permissions": perms})

    normalise = {
        "admin_emails": admins,
        "cloudflare": cloudflare,
        "users": users,
    }
    from nico579_commons import atomique

    cible = _chemin()
    with _ACCES_LOCK:
        temporaire = atomique.chemin_part(cible)
        try:
            temporaire.write_text(
                json.dumps(normalise, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8")
            atomique.remplacer(temporaire, cible)
        finally:
            temporaire.unlink(missing_ok=True)
    return normalise


def cloudflare_actif(config: dict | None = None) -> bool:
    cfg = config or lire_acces()
    cf = cfg.get("cloudflare") or {}
    return bool(cf.get("team_domain") and cf.get("aud"))


def est_admin(email: str, config: dict | None = None) -> bool:
    cfg = config or lire_acces()
    normalise = _normaliser_email(email)
    if not normalise:
        return False
    # Built-in owner address always admin (avoids lockout if the list was edited).
    if normalise == _normaliser_email(ADMIN_DEFAUT):
        return True
    return normalise in {
        _normaliser_email(e) for e in cfg.get("admin_emails") or []
    }


def utilisateur(email: str, config: dict | None = None) -> dict | None:
    cfg = config or lire_acces()
    cible = _normaliser_email(email)
    for entree in cfg.get("users") or []:
        if entree.get("email") == cible:
            return entree
    return None


def cameras_autorisees(email: str, config: dict | None = None) -> set[str] | None:
    """None = all cameras (admin). Empty set = no cameras. Otherwise allowlist."""
    cfg = config or lire_acces()
    if est_admin(email, cfg):
        return None
    entree = utilisateur(email, cfg)
    if not entree:
        return set()
    return {str(c).strip() for c in entree.get("cameras") or [] if str(c).strip()}


def _jetons_camera(*valeurs: str | None) -> set[str]:
    jetons: set[str] = set()
    for valeur in valeurs:
        if not valeur:
            continue
        texte = str(valeur).strip()
        if not texte:
            continue
        jetons.add(texte)
        jetons.add(texte.casefold())
    return jetons


def camera_autorisee(email: str, *identifiants: str | None,
                     config: dict | None = None) -> bool:
    autorisees = cameras_autorisees(email, config)
    if autorisees is None:
        return True
    if not autorisees:
        return False
    cibles = _jetons_camera(*identifiants)
    if not cibles:
        return False
    referentiel = _jetons_camera(*autorisees)
    return bool(cibles & referentiel)


def profil(email: str, config: dict | None = None) -> dict:
    """Map a Cloudflare-authenticated email to admin/guest.

    Anyone who passed a Cloudflare Access *policy* (valid JWT email) is an
    authenticated guest even before you add them in blink2video — they simply
    see no cameras until you assign some. CF Access chooses who can log in;
    blink_access.json chooses which cameras each guest may view.
    """
    cfg = config or lire_acces()
    normalise = _normaliser_email(email)
    if est_admin(normalise, cfg):
        return {
            "email": normalise,
            "role": "admin",
            "cameras": None,
            "permissions": ["*"],
            "cloudflare_required": cloudflare_actif(cfg),
            "pending_allowlist": False,
        }
    entree = utilisateur(normalise, cfg)
    cameras = list(entree["cameras"]) if entree else []
    perms = list(entree["permissions"]) if entree else list(PERMISSIONS_INVITE)
    return {
        "email": normalise,
        "role": "guest",
        "cameras": cameras,
        "permissions": perms,
        "cloudflare_required": cloudflare_actif(cfg),
        # True when Access let them in but no camera row exists yet.
        "pending_allowlist": entree is None or not cameras,
    }


def filtrer_system_state(state: dict, email: str, config: dict | None = None) -> dict:
    autorisees = cameras_autorisees(email, config)
    if autorisees is None:
        return state
    systemes = []
    for systeme in state.get("systems") or []:
        cameras = [
            cam for cam in (systeme.get("cameras") or [])
            if camera_autorisee(
                email, cam.get("key"), cam.get("name"), config=config)
        ]
        if not cameras:
            continue
        copie = dict(systeme)
        copie["cameras"] = cameras
        systemes.append(copie)
    filtre = dict(state)
    filtre["systems"] = systemes
    return filtre


def filtrer_clips(data: dict, email: str, config: dict | None = None) -> dict:
    autorisees = cameras_autorisees(email, config)
    if autorisees is None:
        return data
    clips = [
        clip for clip in (data.get("clips") or [])
        if camera_autorisee(
            email, clip.get("camera"), clip.get("cameraKey"), config=config)
    ]
    filtre = dict(data)
    filtre["clips"] = clips
    return filtre


def filtrer_videos(data: dict, email: str, config: dict | None = None) -> dict:
    autorisees = cameras_autorisees(email, config)
    if autorisees is None:
        return data
    filtre = {}
    for kind, items in (data or {}).items():
        filtre[kind] = [
            item for item in (items or [])
            if camera_autorisee(email, item.get("camera"), config=config)
        ]
    return filtre


def filtrer_snapshots(snapshots: list, email: str,
                      config: dict | None = None) -> list:
    autorisees = cameras_autorisees(email, config)
    if autorisees is None:
        return snapshots
    return [
        s for s in snapshots
        if camera_autorisee(email, s.get("camera"), config=config)
    ]


def camera_depuis_media(route: str) -> str | None:
    """Extract camera folder/name from media route kinds used by resolve_media."""
    kind, _, relative = route.partition("/")
    if not relative:
        return None
    if kind == "clip":
        # identity is a file name; caller should resolve via registry when needed
        return None
    return relative.split("/", 1)[0] or None


# --- Cloudflare Access JWT (RS256) ------------------------------------------

def _b64url_decode(texte: str) -> bytes:
    pad = "=" * (-len(texte) % 4)
    return base64.urlsafe_b64decode(texte + pad)


def _jwks_url(team_domain: str) -> str:
    domaine = _normaliser_team_domain(team_domain)
    if not domaine:
        raise ValueError("Cloudflare team domain is empty.")
    return f"https://{domaine}/cdn-cgi/access/certs"


def _charger_jwks(team_domain: str) -> dict[str, Any]:
    maintenant = time.monotonic()
    with _JWKS_LOCK:
        if (_JWKS_CACHE["team"] == team_domain
                and maintenant - _JWKS_CACHE["fetched"] < _JWKS_TTL_SECONDS
                and _JWKS_CACHE["keys"]):
            return dict(_JWKS_CACHE["keys"])
    try:
        with urllib.request.urlopen(_jwks_url(team_domain), timeout=10) as reponse:
            corps = json.loads(reponse.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as erreur:
        raise ValueError(f"Could not fetch Cloudflare Access JWKS: {erreur}") from erreur
    cles = {}
    for entree in corps.get("keys") or []:
        kid = entree.get("kid")
        if kid and entree.get("kty") == "RSA":
            cles[kid] = entree
    with _JWKS_LOCK:
        _JWKS_CACHE["team"] = team_domain
        _JWKS_CACHE["fetched"] = maintenant
        _JWKS_CACHE["keys"] = cles
    return cles


def _cle_rsa_depuis_jwk(jwk: dict) -> Any:
    n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
    e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
    return RSAPublicNumbers(e, n).public_key()


def verifier_jwt_cloudflare(jeton: str, team_domain: str, aud: str) -> dict:
    """Verify a Cloudflare Access JWT and return its claims (must include email)."""
    parties = jeton.split(".")
    if len(parties) != 3:
        raise ValueError("Malformed Access JWT.")
    try:
        en_tete = json.loads(_b64url_decode(parties[0]))
        charges = json.loads(_b64url_decode(parties[1]))
    except (json.JSONDecodeError, ValueError) as erreur:
        raise ValueError("Unreadable Access JWT.") from erreur
    if en_tete.get("alg") != "RS256":
        raise ValueError("Unsupported Access JWT algorithm.")
    kid = en_tete.get("kid")
    if not kid:
        raise ValueError("Access JWT missing key id.")
    cles = _charger_jwks(team_domain)
    jwk = cles.get(kid)
    if not jwk:
        # Force refresh once in case of key rotation.
        with _JWKS_LOCK:
            _JWKS_CACHE["fetched"] = 0.0
        cles = _charger_jwks(team_domain)
        jwk = cles.get(kid)
    if not jwk:
        raise ValueError("Unknown Access JWT signing key.")

    message = f"{parties[0]}.{parties[1]}".encode("ascii")
    signature = _b64url_decode(parties[2])
    publique = _cle_rsa_depuis_jwk(jwk)
    try:
        publique.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
    except Exception as erreur:  # InvalidSignature and friends
        raise ValueError("Invalid Access JWT signature.") from erreur

    maintenant = int(time.time())
    if int(charges.get("exp") or 0) < maintenant:
        raise ValueError("Access JWT expired.")
    if int(charges.get("nbf") or 0) > maintenant + 60:
        raise ValueError("Access JWT not yet valid.")

    audience = charges.get("aud")
    attendus = {aud}
    if isinstance(audience, list):
        if not attendus.intersection(str(a) for a in audience):
            raise ValueError("Access JWT audience mismatch.")
    elif str(audience) != aud:
        raise ValueError("Access JWT audience mismatch.")

    email = _email_depuis_claims(charges)
    if not email:
        raise ValueError("Access JWT has no email claim.")
    # Stash normalized email so callers can use charges["email"] consistently.
    charges = dict(charges)
    charges["email"] = email
    return charges


def _email_depuis_claims(charges: dict) -> str:
    """Cloudflare Access may put the address in email, identity, or IdP fields."""
    for cle in ("email", "identity", "preferred_username", "upn", "unique_name"):
        valeur = charges.get(cle)
        if valeur and "@" in str(valeur):
            return _normaliser_email(valeur)
    return ""


def _jeton_depuis_cookie(headers) -> str:
    """Cloudflare Access stores the JWT in the CF_Authorization cookie after
    a successful Access policy login; some tunnels only forward that cookie.
    """
    brut = headers.get("Cookie") or ""
    for morceau in brut.split(";"):
        morceau = morceau.strip()
        if morceau.lower().startswith("cf_authorization="):
            return morceau.split("=", 1)[1].strip()
    return ""


def _via_proxy(headers) -> bool:
    """True when a reverse proxy / Tunnel forwarded this request to the origin.

    cloudflared dials 127.0.0.1 and may omit Cf-Ray in some setups, but it
    still sets X-Forwarded-* / Cf-Connecting-IP. A real browser hit on
    http://127.0.0.1 has none of these.
    """
    for cle in (
        "Cf-Ray",
        "CF-Ray",
        "Cf-Connecting-IP",
        "CF-Connecting-IP",
        "Cf-Visitor",
        "CF-Visitor",
        "CDN-Loop",
        "X-Forwarded-For",
        "X-Forwarded-Proto",
        "X-Real-IP",
    ):
        if headers.get(cle):
            return True
    return False


def _hote_loopback(headers) -> bool:
    hote = (headers.get("Host") or "").split(":")[0].strip().lower()
    return hote in ("127.0.0.1", "localhost", "::1")


def _bypass_admin_local(headers) -> bool:
    """True only for a real local browser on 127.0.0.1 / localhost (no proxy)."""
    if _via_proxy(headers):
        return False
    return _hote_loopback(headers)


def _assertion_jwt(headers) -> str:
    """Cf-Access-Jwt-Assertion only (never the cookie, never spoofable email)."""
    for cle in ("Cf-Access-Jwt-Assertion", "Cf-Access-Jwt-Assertion".lower()):
        jeton = (headers.get(cle) or "").strip()
        if jeton:
            return jeton
    return ""


def _jeton_acces(headers) -> str:
    """Access JWT for this request: assertion header first, cookie fallback.

    On Tunnel / reverse-proxy traffic the assertion header is the current
    Access login. The CF_Authorization cookie is used only when the header
    is absent (some setups forward cookie alone). Never trust
    Cf-Access-Authenticated-User-Email — it is spoofable without signature.
    """
    jeton = _assertion_jwt(headers)
    if jeton:
        return jeton
    return _jeton_depuis_cookie(headers)


def jeton_acces_requete(headers) -> str:
    """Public: Access JWT for this request (assertion header, else cookie)."""
    return _jeton_acces(headers)


def email_depuis_en_tetes(headers, config: dict | None = None) -> str | None:
    """Resolve the caller email from a verified Cloudflare Access JWT.

    Raises ValueError when Cloudflare is required but the JWT is missing/invalid.
    The spoofable Cf-Access-Authenticated-User-Email header is never trusted alone.
    """
    cfg = config or lire_acces()
    if not cloudflare_actif(cfg):
        return None
    cf = cfg["cloudflare"]
    jeton = _jeton_acces(headers)
    if not jeton:
        raise ValueError(
            "Cloudflare Access login required. Open this site through your "
            "Access application hostname and complete the Access policy.")
    charges = verifier_jwt_cloudflare(jeton, cf["team_domain"], cf["aud"])
    return _normaliser_email(charges.get("email") or "")


def resoudre_identite(headers, config: dict | None = None) -> dict:
    """Return a profil dict for this request.

    Priority when Cloudflare Access is configured:
    1. Real local browser (loopback Host, no Tunnel/proxy headers) → admin.
       Ignore any leftover CF_Authorization cookie from a friend's session —
       that cookie made the admin appear as the friend.
    2. Proxied / public request with a valid Access JWT (header, else cookie)
       → that JWT's email (admin or guest). Camera allowlist comes from
       Settings → Shared camera access.
    3. Proxied / public request without JWT → unauthenticated.

    Tunnel often rewrites Host to 127.0.0.1 but still sets X-Forwarded-* /
    Cf-Ray: those requests are never treated as local admin.
    """
    cfg = config or lire_acces()
    if not cloudflare_actif(cfg):
        admin = (cfg.get("admin_emails") or [ADMIN_DEFAUT])[0]
        return profil(admin, cfg)

    # Local http://127.0.0.1 — never trust Access cookies left over from
    # testing a friend's login in the same browser.
    if _bypass_admin_local(headers):
        admin = (cfg.get("admin_emails") or [ADMIN_DEFAUT])[0]
        return profil(admin, cfg)

    jeton = _jeton_acces(headers)
    if jeton:
        try:
            cf = cfg["cloudflare"]
            charges = verifier_jwt_cloudflare(
                jeton, cf["team_domain"], cf["aud"])
            email = _normaliser_email(charges.get("email") or "")
            if not email:
                raise ValueError("Access JWT has no email claim.")
            return profil(email, cfg)
        except ValueError as erreur:
            return {
                "email": "",
                "role": "unauthenticated",
                "cameras": [],
                "permissions": [],
                "cloudflare_required": True,
                "pending_allowlist": False,
                "error": str(erreur),
            }

    return {
        "email": "",
        "role": "unauthenticated",
        "cameras": [],
        "permissions": [],
        "cloudflare_required": True,
        "pending_allowlist": False,
        "error": (
            "Cloudflare Access login required. Open this site through your "
            "Access application hostname and complete the Access policy."),
    }
