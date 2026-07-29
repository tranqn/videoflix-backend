"""Tests for the superuser guard in the production entrypoint.

The full script cannot run here (it waits for PostgreSQL and execs gunicorn),
so the guard block is delimited by markers and executed on its own with /bin/sh
— the same interpreter the container uses.
"""

import subprocess
from pathlib import Path
from unittest import TestCase

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROD_ENTRYPOINT = BACKEND_DIR / 'backend.entrypoint.prod.sh'
DEV_ENTRYPOINT = BACKEND_DIR / 'backend.entrypoint.sh'
START_MARKER = '# --- superuser-guard-start ---'
END_MARKER = '# --- superuser-guard-end ---'


def extract_guard():
    """Return the shell snippet between the guard markers."""
    script = PROD_ENTRYPOINT.read_text()
    start = script.index(START_MARKER) + len(START_MARKER)
    return script[start : script.index(END_MARKER)]


def run_guard(env):
    """Run the guard snippet with the given environment."""
    return subprocess.run(
        ['/bin/sh', '-c', extract_guard()],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class ProdSuperuserGuardTests(TestCase):
    """Production must refuse to boot without an explicit superuser password."""

    def test_missing_password_aborts_with_exit_1(self):
        result = run_guard({'PATH': '/usr/bin:/bin'})
        self.assertEqual(result.returncode, 1)
        self.assertIn('DJANGO_SUPERUSER_PASSWORD', result.stderr)
        self.assertEqual(result.stdout, '')

    def test_empty_password_aborts_too(self):
        result = run_guard({'PATH': '/usr/bin:/bin', 'DJANGO_SUPERUSER_PASSWORD': ''})
        self.assertEqual(result.returncode, 1)

    def test_provided_password_passes_the_guard(self):
        result = run_guard({'PATH': '/usr/bin:/bin', 'DJANGO_SUPERUSER_PASSWORD': 'a-real-secret'})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, '')

    def test_no_default_password_remains_in_the_script(self):
        script = PROD_ENTRYPOINT.read_text()
        self.assertNotIn('adminpassword', script)
        self.assertNotIn("environ.get('DJANGO_SUPERUSER_PASSWORD'", script)
        self.assertIn("environ['DJANGO_SUPERUSER_PASSWORD']", script)

    def test_script_is_valid_shell(self):
        result = subprocess.run(
            ['/bin/sh', '-n', str(PROD_ENTRYPOINT)], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dev_entrypoint_is_untouched_by_the_guard(self):
        """The guard is production-only; local dev keeps its convenience default."""
        self.assertNotIn(START_MARKER, DEV_ENTRYPOINT.read_text())
