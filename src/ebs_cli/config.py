import json
import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from ebs_cli.errors import CLIError, MissingConfigError

DEFAULT_URL = "https://ebs-poc.nersc.gov"
TOKEN_PREFIX = "ebs_"
TOKEN_KEY = "token"
PROJECT_KEY = "project"


def config_path() -> Path:
    explicit = os.environ.get("EBS_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base / "ebs" / "config.json"


def validate_project_name(project: str) -> str:
    project = project.strip()
    if not project or not project.isascii() or any(not character.isprintable() for character in project):
        raise CLIError("Project must be a non-empty printable ASCII name.", exit_code=2)
    return project


def validate_token(token: str) -> str:
    if not token.startswith(TOKEN_PREFIX) or len(token) <= len(TOKEN_PREFIX):
        raise CLIError("Enter a personal EBS API token beginning with 'ebs_'.", exit_code=2)
    if any(not (character.isascii() and (character.isalnum() or character in "_-")) for character in token):
        raise CLIError("The personal EBS API token contains invalid characters.", exit_code=2)
    return token


def _check_private_file(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CLIError(f"Could not inspect config file: {path}") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise CLIError(f"Config path is not a regular file: {path}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise CLIError(f"Config file is not owned by the current user: {path}")
    if info.st_mode & 0o077:
        raise CLIError(f"Config file is accessible by other users. Run: chmod 600 {path}")


def read_config(path: Path | None = None) -> dict[str, Any]:
    """Return the parsed config body, rejecting missing, unreadable, or malformed files."""
    path = path or config_path()
    _check_private_file(path)
    try:
        with path.open(encoding="utf-8") as stream:
            body = json.load(stream)
    except FileNotFoundError as exc:
        raise MissingConfigError(f"No config file found at: {path}", exit_code=2) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CLIError(f"Could not read config file: {path}") from exc
    if not isinstance(body, dict):
        raise CLIError(f"Config file does not contain a JSON object: {path}")
    return body


def _write_config(body: dict[str, Any], path: Path) -> Path:
    """Atomically replace the config file with owner-only permissions."""
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _check_private_file(path)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".config.", dir=path.parent)
    except OSError as exc:
        raise CLIError(f"Could not save config file: {path}") from exc
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(body, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise CLIError(f"Could not save config file: {path}") from exc
    return path


def _existing_settings(path: Path) -> dict[str, Any]:
    """Return the current config body, or an empty object when it cannot be read."""
    try:
        return read_config(path)
    except CLIError:
        return {}


def save_token(token: str, path: Path | None = None) -> Path:
    """Save the token, preserving the saved default Project and other settings."""
    token = validate_token(token)
    path = path or config_path()
    settings = _existing_settings(path)
    settings[TOKEN_KEY] = token
    return _write_config(settings, path)


def save_project(project: str, path: Path | None = None) -> Path:
    """Save the default Project, preserving the token and other settings."""
    project = validate_project_name(project)
    path = path or config_path()
    settings = _existing_settings(path)
    settings[PROJECT_KEY] = project
    return _write_config(settings, path)


def saved_project(path: Path | None = None) -> str | None:
    """Return the saved default Project, or None when the config does not set one."""
    path = path or config_path()
    try:
        body = read_config(path)
    except MissingConfigError:
        return None
    project = body.get(PROJECT_KEY)
    if project is None:
        return None
    if not isinstance(project, str) or not project.strip():
        raise CLIError(f"Config file does not contain a valid default Project: {path}")
    try:
        return validate_project_name(project)
    except CLIError as exc:
        raise CLIError(f"Config file does not contain a valid default Project: {path}") from exc


def load_token(path: Path | None = None) -> str:
    environment_token = os.environ.get("EBS_TOKEN")
    if path is None and environment_token is not None:
        return validate_token(environment_token)

    path = path or config_path()
    try:
        body = read_config(path)
    except MissingConfigError:
        raise CLIError("No API token found. Run 'ebs auth login' or set EBS_TOKEN.", exit_code=2) from None
    token = body.get(TOKEN_KEY)
    if not isinstance(token, str):
        raise CLIError(f"Config file does not contain a valid API token: {path}")
    return validate_token(token)


def logout(path: Path | None = None) -> bool:
    """Remove only the saved token, keep other settings, and delete the file when empty."""
    path = path or config_path()
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CLIError(f"Could not inspect config file: {path}") from exc
    try:
        body: dict[str, Any] | None = read_config(path)
    except CLIError:
        # The file cannot be parsed as a config, so there is nothing to preserve.
        body = None
    if body is not None and not isinstance(body.get(TOKEN_KEY), str):
        return False
    remaining = {name: value for name, value in (body or {}).items() if name != TOKEN_KEY}
    if remaining:
        _write_config(remaining, path)
        return True
    try:
        path.unlink()
    except OSError as exc:
        raise CLIError(f"Could not remove config file: {path}") from exc
    return True
