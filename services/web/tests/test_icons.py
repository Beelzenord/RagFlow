"""The browser-tab and home-screen icons: linked from every page, and served
before sign-in, as the sign-in page needs them too."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

try:
    from fastapi.testclient import TestClient

    HTTP_DEPS = True
except ImportError:  # pragma: no cover - depends on the environment
    HTTP_DEPS = False

APP_DIR = Path(__file__).resolve().parent.parent / "app"
PAGES = [APP_DIR / "static" / "index.html", APP_DIR / "static" / "login.html", APP_DIR / "admin" / "dashboard.html"]
ICONS = {"/favicon.ico": b"\x00\x00\x01\x00", "/apple-touch-icon.png": b"\x89PNG\r\n\x1a\n"}


class IconTests(unittest.TestCase):
    def test_every_page_links_both_icons(self) -> None:
        for page in PAGES:
            html = page.read_text(encoding="utf-8")
            with self.subTest(page=page.name):
                self.assertIn('<link rel="icon" href="/favicon.ico" />', html)
                self.assertIn('<link rel="apple-touch-icon" href="/apple-touch-icon.png" />', html)

    def test_the_files_are_what_their_names_say(self) -> None:
        for path, magic in ICONS.items():
            with self.subTest(path=path):
                self.assertTrue((APP_DIR / "static" / path.lstrip("/")).read_bytes().startswith(magic))


@unittest.skipUnless(HTTP_DEPS, "needs fastapi")
class IconRouteTests(unittest.TestCase):
    def test_icons_are_served_before_sign_in(self) -> None:
        from app import auth, main

        # Entra mode with no Easy Auth headers: an anonymous caller.
        with mock.patch.object(auth, "AUTH_MODE", "entra"), mock.patch.object(auth, "FORCED_ROLE", ""):
            with TestClient(main.app, follow_redirects=False) as client:
                self.assertEqual(client.get("/").status_code, 302)
                for path, magic in ICONS.items():
                    with self.subTest(path=path):
                        resp = client.get(path)
                        self.assertEqual(resp.status_code, 200)
                        self.assertTrue(resp.content.startswith(magic))


if __name__ == "__main__":
    unittest.main()
