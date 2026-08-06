"""Build a secret-free, capability-restricted MyHermes config per Trial."""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from myhermes_audit.artifacts import atomic_write_text
from myhermes_audit.environment import (
    MODEL_ENVIRONMENT_ALLOWLIST,
    SUITE_ENVIRONMENT_ALLOWLIST,
)
from myhermes_audit.errors import ConfigBuildError, ReportError


_MAX_CONFIG_BYTES = 2 * 1024 * 1024
_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_EXACT_ENV_REFERENCE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_SENSITIVE_CONFIG_SUFFIXES = (
    "api_key",
    "apikey",
    "password",
    "secret",
    "token",
)
_ALLOWED_CONFIG_REFERENCES = (
    MODEL_ENVIRONMENT_ALLOWLIST | SUITE_ENVIRONMENT_ALLOWLIST
)


@dataclass(frozen=True, slots=True)
class PreparedMyHermesConfig:
    document: dict[str, Any]
    environment_references: tuple[str, ...]


class MyHermesConfigBuilder:
    def __init__(self, base_config_path: Path) -> None:
        requested = Path(base_config_path).expanduser()
        if requested.is_symlink():
            raise ConfigBuildError("subject config must not be a symbolic link")
        self.base_config_path = requested.resolve(strict=False)
        self._base_document = self._load_base_config(self.base_config_path)

    @staticmethod
    def _load_base_config(path: Path) -> dict[str, Any]:
        if path.is_symlink():
            raise ConfigBuildError("subject config must not be a symbolic link")
        try:
            stat = path.stat()
        except OSError as exc:
            raise ConfigBuildError("cannot stat subject config") from exc
        if not path.is_file():
            raise ConfigBuildError("subject config is not a regular file")
        if stat.st_size > _MAX_CONFIG_BYTES:
            raise ConfigBuildError("subject config exceeds the size limit")
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ConfigBuildError("cannot safely load subject config") from exc
        if not isinstance(raw, dict):
            raise ConfigBuildError("subject config root must be a mapping")
        _validate_plain_config_value(raw, path="<root>")
        return copy.deepcopy(raw)

    def prepare(self, overrides: Mapping[str, Any]) -> PreparedMyHermesConfig:
        if not isinstance(overrides, Mapping):
            raise ConfigBuildError("config_overrides must be a mapping")
        override_document = dict(overrides)
        _reject_capability_reenable(override_document)
        document = copy.deepcopy(self._base_document)
        _deep_merge_existing(document, override_document, path="")
        _enforce_isolated_runtime_boundary(document)
        _reject_literal_secrets(document, path="<root>")
        references = _collect_environment_references(document)
        unsupported = sorted(set(references) - _ALLOWED_CONFIG_REFERENCES)
        if unsupported:
            raise ConfigBuildError(
                "config references environment names outside the worker allowlist",
                references=unsupported,
            )
        return PreparedMyHermesConfig(
            document=document,
            environment_references=references,
        )

    def write(
        self,
        destination: Path,
        overrides: Mapping[str, Any],
    ) -> PreparedMyHermesConfig:
        prepared = self.prepare(overrides)
        self.write_prepared(destination, prepared)
        return prepared

    def write_prepared(
        self,
        destination: Path,
        prepared: PreparedMyHermesConfig,
    ) -> None:
        """Publish the exact prepared document already used for identity resolution."""

        if not isinstance(prepared, PreparedMyHermesConfig):
            raise ConfigBuildError("prepared config has an invalid type")
        _validate_plain_config_value(prepared.document, path="<root>")
        _reject_literal_secrets(prepared.document, path="<root>")
        target = Path(destination)
        if target.is_symlink() or target.parent.is_symlink():
            raise ConfigBuildError("generated config path must not be a symlink")
        try:
            text = yaml.safe_dump(
                prepared.document,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=True,
            )
            atomic_write_text(target, text, mode=0o600)
        except (OSError, ReportError) as exc:
            raise ConfigBuildError("cannot publish generated MyHermes config") from exc


