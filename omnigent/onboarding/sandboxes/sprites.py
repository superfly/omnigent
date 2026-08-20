"""
Sprites sandbox launcher.

Implements the server-managed subset of
:class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher` on top of
`Sprites <https://sprites.dev>`_. Sprites boot a persistent Ubuntu filesystem
rather than a caller-supplied container image, so provisioning installs the
Omnigent host toolchain once and records a version marker. Subsequent warm/cold
wakes reuse that filesystem and only refresh the ``omnigent-host`` service.

The Sprites SDK is an optional dependency (``pip install 'omnigent[sprites]'``)
and is imported lazily so base Omnigent installs do not require it.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Sequence
from contextlib import suppress
from typing import TYPE_CHECKING, Any, ClassVar

import click

from omnigent.host.identity import HOST_ID_ENV_VAR, HOST_NAME_ENV_VAR, HOST_TOKEN_ENV_VAR
from omnigent.onboarding.sandboxes.base import (
    RemoteCommandResult,
    SandboxLauncher,
    render_host_config_write_command,
)
from omnigent.version import VERSION

if TYPE_CHECKING:
    from collections.abc import Callable


TOKEN_ENV_VAR: str = "SPRITE_TOKEN"
"""Sprites organization token read by the server-side launcher."""

API_URL_ENV_VAR: str = "SPRITES_API_URL"
"""Optional Sprites API base URL override."""

INSTALL_SPEC_ENV_VAR: str = "OMNIGENT_SPRITES_INSTALL_SPEC"
"""Optional package spec installed into new Sprites.

The default is ``omnigent==<running server version>``. Development checkouts
whose ``.dev`` version is not published can point this at a wheel URL or another
pip-compatible artifact.
"""

SANDBOX_ENV_PASSTHROUGH_ENV_VAR: str = "OMNIGENT_SPRITES_SANDBOX_ENV"
"""Comma-separated server env names injected into host service + execs."""

_SERVICE_NAME: str = "omnigent-host"
_BOOTSTRAP_SCHEMA_VERSION: int = 1
_COMMAND_TIMEOUT_S: float = 15 * 60
_SPRITE_NAME_MAX_LEN: int = 63
_RUNNING_STATUS: str = "running"
_DORMANT_STATUSES: frozenset[str] = frozenset({"warm", "cold", "stopped"})
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _ensure_sdk() -> None:
    """Verify the optional Sprites SDK is importable."""
    try:
        import sprites  # noqa: F401
    except ImportError as exc:
        raise click.ClickException(
            "The Sprites SDK is required for the 'sprites' sandbox provider. "
            "Install it with `pip install 'omnigent[sprites]'`, then set "
            f"{TOKEN_ENV_VAR}."
        ) from exc


def _managed_sprite_name(name: str) -> str:
    """Return a DNS-safe, provider-prefixed Sprite name."""
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    if not slug:
        slug = "host"
    return f"omnigent-{slug}"[:_SPRITE_NAME_MAX_LEN].rstrip("-")


def _default_install_spec() -> str:
    """Resolve the package artifact installed into newly created Sprites."""
    return os.environ.get(INSTALL_SPEC_ENV_VAR) or f"omnigent=={VERSION}"


def render_bootstrap_command(install_spec: str) -> str:
    """
    Build the idempotent shell bootstrap for a new Sprite.

    Sprites persist their full filesystem, so this command translates the
    runtime contract of Omnigent's ``host`` container target into a one-time
    native install: required OS tools, an isolated Python environment, and the
    Claude/Codex/Pi CLIs. A marker is written only after every verification
    passes, making retries safe after partial failures.
    """
    marker_value = f"{_BOOTSTRAP_SCHEMA_VERSION}:{install_spec}"
    q_marker_value = shlex.quote(marker_value)
    q_install_spec = shlex.quote(install_spec)
    return f"""\
set -eu
root="$HOME/.local/share/omnigent-host"
venv="$root/venv"
marker="$root/bootstrap-version"
expected={q_marker_value}
mkdir -p "$root" "$HOME/.local/bin"
if [ -f "$marker" ] && [ "$(cat "$marker")" = "$expected" ] \
  && [ -x "$venv/bin/omnigent" ] \
  && command -v tmux >/dev/null 2>&1 \
  && command -v bwrap >/dev/null 2>&1 \
  && [ -x "$HOME/.local/bin/claude" ] \
  && [ -x "$HOME/.local/bin/codex" ] \
  && [ -x "$HOME/.local/bin/pi" ]; then
  :
