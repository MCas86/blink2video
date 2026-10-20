"""Access identity: JWT wins; local admin only without proxy/JWT."""

from __future__ import annotations

import unittest
from unittest import mock

import access_control


class _H(dict):
    """Minimal case-insensitive header map like BaseHTTPRequestHandler.headers."""

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


class TestAccessControlBypassLocal(unittest.TestCase):
    def test_tunnel_host_localhost_sans_jwt_n_est_pas_admin(self):
        headers = _H({
            "Host": "127.0.0.1:8765",
            "Cf-Ray": "abcdef123-EWR",
            "Cf-Connecting-IP": "203.0.113.10",
        })
        profil = access_control.resoudre_identite(headers, CFG)
        self.assertEqual(profil["role"], "unauthenticated")
        self.assertEqual(profil.get("email"), "")

    def test_tunnel_x_forwarded_sans_jwt_n_est_pas_admin(self):
        """Some tunnels omit Cf-Ray but still set X-Forwarded-For."""
        headers = _H({
            "Host": "127.0.0.1:8765",
            "X-Forwarded-For": "203.0.113.10",
            "X-Forwarded-Proto": "https",
        })
        profil = access_control.resoudre_identite(headers, CFG)
        self.assertEqual(profil["role"], "unauthenticated")

    def test_tunnel_jwt_ami_est_invite_meme_si_host_local(self):
        """Friend Access JWT must win even when origin Host is 127.0.0.1."""
        headers = _H({
            "Host": "127.0.0.1:8765",
            "X-Forwarded-For": "203.0.113.10",
            "Cf-Access-Jwt-Assertion": "fake.jwt.token",
        })
        with mock.patch.object(
            access_control,
            "verifier_jwt_cloudflare",
            return_value={"email": "friend@example.com"},
        ):
            profil = access_control.resoudre_identite(headers, CFG)
        self.assertEqual(profil["role"], "guest")
        self.assertEqual(profil["email"], "friend@example.com")
        self.assertIn("Yard", profil.get("cameras") or [])

    def test_tunnel_jwt_admin_est_admin(self):
        headers = _H({
            "Host": "cams.example.com",
            "Cf-Ray": "abc",
            "Cf-Access-Jwt-Assertion": "fake.jwt.token",
        })
        with mock.patch.object(
            access_control,
            "verifier_jwt_cloudflare",
            return_value={"email": "admin@example.com"},
        ):
            profil = access_control.resoudre_identite(headers, CFG)
        self.assertEqual(profil["role"], "admin")
        self.assertEqual(profil["email"], "admin@example.com")

    def test_navigateur_local_sans_cf_reste_admin(self):
        headers = _H({"Host": "127.0.0.1:8765"})
        profil = access_control.resoudre_identite(headers, CFG)
        self.assertEqual(profil["role"], "admin")
        self.assertEqual(profil["email"], "admin@example.com")

    def test_navigateur_local_cookie_invalide_reste_admin(self):
        headers = _H({
            "Host": "127.0.0.1:8765",
            "Cookie": "CF_Authorization=eyJhbGciOiJSUzI1NiJ9.e30.fake",
        })
        profil = access_control.resoudre_identite(headers, CFG)
        self.assertEqual(profil["role"], "admin")
        self.assertEqual(profil["email"], "admin@example.com")

    def test_navigateur_local_cookie_ami_valide_reste_admin(self):
        """Leftover friend Access cookie on localhost must not override admin."""
        headers = _H({
            "Host": "127.0.0.1:8765",
            "Cookie": "CF_Authorization=eyJhbGciOiJSUzI1NiJ9.e30.friend",
            "Cf-Access-Jwt-Assertion": "fake.jwt.token",
        })
        with mock.patch.object(
            access_control,
            "verifier_jwt_cloudflare",
            return_value={"email": "friend@example.com"},
        ):
            profil = access_control.resoudre_identite(headers, CFG)
        self.assertEqual(profil["role"], "admin")
        self.assertEqual(profil["email"], "admin@example.com")

    def test_tunnel_header_jwt_gagne_sur_cookie(self):
        """Assertion header is the current Access login; ignore a stale cookie."""
        headers = _H({
            "Host": "cams.example.com",
            "Cf-Ray": "abc",
            "Cf-Access-Jwt-Assertion": "header.jwt.token",
            "Cookie": "CF_Authorization=stale.friend.cookie",
        })

        def _verif(jeton, team_domain, aud):
            if jeton == "header.jwt.token":
                return {"email": "admin@example.com"}
            return {"email": "friend@example.com"}

        with mock.patch.object(
                access_control, "verifier_jwt_cloudflare", side_effect=_verif):
            profil = access_control.resoudre_identite(headers, CFG)
        self.assertEqual(profil["role"], "admin")
        self.assertEqual(profil["email"], "admin@example.com")
        self.assertEqual(
            access_control.jeton_acces_requete(headers), "header.jwt.token")
        self.assertEqual(
            access_control._assertion_jwt(headers), "header.jwt.token")

    def test_hote_public_sans_jwt_non_authentifie(self):
        headers = _H({"Host": "cams.example.com"})
        profil = access_control.resoudre_identite(headers, CFG)
        self.assertEqual(profil["role"], "unauthenticated")


if __name__ == "__main__":
    unittest.main()