def _validate_plain_config_value(value: object, *, path: str) -> None:
    if value is None or type(value) in (str, int, bool):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ConfigBuildError("config numbers must be finite", path=path)
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _validate_plain_config_value(item, path=f"{path}[{index}]")
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or not key:
                raise ConfigBuildError(
                    "config mapping keys must be non-empty strings",
                    path=path,
                )
            child = f"{path}.{key}" if path != "<root>" else key
            _validate_plain_config_value(item, path=child)
        return
    raise ConfigBuildError(
        "config contains a non-JSON value",
        path=path,
        value_type=type(value).__name__,
    )


def _deep_merge_existing(
    base: dict[str, Any],
    overrides: dict[str, Any],
    *,
    path: str,
) -> None:
    for key, override in overrides.items():
        child_path = f"{path}.{key}" if path else key
        if key not in base:
            raise ConfigBuildError(
                "config override introduces an unknown path",
                path=child_path,
            )
        current = base[key]
        current_is_mapping = isinstance(current, dict)
        override_is_mapping = isinstance(override, dict)
        if current_is_mapping and override_is_mapping:
            _deep_merge_existing(current, override, path=child_path)
        elif current_is_mapping != override_is_mapping:
            raise ConfigBuildError(
                "config override changes a mapping shape",
                path=child_path,
            )
        else:
            _validate_plain_config_value(override, path=child_path)
            base[key] = copy.deepcopy(override)


def _mapping_section(document: dict[str, Any], name: str) -> dict[str, Any]:
    value = document.get(name)
    if value is None:
        value = {}
        document[name] = value
    if not isinstance(value, dict):
        raise ConfigBuildError(
            "required MyHermes config section must be a mapping",
            path=name,
        )
    return value


def _reject_capability_reenable(document: dict[str, Any]) -> None:
    background = document.get("background_review")
    if isinstance(background, dict) and background.get("enabled") not in (None, False):
        raise ConfigBuildError(
            "background_review cannot be enabled in the foreground configuration"
        )
    browser = document.get("browser")
    if isinstance(browser, dict) and browser.get("enabled") not in (None, False):
        raise ConfigBuildError("browser cannot be enabled by the isolated Audit runtime")
    plugins = document.get("plugins")
    if isinstance(plugins, dict):
        if plugins.get("enabled") not in (None, []):
            raise ConfigBuildError("plugins cannot be enabled by the isolated Audit runtime")
        if plugins.get("search_paths") not in (None, []):
            raise ConfigBuildError(
                "plugin search paths are not supported by the isolated Audit runtime"
            )
        if plugins.get("enable_project_plugins") not in (None, False):
            raise ConfigBuildError(
                "project plugins cannot be enabled by the isolated Audit runtime"
            )


def _enforce_isolated_runtime_boundary(document: dict[str, Any]) -> None:
    _mapping_section(document, "background_review")["enabled"] = False
    _mapping_section(document, "browser")["enabled"] = False
    plugins = _mapping_section(document, "plugins")
    plugins["enabled"] = []
    plugins["search_paths"] = []
    plugins["enable_project_plugins"] = False


def _is_sensitive_config_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    return any(
        normalized == suffix or normalized.endswith(f"_{suffix}")
        for suffix in _SENSITIVE_CONFIG_SUFFIXES
    )


def _reject_literal_secrets(value: object, *, path: str) -> None:
    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_literal_secrets(item, path=f"{path}[{index}]")
        return
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        child_path = f"{path}.{key}" if path != "<root>" else key
        if _is_sensitive_config_key(key):
            if item in (None, ""):
                continue
            if not isinstance(item, str) or _EXACT_ENV_REFERENCE.fullmatch(item) is None:
                raise ConfigBuildError(
                    "literal secret material is forbidden in generated config",
                    path=child_path,
                )
        if isinstance(item, (dict, list)):
            _reject_literal_secrets(item, path=child_path)


def _collect_environment_references(value: object) -> tuple[str, ...]:
    found: dict[str, None] = {}

    def visit(item: object) -> None:
        if isinstance(item, str):
            for match in _ENV_REFERENCE.finditer(item):
                found.setdefault(match.group(1), None)
        elif isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return tuple(found)


__all__ = ("MyHermesConfigBuilder", "PreparedMyHermesConfig")
