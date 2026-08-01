"""Read-only subprocess probe for the public MyHermes surface used by Audit."""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import re
from pathlib import Path
from typing import Callable, Sequence

from pydantic import ValidationError

from myhermes_audit.artifacts import atomic_write_json
from myhermes_audit.integrations.myhermes.capability_contracts import (
    CAPABILITY_PROTOCOL_VERSION,
    SubjectCapabilityCheck,
    SubjectCapabilityProbeError,
    SubjectCapabilityProbeRequest,
    SubjectCapabilityReport,
    SubjectCapabilityWarning,
)
from myhermes_audit.serialization import canonical_sha256


_MAX_REQUEST_BYTES = 256 * 1024
_MEMORY_ADDRESS = re.compile(r"0x[0-9A-Fa-f]+")
_BIND_PLACEHOLDER = object()
_RUN_CONVERSATION_KEYWORDS = (
    "session_key",
    "enabled_toolsets",
    "tool_context",
    "tool_policy",
    "hook_registry",
)


class _ProjectedDefault:
    def __repr__(self) -> str:
        return "<default>"


_PROJECTED_DEFAULT = _ProjectedDefault()


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="myhermes-audit-capability-probe")
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    return parser.parse_args(argv)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def _load_request(path: Path) -> SubjectCapabilityProbeRequest:
    if path.is_symlink() or not path.is_file():
        raise ValueError("probe request must be a regular file")
    if path.stat().st_size > _MAX_REQUEST_BYTES:
        raise ValueError("probe request exceeds the size limit")
    text = path.read_text(encoding="utf-8")
    json.loads(
        text,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_unique_json_object,
    )
    return SubjectCapabilityProbeRequest.model_validate_json(text)


def _safe_signature(signature: inspect.Signature) -> str:
    parameters = [
        parameter
        if parameter.default is inspect.Parameter.empty
        else parameter.replace(default=_PROJECTED_DEFAULT)
        for parameter in signature.parameters.values()
    ]
    rendered = str(signature.replace(parameters=parameters))
    if len(rendered) > 4096 or _MEMORY_ADDRESS.search(rendered):
        raise ValueError("public signature is not stable")
    return rendered


def _bind_run_conversation_worker_call(signature: inspect.Signature) -> None:
    signature.bind(
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
        **{
            name: _BIND_PLACEHOLDER
            for name in _RUN_CONVERSATION_KEYWORDS
        },
    )


class _ProbeBuilder:
    def __init__(self) -> None:
        self.checks: list[SubjectCapabilityCheck] = []
        self.warnings: list[SubjectCapabilityWarning] = []
        self.api_entries: list[dict[str, str | None]] = []

    def check(
        self,
        name: str,
        module_name: str,
        object_name: str,
        predicate: Callable[[object], bool] | None = None,
        signature_validator: Callable[[inspect.Signature], None] | None = None,
    ) -> object | None:
        value: object | None = None
        available = False
        signature: str | None = None
        failure_type: str | None = None
        try:
            module = importlib.import_module(module_name)
        except Exception:
            failure_type = "module_unavailable"
        else:
            try:
                value = (
                    module
                    if object_name == "<module>"
                    else getattr(module, object_name)
                )
            except AttributeError:
                failure_type = "symbol_missing"
            except Exception:
                failure_type = "symbol_unavailable"
        if failure_type is None:
            inspected_signature: inspect.Signature | None = None
            if callable(value):
                try:
                    inspected_signature = inspect.signature(value)
                    signature = _safe_signature(inspected_signature)
                except (TypeError, ValueError):
                    self.warnings.append(
                        SubjectCapabilityWarning(
                            warning_type=f"signature_unavailable_{name}",
                            message=f"public signature unavailable for capability {name}",
                        )
                    )
            if signature_validator is not None:
                if not callable(value):
                    failure_type = "symbol_not_callable"
                elif inspected_signature is None:
                    failure_type = "signature_unavailable"
                else:
                    try:
                        signature_validator(inspected_signature)
                    except TypeError:
                        failure_type = "call_shape_incompatible"
                    except Exception:
                        failure_type = "signature_validation_failed"
                    else:
                        available = True
                        failure_type = None
            else:
                try:
                    available = True if predicate is None else bool(predicate(value))
                except Exception:
                    available = False
                    failure_type = "capability_check_failed"
                else:
                    failure_type = (
                        None if available else "capability_incompatible"
                    )
        self.checks.append(
            SubjectCapabilityCheck(
                name=name,
                available=available,
                module=module_name,
                public_object=object_name,
                signature=signature,
                failure_type=failure_type,
            )
        )
        self.api_entries.append(
            {
                "module": module_name,
                "object": object_name,
                "signature": signature,
            }
        )
        return value if available else None

    def result_check(
        self,
        name: str,
        module_name: str,
        object_name: str,
        operation: Callable[[], bool],
    ) -> None:
        available = False
        failure_type: str | None = None
        try:
            available = bool(operation())
        except Exception:
            available = False
            failure_type = "capability_check_failed"
        else:
            if not available:
                failure_type = "capability_incompatible"
        self.checks.append(
            SubjectCapabilityCheck(
                name=name,
                available=available,
                module=module_name,
                public_object=object_name,
                failure_type=failure_type,
            )
        )
        self.api_entries.append(
            {"module": module_name, "object": object_name, "signature": None}
        )


