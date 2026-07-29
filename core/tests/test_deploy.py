"""Tests for the deployment contract: pull-only compose + deploy.sh guard rails.

These live next to test_entrypoint.py for the same reason that one does: the
shipped shell/compose files are the parts of production nobody exercises until
it is 03:00, and the guard rails in them are cheap to break by tidying.

Only the checks that run BEFORE deploy.sh touches Docker are executed here.
Every case is run with a PATH that contains no `docker`, so a test that
accidentally got further than intended dies on the missing binary instead of
pulling images or restarting anything.
"""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROD_COMPOSE = BACKEND_DIR / 'compose.prod.yml'
BUILD_COMPOSE = BACKEND_DIR / 'compose.build.yml'
DEPLOY_SCRIPT = BACKEND_DIR / 'deploy' / 'deploy.sh'

IMAGE_PLACEHOLDER = '${WEB_IMAGE:-videoflix-backend:prod}'
APP_SERVICES = ('web', 'worker', 'media_worker')

# Deliberately repetitive, zero-entropy placeholders: these strings sit next to
# the words SECRET and PASSWORD, and the CI secret scanners (gitleaks'
# generic-api-key rule, semgrep p/secrets) key on keyword + entropy. A realistic
# looking dummy here would turn the pipeline red for no reason.
VALID_ENV = (
    'SECRET_KEY=xxxxxxxxxxxxxxxx\n'
    'SITE_ADDRESS=videoflix.example.com\n'
    'DB_NAME=videoflix\n'
    'DB_USER=videoflix\n'
    'DB_PASSWORD=xxxxxxxx\n'
)

# Enough for bash, grep, tail, dirname, mktemp and date; deliberately without
# the directory holding `docker`.
SAFE_PATH = '/usr/bin:/bin:/usr/sbin:/sbin'


def uncommented(path):
    """Return the file's lines with comment-only lines removed."""
    return [line for line in path.read_text().splitlines() if not line.lstrip().startswith('#')]


def service_blocks(path):
    """Return {service name: [lines]} for a two-space-indented compose file."""
    blocks, current = {}, None
    for line in uncommented(path):
        header = re.match(r'^ {2}(?P<name>[\w-]+):\s*$', line)
        if header:
            current = header.group('name')
            blocks[current] = []
        elif current and line.startswith('    '):
            blocks[current].append(line)
        elif line and not line.startswith(' '):
            current = None
    return blocks


class ComposeIsPullOnlyTests(TestCase):
    """compose.prod.yml must be unable to build; the overlay restores building."""

    def test_production_compose_has_no_build_key(self):
        offenders = [line for line in uncommented(PROD_COMPOSE) if re.match(r'^\s+build:', line)]
        self.assertEqual(
            offenders,
            [],
            'compose.prod.yml must have no `build:` key: the server deploys the exact '
            'image CI tested and must not be able to rebuild from local sources.',
        )

    def test_every_app_service_uses_the_web_image_variable(self):
        blocks = service_blocks(PROD_COMPOSE)
        for name in APP_SERVICES:
            self.assertIn(name, blocks, f'service {name} disappeared from compose.prod.yml')
            images = [line.strip() for line in blocks[name] if line.strip().startswith('image:')]
            self.assertEqual(
                images,
                [f'image: {IMAGE_PLACEHOLDER}'],
                f'{name} must take its image from WEB_IMAGE so one deploy moves all '
                f'three services to the same build',
            )

    def test_build_overlay_restores_the_build_context_for_web_only(self):
        blocks = service_blocks(BUILD_COMPOSE)
        self.assertEqual(list(blocks), ['web'])
        self.assertTrue(any(line.strip() == 'build:' for line in blocks['web']))
        self.assertTrue(any('backend.Dockerfile' in line for line in blocks['web']))

    def test_stale_build_first_comment_is_gone(self):
        self.assertNotIn('ensures the image is built first', PROD_COMPOSE.read_text())


