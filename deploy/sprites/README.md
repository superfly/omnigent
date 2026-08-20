# Omnigent on Sprites

[Sprites](https://sprites.dev) are persistent Linux environments that
hibernate when idle. Omnigent supports them as a **server-managed** sandbox:
creating a session with `host_type: "managed"` provisions a Sprite, installs
the host toolchain, clones the requested repository, and registers the host
with the server. Deleting the session destroys the Sprite.

The Sprites launcher is managed-only. It does not currently participate in
the interactive `omnigent sandbox create` / `connect` CLI flow.

## Prerequisites

Install the optional SDK extra in the **server** environment and provide a
Sprites organization token:

```bash
pip install 'omnigent[sprites]'
export SPRITE_TOKEN=...
```

The server must have a public URL that a Sprite can reach.

## Server configuration

Add this block to the config passed to `omnigent server -c config.yaml` (or
the server data directory's `config.yaml`):

```yaml
sandbox:
  provider: sprites
  server_url: https://omnigent.example.com
  sprites:
    env: [OPENAI_API_KEY, GIT_TOKEN]
```

`sandbox.sprites.env` contains environment variable **names**, not values.
Each name must exist in the server process environment; the launcher copies
its value into the Sprite service. The usual Omnigent runner forwarding rules
then carry model credentials to agent processes. `GIT_TOKEN` enables cloning
and later fetch/push for private HTTPS repositories.

Optional settings:

```yaml
sandbox:
  provider: sprites
  server_url: https://omnigent.example.com
  sprites:
    api_url: https://api.sprites.dev
    runtime: default                 # default or dev
    install_spec: omnigent==0.7.0    # any pip-compatible requirement
    env: [OPENAI_API_KEY, GIT_TOKEN]
```

`SPRITES_API_URL` provides the API URL fallback when `api_url` is omitted.

## Why there is no custom image

Image-based providers start Omnigent from
`ghcr.io/omnigent-ai/omnigent-host`. Sprites do not expose a caller-supplied
container-image boot path; they provide a persistent Ubuntu filesystem
instead. The launcher translates the host image's runtime contract into a
one-time native bootstrap:

1. Install the required OS tools (`git`, `tmux`, `procps`, `lsof`,
   `bubblewrap`, `curl`, and certificates).
2. Create a persistent virtual environment under
   `~/.local/share/omnigent-host/venv`.
3. Install the configured Omnigent package requirement.
4. Install the Claude Code, Codex, and Pi coding CLIs under `~/.local`.
5. Record a bootstrap marker only after every binary check succeeds.

The Sprite filesystem survives hibernation, so later launches reuse that
installation. Changing `install_spec` invalidates the marker and upgrades the
environment. The bootstrap is retry-safe after a partial failure; if initial
provisioning fails, Omnigent destroys the incomplete Sprite.

The default install requirement is the exact version running on the server
(`omnigent==<version>`). For an unpublished development build, publish a wheel
to an authenticated package source reachable from the Sprite or set
`install_spec` to a reachable wheel URL:

```yaml
sprites:
  install_spec: "omnigent @ https://artifacts.example.com/omnigent-dev.whl"
```

That requirement is the practical custom-image replacement for Python code.
Additional system packages still require extending Omnigent's bootstrap (or a
future configurable bootstrap hook); there is no Dockerfile escape hatch.
New Sprites must provide Python 3, npm, and root or passwordless `sudo`.

## Services, hibernation, and active turns

The launcher creates an `omnigent-host` Sprite Service with the host identity,
launch token, model/git environment, repository working directory, and
augmented `PATH`. Services restart after a cold wake, so Omnigent refreshes
the token and service definition whenever a managed host resumes.

A Service alone does not protect an outbound WebSocket from suspension.
During an active agent turn, the Omnigent runner therefore uses the local
Sprites Tasks API (`/.sprite/api.sock`) to upsert a five-minute activity task
every minute. It deletes the task as soon as work drains; if the runner
crashes, expiry releases the hold. This keeps streaming model calls and the
runner tunnel alive during work while allowing idle Sprites to hibernate.

An idle Sprite may appear offline after its tunnel is suspended. Resuming the
managed host wakes it with an exec, refreshes its host token, and reapplies the
Service definition without reinstalling the filesystem.

## Operational notes

- First provision is slower than an image-backed provider because apt, pip,
  and npm run inside the Sprite. Warm/cold resumes skip this bootstrap.
- The token is read only from `SPRITE_TOKEN`; do not store it in YAML.
- Environment values listed in `sprites.env` cross the Sprites control plane.
  Prefer narrowly scoped model and repository credentials.
- Session deletion is destructive: it destroys the associated Sprite and its
  persistent filesystem.
