import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - supported CLI platforms provide fcntl.
    fcntl = None  # type: ignore[assignment]

from ebs_cli.errors import CLIError, MissingConfigError

DEFAULT_URL = "https://ebs-poc.nersc.gov"
TOKEN_PREFIX = "ebs_"
TOKEN_KEY = "token"
PROJECT_KEY = "project"
GLOBUS_REFRESH_TOKEN_KEY = "globus_refresh_token"
GLOBUS_CLIENT_ID_KEY = "globus_client_id"
GLOBUS_CREDENTIAL_KEYS = frozenset({GLOBUS_REFRESH_TOKEN_KEY, GLOBUS_CLIENT_ID_KEY})
AUTH_CREDENTIAL_KEYS = frozenset({TOKEN_KEY, *GLOBUS_CREDENTIAL_KEYS})
CONCURRENT_LOGIN_ERROR = (
    "Concurrent login state changed. Local credentials were left unchanged; retry the authentication command."
)


@dataclass(frozen=True)
class AuthSnapshot:
    """Opaque immutable auth state for optimistic compare-and-write transactions."""

    _values: tuple[tuple[str, str], ...] = field(repr=False)
    _invalid_digest: str | None = field(default=None, repr=False)


def _snapshot_from_settings(settings: dict[str, Any]) -> AuthSnapshot:
    return AuthSnapshot(
        tuple(
            (name, json.dumps(settings[name], sort_keys=True, separators=(",", ":")))
            for name in sorted(AUTH_CREDENTIAL_KEYS)
            if name in settings
        )
    )


def _opaque_snapshot(path: Path, error: CLIError) -> AuthSnapshot:
    """Fingerprint an unchanged malformed regular file without exposing its contents."""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise error
        content = path.read_bytes()
    except (OSError, CLIError):
        raise error from None
    return AuthSnapshot((), hashlib.sha256(content).hexdigest())


def read_auth_snapshot(path: Path | None = None) -> AuthSnapshot:
    """Read all auth fields together, including a consistent missing/invalid state."""
    path = path or config_path()
    try:
        settings = read_config(path)
    except MissingConfigError:
        return _snapshot_from_settings({})
    except CLIError as exc:
        return _opaque_snapshot(path, exc)
    return _snapshot_from_settings(settings)


def _check_auth_snapshot(current: AuthSnapshot, expected: AuthSnapshot | None) -> None:
    if expected is not None and current != expected:
        raise CLIError(CONCURRENT_LOGIN_ERROR)


def _snapshot_values(snapshot: AuthSnapshot) -> dict[str, Any]:
    if snapshot._invalid_digest is not None:
        return {}
    return {name: json.loads(value) for name, value in snapshot._values}


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