else
missing_os_tools=0
for tool in python3 git tmux pgrep lsof bwrap curl; do
  command -v "$tool" >/dev/null 2>&1 || missing_os_tools=1
done
venv_probe="$root/.venv-probe"
if command -v python3 >/dev/null 2>&1; then
  python3 -m venv "$venv_probe" >/dev/null 2>&1 || missing_os_tools=1
  rm -rf "$venv_probe"
fi
if [ "$missing_os_tools" -ne 0 ]; then
  if [ "$(id -u)" -eq 0 ]; then
    apt-get update
    apt-get install -y --no-install-recommends \
      python3-venv git tmux procps lsof bubblewrap curl ca-certificates unzip
  elif command -v sudo >/dev/null 2>&1; then
    sudo -n apt-get update
    sudo -n apt-get install -y --no-install-recommends \
      python3-venv git tmux procps lsof bubblewrap curl ca-certificates unzip
  else
    echo "Omnigent host bootstrap needs root or passwordless sudo to install OS tools" >&2
    exit 1
  fi
fi
command -v python3 >/dev/null 2>&1 || {{
  echo "python3 is required in the Sprite runtime" >&2
  exit 1
}}
command -v npm >/dev/null 2>&1 || {{
  echo "npm is required in the Sprite runtime" >&2
  exit 1
}}
python3 -m venv "$venv"
"$venv/bin/python" -m pip install --disable-pip-version-check --upgrade pip
"$venv/bin/python" -m pip install --disable-pip-version-check --upgrade {q_install_spec}
install_npm_cli() {{
  binary="$1"
  package="$2"
  [ -x "$HOME/.local/bin/$binary" ] && return
  # A broken user-local shim should not make npm fail with EEXIST.
  rm -f "$HOME/.local/bin/$binary"
  npm install --global --prefix "$HOME/.local" --no-audit --no-fund "$package"
}}
install_npm_cli claude @anthropic-ai/claude-code
install_npm_cli codex @openai/codex
install_npm_cli pi @earendil-works/pi-coding-agent
git config --global credential.helper \
  '!f() {{ [ "$1" = get ] || return 0; '\
