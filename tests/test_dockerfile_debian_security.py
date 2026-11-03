"""HF-300: Agent runtime must upgrade Debian packages with critical fixes."""
from pathlib import Path
import unittest

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"
REQUIRED = (
    "libglib2.0-0t64",
    "libmbedcrypto16",
    "perl",
    "perl-base",
    "libperl5.40",
    "perl-modules-5.40",
    "linux-libc-dev",
)


class DockerfileDebianSecurityTests(unittest.TestCase):
    def test_runtime_image_upgrades_critical_debian_packages(self) -> None:
        text = DOCKERFILE.read_text()
        self.assertIn("apt-get -o Acquire::Retries=3 install -y --no-install-recommends --only-upgrade", text)
        for package in REQUIRED:
            self.assertIn(package, text)

    def test_runtime_image_fails_closed_on_vulnerable_linux_libc_dev(self) -> None:
        # CVE-2026-99083 / DSA-6602-1: a cached or stale apt layer must not
        # bake linux-libc-dev below the trixie-security fix.
        text = DOCKERFILE.read_text()
        self.assertIn(
            "dpkg --compare-versions \"$(dpkg-query -W -f='${Version}' linux-libc-dev)\" ge 6.12.115-2",
            text,
        )


if __name__ == "__main__":
    unittest.main()
