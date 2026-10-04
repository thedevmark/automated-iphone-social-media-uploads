import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from video_drop import server


class StaticFileTests(unittest.TestCase):
    def setUp(self):
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.http.server_port}"

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)

    def test_brand_files_are_served_with_their_types(self):
        for path, (name, content_type) in server.STATIC_FILES.items():
            with self.subTest(path=path), urllib.request.urlopen(self.base + path, timeout=5) as response:
                self.assertEqual(response.headers["Content-Type"], content_type)
                self.assertEqual(response.read(), (server.ROOT / "web" / name).read_bytes())

    def test_only_listed_files_are_served(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + "/fonts/OFL.txt", timeout=5)
        self.assertEqual(caught.exception.code, 404)

    def test_page_loads_the_bundled_font_offline(self):
        page = (server.ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("url(/fonts/inter-variable.woff2)", page)
        self.assertNotIn("fonts.googleapis.com", page)
        # Local-first: no script, style or font comes from the network. A link the user clicks
        # (Settings > Setup's GitHub link) loads nothing until clicked, so it is allowed.
        self.assertNotRegex(page, r"src=[\"']https?://")
        self.assertNotRegex(page, r"<link[^>]+href=[\"']https?://")

    def test_first_run_flow_and_theme_toggle_are_in_the_page(self):
        page = (server.ROOT / "web" / "index.html").read_text(encoding="utf-8")
        # The guided flow: progress, one card, the full checklist stays in Settings with a way back to the guide.
        for marker in ('id="onboarding"', 'id="obProgress"', 'id="obCard"', 'id="setupGuide"', "Show me how",
                       "already done", "Extras"):
            self.assertIn(marker, page)
        self.assertNotIn("Recommended (", page)
        # Theme: Pathos's stoplight toggle, 1:1 (three borderless icon buttons, aria-pressed, titles), applied
        # before paint from localStorage and following the OS in Auto.
        self.assertIn('class="pt-theme" role="group" aria-label="Theme"', page)
        for title in ("Auto theme", "Light theme", "Dark theme"):
            self.assertIn(f'title="{title}"', page)
        self.assertEqual(page.count('class="pt-theme-opt"'), 3)
        self.assertIn('stroke-width="1.9"', page)
        self.assertIn("opacity:.38", page)
        self.assertIn('localStorage.getItem("video-drop-theme")', page.split("<style>")[0])
        self.assertIn(":root[data-theme=light]{", page)
        self.assertIn("@media(prefers-color-scheme:light){:root:not([data-theme=dark]){", page)
        self.assertIn("prefers-color-scheme: dark", page)
        # Every colour flows through tokens: no raw hex outside the two token blocks and the console block.
        style = page.split("<style>")[1].split("</style>")[0]
        body = style.split("@media(prefers-color-scheme:light)")[1].split("}}", 1)[1]
        stray = [hexv for hexv in __import__("re").findall(r"#[0-9a-fA-F]{3,8}\b", body)
                 if hexv not in {"#0d0d0f", "#ebeae7", "#2c2c31", "#8b8a90"}]  # the always-dark console
        self.assertEqual(stray, [])
        # The Settings switch the lead is building stays.
        self.assertIn('data-check="removeAfterPost"', page)


if __name__ == "__main__":
    unittest.main()
