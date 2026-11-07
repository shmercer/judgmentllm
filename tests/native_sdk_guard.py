"""Fail-closed subprocess guards for isolated, synthetic SDK converters only.

Install before importing an SDK. Constructors and credential entrypoints are
then protected immediately after import. Every protected entrypoint self-tests.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, cast

_CREDENTIAL_KEYS = {
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_CONFIG_FILE",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "TOGETHER_API_KEY",
    "OLLAMA_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "GCE_METADATA_HOST",
    "GCE_METADATA_IP",
    "GOOGLE_AUTH_TOKEN_FOR_TESTING",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_CONFIG_FILE",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
}


class OfflineBlocked(RuntimeError):
    """A controlled message never includes intercepted arguments."""


class OfflineGuards:
    def __init__(self) -> None:
        self.checked: list[str] = []
        self.events = 0
        self.profile: Callable[[Any, str, Any], None] | None = None

    def deny(self, *_args: Any, **_kwargs: Any) -> Any:
        self.events += 1
        raise OfflineBlocked("Offline converter operation blocked")

    def check(self, label: str, operation: Callable[[], Any]) -> None:
        try:
            operation()
        except OfflineBlocked:
            self.checked.append(label)
        else:
            raise AssertionError("Offline guard ineffective: " + label)

    def protect_client(self, cls: type[Any]) -> None:
        cls.__init__ = self.deny
        self.check("client_constructor", lambda: cls())

    def protect_callable(self, module: ModuleType, name: str) -> None:
        setattr(module, name, self.deny)
        self.check(
            "credential_or_transport_entrypoint", lambda: getattr(module, name)()
        )

    def assert_intact(self) -> None:
        """Detect swallowed blocked operations and disabled profile callbacks."""
        if self.events != len(self.checked) or sys.getprofile() is not self.profile:
            raise AssertionError("Offline converter guard integrity failure")

    def self_test(self) -> None:
        self.check("socket", lambda: socket.socket())
        self.check("dns", lambda: socket.getaddrinfo("native-guard.invalid", 80))
        self.check(
            "subprocess",
            lambda: subprocess.run(["native-guard-invalid-command"], check=True),
        )
        # Trigger only the audit events: never attempt a real exec or fork even
        # when testing whether the guard is ineffective.
        for event in ("os.exec", "os.fork", "os.forkpty"):
            self.check("process_audit", lambda event=event: sys.audit(event))
        self.check(
            "write", lambda: Path("native-guard-write-canary").write_bytes(b"forbidden")
        )
        self.check("mkdir", lambda: Path("native-guard-mkdir-canary").mkdir())
        for key in sorted(_CREDENTIAL_KEYS):
            self.check("credential_environment", lambda key=key: os.environ.get(key))
        if sys.platform != "win32" and os.supports_bytes_environ:
            self.check(
                "credential_bytes_environment",
                lambda: os.environb.get(b"ANTHROPIC_AUTH_TOKEN"),
            )
            self.check(
                "credential_getenvb", lambda: os.getenvb(b"AWS_SECRET_ACCESS_KEY")
            )
        self.check(
            "credential_file",
            lambda: Path("application_default_credentials.json").read_bytes(),
        )
        self.check(
            "cloud_metadata", lambda: socket.getaddrinfo("metadata.google.internal", 80)
        )


def install_guards() -> OfflineGuards:
    sys.dont_write_bytecode = True
    state = OfflineGuards()
    original_getitem = cast(
        Callable[[Any, str | bytes], Any], type(os.environ).__getitem__
    )

    def guarded_getitem(self: Any, key: str | bytes) -> Any:
        # os.environ and os.environb use the same _Environ class. Do not let a
        # bytes key bypass the string-name discovery guard on POSIX.
        normalized = os.fsdecode(key)
        if normalized in _CREDENTIAL_KEYS:
            return state.deny()
        return original_getitem(self, key)

    type(os.environ).__getitem__ = guarded_getitem

    def guard(event: str, args: tuple[Any, ...]) -> None:
        if event.startswith("socket.") or event in {
            "subprocess.Popen",
            "os.system",
            "os.posix_spawn",
            "os.spawn",
            "os.exec",
            "os.fork",
            "os.forkpty",
            "os.mkdir",
            "os.remove",
            "os.rename",
            "os.rmdir",
            "os.link",
            "os.symlink",
            "os.truncate",
            "os.chmod",
            "os.chown",
        }:
            state.deny()
        if event == "open":
            path, mode, flags = args[:3]
            if isinstance(path, (str, bytes)):
                normalized = os.fsdecode(path).replace("\\", "/").lower()
                if normalized.rsplit("/", 1)[-1] in {
                    ".env",
                    ".netrc",
                    "application_default_credentials.json",
                    "credentials.json",
                    "service_account.json",
                } or any(
                    part in normalized
                    for part in (
                        "/.config/gcloud/",
                        "/.aws/",
                        "/.anthropic/",
                        "/.claude/",
                    )
                ):
                    state.deny()
            if (isinstance(mode, str) and any(char in mode for char in "wax+")) or (
                isinstance(flags, int)
                and flags
                & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            ):
                state.deny()

    blocked_client_modules = {
        "anthropic._client",
        "together._client",
        "ollama._client",
        "google.genai.client",
        "google.genai._api_client",
        "httpx._client",
        "httpx2._client",
    }

    def profile(frame: Any, event: str, _arg: Any) -> None:
        if event != "call":
            return
        module = frame.f_globals.get("__name__", "") or ""
        name = frame.f_code.co_name
        if name == "__init__" and module in blocked_client_modules:
            state.deny()
        if module.startswith(("google.auth", "google.oauth2")) and name in {
            "default",
            "load_credentials_from_file",
            "load_credentials_from_dict",
            "refresh",
            "_get_gcloud_sdk_credentials",
            "_get_gce_credentials",
            "_get_explicit_environ_credentials",
        }:
            state.deny()
        if module == "google.auth.compute_engine._metadata" and name in {
            "get",
            "ping",
            "is_on_gce",
            "get_service_account_info",
        }:
            state.deny()

    sys.addaudithook(guard)
    state.profile = profile
    sys.setprofile(profile)
    # Compiled stubs exercise the pre-import interception without loading clients.
    for module, code, entrypoint in [
        (name, "def __init__(): pass", "__init__")
        for name in sorted(blocked_client_modules)
    ] + [
        ("google.auth._default", "def default(): pass", "default"),
        ("google.auth.compute_engine._metadata", "def get(): pass", "get"),
    ]:
        namespace: dict[str, Any] = {"__name__": module}
        exec(compile(code, "<offline-guard-self-test>", "exec"), namespace)
        state.check("preimport_client_auth_metadata", namespace[entrypoint])
        # A raised profile callback disables profiling; reinstall after self-test.
        sys.setprofile(profile)
    state.self_test()
    sys.setprofile(profile)
    state.assert_intact()
    return state
