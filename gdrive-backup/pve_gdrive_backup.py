#!/usr/bin/env python3
"""
Upload Proxmox VE backups (vzdump archives) to Google Drive with rclone.

How it works
------------
* A vzdump hook (``script:`` in /etc/vzdump.conf) runs at the end of every
  backup job. When the job wrote to the configured storage, it flags a pending
  upload and starts pve-gdrive-backup.service without waiting for it, so a slow
  upload never keeps the backup task running.
* The service runs ``pve-gdrive-backup upload``: rclone copies the storage's
  dump/ directory to Google Drive, optionally through an rclone crypt remote.
  In mirror mode it also deletes from Drive what vzdump pruned locally, so
  Drive follows the retention of the backup jobs. If another job finishes
  while an upload is running, the service uploads again right after.
* Failed (and optionally successful) uploads raise a Proxmox notification,
  routed by the matchers and targets of Datacenter > Notifications.

``setup`` is an interactive wizard: it installs rclone, connects a Google
account (guiding you through creating your own OAuth client), asks for the
storage, the Drive folder and the upload options, and installs everything.
Re-run it at any time to change the configuration.

Usage (as root on the node that holds the backups)::

    pve-gdrive-backup setup                 # wizard: install or reconfigure
    pve-gdrive-backup status                # configuration, last upload, drift
    pve-gdrive-backup run                   # upload now, in the background
    pve-gdrive-backup test-notification
    pve-gdrive-backup uninstall [--purge]
"""

from __future__ import annotations

import argparse
import base64
import collections
import configparser
import datetime
import fcntl
import hashlib
import io
import itertools
import json
import os
import re
import secrets
import select
import shlex
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Sequence

TOOL_NAME = "pve-gdrive-backup"
TOOL_DIR = Path(__file__).resolve().parent
INSTALL_DIR = Path("/usr/local/lib/pve-gdrive-backup")
COMMAND_PATH = Path("/usr/local/sbin/pve-gdrive-backup")
INSTALLED_FILES = ("pve_gdrive_backup.py", "vzdump-hook.sh", "pve-gdrive-backup.service")
HOOK_PATH = INSTALL_DIR / "vzdump-hook.sh"
SERVICE_NAME = "pve-gdrive-backup.service"
UNIT_PATH = Path("/etc/systemd/system") / SERVICE_NAME

CONFIG_PATH = Path("/etc/pve-gdrive-backup.conf")
VZDUMP_CONF = Path("/etc/vzdump.conf")
PENDING_FLAG = Path("/run/pve-gdrive-backup.pending")
LOCK_PATH = Path("/run/pve-gdrive-backup.lock")

# pmxcfs is shared by the whole cluster: the templates are installed once.
TEMPLATE_SOURCE_DIR = "notification-templates"
TEMPLATE_DIR = Path("/etc/pve/notification-templates/default")
TEMPLATE_NAME = "pve-gdrive-backup"

CRYPT_REMOTE = "pve-gdrive-backup-crypt"
REMOTE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")

# Google OAuth for installed apps: loopback redirect plus PKCE. The redirect
# reaches this script directly through `ssh -L` (remote-deploy.sh), otherwise
# the user pastes the address the browser failed to open.
OAUTH_PORT = 53682
OAUTH_REDIRECT_URI = f"http://127.0.0.1:{OAUTH_PORT}/"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_ABOUT_URL = "https://www.googleapis.com/drive/v3/about?fields=user(displayName,emailAddress)"
DRIVE_SCOPES = {
    "drive.file": "https://www.googleapis.com/auth/drive.file",
    "drive": "https://www.googleapis.com/auth/drive",
}

# 4 parallel uploads of 64 MiB chunks (~256 MiB of RAM). The daily upload quota
# of Google Drive is 750 GiB: past it rclone stops instead of retrying all day.
RCLONE_UPLOAD_FLAGS = (
    "--fast-list",
    "--transfers", "4",
    "--checkers", "8",
    "--drive-chunk-size", "64M",
    "--drive-stop-on-upload-limit",
    "--retries", "3",
    "--low-level-retries", "10",
    "--stats", "5m",
    "--log-level", "INFO",
)
# Only finished vzdump files: *.dat is an archive still being written and
# *.tmp a staging directory of suspend-mode container backups.
BACKUP_FILTERS = (
    "--filter", "- *.dat",
    "--filter", "- *.tmp/**",
    "--filter", "+ vzdump-*",
    "--filter", "- *",
)

NOTIFY_PERL = r"""
use strict;
use warnings;
use Encode qw(decode);
use PVE::Notify;

my ($severity, $summary, $details) = map { decode('UTF-8', $_) } @ARGV;
my $data = PVE::Notify::common_template_data();
$data->{summary} = $summary;
$data->{details} = $details;
PVE::Notify::notify($severity, 'pve-gdrive-backup', $data, {
    type => 'gdrive-backup',
    hostname => $data->{hostname},
});
"""


class ToolError(RuntimeError):
    """A fatal, user-facing error."""


def log(message: str) -> None:
    print(f"==> {message}", flush=True)


# --------------------------------------------------------------------------
# Command helpers
# --------------------------------------------------------------------------


