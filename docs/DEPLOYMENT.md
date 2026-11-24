# Deployment runbook

How code gets from a merged PR to the running production stack, and what to
do when it doesn't go cleanly.

## 1. What triggers a deploy, end to end

```
merge to main (this repo)
  -> .github/workflows/release.yml
       verify:      calls ci.yml (workflow_call), same gate PRs run, so a
                    release can never test something a PR didn't
       build-push:  builds the image, pushes it to GHCR tagged with the FULL
                    commit SHA (and `:latest`, for humans browsing the package,
                    never for deploys)
       dispatch:    POSTs a repository_dispatch (event_type: backend-release,
                    payload: {sha, image}) to tranqn/videoflix (the umbrella),
                    using the UMBRELLA_DISPATCH_TOKEN repo secret. If that
                    secret is not set, this step no-ops loudly (a GitHub
                    Actions notice) instead of failing `main`.
  -> tranqn/videoflix .github/workflows/deploy.yml
       validates the payload (image must match ghcr.io/tranqn/videoflix-backend,
       tag must not be :latest, tag must equal sha), then SSHes onto the server
       and runs:
         cd ~/videoflix
         git pull --ff-only                          # umbrella itself
         git submodule update --init --remote frontend  # frontend tracks its branch
         git submodule update --init backend
         git -C backend checkout --detach "$SHA"      # backend pinned to the exact commit
         ~/videoflix/backend/deploy/deploy.sh "$IMAGE"
       then smoke-tests the public site over HTTPS (frontend 200, /api/video/
       returns 401 for an anonymous caller).
```

You can also trigger `deploy.yml` directly (see §3), which is how you deploy a
specific commit by hand or re-run a deploy that never got dispatched.

## 2. Where images live, how they're tagged, and why never `:latest`

- Registry: `ghcr.io/tranqn/videoflix-backend`, public (no registry
  credentials needed anywhere, including on the server).
- Every build is tagged with the **full commit SHA**. That tag is immutable:
  the same string always means the same bytes, so it's the only thing a
  rollback can trust.
- `:latest` is also pushed, for humans browsing the GHCR package page. It is
  never used in a deploy command or in `deploy.sh` (which actively refuses a
  `:latest` argument unless `ALLOW_MUTABLE_TAG=1` is set), because a moving
  tag means "the same command deploys different code tomorrow", you couldn't
  reproduce or roll back to it with any confidence.

## 3. Deploying a specific commit by hand

Use the umbrella's `workflow_dispatch` on `deploy.yml`, giving it the image and
the commit it was built from (they must match):

```bash
gh workflow run deploy.yml -R tranqn/videoflix \
  -f image=ghcr.io/tranqn/videoflix-backend:<full-commit-sha> \
  -f sha=<full-commit-sha>
```

Or via the GitHub UI: Actions -> Deploy -> Run workflow, filling in the same
two fields. This runs the identical SSH + `deploy.sh` path a normal dispatch
would; it's the way to deploy an older, known-good commit, or to retry a
deploy whose automatic dispatch never arrived (event-type mismatch, missing
`UMBRELLA_DISPATCH_TOKEN`, etc.).

## 4. `deploy.sh` exit codes

`deploy.sh` (`backend/deploy/deploy.sh`) is what actually runs on the server.
It pulls the given image, gates the switch on `/api/health/`, and decides
whether an automatic rollback is safe:

| Exit | Meaning | What to do |
| ---- | ------- | ---------- |
| `0` | Deployed and healthy. | Nothing. |
| `1` | Deploy failed; rolled back automatically. Stack is serving the **previous** version. | Read `docker compose -f compose.prod.yml logs web worker media_worker` for why the new image failed; fix and re-deploy. No data risk: rollback only happens automatically when no migrations were applied. |
| `2` | Refused to start (bad arguments, or missing/invalid production config: e.g. `SITE_ADDRESS` unset). | Fix the invocation or `.env` on the server; nothing was touched. |
| `3` | **Rollback refused.** A human must act now. | See below, do not skip this. |

### Exit 3: what it means and how to respond

