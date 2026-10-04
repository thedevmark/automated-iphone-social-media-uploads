"""Keep SECURITY.md honest: shipped code talks only to this computer."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
URL = re.compile(r"(?:https?:)?//([A-Za-z0-9.\-{}\[\]]+)")
LOOPBACK = {"127.0.0.1", "localhost", "{host}"}
# Documented in SECURITY.md: a clicked link to this repository, never requested by the app.
CLICKED_LINKS = {("web/index.html", "github.com")}


def shipped_files():
    yield ROOT / "launch_video_drop.py"
    for folder, patterns in (("video_drop", ("*.py",)), ("scripts", ("*.py",)), ("web", ("*.html", "*.js", "*.css"))):
        for pattern in patterns:
            yield from sorted((ROOT / folder).rglob(pattern))


class SecurityClaimTests(unittest.TestCase):
    def test_shipped_code_names_only_loopback_hosts(self):
        found = []
        for path in shipped_files():
            for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                for host in URL.findall(line):
                    if "." in host or host in {"localhost", "{host}"}:
                        if host not in LOOPBACK and (path.relative_to(ROOT).as_posix(), host) not in CLICKED_LINKS:
                            found.append(f"{path.relative_to(ROOT)}:{line_no}: {host}")
        self.assertEqual(found, [], "update SECURITY.md before adding a non-local address")

    def test_server_binds_loopback_only(self):
        source = (ROOT / "video_drop" / "server.py").read_text(encoding="utf-8")
        self.assertIn('ThreadingHTTPServer(("127.0.0.1", args.port), Handler)', source)

    def test_requirements_are_pinned(self):
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#"):
                self.assertRegex(line, r"^[A-Za-z0-9_.\-]+==[0-9]", line)


if __name__ == "__main__":
    unittest.main()
