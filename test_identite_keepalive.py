"""Keep-alive + Tunnel must not leak Access identity across users on one Handler."""

from __future__ import annotations

import unittest
from unittest import mock

import access_control


class _H(dict):
    def get(self, key, default=None):
        cible = str(key).casefold()
        for k, v in self.items():
            if str(k).casefold() == cible:
                return v
        return default


CFG = {
    "admin_emails": ["admin@example.com"],
    "cloudflare": {
        "team_domain": "example.cloudflareaccess.com",
        "aud": "test-aud",
    },
    "users": [
        {
            "email": "friend@example.com",
            "cameras": ["Yard"],
            "permissions": list(access_control.PERMISSIONS_INVITE),
        }
    ],
}


class _FakeHandler:
    """Mirrors Handler.identite_acces + handle_one_request clear semantics."""

    def __init__(self):
        self.headers = _H()
        self._identite_acces_cache = None
        self._identite_acces_cle = None

    def handle_one_request_begin(self) -> None:
        self._identite_acces_cache = None
        self._identite_acces_cle = None

    def identite_acces(self) -> dict:
        jeton = access_control.jeton_acces_requete(self.headers)
        cle = jeton or f"local:{(self.headers.get('Host') or '').strip().lower()}"
        if (self._identite_acces_cache is not None
                and self._identite_acces_cle == cle):
            return self._identite_acces_cache
        profil = access_control.resoudre_identite(self.headers, CFG)
        self._identite_acces_cache = profil
        self._identite_acces_cle = cle
        return profil


class TestIdentiteKeepalive(unittest.TestCase):
    def test_deux_utilisateurs_sur_meme_handler_keepalive(self):
        """Simulate Tunnel keep-alive: friend then admin on one Handler instance."""
        h = _FakeHandler()

        def _verif(jeton, team_domain, aud):
            if jeton == "jwt-friend":
                return {"email": "friend@example.com"}
            if jeton == "jwt-admin":
                return {"email": "admin@example.com"}
            raise ValueError("bad jwt")

        with mock.patch.object(
                access_control, "verifier_jwt_cloudflare", side_effect=_verif):
            h.handle_one_request_begin()
            h.headers = _H({
                "Host": "127.0.0.1:8765",
                "X-Forwarded-For": "203.0.113.10",
                "Cf-Access-Jwt-Assertion": "jwt-friend",
            })
            ami = h.identite_acces()
            self.assertEqual(ami["email"], "friend@example.com")
            self.assertEqual(ami["role"], "guest")

            h.handle_one_request_begin()
            h.headers = _H({
                "Host": "127.0.0.1:8765",
                "X-Forwarded-For": "203.0.113.11",
                "Cf-Access-Jwt-Assertion": "jwt-admin",
            })
            admin = h.identite_acces()
            self.assertEqual(admin["email"], "admin@example.com")
            self.assertEqual(admin["role"], "admin")

    def test_changement_jwt_sans_clear_ne_recycle_pas_ancien_profil(self):
        """Even if clear is skipped, a different JWT must not reuse the cache."""
        h = _FakeHandler()

        def _verif(jeton, team_domain, aud):
            return {"email": (
                "friend@example.com" if jeton == "jwt-friend"
                else "admin@example.com"
            )}

        with mock.patch.object(
                access_control, "verifier_jwt_cloudflare", side_effect=_verif):
            h.headers = _H({
                "Host": "cams.example.com",
                "Cf-Ray": "1",
                "Cf-Access-Jwt-Assertion": "jwt-friend",
            })
            self.assertEqual(h.identite_acces()["email"], "friend@example.com")

            h.headers = _H({
                "Host": "cams.example.com",
                "Cf-Ray": "2",
                "Cf-Access-Jwt-Assertion": "jwt-admin",
            })
            self.assertEqual(h.identite_acces()["email"], "admin@example.com")


if __name__ == "__main__":
    unittest.main()