`deploy.sh` refuses to roll back automatically whenever doing so would be a
guess rather than a known-safe action. The most consequential case: **this
deploy applied one or more migrations, and then failed its health check.**
Rolling the image back in that state would run the *old* code against the
*new* schema, which risks silent data corruption, worse than a loud outage.
So the script deliberately leaves the new (broken) image running and exits 3
instead of guessing.

(The same exit code also covers: no previous deployment recorded to roll back
to; no previous commit recorded in the state file (expected on the very first
deploy through this script); the database was unreachable so migration state
is unknown, treated as "yes, migrations changed" on purpose; or the rollback
itself failed partway through.)

When you see exit 3:

1. **Do not panic-rollback the image by itself.** The runbook `deploy.sh`
   prints (also visible in the GitHub Actions log for that run) has the exact
   image/commit pair involved.
2. Look first:
   ```bash
   cd ~/videoflix/backend
   docker compose -f compose.prod.yml logs --tail=200 web worker media_worker
   docker compose -f compose.prod.yml ps
   ```
3. **Fixing forward is the preferred option.** If the new code has a bug but
   the migrated schema is fine, ship a fix and deploy again rather than
   reverting.
4. If you must roll back, do **both halves, in this order**, by hand (never
   just the image):
   ```bash
   # 1. Reverse the migrations this deploy applied, check first that they
   #    are reversible AND that no data would be lost; take a database dump
   #    before touching anything.
   docker compose -f compose.prod.yml exec web python manage.py showmigrations
   docker compose -f compose.prod.yml exec web python manage.py migrate <app> <previous_migration>

   # 2. Restore code and image together (never the image alone: compose.prod.yml,
   #    the Caddyfile and the entrypoint all live in the backend submodule and
   #    must match the image that's running).
   git -C ~/videoflix/backend checkout --detach <previous_commit>
   WEB_IMAGE=<previous_image> docker compose -f compose.prod.yml up -d
   ```
   This leaves the `backend` submodule detached at the old commit; the
   umbrella repo will show it as modified until you re-point it (e.g. by
   deploying forward again).

## 5. Rolling back by hand (no migrations involved)

If a bad deploy did **not** apply any migrations, `deploy.sh` already rolled
back automatically (exit 1), there's nothing to do by hand. To roll back to
an older commit deliberately (e.g. after fixing forward turns out to be the
wrong call and no schema change is at stake), redeploy the previous SHA
through the normal path:

```bash
gh workflow run deploy.yml -R tranqn/videoflix \
  -f image=ghcr.io/tranqn/videoflix-backend:<previous-full-sha> \
  -f sha=<previous-full-sha>
```

This runs through `deploy.sh` again with the same health gate, rather than
bypassing it.

## 6. Secrets, by repository

| Secret | Repository | Purpose |
| ------ | ---------- | ------- |
| `GITHUB_TOKEN` (automatic) | `tranqn/videoflix-backend` (this repo) | Pushes the built image to GHCR (`packages: write`). Scoped to this repo only, so it cannot dispatch to the umbrella. |
| `UMBRELLA_DISPATCH_TOKEN` | `tranqn/videoflix-backend` (this repo) | Cross-repo PAT used to POST the `backend-release` `repository_dispatch` to the umbrella. Needs write access to the umbrella's contents (fine-grained: Contents read+write on `tranqn/videoflix`; classic: `repo`). If unset, the dispatch step no-ops loudly instead of failing `main`. |
| `SSH_HOST`, `SSH_USER`, `SSH_PRIVATE_KEY` | `tranqn/videoflix` (umbrella) | Let `deploy.yml` SSH onto the production server to run `deploy.sh`. |
| `vars.SITE_URL` (repo variable, not a secret) | `tranqn/videoflix` (umbrella) | Overrides the URL the post-deploy smoke test hits; falls back to the production domain. |

No registry credentials exist anywhere: the GHCR package is public, so the
server pulls anonymously. If the package is ever made private, a read-only
registry token would need to be added to the server and to `deploy.sh` (add
`docker login` before the pull), a deliberate trade-off to weigh, not a
default to flip casually.