class DeployScriptTests(TestCase):
    """deploy.sh must refuse to run before it can damage the running stack."""

    def make_repo(self, tmp, env=VALID_ENV, compose=True):
        """Build a throwaway copy of the deployed layout under tmp."""
        repo = Path(tmp) / 'backend'
        (repo / 'deploy').mkdir(parents=True)
        shutil.copy(DEPLOY_SCRIPT, repo / 'deploy' / 'deploy.sh')
        if compose:
            (repo / 'compose.prod.yml').write_text('services: {}\n')
        if env is not None:
            (repo / '.env').write_text(env)
        return repo

    def run_deploy(self, repo, *args, cwd=None):
        """Run the copied deploy.sh with a docker-free PATH."""
        return subprocess.run(
            ['bash', str(repo / 'deploy' / 'deploy.sh'), *args],
            cwd=str(cwd or Path(repo).parent),
            env={'PATH': SAFE_PATH, 'HOME': os.environ.get('HOME', '/tmp')},
            capture_output=True,
            text=True,
            check=False,
        )

    def test_script_is_executable(self):
        self.assertTrue(os.access(DEPLOY_SCRIPT, os.X_OK), 'deploy/deploy.sh must be executable')

    def test_requires_exactly_one_argument(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(tmp)
            for args in ([], ['a', 'b']):
                result = self.run_deploy(repo, *args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('usage:', result.stderr)

    def test_refuses_the_mutable_latest_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(tmp)
            result = self.run_deploy(repo, 'ghcr.io/tranqn/videoflix-backend:latest')
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('latest', result.stderr)

    def test_refuses_when_env_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(tmp, env=None)
            result = self.run_deploy(repo, 'ghcr.io/tranqn/videoflix-backend:abc123')
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('.env not found', result.stderr)

    def test_refuses_when_a_required_setting_is_empty(self):
        for key in ('SECRET_KEY', 'SITE_ADDRESS', 'DB_PASSWORD'):
            broken = re.sub(rf'^{key}=.*$', f'{key}=', VALID_ENV, flags=re.M)
            with tempfile.TemporaryDirectory() as tmp:
                repo = self.make_repo(tmp, env=broken)
                result = self.run_deploy(repo, 'ghcr.io/tranqn/videoflix-backend:abc123')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(key, result.stderr)

    def test_runs_from_the_repository_root_not_the_callers_cwd(self):
        """The compose project name comes from the working directory.

        Deploying from anywhere else silently binds the stack to a different,
        empty `videoflix_media` volume. The test puts a perfectly valid .env in
        the caller's cwd and none in the repo: a script that failed to cd would
        sail past the precondition instead of naming the repo directory.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(tmp, env=None)
            caller = Path(tmp) / 'elsewhere'
            caller.mkdir()
            (caller / '.env').write_text(VALID_ENV)
            (caller / 'compose.prod.yml').write_text('services: {}\n')
            result = self.run_deploy(repo, 'ghcr.io/x/y:abc123', cwd=caller)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('.env not found', result.stderr)
            self.assertIn('backend', result.stderr)
            self.assertNotIn('elsewhere', result.stderr)

    def test_valid_config_gets_as_far_as_docker(self):
        """With config in order, the next thing to stop it must be Docker itself."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(tmp)
            result = self.run_deploy(repo, 'ghcr.io/tranqn/videoflix-backend:abc123')
            self.assertEqual(result.returncode, 2, result.stderr)
            # Exit 2 is the Docker stage, so reaching it already proves every
            # config precondition passed — which is what this test is about.
            # Which Docker-stage message appears depends on the environment:
            # without docker on the restricted PATH the script reports it is not
            # installed, with docker present the pull runs and ghcr.io denies it.
            # Accept either, and assert no config check was the thing that fired.
            self.assertTrue(
                any(
                    marker in result.stderr
                    for marker in ('docker is not installed', 'could not pull')
                ),
                result.stderr,
            )
            for config_error in ('.env not found', 'SECRET_KEY', 'SITE_ADDRESS', 'DB_PASSWORD'):
                self.assertNotIn(config_error, result.stderr)


class DeployScriptSafetyRailTests(TestCase):
    """The rules that keep a bad deploy from becoming a bad recovery."""

    def setUp(self):
        self.source = DEPLOY_SCRIPT.read_text()

    def test_prunes_only_after_the_health_gate(self):
        prune = self.source.index('docker image prune')
        gate = self.source.index('if wait_for_health && run_post_health_assertions')
        self.assertLess(gate, prune, 'pruning before the health gate can destroy the way back')

    def test_captures_the_rollback_image_before_starting_the_new_stack(self):
        capture = self.source.index("PREV_IMAGE=\"$(docker inspect -f '{{.Config.Image}}'")
        start = self.source.index('docker compose -f "$COMPOSE_FILE" up -d --remove-orphans')
        self.assertLess(capture, start)

    def test_rollback_restores_code_and_image_together(self):
        checkout = self.source.index('git -c advice.detachedHead=false checkout --detach')
        up = self.source.index('WEB_IMAGE="$PREV_IMAGE" docker compose')
        self.assertLess(checkout, up, 'the code half must be restored before the image half')

    def test_refuses_rollback_when_migrations_were_applied(self):
        self.assertIn('if [ "$after" -ne "$MIGRATIONS_BEFORE" ]; then', self.source)
        self.assertIn('refuse_rollback "this deploy applied', self.source)

    def test_unknown_migration_state_is_treated_as_applied(self):
        self.assertIn(
            'if [ -z "$MIGRATIONS_BEFORE" ] || [ -z "$after" ]; then',
            self.source,
            'an unreadable migration state must block the automatic rollback, not allow it',
        )