'[ -n "$GIT_TOKEN" ] || return 0; '\
'printf "username=%s\\npassword=%s\\n" '\
'"${{GIT_USERNAME:-x-access-token}}" "$GIT_TOKEN"; }}; f'
test -x "$venv/bin/omnigent"
test -x "$HOME/.local/bin/claude"
test -x "$HOME/.local/bin/codex"
test -x "$HOME/.local/bin/pi"
printf %s "$expected" > "$marker"
fi
"""


class SpritesSandboxLauncher(SandboxLauncher):
    """Managed Omnigent hosts backed by persistent Sprites."""

    provider: ClassVar[str] = "sprites"
    supports_cli_bootstrap: ClassVar[bool] = False
    can_resume: ClassVar[bool] = True

    def __init__(
        self,
        *,
        api_url: str | None = None,
        env: Sequence[str] | None = None,
        runtime: str | None = None,
        install_spec: str | None = None,
    ) -> None:
        self._api_url = api_url
        self._env_names = tuple(env) if env is not None else None
        self._runtime = runtime
        self._install_spec = install_spec
        self._client: Any | None = None

    def _sprites(self) -> Any:
        """Return the lazily constructed SDK client."""
        if self._client is None:
            _ensure_sdk()
            from sprites import SpritesClient

            api_url = self._api_url or os.environ.get(API_URL_ENV_VAR)
            if api_url:
                self._client = SpritesClient(
                    os.environ[TOKEN_ENV_VAR],
                    base_url=api_url,
                )
            else:
                self._client = SpritesClient(os.environ[TOKEN_ENV_VAR])
        return self._client

    def _sprite(self, sandbox_id: str) -> Any:
        """Return an SDK handle for *sandbox_id* without a network lookup."""
        return self._sprites().sprite(sandbox_id)

    def _resolve_sandbox_env(self) -> dict[str, str]:
        """Resolve configured server environment variables by name."""
        if self._env_names is not None:
            names: Sequence[str] = self._env_names
        else:
            names = [
                name.strip()
                for name in os.environ.get(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, "").split(",")
                if name.strip()
            ]
        resolved: dict[str, str] = {}
        for name in names:
            if _ENV_NAME_RE.fullmatch(name) is None:
                raise click.ClickException(
                    f"sandbox.sprites.env contains invalid environment variable name {name!r}."
                )
            value = os.environ.get(name)
            if value is None:
                raise click.ClickException(
                    f"sandbox env passthrough names '{name}' but it is not set "
                    "in the server's environment — set it (or remove it from "
                    "sandbox.sprites.env / "
                    f"{SANDBOX_ENV_PASSTHROUGH_ENV_VAR})."
                )
            resolved[name] = value
        return resolved

    def _command_with_env(self, command: str) -> str:
        """Prefix configured workload env while preserving the Sprite base env."""
        env = self._resolve_sandbox_env()
        if not env:
            return command
        exports = "; ".join(f"export {name}={shlex.quote(value)}" for name, value in env.items())
        return f"{exports}; {command}"

    def prepare(self) -> None:
        """Verify the SDK and server-side Sprites token."""
        _ensure_sdk()
        if not os.environ.get(TOKEN_ENV_VAR):
            raise click.ClickException(
                f"No Sprites credentials found. Create an organization token and set "
                f"{TOKEN_ENV_VAR}."
            )
        self._resolve_sandbox_env()

    def provision(self, name: str) -> str:
        """Create and bootstrap a new persistent Sprite."""
        from sprites.exceptions import SpriteError

        sprite_name = _managed_sprite_name(name)
        click.echo(f"▸ Creating Sprite '{sprite_name}'")
        try:
            sprite = self._sprites().create_sprite(
                sprite_name,
                labels=["omnigent-managed"],
                wait_for_capacity=True,
                runtime=self._runtime,
            )
        except SpriteError as exc:
            raise click.ClickException(f"Sprite creation failed: {exc}") from exc
        sandbox_id = str(sprite.name)
        try:
            install_spec = self._install_spec or _default_install_spec()
            click.echo(f"  → bootstrapping Omnigent host ({install_spec})")
            self.run(sandbox_id, render_bootstrap_command(install_spec))
        except Exception:
            with suppress(Exception):
                sprite.destroy()
            raise
        click.echo(f"  → created {sandbox_id}")
        return sandbox_id

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """Run a shell command inside a Sprite and capture both streams."""
        from sprites.exceptions import SpriteError

        try:
            result = self._sprite(sandbox_id).run(
                "bash",
                "-lc",
                self._command_with_env(command),
                capture_output=True,
                check=False,
                timeout=_COMMAND_TIMEOUT_S,
            )
        except SpriteError as exc:
            raise click.ClickException(
                f"Remote command failed to execute on Sprite '{sandbox_id}': {exc}"
            ) from exc
        stdout = (result.stdout or b"").decode("utf-8", errors="replace")
        stderr = (result.stderr or b"").decode("utf-8", errors="replace")
        if stdout:
            click.echo(stdout, nl=not stdout.endswith("\n"))
        if stderr:
            click.echo(stderr, nl=not stderr.endswith("\n"), err=True)
        if check and result.returncode != 0:
            diagnostic = (stderr.strip() or stdout.strip())[-2000:]
            suffix = f"\nRemote output tail:\n{diagnostic}" if diagnostic else ""
            raise click.ClickException(
                f"Remote command failed on Sprite '{sandbox_id}' "
                f"(exit {result.returncode}).{suffix}"
            )
        return RemoteCommandResult(returncode=result.returncode, stdout=stdout, stderr=stderr)

    def start_host(
        self,
        sandbox_id: str,
        *,
        token: str,
        host_id: str,
        host_name: str,
        server_url: str,
        repo_url: str | None = None,
        repo_branch: str | None = None,
        repo_name: str | None = None,
        host_config: dict[str, object] | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> str:
        """Create/update the cold-wake-safe ``omnigent-host`` service."""
        from sprites.exceptions import SpriteError

        home = self.run(sandbox_id, 'printf %s "$HOME"').stdout.strip()
        if not home:
            raise click.ClickException(f"could not resolve $HOME inside Sprite '{sandbox_id}'")
        workspace = f"{home}/workspace"
        self.run(sandbox_id, f"mkdir -p {shlex.quote(workspace)}")
        if repo_url is not None:
            workspace = self.materialize_workspace(
                sandbox_id,
                workspace=workspace,
                repo_url=repo_url,
                repo_branch=repo_branch,
                repo_name=repo_name,
                on_stage=on_stage,
            )
        if on_stage is not None:
            on_stage("starting")
        host_python = f"{home}/.local/share/omnigent-host/venv/bin/python"
        self.run(
            sandbox_id,
            render_host_config_write_command(
                host_config or {},
                python_executable=host_python,
            ),
        )
        base_path = self.run(sandbox_id, 'printf %s "$PATH"').stdout.strip()
        service_env = {
            **self._resolve_sandbox_env(),
            "PATH": f"{home}/.local/bin:{base_path or '/usr/local/bin:/usr/bin:/bin'}",
            "IS_SANDBOX": "1",
            HOST_TOKEN_ENV_VAR: token,
            HOST_ID_ENV_VAR: host_id,
            HOST_NAME_ENV_VAR: host_name,
        }
        executable = f"{home}/.local/share/omnigent-host/venv/bin/omnigent"
        click.echo(f"▸ Starting Omnigent host service on Sprite '{sandbox_id}'")
        try:
            stream = self._sprite(sandbox_id).create_service(
                _SERVICE_NAME,
                cmd=executable,
                args=["host", "--server", server_url],
                env=service_env,
                dir=workspace,
            )
            for event in stream:
                event_type = getattr(event, "type", "")
                data = getattr(event, "data", None)
                if event_type in {"stdout", "stderr", "info"} and data:
                    click.echo(str(data), err=event_type == "stderr")
                if event_type == "error":
                    raise click.ClickException(
                        f"Omnigent host service failed on Sprite '{sandbox_id}': "
                        f"{data or getattr(event, 'error', 'unknown service error')}"
                    )
        except SpriteError as exc:
            raise click.ClickException(
                f"Could not start Omnigent host service on Sprite '{sandbox_id}': {exc}"
            ) from exc
        return workspace

    def resume(self, sandbox_id: str) -> None:
        """Wake a Sprite and remove the stale auto-started host service.

        Sprites automatically restart persisted services when cold compute
        wakes.  The existing ``omnigent-host`` definition still carries the
        previous managed-host token, while :func:`resume_managed_host` mints a
        new token before calling :meth:`start_host`.  If the old service is
        left running, updating its definition does not reliably restart the
        process and it reconnects with the revoked token (HTTP 403).

        Delete the persisted definition after waking.  ``start_host`` then
        recreates it with the fresh token and the workspace remains untouched.
        """
        click.echo(f"▸ Waking Sprite '{sandbox_id}'")
        self.run(sandbox_id, "true")
        sprite = self._sprite(sandbox_id)
        services = sprite.list_services()
        if any(getattr(service, "name", None) == _SERVICE_NAME for service in services):
            sprite.delete_service(_SERVICE_NAME)

    def is_running(self, sandbox_id: str) -> bool | None:
        """Return whether Sprites reports this sandbox as actively running."""
        from sprites.exceptions import NotFoundError, SpriteError

        try:
            status = str(self._sprites().get_sprite(sandbox_id).status or "").lower()
        except NotFoundError:
            return False
        except SpriteError as exc:
            raise click.ClickException(f"Could not inspect Sprite '{sandbox_id}': {exc}") from exc
        if status == _RUNNING_STATUS:
            return True
        if status in _DORMANT_STATUSES:
            return False
        return None

    def terminate(self, sandbox_id: str) -> None:
        """Destroy a Sprite; missing Sprites are already terminated."""
        from sprites.exceptions import NotFoundError, SpriteError

        try:
            self._sprites().destroy_sprite(sandbox_id)
        except NotFoundError:
            return
        except SpriteError as exc:
            raise click.ClickException(f"Could not destroy Sprite '{sandbox_id}': {exc}") from exc