def _check_private_info(path: Path, info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise CLIError(f"Config path is not a regular file: {path}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise CLIError(f"Config file is not owned by the current user: {path}")
    if info.st_mode & 0o077:
        raise CLIError(f"Config file is accessible by other users. Run: chmod 600 {path}")


def _check_private_file(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CLIError(f"Could not inspect config file: {path}") from exc
    _check_private_info(path, info)


def _check_lock_identity(path: Path, info: os.stat_result) -> None:
    try:
        current = path.lstat()
    except OSError:
        raise CLIError(f"Could not verify config lock file: {path}") from None
    _check_private_info(path, current)
    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
        raise CLIError(f"Config lock file changed while opening it: {path}")


@contextmanager
def _config_lock(path: Path) -> Iterator[None]:
    """Serialize complete config transactions on one persistent sibling inode."""
    if fcntl is None:  # pragma: no cover - macOS/Linux test paths always lock.
        raise CLIError("Config file locking is unavailable on this platform; no changes were saved.")
    lock_path = path.with_name(f".{path.name}.lock")
    descriptor: int | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        info = os.fstat(descriptor)
        _check_private_info(lock_path, info)
        os.fchmod(descriptor, 0o600)
        _check_lock_identity(lock_path, info)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        # Another process must not be able to swap the path while this writer
        # waits on its stable open lock inode. Application code never unlinks it.
        _check_lock_identity(lock_path, info)
    except CLIError:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        raise
    except OSError:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        raise CLIError(f"Could not lock config file: {path}") from None
    try:
        yield
    finally:
        # Releasing an already-committed write cannot turn success into failure.
        with suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        with suppress(OSError):
            os.close(descriptor)


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
    with _config_lock(path):
        settings = _existing_settings(path)
        settings[TOKEN_KEY] = token
        return _write_config(settings, path)


def _settings_for_auth_update(path: Path) -> dict[str, Any]:
    """Read once for an atomic credential update, failing closed on damage."""
    try:
        return read_config(path)
    except MissingConfigError:
        return {}


def _validate_globus_value(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 16384
        or any(not character.isascii() or not character.isprintable() for character in value)
    ):
        raise CLIError(f"Invalid saved {label}. Run 'ebs auth login' to create a new login.", exit_code=2)
    return value


def save_auth_credentials(
    token: str,
    refresh_token: str,
    client_id: str,
    path: Path | None = None,
    *,
    expected: AuthSnapshot | None = None,
) -> Path:
    """Save both personal and Globus credentials in one private atomic write."""
    token = validate_token(token)
    refresh_token = _validate_globus_value(refresh_token, "Globus refresh credential")
    client_id = _validate_globus_value(client_id, "Globus client ID")
    path = path or config_path()
    with _config_lock(path):
        settings = _settings_for_auth_update(path)
        _check_auth_snapshot(_snapshot_from_settings(settings), expected)
        settings.update(
            {
                TOKEN_KEY: token,
                GLOBUS_REFRESH_TOKEN_KEY: refresh_token,
                GLOBUS_CLIENT_ID_KEY: client_id,
            }
        )
        return _write_config(settings, path)


def load_globus_snapshot(path: Path | None = None) -> tuple[AuthSnapshot, str, str]:
    """Return one consistent snapshot and its refresh credential/public client pair."""
    snapshot = read_auth_snapshot(path)
    values = _snapshot_values(snapshot)
    refresh_token = values.get(GLOBUS_REFRESH_TOKEN_KEY)
    client_id = values.get(GLOBUS_CLIENT_ID_KEY)
    if not isinstance(refresh_token, str) or not isinstance(client_id, str):
        raise CLIError("No complete saved Globus login found. Run 'ebs auth login' first.", exit_code=2)
    return (
        snapshot,
        _validate_globus_value(refresh_token, "Globus refresh credential"),
        _validate_globus_value(client_id, "Globus client ID"),
    )


def load_globus_credentials(path: Path | None = None) -> tuple[str, str]:
    """Load the refresh credential/public client pair for an explicit refresh."""
    _snapshot, refresh_token, client_id = load_globus_snapshot(path)
    return refresh_token, client_id


def optional_globus_credentials(
    path: Path | None = None,
    *,
    snapshot: AuthSnapshot | None = None,
) -> tuple[str, str] | None:
    """Return a revocable pair when present; logout still clears partial data."""
    try:
        snapshot = snapshot if snapshot is not None else read_auth_snapshot(path)
        values = _snapshot_values(snapshot)
        refresh_token = values.get(GLOBUS_REFRESH_TOKEN_KEY)
        client_id = values.get(GLOBUS_CLIENT_ID_KEY)
        if not isinstance(refresh_token, str) or not isinstance(client_id, str):
            return None
        return (
            _validate_globus_value(refresh_token, "Globus refresh credential"),
            _validate_globus_value(client_id, "Globus client ID"),
        )
    except CLIError:
        return None


def save_project(project: str, path: Path | None = None) -> Path:
    """Save the default Project, preserving the token and other settings."""
    project = validate_project_name(project)
    path = path or config_path()
    with _config_lock(path):
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


def logout(path: Path | None = None, *, expected: AuthSnapshot | None = None) -> bool:
    """Clear all saved login credentials while retaining Project and settings."""
    path = path or config_path()
    with _config_lock(path):
        if expected is not None:
            _check_auth_snapshot(read_auth_snapshot(path), expected)
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
        if body is not None and not any(name in body for name in AUTH_CREDENTIAL_KEYS):
            return False
        remaining = {name: value for name, value in (body or {}).items() if name not in AUTH_CREDENTIAL_KEYS}
        if remaining:
            _write_config(remaining, path)
            return True
        try:
            path.unlink()
        except OSError as exc:
            raise CLIError(f"Could not remove config file: {path}") from exc
        return True