def _has_parameters(*names: str) -> Callable[[object], bool]:
    def predicate(value: object) -> bool:
        parameters = inspect.signature(value).parameters
        return all(name in parameters for name in names)

    return predicate


def _tool_registry_surface(value: object) -> bool:
    return all(
        callable(getattr(value, name, None))
        for name in ("register", "register_declaration", "resolve")
    )


def _observation_repository_surface(value: object) -> bool:
    return all(
        callable(getattr(value, name, None))
        for name in ("list_observations", "list_run_timeline")
    )


def _public_config_surface(value: object) -> bool:
    required = (
        "BACKGROUND_REVIEW_CONFIG",
        "BROWSER_CONFIG",
        "DB_PATH",
        "HERMES_HOME",
        "client",
    )
    return all(hasattr(value, name) for name in required)


def _resolve_file_and_terminal_toolsets() -> bool:
    tools = importlib.import_module("hermes.tools")
    file_declarations = getattr(
        importlib.import_module("hermes.tool_declarations.file"),
        "TOOL_DECLARATIONS",
    )
    terminal_declarations = getattr(
        importlib.import_module("hermes.tool_declarations.terminal"),
        "TOOL_DECLARATIONS",
    )
    registry = tools.ToolRegistry()

    def never_execute(_arguments: dict, **_kwargs) -> str:
        raise RuntimeError("capability probe handlers must never execute")

    for declaration in (*file_declarations, *terminal_declarations):
        registry.register_declaration(declaration, never_execute)
    policy = tools.ToolPolicy(
        tools.ExecutionEnvironment.CLI,
        enabled_toolsets=frozenset({"file", "terminal"}),
        unattended=True,
    )
    resolution = registry.resolve(policy)
    return resolution.toolsets == frozenset({"file", "terminal"})


def _validate_subject_origin(request: SubjectCapabilityProbeRequest) -> bool:
    root = Path(request.subject_repo).expanduser().resolve(strict=True)
    hermes = importlib.import_module("hermes")
    module_file = Path(inspect.getfile(hermes)).resolve(strict=True)
    expected_file = root / "hermes" / "__init__.py"
    return module_file.is_relative_to(root) and module_file == expected_file


def _run_probe(request: SubjectCapabilityProbeRequest) -> SubjectCapabilityReport:
    builder = _ProbeBuilder()
    builder.check("hermes_package", "hermes", "<module>")
    builder.result_check(
        "hermes_subject_origin",
        "hermes",
        "__file__",
        lambda: _validate_subject_origin(request),
    )
    builder.check(
        "run_conversation",
        "hermes.conversation",
        "run_conversation",
        signature_validator=_bind_run_conversation_worker_call,
    )
    builder.check(
        "tool_registry",
        "hermes.tools",
        "ToolRegistry",
        _tool_registry_surface,
    )
    builder.check("tool_registration", "hermes.tools", "register_all", callable)
    builder.check("tool_policy", "hermes.tools", "ToolPolicy", callable)
    builder.check(
        "build_system_prompt",
        "hermes.prompt",
        "build_system_prompt",
        _has_parameters("cwd", "enabled_toolsets"),
    )
    builder.check(
        "observation_repository",
        "hermes.observability",
        "ObservationReadRepository",
        _observation_repository_surface,
    )
    for name in (
        "RunObservationView",
        "ModelCallObservationView",
        "ToolCallObservationView",
    ):
        builder.check(
            f"observation_view_{name.removesuffix('ObservationView').lower()}",
            "hermes.observability",
            name,
            callable,
        )
    builder.check(
        "database_initialization",
        "hermes.persistence.schema",
        "init_db",
        _has_parameters("db_path"),
    )
    builder.check(
        "session_creation",
        "hermes.persistence.core",
        "create_session",
        _has_parameters("conn"),
    )
    builder.check(
        "session_resource_cleanup",
        "hermes.session_resources",
        "cleanup_session_resources",
        _has_parameters("session_key"),
    )
    builder.check(
        "global_resource_cleanup",
        "hermes.session_resources",
        "cleanup_all_session_resources",
        callable,
    )
    builder.check(
        "public_config_projection",
        "hermes.config",
        "<module>",
        _public_config_surface,
    )
    builder.result_check(
        "file_terminal_toolsets",
        "hermes.tool_declarations",
        "file+terminal",
        _resolve_file_and_terminal_toolsets,
    )

    missing = [item.name for item in builder.checks if not item.available]
    fingerprint = canonical_sha256(
        {
            "protocol_version": CAPABILITY_PROTOCOL_VERSION,
            "public_api": builder.api_entries,
        }
    )
    error = (
        None
        if not missing
        else SubjectCapabilityProbeError(
            error_type="missing_public_capability",
            message="one or more required public MyHermes capabilities are missing",
        )
    )
    return SubjectCapabilityReport(
        subject_commit=request.subject_commit,
        compatible=not missing,
        capabilities=builder.checks,
        missing_capabilities=missing,
        warnings=builder.warnings,
        public_api_fingerprint=fingerprint,
        error=error,
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parse_arguments(argv)
    try:
        if arguments.result.is_symlink():
            raise ValueError("probe result cannot be a symbolic link")
        request = _load_request(arguments.request)
        report = _run_probe(request)
        atomic_write_json(arguments.result, report)
        return 0 if report.compatible else 1
    except (OSError, UnicodeError, ValueError, ValidationError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
