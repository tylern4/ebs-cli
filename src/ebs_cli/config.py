import json
import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path

from ebs_cli.errors import CLIError

DEFAULT_URL = "https://ebs-poc.nersc.gov"
TOKEN_PREFIX = "ebs_"


def config_path() -> Path:
    explicit = os.environ.get("EBS_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base / "ebs" / "config.json"


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


def save_token(token: str, path: Path | None = None) -> Path:
    token = validate_token(token)
    path = path or config_path()
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
            json.dump({"token": token}, stream, separators=(",", ":"))
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


def load_token(path: Path | None = None) -> str:
    environment_token = os.environ.get("EBS_TOKEN")
    if path is None and environment_token is not None:
        return validate_token(environment_token)

    path = path or config_path()
    _check_private_file(path)
    try:
        with path.open(encoding="utf-8") as stream:
            body = json.load(stream)
    except FileNotFoundError as exc:
        raise CLIError("No API token found. Run 'ebs auth login' or set EBS_TOKEN.", exit_code=2) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CLIError(f"Could not read config file: {path}") from exc
    if not isinstance(body, dict) or not isinstance(body.get("token"), str):
        raise CLIError(f"Config file does not contain a valid API token: {path}")
    return validate_token(body["token"])


def logout(path: Path | None = None) -> bool:
    path = path or config_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CLIError(f"Could not remove config file: {path}") from exc
    return True