def run(args: Sequence[str], input_text: str | None = None, check: bool = True) -> str:
    """Run a command, returning stdout; raise ToolError with stderr on failure."""
    result = subprocess.run(list(args), input=input_text, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise ToolError(f"command failed: {shlex.join(args)}\n{details}")
    return result.stdout


def pvesh(method: str, path: str, **params: Any) -> Any:
    """Call the Proxmox API through pvesh and decode its JSON output."""
    args = ["pvesh", method, path, "--output-format", "json"]
    for key, value in params.items():
        if value is not None:
            args += ["--" + key.replace("_", "-"), str(value)]
    output = run(args).strip()
    return json.loads(output) if output else None


def local_node_name() -> str:
    """Proxmox node names are the short hostname."""
    return socket.gethostname().split(".", 1)[0]


def atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(temporary, "w") as handle:
        os.fchmod(handle.fileno(), mode)
        handle.write(content)
    os.replace(temporary, path)


def human_size(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


def parse_key_value_file(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE file without any shell expansion."""
    values: dict[str, str] = {}
    for number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise ToolError(f"{path}:{number}: expected KEY=VALUE")
        values[key.strip()] = value.strip()
    return values


@dataclass(frozen=True)
class Settings:
    """What to upload where, as stored in CONFIG_PATH."""

    storage: str = ""
    rclone_config: str = ""
    drive_remote: str = ""
    folder: str = ""
    encrypt: bool = False
    mode: str = "mirror"  # mirror | copy
    use_trash: bool = True
    bwlimit: str = ""
    notify_failure: bool = True
    notify_success: bool = False
    previous_hook: str = ""
    chain_previous_hook: bool = False

    @property
    def drive_folder(self) -> str:
        return f"{self.drive_remote}:{self.folder}"

    @property
    def destination(self) -> str:
        """rclone path mirroring the storage's dump/ directory."""
        if self.encrypt:
            return f"{CRYPT_REMOTE}:dump"
        return f"{self.drive_remote}:{self.folder}/dump" if self.folder else f"{self.drive_remote}:dump"

    @classmethod
    def load(cls) -> "Settings":
        if not CONFIG_PATH.exists():
            raise ToolError(f"{CONFIG_PATH} not found: run '{TOOL_NAME} setup' first")
        values = parse_key_value_file(CONFIG_PATH)

        def flag(key: str, default: bool) -> bool:
            return values.get(key, "yes" if default else "no").lower() in ("yes", "true", "1")

        settings = cls(
            storage=values.get("STORAGE", ""),
            rclone_config=values.get("RCLONE_CONFIG", ""),
            drive_remote=values.get("DRIVE_REMOTE", ""),
            folder=values.get("FOLDER", "").strip("/"),
            encrypt=flag("ENCRYPT", False),
            mode=values.get("MODE", "mirror"),
            use_trash=flag("USE_TRASH", True),
            bwlimit=values.get("BWLIMIT", ""),
            notify_failure=flag("NOTIFY_ON_FAILURE", True),
            notify_success=flag("NOTIFY_ON_SUCCESS", False),
            previous_hook=values.get("PREVIOUS_HOOK", ""),
            chain_previous_hook=flag("CHAIN_PREVIOUS_HOOK", False),
        )
        if not (settings.storage and settings.rclone_config and settings.drive_remote):
            raise ToolError(f"{CONFIG_PATH} is incomplete: run '{TOOL_NAME} setup' again")
        if settings.mode not in ("mirror", "copy"):
            raise ToolError(f"{CONFIG_PATH}: MODE must be mirror or copy")
        return settings

    def save(self) -> None:
        def yes_no(value: bool) -> str:
            return "yes" if value else "no"

        timestamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        content = f"""\
# Written by '{TOOL_NAME} setup' on {timestamp}.
# Re-run the setup to change it; the vzdump hook reads STORAGE and
# PREVIOUS_HOOK/CHAIN_PREVIOUS_HOOK directly from this file.

# Backup storage whose dump/ directory is uploaded.
STORAGE={self.storage}
# rclone configuration file and Google Drive remote.
RCLONE_CONFIG={self.rclone_config}
DRIVE_REMOTE={self.drive_remote}
# Folder on Drive: the backups go to <FOLDER>/dump.
FOLDER={self.folder}
# Encrypt through the rclone crypt remote {CRYPT_REMOTE}.
ENCRYPT={yes_no(self.encrypt)}
# mirror: delete from Drive what was pruned locally; copy: never delete.
MODE={self.mode}
# Deleted files go to the Drive trash (yes) or are deleted permanently (no).
USE_TRASH={yes_no(self.use_trash)}
# rclone --bwlimit value, empty for unlimited.
BWLIMIT={self.bwlimit}
NOTIFY_ON_FAILURE={yes_no(self.notify_failure)}
NOTIFY_ON_SUCCESS={yes_no(self.notify_success)}
# Hook script configured before this tool: restored on uninstall and,
# with CHAIN_PREVIOUS_HOOK=yes, run after this tool's hook.
PREVIOUS_HOOK={self.previous_hook}
CHAIN_PREVIOUS_HOOK={yes_no(self.chain_previous_hook)}
"""
        atomic_write(CONFIG_PATH, content, 0o644)


def load_previous_settings() -> Settings | None:
    try:
        return Settings.load() if CONFIG_PATH.exists() else None
    except ToolError:
        return None


# --------------------------------------------------------------------------
# Proxmox: storages, jobs, vzdump.conf, notifications
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BackupStorage:
    storage_id: str
    storage_type: str
    path: Path | None
    active: bool
    shared: bool

    @property
    def dump_dir(self) -> Path:
        assert self.path is not None
        return self.path / "dump"


def backup_storages() -> list[BackupStorage]:
    """Storages enabled on this node that can hold vzdump backups."""
    storages = []
    for entry in pvesh("get", f"/nodes/{local_node_name()}/storage", content="backup") or []:
        config = pvesh("get", f"/storage/{entry['storage']}") or {}
        path = config.get("path")
        storages.append(
            BackupStorage(
                storage_id=entry["storage"],
                storage_type=entry.get("type", ""),
                path=Path(path) if path else None,
                active=bool(entry.get("active")),
                shared=bool(entry.get("shared")),
            )
        )
    return sorted(storages, key=lambda storage: storage.storage_id)


def storage_dump_dir(storage_id: str) -> Path:
    config = pvesh("get", f"/storage/{storage_id}") or {}
    if not config.get("path"):
        raise ToolError(f"storage {storage_id} does not exist or is not a directory storage")
    return Path(config["path"]) / "dump"


def storage_is_active(storage_id: str) -> bool:
    status = pvesh("get", f"/nodes/{local_node_name()}/storage/{storage_id}/status") or {}
    return bool(status.get("active"))


def read_vzdump_option(key: str) -> str | None:
    if not VZDUMP_CONF.exists():
        return None
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*:\s*(.*?)\s*$")
    for line in VZDUMP_CONF.read_text().splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1)
    return None


def write_vzdump_option(key: str, value: str | None) -> None:
    """Set (or with None remove) one option of /etc/vzdump.conf, keeping the rest."""
    lines = VZDUMP_CONF.read_text().splitlines() if VZDUMP_CONF.exists() else []
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*:")
    output, written = [], False
    for line in lines:
        if pattern.match(line):
            if value is not None and not written:
                output.append(f"{key}: {value}")
                written = True
            continue
        output.append(line)
    if value is not None and not written:
        output.append(f"{key}: {value}")
    atomic_write(VZDUMP_CONF, "\n".join(output) + "\n", 0o644)


def backup_jobs_for(storage_id: str) -> list[dict[str, Any]]:
    """Scheduled backup jobs that run on this node and write to the storage."""
    default_storage = read_vzdump_option("storage") or "local"
    node = local_node_name()
    jobs = []
    for job in pvesh("get", "/cluster/backup") or []:
        if job.get("node") not in (None, node):
            continue
        if job.get("storage", default_storage) == storage_id:
            jobs.append(job)
    return jobs


def describe_job(job: dict[str, Any]) -> str:
    state = "" if job.get("enabled", 1) else ", disabled"
    return f"{job.get('id', '?')} ({job.get('schedule', 'no schedule')}{state})"


def send_notification(severity: str, summary: str, details: str) -> None:
    """Send through the Proxmox notification system (Datacenter > Notifications)."""
    run(["perl", "-e", NOTIFY_PERL, severity, summary, details])


# --------------------------------------------------------------------------
# Backups on both sides
# --------------------------------------------------------------------------


def is_backup_name(name: str) -> bool:
    return name.startswith("vzdump-") and not name.endswith(".dat")


def list_local_backups(dump_dir: Path) -> dict[str, int]:
    """Finished vzdump files in the dump directory, by name, with their size."""
    files = {}
    for entry in os.scandir(dump_dir):
        if entry.is_file(follow_symlinks=False) and is_backup_name(entry.name):
            files[entry.name] = entry.stat().st_size
    return files


def list_remote_files(rclone_config: str, path: str) -> list[dict[str, Any]] | None:
    """Files in an rclone directory, or None if the directory does not exist."""
    result = subprocess.run(
        ["rclone", "--config", rclone_config, "lsjson", "--files-only", "--no-mimetype", path],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 3:  # rclone: directory not found
        return None
    if result.returncode != 0:
        raise ToolError(f"cannot list {path}:\n{result.stderr.strip()}")
    return json.loads(result.stdout or "[]")


def list_remote_backups(settings: Settings) -> dict[str, int]:
    files = list_remote_files(settings.rclone_config, settings.destination) or []
    return {entry["Name"]: entry["Size"] for entry in files if is_backup_name(entry["Name"])}


# --------------------------------------------------------------------------
# rclone configuration
# --------------------------------------------------------------------------


def default_rclone_config_path() -> Path:
    """rclone's own default, so that plain `rclone` commands see the same remotes."""
    output = run(["rclone", "config", "file"]).strip().splitlines()
    return Path(output[-1].strip())


def read_rclone_config(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False, default_section="\0")
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    if path.exists():
        text = path.read_text()
        if text.lstrip().startswith("# Encrypted rclone configuration File"):
            raise ToolError(f"{path} is encrypted: this tool needs an unencrypted rclone configuration")
        parser.read_string(text)
    return parser


def write_rclone_remote(path: Path, name: str, values: dict[str, str]) -> None:
    """Create or replace one remote, keeping every other section of the file."""
    parser = read_rclone_config(path)
    if parser.has_section(name):
        parser.remove_section(name)
    parser.add_section(name)
    for key, value in values.items():
        parser.set(name, key, value)
    buffer = io.StringIO()
    parser.write(buffer)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write(path, buffer.getvalue(), 0o600)


def drive_remotes(parser: configparser.ConfigParser) -> list[str]:
    return [name for name in parser.sections() if parser.get(name, "type", fallback="") == "drive"]


def describe_drive_remote(section: configparser.SectionProxy) -> str:
    scope = section.get("scope", "") or "drive"
    client_id = section.get("client_id", "")
    client = f"OAuth client {client_id[:16]}..." if client_id else "rclone's shared OAuth client"
    return f"scope {scope}, {client}"


def rclone_obscure(secret: str) -> str:
    return run(["rclone", "obscure", "-"], input_text=secret).strip()


def rclone_reveal(obscured: str) -> str:
    return run(["rclone", "reveal", obscured]).strip()


def drive_usage(rclone_config: str, remote: str) -> dict[str, int]:
    output = run(["rclone", "--config", rclone_config, "about", "--json", f"{remote}:"])
    return json.loads(output)


def describe_usage(usage: dict[str, int]) -> str:
    parts = [f"{human_size(usage.get('used', 0))} used"]
    if usage.get("total"):
        parts[0] += f" of {human_size(usage['total'])}"
    if "free" in usage:
        parts.append(f"{human_size(usage['free'])} free")
    if usage.get("trashed"):
        parts.append(f"{human_size(usage['trashed'])} in the trash")
    return ", ".join(parts)


# --------------------------------------------------------------------------
# Google OAuth
# --------------------------------------------------------------------------


def http_json(url: str, form: dict[str, str] | None = None, token: str | None = None) -> dict[str, Any]:
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    request = urllib.request.Request(url, data=data)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            error = json.loads(body)
            if isinstance(error.get("error"), dict):
                message = error["error"].get("message", body)
            else:
                message = f"{error.get('error')}: {error.get('error_description', '')}".strip(": ")
        except ValueError:
            message = body
        raise ToolError(f"{url}: HTTP {exc.code}: {message}") from None
    except urllib.error.URLError as exc:
        raise ToolError(f"{url}: {exc.reason}") from None


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    client_secret: str


def code_from_redirect(text: str, state: str) -> str | None:
    """Extract the authorization code from a pasted redirect URL (or a bare code)."""
    text = text.strip()
    if not text:
        return None
    if "code=" not in text and "error=" not in text:
        return text if re.fullmatch(r"[\w/.-]{10,}", text) else None
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(text).query or text.partition("?")[2])
    if "error" in params:
        raise ToolError(f"Google refused the authorization: {params['error'][0]}")
    if params.get("state", [state])[0] != state:
        print("  That address belongs to an older attempt: use the link printed above.")
        return None
    return params.get("code", [None])[0]


def wait_for_authorization_code(state: str) -> str:
    """Wait for the OAuth redirect on the loopback port or for a pasted address."""
    received: dict[str, str] = {}
    done = threading.Event()

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if params.get("state", [""])[0] != state:
                self.reply(400, "This authorization link is stale: use the one printed in the terminal.")
                return
            if "error" in params:
                received["error"] = params["error"][0]
                self.reply(400, f"Authorization refused ({received['error']}). Go back to the terminal.")
            else:
                received["code"] = params.get("code", [""])[0]
                self.reply(200, "Authorization received: you can close this tab and go back to the terminal.")
            done.set()

        def reply(self, status: int, message: str) -> None:
            body = f"<!doctype html><title>{TOOL_NAME}</title><p>{message}</p>".encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = None
    try:
        server = HTTPServer(("127.0.0.1", OAUTH_PORT), CallbackHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    except OSError:
        print(f"  (port {OAUTH_PORT} is busy on this node: paste the address when asked)")

    try:
        print("Paste the address here (or just wait if the browser says it is done): ", end="", flush=True)
        while not done.is_set():
            ready, _, _ = select.select([sys.stdin], [], [], 0.5)
            if not ready:
                continue
            line = sys.stdin.readline()
            if not line:
                raise ToolError("standard input closed while waiting for the authorization")
            code = code_from_redirect(line, state)
            if code:
                return code
            print("Paste the whole address from the browser's address bar: ", end="", flush=True)
    finally:
        if server:
            server.shutdown()
            server.server_close()

    print("\n  Authorization received from the browser.")
    if "error" in received:
        raise ToolError(f"Google refused the authorization: {received['error']}")
    return received["code"]


def authorize(client: OAuthClient, scope: str) -> dict[str, Any]:
    """Run the OAuth consent flow and return Google's token response."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(
        {
            "client_id": client.client_id,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "response_type": "code",
            "scope": DRIVE_SCOPES[scope],
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    paragraph(
        f"""
        Open this link in a browser, sign in with the Google account that should
        hold the backups and allow access:

          {url}

        * "Google hasn't verified this app": it is your own app, click
          "Advanced" and then "Go to <app name> (unsafe)".
        * If Google shows a checkbox for Drive access, tick it.
        * At the end the browser is sent to {OAUTH_REDIRECT_URI}:
          - through ./remote-deploy.sh (SSH port forwarding) this completes
            by itself;
          - otherwise the page fails to load: copy the whole address from the
            browser's address bar and paste it here.
        """
    )
    code = wait_for_authorization_code(state)
    response = http_json(
        GOOGLE_TOKEN_URL,
        form={
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": OAUTH_REDIRECT_URI,
        },
    )
    if not response.get("refresh_token"):
        raise ToolError(
            "Google returned no refresh token. Remove the app's access at "
            "https://myaccount.google.com/connections and run the setup again."
        )
    if DRIVE_SCOPES[scope] not in response.get("scope", "").split():
        raise ToolError("access to Google Drive was not granted: tick the Drive checkbox on the consent screen")
    return response


def rclone_token(response: dict[str, Any]) -> str:
    """Google's token response in the JSON format rclone stores."""
    expiry = datetime.datetime.now().astimezone() + datetime.timedelta(seconds=int(response.get("expires_in", 3600)))
    token = {
        "access_token": response["access_token"],
        "token_type": response.get("token_type", "Bearer"),
        "refresh_token": response["refresh_token"],
        "expiry": expiry.isoformat(),
    }
    return json.dumps(token, separators=(",", ":"))


def describe_account(access_token: str) -> str:
    """Name and email of the signed-in account; only informative, so never fatal."""
    try:
        user = http_json(DRIVE_ABOUT_URL, token=access_token).get("user", {})
    except ToolError:
        return "your Google account"
    return f"{user.get('displayName', '?')} <{user.get('emailAddress', '?')}>"


# --------------------------------------------------------------------------
# Wizard helpers
# --------------------------------------------------------------------------

BOLD, RESET = ("\033[1m", "\033[0m") if sys.stdout.isatty() else ("", "")


def heading(text: str) -> None:
    print(f"\n{BOLD}== {text}{RESET}\n")


def paragraph(text: str) -> None:
    print(textwrap.dedent(text).strip("\n") + "\n")


def ask(prompt: str, default: str = "", validate: Callable[[str], str | None] | None = None) -> str:
    """Ask for a value; validate returns an error message or None."""
    while True:
        suffix = f" [{default}]" if default else ""
        value = input(f"{prompt}{suffix}: ").strip() or default
        error = validate(value) if validate else None
        if error is None:
            return value
        print(f"  {error}")


def ask_yes_no(prompt: str, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        answer = input(f"{prompt} [{hint}]: ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes", "s", "si", "sì"):
            return True
        if answer in ("n", "no"):
            return False
        print("  answer y or n")


def choose(prompt: str, options: Sequence[str], default: int = 0) -> int:
    """Pick one of the options; returns its 0-based index."""
    for number, label in enumerate(options, start=1):
        print(f"  {number}) {label}")
    while True:
        answer = input(f"{prompt} [{default + 1}]: ").strip() or str(default + 1)
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1
        print(f"  choose a number from 1 to {len(options)}")


def show_file_head(path: Path, lines: int = 20) -> None:
    try:
        content = path.read_text(errors="replace").splitlines()
    except OSError as exc:
        print(f"  (cannot read it: {exc.strerror})")
        return
    for line in content[:lines]:
        print(f"  | {line}")
    if len(content) > lines:
        print(f"  | ... ({len(content) - lines} more lines)")


# --------------------------------------------------------------------------
# Wizard steps
# --------------------------------------------------------------------------

STEPS = 6


def step_prerequisites() -> None:
    heading(f"Step 1/{STEPS} - Prerequisites")
    if shutil.which("rclone"):
        print(f"rclone: {run(['rclone', 'version']).splitlines()[0]} ({shutil.which('rclone')})")
        return
    paragraph(
        """
        rclone is not installed. It does the actual transfers to Google Drive
        (resumable, checksummed, retried).
        """
    )
    if not ask_yes_no("Install it from the Debian repositories (apt-get install rclone)?", True):
        raise ToolError("rclone is required")
    for command in (["apt-get", "update"], ["apt-get", "install", "-y", "rclone"]):
        if subprocess.run(command, check=False).returncode != 0:
            raise ToolError(f"{shlex.join(command)} failed")
    print(f"\nrclone: {run(['rclone', 'version']).splitlines()[0]}")


def step_storage(previous: Settings | None) -> BackupStorage:
    heading(f"Step 2/{STEPS} - Backup storage to upload")
    storages = backup_storages()
    usable = [storage for storage in storages if storage.path]
    for storage in storages:
        if not storage.path:
            print(f"  (skipping {storage.storage_id}: {storage.storage_type} storages are not plain directories)")
    if not usable:
        raise ToolError(
            "no directory-based backup storage on this node: add one in Datacenter > Storage "
            "with content 'VZDump backup file'"
        )

    jobs_by_storage = {storage.storage_id: backup_jobs_for(storage.storage_id) for storage in usable}
    labels = []
    for storage in usable:
        details = []
        if storage.active and storage.dump_dir.is_dir():
            local = list_local_backups(storage.dump_dir)
            details.append(f"{len(local)} files, {human_size(sum(local.values()))}")
        elif not storage.active:
            details.append("NOT ACTIVE")
        jobs = jobs_by_storage[storage.storage_id]
        details.append("jobs: " + (", ".join(describe_job(job) for job in jobs) or "none"))
        labels.append(f"{storage.storage_id}  {storage.dump_dir}  ({'; '.join(details)})")
    # Default: the storage configured before, else the first one a job writes to.
    ids = [storage.storage_id for storage in usable]
    if previous and previous.storage in ids:
        default = ids.index(previous.storage)
    else:
        default = next((index for index, storage_id in enumerate(ids) if jobs_by_storage[storage_id]), 0)
    print("Backup storages on this node:")
    storage = usable[choose("Storage", labels, default)]

    if not storage.active:
        print(f"\n  WARNING: {storage.storage_id} is not active right now; uploads fail until it is.")
    if storage.shared:
        print(
            f"\n  NOTE: {storage.storage_id} is shared by several nodes: set up the upload"
            " on one node only, or they will fight over the same Drive folder."
        )
    jobs = jobs_by_storage[storage.storage_id]
    if not jobs:
        print(
            f"\n  NOTE: no backup job writes to {storage.storage_id} yet. Uploads start at the end"
            " of every backup to it, scheduled or manual."
        )
    for job in jobs:
        if job.get("script"):
            print(
                f"\n  WARNING: job {job['id']} has its own hook script ({job['script']}), which replaces"
                " the one in /etc/vzdump.conf: its backups will not trigger an upload."
            )
    return storage


def ask_oauth_client(parser: configparser.ConfigParser) -> OAuthClient:
    clients: dict[str, tuple[str, str]] = {}
    for name in drive_remotes(parser):
        client_id = parser.get(name, "client_id", fallback="")
        client_secret = parser.get(name, "client_secret", fallback="")
        if client_id and client_secret and client_id not in clients:
            clients[client_id] = (name, client_secret)
    if clients:
        print("Which Google OAuth client should the new remote use?")
        options = [
            f"reuse the OAuth client of remote '{name}' ({client_id[:16]}...)"
            for client_id, (name, _) in clients.items()
        ]
        options.append("use another OAuth client")
        choice = choose("OAuth client", options)
        print()
        if choice < len(clients):
            client_id = list(clients)[choice]
            return OAuthClient(client_id, clients[client_id][1])

    paragraph(
        """
        Google needs an OAuth client to let rclone use your Drive. Create your
        own (free, about 5 minutes): rclone's shared client is heavily rate
        limited. In the Google Cloud console, check that the project selected at
        the top of the page is the right one at every step.

         1. Create a project (any name, e.g. "pve-backup"):
              https://console.cloud.google.com/projectcreate
         2. Enable the Google Drive API in it:
              https://console.cloud.google.com/apis/library/drive.googleapis.com
         3. Configure the consent screen ("Get started"): any app name, your
            email as support and contact address, audience "External":
              https://console.cloud.google.com/auth/overview
         4. Publish the app, otherwise Google revokes the login after 7 days:
              https://console.cloud.google.com/auth/audience  ->  "Publish app"
            No verification is needed for your own account: Google only shows an
            "unverified app" warning when you sign in.
         5. Create the OAuth client, application type "Desktop app":
              https://console.cloud.google.com/auth/clients/create
            and copy its Client ID and Client secret.
        """
    )
    client_id = ask(
        "Client ID",
        validate=lambda value: None
        if value.endswith(".apps.googleusercontent.com")
        else "a client ID ends with .apps.googleusercontent.com",
    )
    client_secret = ask("Client secret", validate=lambda value: None if value else "required")
    return OAuthClient(client_id, client_secret)


def ask_scope() -> str:
    print("How much of your Drive may this node access?")
    options = [
        "drive.file (recommended): only the files and folders this tool creates. A\n"
        "     compromised node cannot read or delete anything else in your Drive. Let the\n"
        "     tool create the backup folder: one you create by hand stays invisible to it.",
        "drive: your whole Drive. Only needed to upload into a folder that already\n"
        "     exists and was not created with this OAuth client.",
    ]
    return ("drive.file", "drive")[choose("Access", options)]


def verify_drive_remote(rclone_config: Path, name: str) -> bool:
    try:
        usage = drive_usage(str(rclone_config), name)
    except ToolError as exc:
        print(f"\n  Cannot access remote '{name}':\n    " + str(exc).replace("\n", "\n    "))
        return False
    print(f"  Remote '{name}' works: {describe_usage(usage)}.")
    return True


def connect_new_remote(rclone_config: Path, parser: configparser.ConfigParser) -> str:
    client = ask_oauth_client(parser)
    print()
    scope = ask_scope()
    response = authorize(client, scope)
    print(f"\n  Connected to Google Drive as {describe_account(response['access_token'])}.\n")

    existing = set(parser.sections())
    default = next(name for name in ["gdrive"] + [f"gdrive{n}" for n in itertools.count(2)] if name not in existing)

    def validate(value: str) -> str | None:
        if not REMOTE_NAME_PATTERN.match(value):
            return "use letters, digits, '_', '-' and '.'"
        if value == CRYPT_REMOTE:
            return f"{CRYPT_REMOTE} is reserved for the encryption layer"
        return None

    while True:
        name = ask("Name of the new rclone remote", default, validate)
        if name not in existing or ask_yes_no(f"  Remote '{name}' exists: replace it?", False):
            break
    write_rclone_remote(
        rclone_config,
        name,
        {
            "type": "drive",
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "scope": scope,
            "token": rclone_token(response),
            "team_drive": "",
        },
    )
    print(f"  Saved remote '{name}' in {rclone_config}.")
    if not verify_drive_remote(rclone_config, name):
        raise ToolError(f"the new remote '{name}' does not work")
    return name


def reauthorize_remote(rclone_config: Path, parser: configparser.ConfigParser, name: str) -> None:
    section = parser[name]
    if not (section.get("client_id") and section.get("client_secret")):
        raise ToolError(f"remote '{name}' uses rclone's shared client: connect a new remote instead")
    client = OAuthClient(section["client_id"], section["client_secret"])
    scope = section.get("scope", "") or "drive"
    if scope not in DRIVE_SCOPES:
        raise ToolError(f"remote '{name}' uses scope {scope!r}, which this tool cannot renew: use 'rclone config reconnect {name}:'")
    response = authorize(client, scope)
    print(f"\n  Connected to Google Drive as {describe_account(response['access_token'])}.")
    write_rclone_remote(rclone_config, name, {**dict(section), "token": rclone_token(response)})
    if not verify_drive_remote(rclone_config, name):
        raise ToolError(f"remote '{name}' still does not work")


def step_google_account(previous: Settings | None) -> tuple[Path, str]:
    heading(f"Step 3/{STEPS} - Google account")
    rclone_config = Path(previous.rclone_config) if previous else default_rclone_config_path()
    parser = read_rclone_config(rclone_config)
    remotes = drive_remotes(parser)
    if not remotes:
        print(f"No Google Drive remote in {rclone_config} yet: let's connect your account.\n")
        return rclone_config, connect_new_remote(rclone_config, parser)

    print(f"Google Drive remotes in {rclone_config}:")
    options = [f"use '{name}' ({describe_drive_remote(parser[name])})" for name in remotes]
    options.append("connect a Google account as a new remote")
    default = remotes.index(previous.drive_remote) if previous and previous.drive_remote in remotes else 0
    choice = choose("Remote", options, default)
    if choice == len(remotes):
        print()
        return rclone_config, connect_new_remote(rclone_config, parser)

    name = remotes[choice]
    if not verify_drive_remote(rclone_config, name):
        if not ask_yes_no(f"Sign in again for '{name}' (keeps its OAuth client and scope)?", True):
            raise ToolError(f"remote '{name}' does not work")
        reauthorize_remote(rclone_config, parser, name)
    return rclone_config, name


@dataclass(frozen=True)
class Destination:
    folder: str
    encrypt: bool
    crypt_values: dict[str, str] | None  # the crypt remote to write, if encrypting
    new_passwords: tuple[str, str] | None  # shown to the user once installed


def validate_folder(value: str) -> str | None:
    folder = value.strip("/")
    if not folder:
        return "use a folder, not the root of your Drive"
    if any(part in ("", ".", "..") for part in folder.split("/")):
        return "invalid folder path"
    return None


def step_destination(previous: Settings | None, rclone_config: Path, remote: str, node: str) -> Destination:
    heading(f"Step 4/{STEPS} - Folder on Google Drive and encryption")
    parser = read_rclone_config(rclone_config)
    crypt = parser[CRYPT_REMOTE] if parser.has_section(CRYPT_REMOTE) else None
    paragraph(
        """
        The storage's dump/ directory is mirrored to <folder>/dump on Drive. Use
        a different folder for every node that uploads its backups.
        """
    )
    default_folder = previous.folder if previous and previous.drive_remote == remote else f"Backups/Proxmox/{node}"

    while True:
        folder = ask("Folder on Google Drive", default_folder, validate_folder).strip("/")
        files = list_remote_files(str(rclone_config), f"{remote}:{folder}/dump")
        plain = [entry for entry in files or [] if is_backup_name(entry["Name"]) and not entry["Name"].endswith(".bin")]
        encrypted = [entry for entry in files or [] if entry["Name"].endswith(".bin")]
        if files is None:
            print(f"  {folder}/dump does not exist yet: it will be created.")
        else:
            print(f"  {folder}/dump already has {len(files)} files ({human_size(sum(e['Size'] for e in files))}):")
            print(f"  {len(plain)} unencrypted backups, {len(encrypted)} encrypted, {len(files) - len(plain) - len(encrypted)} others.")
            print("  Files that are already there are not uploaded again.")
        crypt_matches = crypt is not None and crypt.get("remote", "") == f"{remote}:{folder}"
        if encrypted and not crypt_matches:
            print(
                f"\n  The encrypted backups there were not made with the crypt remote {CRYPT_REMOTE}"
                f" of {rclone_config}: without their passwords they would look up to date but"
                " be unreadable. Choose another folder, or restore the old crypt remote first."
            )
            continue
        break

    print()
    paragraph(
        """
        Encryption (rclone crypt) makes Google store only ciphertext. File names
        stay readable (vzdump-qemu-100-<date>.vma.zst.bin), so you can still pick
        the backup to restore. Without the password the backups are lost, so it
        must also be stored somewhere safe outside this node.
        """
    )
    if previous and previous.drive_remote == remote and previous.folder == folder:
        default_encrypt = previous.encrypt
    else:
        default_encrypt = bool(encrypted) or not plain
    encrypt = ask_yes_no("Encrypt the backups?", default_encrypt)
    if encrypt and plain:
        print(f"  The {len(plain)} unencrypted backups on Drive will be uploaded again, encrypted.")
    if not encrypt and encrypted:
        print(f"  The {len(encrypted)} encrypted backups on Drive will be uploaded again, unencrypted.")
    if not encrypt:
        return Destination(folder, False, None, None)

    values = {
        "type": "crypt",
        "remote": f"{remote}:{folder}",
        "filename_encryption": "off",
        "directory_name_encryption": "false",
    }
    if crypt is not None and crypt.get("password"):
        # Reuse the existing passwords: anything already encrypted stays readable.
        values.update(password=crypt["password"], password2=crypt.get("password2", ""))
        print(f"  Reusing the encryption passwords of the rclone remote {CRYPT_REMOTE}.")
        return Destination(folder, True, values, None)
    passwords = (secrets.token_urlsafe(32), secrets.token_urlsafe(32))
    values.update(password=rclone_obscure(passwords[0]), password2=rclone_obscure(passwords[1]))
    print("  New encryption passwords will be generated and shown at the end.")
    return Destination(folder, True, values, passwords)


def validate_bwlimit(value: str) -> str | None:
    if not value:
        return None
    result = subprocess.run(
        ["rclone", "--config", os.devnull, "--bwlimit", value, "listremotes"],
        capture_output=True,
        text=True,
        check=False,
    )
    return None if result.returncode == 0 else "rclone does not accept this limit (see https://rclone.org/docs/#bwlimit-bandwidth-spec)"


def step_upload_options(previous: Settings | None) -> Settings:
    heading(f"Step 5/{STEPS} - Upload behaviour")
    defaults = previous or Settings()
    print("When the backup job prunes an old backup:")
    mirror = choose(
        "Mode",
        [
            "mirror (recommended): delete it from Drive too, Drive keeps the same backups",
            "copy: keep it on Drive, which then grows until you clean it up yourself",
        ],
        0 if defaults.mode == "mirror" else 1,
    ) == 0
    use_trash = defaults.use_trash
    if mirror:
        print("\nBackups deleted from Drive:")
        use_trash = choose(
            "Deletion",
            [
                "go to the Drive trash: recoverable for 30 days, but they use your quota until then",
                "are deleted permanently: the space is freed immediately",
            ],
            0 if defaults.use_trash else 1,
        ) == 0

    print()
    bwlimit = ask(
        "Upload bandwidth limit, e.g. 30M (MiB/s) or a timetable like '08:00,5M 23:00,off' (empty: unlimited)",
        defaults.bwlimit,
        validate_bwlimit,
    )

    print()
    paragraph(
        """
        Notifications go through the Proxmox notification system (Datacenter >
        Notifications), to the same targets as your backup jobs: email, webhooks...
        """
    )
    notify_failure = ask_yes_no("Notify when an upload fails?", defaults.notify_failure)
    notify_success = ask_yes_no("Notify also when an upload succeeds?", defaults.notify_success)
    return replace(
        defaults,
        mode="mirror" if mirror else "copy",
        use_trash=use_trash,
        bwlimit=bwlimit,
        notify_failure=notify_failure,
        notify_success=notify_success,
    )


def step_hook(previous: Settings | None) -> tuple[str, bool]:
    """Decide what happens to a hook script already set in /etc/vzdump.conf."""
    current = read_vzdump_option("script")
    if current == str(HOOK_PATH):
        return (previous.previous_hook, previous.chain_previous_hook) if previous else ("", False)
    if not current:
        return "", False
    print(f"/etc/vzdump.conf already runs a hook script, {current}:")
    show_file_head(Path(current))
    choice = choose(
        "What should happen to it",
        [
            "replace it: it stops running (it is restored if you uninstall this tool)",
            "keep it: it runs after this tool's hook, with the same arguments",
            "abort the setup",
        ],
    )
    if choice == 2:
        raise ToolError("aborted, nothing installed")
    return current, choice == 1


# --------------------------------------------------------------------------
# Installation
# --------------------------------------------------------------------------


def install_files() -> None:
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    if TOOL_DIR != INSTALL_DIR:
        for name in INSTALLED_FILES:
            shutil.copyfile(TOOL_DIR / name, INSTALL_DIR / name)
        shutil.copytree(TOOL_DIR / TEMPLATE_SOURCE_DIR, INSTALL_DIR / TEMPLATE_SOURCE_DIR, dirs_exist_ok=True)
    for name in ("pve_gdrive_backup.py", "vzdump-hook.sh"):
        (INSTALL_DIR / name).chmod(0o755)
    if COMMAND_PATH.is_symlink() or COMMAND_PATH.exists():
        COMMAND_PATH.unlink()
    COMMAND_PATH.symlink_to(INSTALL_DIR / "pve_gdrive_backup.py")

    shutil.copyfile(INSTALL_DIR / SERVICE_NAME, UNIT_PATH)
    run(["systemctl", "daemon-reload"])

    # pmxcfs allows neither chmod nor the metadata copy of shutil.copy2.
    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    for template in sorted((INSTALL_DIR / TEMPLATE_SOURCE_DIR).glob("*.hbs")):
        target = TEMPLATE_DIR / template.name
        content = template.read_text()
        if not target.exists() or target.read_text() != content:
            target.write_text(content)


def trigger_upload() -> None:
    PENDING_FLAG.touch()
    run(["systemctl", "start", "--no-block", SERVICE_NAME])


def show_new_passwords(passwords: tuple[str, str], settings: Settings) -> None:
    heading("IMPORTANT: save the encryption passwords")
    paragraph(
        f"""
        Store these two values in a password manager now. They are also in
        {settings.rclone_config} (obfuscated, not encrypted), but if this node
        is lost they are the only way to decrypt the backups on Drive.

          password:  {passwords[0]}
          password2: {passwords[1]}

        To restore from any machine with rclone: add the Google Drive remote,
        then a "crypt" remote with remote = <drive remote>:{settings.folder},
        filename_encryption = off, directory_name_encryption = false and these
        two passwords ("Yes, type in my own password").
        """
    )
    while input('Type "saved" once you have stored them: ').strip().lower() != "saved":
        pass


def cmd_setup(_args: argparse.Namespace) -> None:
    previous = load_previous_settings()
    node = local_node_name()
    action = "Changing the existing configuration" if previous else "First-time setup"
    paragraph(
        f"""
        {BOLD}{TOOL_NAME}{RESET}: upload the backups of node {node} to Google Drive.
        {action}: nothing is installed before the final confirmation, Ctrl+C aborts.
        """
    )
    step_prerequisites()
    storage = step_storage(previous)
    rclone_config, drive_remote = step_google_account(previous)
    destination = step_destination(previous, rclone_config, drive_remote, node)
    options = step_upload_options(previous)

    heading(f"Step {STEPS}/{STEPS} - Review and install")
    previous_hook, chain = step_hook(previous)
    settings = replace(
        options,
        storage=storage.storage_id,
        rclone_config=str(rclone_config),
        drive_remote=drive_remote,
        folder=destination.folder,
        encrypt=destination.encrypt,
        previous_hook=previous_hook,
        chain_previous_hook=chain,
    )
    deletion = "" if settings.mode == "copy" else (", deleted files go to the trash" if settings.use_trash else ", deleted files are removed permanently")
    notify = [kind for kind, enabled in (("failures", settings.notify_failure), ("successes", settings.notify_success)) if enabled]
    hook = str(HOOK_PATH)
    if previous_hook:
        hook += f" (then {previous_hook})" if chain else f" (replaces {previous_hook})"
    print()
    for label, value in (
        ("Storage", f"{storage.storage_id} ({storage.dump_dir})"),
        ("Google Drive", f"{settings.drive_folder}/dump" + (f", encrypted through {CRYPT_REMOTE}" if settings.encrypt else "")),
        ("Mode", settings.mode + deletion),
        ("Bandwidth", settings.bwlimit or "unlimited"),
        ("Notifications", " and ".join(notify) or "none"),
        ("vzdump hook", hook),
    ):
        print(f"  {label + ':':<15}{value}")
    print()
    if not ask_yes_no("Install with these settings?", True):
        raise ToolError("aborted, nothing installed")

    install_files()
    if destination.crypt_values:
        write_rclone_remote(rclone_config, CRYPT_REMOTE, destination.crypt_values)
    settings.save()
    write_vzdump_option("script", str(HOOK_PATH))
    log(f"installed; configuration in {CONFIG_PATH}")
    if destination.new_passwords:
        show_new_passwords(destination.new_passwords, settings)

    print()
    if ask_yes_no("Upload the current backups now?", True):
        trigger_upload()
        print(f"Upload started in the background: journalctl -fu {TOOL_NAME} to follow it.")
    print(f"\nFrom now on every backup to {storage.storage_id} is uploaded when its job ends.")
    print(f"Check it any time with '{TOOL_NAME} status'.")
    if notify:
        print(f"Try the notifications with '{TOOL_NAME} test-notification'.")


# --------------------------------------------------------------------------
# Upload (run by the service)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class UploadResult:
    ok: bool
    message: str
    log_tail: str
    seconds: float


def build_rclone_command(settings: Settings, dump_dir: Path) -> list[str]:
    command = [
        "rclone",
        "sync" if settings.mode == "mirror" else "copy",
        str(dump_dir),
        settings.destination,
        "--config", settings.rclone_config,
        *RCLONE_UPLOAD_FLAGS,
        *BACKUP_FILTERS,
        f"--drive-use-trash={'true' if settings.use_trash else 'false'}",
    ]
    if settings.bwlimit:
        command += ["--bwlimit", settings.bwlimit]
    return command


def upload_once(settings: Settings) -> UploadResult:
    started = time.monotonic()
    tail: collections.deque[str] = collections.deque(maxlen=25)
    try:
        dump_dir = storage_dump_dir(settings.storage)
        if not storage_is_active(settings.storage):
            raise ToolError(f"storage {settings.storage} is not active")
        if not dump_dir.is_dir():
            raise ToolError(f"{dump_dir} does not exist")
        if settings.mode == "mirror" and not list_local_backups(dump_dir) and list_remote_backups(settings):
            raise ToolError(
                f"{dump_dir} has no backups but {settings.destination} has: refusing to mirror an empty"
                " directory, it would delete every backup on Drive. Check the storage."
            )
        command = build_rclone_command(settings, dump_dir)
        log("running " + shlex.join(command))
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            if line.strip():
                tail.append(line.rstrip())
        code = process.wait()
        if code != 0:
            raise ToolError(f"rclone exited with code {code}")
    except ToolError as exc:
        return UploadResult(False, str(exc), "\n".join(tail), time.monotonic() - started)
    return UploadResult(True, "upload completed", "\n".join(tail), time.monotonic() - started)


def notify_result(settings: Settings, result: UploadResult) -> None:
    if not (settings.notify_failure if not result.ok else settings.notify_success):
        return
    node = local_node_name()
    outcome = "completed" if result.ok else "FAILED"
    details = (
        f"{result.message}\n\n"
        f"Storage:     {settings.storage}\n"
        f"Destination: {settings.destination}\n"
        f"Duration:    {datetime.timedelta(seconds=round(result.seconds))}\n\n"
        f"Last rclone output:\n{result.log_tail or '(none)'}\n\n"
        f"Full log: journalctl -u {TOOL_NAME}"
    )
    try:
        send_notification("info" if result.ok else "error", f"Backup upload to Google Drive {outcome} on {node}", details)
    except ToolError as exc:
        log(f"WARNING: cannot send the notification: {exc}")


def cmd_upload(_args: argparse.Namespace) -> int:
    settings = Settings.load()
    with open(LOCK_PATH, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            PENDING_FLAG.touch()
            log("another upload is running: it will upload again when done")
            return 0
        failed = False
        while True:
            PENDING_FLAG.unlink(missing_ok=True)
            result = upload_once(settings)
            log(("" if result.ok else "ERROR: ") + result.message)
            notify_result(settings, result)
            failed = failed or not result.ok
            if not PENDING_FLAG.exists():
                break
            log("another backup job ended during the upload: uploading again")
    return 1 if failed else 0


# --------------------------------------------------------------------------
# Other commands
# --------------------------------------------------------------------------


def cmd_run(_args: argparse.Namespace) -> None:
    Settings.load()
    trigger_upload()
    print(f"Upload started in the background: journalctl -fu {TOOL_NAME} to follow it.")


def cmd_test_notification(_args: argparse.Namespace) -> None:
    node = local_node_name()
    send_notification(
        "info",
        f"{TOOL_NAME} test notification from {node}",
        "If you read this, failed backup uploads to Google Drive will reach you here.",
    )
    print("Test notification sent: check your notification targets (Datacenter > Notifications).")


def service_state() -> str:
    output = run(
        ["systemctl", "show", SERVICE_NAME, "-p", "ActiveState", "-p", "ExecMainStartTimestamp",
         "-p", "ExecMainExitTimestamp", "-p", "ExecMainStatus"],
        check=False,
    )
    state = dict(line.partition("=")[::2] for line in output.splitlines())
    if state.get("ActiveState") == "activating":
        return f"uploading since {state.get('ExecMainStartTimestamp')}"
    if not state.get("ExecMainStartTimestamp"):
        return "never ran since boot"
    result = "succeeded" if state.get("ExecMainStatus") == "0" else f"FAILED (exit {state.get('ExecMainStatus')})"
    return f"{result}, {state.get('ExecMainStartTimestamp')} -> {state.get('ExecMainExitTimestamp')}"


def cmd_status(_args: argparse.Namespace) -> None:
    settings = Settings.load()
    dump_dir = storage_dump_dir(settings.storage)
    active = storage_is_active(settings.storage)
    hook = read_vzdump_option("script")
    jobs = backup_jobs_for(settings.storage)

    rows = [
        ("Storage", f"{settings.storage} ({dump_dir}){'' if active else ', NOT ACTIVE'}"),
        ("Destination", settings.destination + (f" -> {settings.drive_folder}, encrypted" if settings.encrypt else "")),
        ("Mode", settings.mode + ("" if settings.mode == "copy" else (", deletions to the trash" if settings.use_trash else ", permanent deletions"))),
        ("Bandwidth", settings.bwlimit or "unlimited"),
        ("vzdump hook", "installed" if hook == str(HOOK_PATH) else f"NOT INSTALLED (script: {hook or 'none'}): run setup again"),
        ("Backup jobs", ", ".join(describe_job(job) for job in jobs) or "none, only manual backups are uploaded"),
        ("Last upload", service_state()),
    ]
    if active and dump_dir.is_dir():
        local = list_local_backups(dump_dir)
        rows.append(("Local", f"{len(local)} files, {human_size(sum(local.values()))}"))
        try:
            remote = list_remote_backups(settings)
            to_upload = [name for name, size in local.items() if remote.get(name) != size]
            to_delete = [name for name in remote if name not in local] if settings.mode == "mirror" else []
            rows.append(("On Drive", f"{len(remote)} files, {human_size(sum(remote.values()))}"))
            rows.append(("Sync", "up to date" if not (to_upload or to_delete) else f"{len(to_upload)} to upload, {len(to_delete)} to delete"))
            rows.append(("Drive account", describe_usage(drive_usage(settings.rclone_config, settings.drive_remote))))
        except ToolError as exc:
            rows.append(("On Drive", f"ERROR: {exc}"))
    for label, value in rows:
        print(f"{label + ':':<15}{value}")


def cmd_uninstall(args: argparse.Namespace) -> None:
    settings = load_previous_settings()
    if read_vzdump_option("script") == str(HOOK_PATH):
        restore = settings.previous_hook if settings else ""
        # vzdump fails every backup whose hook script is missing.
        if restore and not os.access(restore, os.X_OK):
            log(f"WARNING: the previous hook {restore} no longer exists, not restoring it")
            restore = ""
        write_vzdump_option("script", restore or None)
        log(f"/etc/vzdump.conf: {'restored hook ' + restore if restore else 'removed the hook'}")
    run(["systemctl", "stop", SERVICE_NAME], check=False)
    UNIT_PATH.unlink(missing_ok=True)
    run(["systemctl", "daemon-reload"])
    if COMMAND_PATH.is_symlink():
        COMMAND_PATH.unlink()
    shutil.rmtree(INSTALL_DIR, ignore_errors=True)
    PENDING_FLAG.unlink(missing_ok=True)
    log("service, hook and program removed")
    if args.purge:
        CONFIG_PATH.unlink(missing_ok=True)
        for template in TEMPLATE_DIR.glob(f"{TEMPLATE_NAME}-*.hbs"):
            template.unlink()
        log(f"removed {CONFIG_PATH} and the notification templates (shared by the cluster)")
    else:
        log(f"kept {CONFIG_PATH} (use --purge to remove it)")
    rclone_config = settings.rclone_config if settings else "the rclone configuration"
    log(f"kept the rclone remotes in {rclone_config} and the backups on Google Drive")


def require_root_on_pve() -> None:
    if os.geteuid() != 0:
        raise ToolError("run as root on a Proxmox VE node")
    if not Path("/usr/bin/pvesh").exists():
        raise ToolError("pvesh not found: this is not a Proxmox VE node")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=TOOL_NAME, description="Upload Proxmox VE backups to Google Drive.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, handler, help_text in (
        ("setup", cmd_setup, "interactive wizard: install or reconfigure"),
        ("status", cmd_status, "show the configuration, the last upload and what is pending"),
        ("run", cmd_run, "upload now, in the background"),
        ("test-notification", cmd_test_notification, "send a test notification"),
        ("uninstall", cmd_uninstall, "remove hook, service and program"),
        ("upload", cmd_upload, "upload in the foreground (used by the service)"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.set_defaults(handler=handler)
        if name == "uninstall":
            command.add_argument("--purge", action="store_true", help="also remove the configuration and templates")

    args = parser.parse_args(argv)
    try:
        require_root_on_pve()
        return args.handler(args) or 0
    except ToolError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\naborted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
