"""Read-only subprocess probe for the public MyHermes surface used by Audit."""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import re
from collections.abc import Mapping
from enum import Enum
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
from myhermes_audit.contracts.ablation import (
    CompressionControl,
    CompressionMode,
    MemoryMode,
)
from myhermes_audit.contracts.memory import MemoryKind, RetrievalStrategy
from myhermes_audit.contracts.background_review import ReviewKind
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
    "background_review_coordinator",
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


def _bind_memory_read(signature: inspect.Signature) -> None:
    signature.bind(target=_BIND_PLACEHOLDER)


def _bind_memory_write(signature: inspect.Signature) -> None:
    signature.bind(
        _BIND_PLACEHOLDER,
        target=_BIND_PLACEHOLDER,
        content=_BIND_PLACEHOLDER,
        old_text=_BIND_PLACEHOLDER,
    )


def _bind_memory_render(signature: inspect.Signature) -> None:
    signature.bind(
        include_long=_BIND_PLACEHOLDER,
        include_user=_BIND_PLACEHOLDER,
    )


def _bind_memory_handler(signature: inspect.Signature) -> None:
    signature.bind(_BIND_PLACEHOLDER)


def _bind_memory_register(signature: inspect.Signature) -> None:
    signature.bind(_BIND_PLACEHOLDER)


def _bind_skill_handler(signature: inspect.Signature) -> None:
    signature.bind(_BIND_PLACEHOLDER)


def _bind_skill_register(signature: inspect.Signature) -> None:
    signature.bind(_BIND_PLACEHOLDER)


def _bind_prompt_memory_toggle(signature: inspect.Signature) -> None:
    signature.bind(
        _BIND_PLACEHOLDER,
        enabled_toolsets=_BIND_PLACEHOLDER,
        include_memory=_BIND_PLACEHOLDER,
        include_user_profile=_BIND_PLACEHOLDER,
    )


def _bind_review_agent_loop(signature: inspect.Signature) -> None:
    signature.bind(
        review_messages=_BIND_PLACEHOLDER,
        review_instruction=_BIND_PLACEHOLDER,
        allowed_tool_names=_BIND_PLACEHOLDER,
        model=_BIND_PLACEHOLDER,
        max_iterations=_BIND_PLACEHOLDER,
        tools=_BIND_PLACEHOLDER,
        system_prompt=_BIND_PLACEHOLDER,
        registry=_BIND_PLACEHOLDER,
        client=_BIND_PLACEHOLDER,
        session_key=_BIND_PLACEHOLDER,
        model_kwargs=_BIND_PLACEHOLDER,
        cancel_checker=_BIND_PLACEHOLDER,
        tool_context=_BIND_PLACEHOLDER,
        hook_registry=_BIND_PLACEHOLDER,
    )


def _bind_review_observation_sink(signature: inspect.Signature) -> None:
    signature.bind(
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
        hook_id_prefix="audit-review",
    )


def _bind_foreground_review_event(signature: inspect.Signature) -> None:
    signature.bind(
        session_id=_BIND_PLACEHOLDER,
        completed=True,
        tool_batches=0,
    )


def _bind_review_tool_registration(signature: inspect.Signature) -> None:
    signature.bind(
        _BIND_PLACEHOLDER,
        process_manager=_BIND_PLACEHOLDER,
    )


def _bind_background_review_executor(signature: inspect.Signature) -> None:
    signature.bind(
        driver_registry=_BIND_PLACEHOLDER,
        config=_BIND_PLACEHOLDER,
        model=_BIND_PLACEHOLDER,
        client=_BIND_PLACEHOLDER,
        db_path=_BIND_PLACEHOLDER,
        tool_registry=_BIND_PLACEHOLDER,
    )


def _bind_background_review_config(signature: inspect.Signature) -> None:
    signature.bind(
        max_iterations=1,
        retry_cooldown_seconds=0,
        max_concurrent_jobs=1,
        max_pending_jobs=0,
    )


def _bind_background_review_coordinator(signature: inspect.Signature) -> None:
    signature.bind(
        driver_registry=_BIND_PLACEHOLDER,
        executor=_BIND_PLACEHOLDER,
        enabled=False,
    )


def _bind_session_message_range(signature: inspect.Signature) -> None:
    signature.bind(
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
        after_message_id=_BIND_PLACEHOLDER,
        upto_message_id=_BIND_PLACEHOLDER,
    )


def _bind_no_arguments(signature: inspect.Signature) -> None:
    signature.bind()


def _bind_memory_review_driver(signature: inspect.Signature) -> None:
    signature.bind(
        store=_BIND_PLACEHOLDER,
        memory_interval=1,
        claim_ttl_seconds=1.0,
        retry_cooldown_seconds=0.0,
        max_iterations=1,
    )


def _bind_skill_review_driver(signature: inspect.Signature) -> None:
    signature.bind(
        store=_BIND_PLACEHOLDER,
        skill_tool_batch_interval=1,
        claim_ttl_seconds=1.0,
        retry_cooldown_seconds=0.0,
        max_iterations=1,
    )


def _bind_instance_method(
    value: object,
    name: str,
    *args: object,
    **kwargs: object,
) -> None:
    method = getattr(value, name, None)
    if not callable(method):
        raise TypeError(f"required public method is unavailable: {name}")
    inspect.signature(method).bind(_BIND_PLACEHOLDER, *args, **kwargs)


def _review_driver_surface(value: object) -> bool:
    if getattr(value, "kind", None) is None:
        return False
    _bind_instance_method(value, "record_progress", _BIND_PLACEHOLDER, _BIND_PLACEHOLDER)
    _bind_instance_method(value, "claim_due", _BIND_PLACEHOLDER, _BIND_PLACEHOLDER)
    _bind_instance_method(value, "validate_claim", _BIND_PLACEHOLDER)
    _bind_instance_method(
        value,
        "claim_is_valid",
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
    )
    _bind_instance_method(
        value,
        "prepare_run",
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
    )
    _bind_instance_method(value, "complete", _BIND_PLACEHOLDER, _BIND_PLACEHOLDER)
    _bind_instance_method(
        value,
        "fail",
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
        "audit_review_failure",
    )
    return True


def _memory_review_store_surface(value: object) -> bool:
    _bind_instance_method(
        value,
        "record_progress",
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
        completed_turns=1,
        message_upto=1,
    )
    _bind_instance_method(
        value,
        "claim_due",
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
        memory_interval=1,
        claim_ttl_seconds=1.0,
    )
    _bind_instance_method(
        value,
        "load_message_window",
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
        after_message_id=0,
        upto_message_id=1,
    )
    _bind_instance_method(value, "get_last_message_id", _BIND_PLACEHOLDER, _BIND_PLACEHOLDER)
    _bind_instance_method(
        value,
        "claim_is_valid",
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
    )
    _bind_instance_method(
        value,
        "complete",
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
    )
    _bind_instance_method(
        value,
        "fail",
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
        error="audit_review_failure",
        retry_cooldown_seconds=0.0,
    )
    return True


def _skill_review_store_surface(value: object) -> bool:
    _bind_instance_method(
        value,
        "record_progress",
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
        tool_batches=1,
        message_upto=1,
    )
    _bind_instance_method(
        value,
        "claim_due",
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
        skill_tool_batch_interval=1,
        claim_ttl_seconds=1.0,
    )
    _bind_instance_method(
        value,
        "load_message_window",
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
        after_message_id=0,
        upto_message_id=1,
    )
    _bind_instance_method(value, "get_last_message_id", _BIND_PLACEHOLDER, _BIND_PLACEHOLDER)
    _bind_instance_method(
        value,
        "claim_is_valid",
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
    )
    _bind_instance_method(
        value,
        "complete",
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
    )
    _bind_instance_method(
        value,
        "fail",
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
        error="audit_review_failure",
        retry_cooldown_seconds=0.0,
    )
    return True


def _review_driver_registry_surface(value: object) -> bool:
    _bind_instance_method(value, "register", _BIND_PLACEHOLDER)
    _bind_instance_method(value, "get", _BIND_PLACEHOLDER)
    _bind_instance_method(value, "enabled_drivers")
    return True


def _review_agent_loop_surface(value: object) -> bool:
    _bind_instance_method(value, "run", "")
    return True


def _background_review_coordinator_surface(value: object) -> bool:
    _bind_instance_method(
        value,
        "after_foreground_result",
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
    )
    return True


def _background_review_executor_surface(value: object) -> bool:
    _bind_instance_method(value, "shutdown", 2.0)
    return True


def _review_tool_registry_surface(value: object) -> bool:
    _bind_instance_method(value, "resolve", _BIND_PLACEHOLDER)
    _bind_instance_method(value, "get_entry", "audit_review_tool")
    _bind_instance_method(
        value,
        "register",
        "audit_review_tool",
        "audit_review_toolset",
        {},
        _BIND_PLACEHOLDER,
        execution_environments=_BIND_PLACEHOLDER,
        unattended_allowed=True,
        required_trusted_context=_BIND_PLACEHOLDER,
        approval_mode=_BIND_PLACEHOLDER,
        risk_level=_BIND_PLACEHOLDER,
        default_enabled_environments=_BIND_PLACEHOLDER,
        retry_safe=False,
        unknown_on_crash=True,
        status_check=None,
        supports_cancellation=False,
    )
    module = importlib.import_module("hermes.tools")
    entry = getattr(module, "ToolEntry", None)
    resolution = getattr(module, "ToolResolution", None)
    return (
        entry is not None
        and _dataclass_fields_surface(
            "name",
            "toolset",
            "schema",
            "handler",
            "execution_environments",
            "unattended_allowed",
            "required_trusted_context",
            "approval_mode",
            "risk_level",
            "default_enabled_environments",
            "retry_safe",
            "unknown_on_crash",
            "status_check",
            "supports_cancellation",
        )(entry)
        and resolution is not None
        and _dataclass_fields_surface(
            "definitions",
            "allowed_tool_names",
            "toolsets",
        )(resolution)
    )


def _skill_service_surface(value: object) -> bool:
    _bind_instance_method(value, "list_skills")
    _bind_instance_method(
        value,
        "create_skill",
        "audit-review-skill",
        actor=_BIND_PLACEHOLDER,
        body="audit fixture body",
        description="audit fixture",
    )
    _bind_instance_method(
        value,
        "pin_skill",
        "audit-review-skill",
        actor=_BIND_PLACEHOLDER,
        expected_revision=_BIND_PLACEHOLDER,
        expected_governance_revision=_BIND_PLACEHOLDER,
    )
    module = importlib.import_module("hermes.skills")
    actor = getattr(module, "SkillActor", None)
    return all(
        getattr(actor, name, None) is not None
        for name in ("FOREGROUND", "BACKGROUND_REVIEW", "SYSTEM")
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
        *,
        required: bool = True,
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
                        try:
                            available = (
                                True if predicate is None else bool(predicate(value))
                            )
                        except Exception:
                            available = False
                            failure_type = "capability_check_failed"
                        else:
                            failure_type = (
                                None
                                if available
                                else "capability_incompatible"
                            )
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
                required=required,
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
                "available": "yes" if available else "no",
            }
        )
        return value if available else None

    def derived_check(
        self,
        name: str,
        *,
        available: bool,
        public_object: str,
    ) -> None:
        self.checks.append(
            SubjectCapabilityCheck(
                name=name,
                required=False,
                available=available,
                module="<derived-public-capability>",
                public_object=public_object,
                failure_type=None if available else "capability_incompatible",
            )
        )
        self.api_entries.append(
            {
                "module": "<derived-public-capability>",
                "object": public_object,
                "signature": None,
                "available": "yes" if available else "no",
            }
        )

    def result_check(
        self,
        name: str,
        module_name: str,
        object_name: str,
        operation: Callable[[], bool],
        *,
        required: bool = True,
    ) -> bool:
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
                required=required,
                available=available,
                module=module_name,
                public_object=object_name,
                failure_type=failure_type,
            )
        )
        self.api_entries.append(
            {
                "module": module_name,
                "object": object_name,
                "signature": None,
                "available": "yes" if available else "no",
            }
        )
        return available

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


def _review_tool_policy_surface(value: object) -> bool:
    return (
        callable(getattr(value, "ToolPolicy", None))
        and callable(getattr(value, "ToolRegistry", None))
        and callable(getattr(getattr(value, "ToolRegistry", None), "resolve", None))
        and getattr(getattr(value, "ExecutionEnvironment", None), "BACKGROUND_REVIEW", None)
        is not None
    )


def _methods_surface(*names: str) -> Callable[[object], bool]:
    def predicate(value: object) -> bool:
        return all(callable(getattr(value, name, None)) for name in names)

    return predicate


def _dataclass_fields_surface(*names: str) -> Callable[[object], bool]:
    def predicate(value: object) -> bool:
        fields = getattr(value, "__dataclass_fields__", {})
        return all(name in fields for name in names)

    return predicate


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


def _compression_runtime_surface(value: object) -> bool:
    inspect.signature(value).bind(
        model=_BIND_PLACEHOLDER,
        max_iterations=_BIND_PLACEHOLDER,
        tools=_BIND_PLACEHOLDER,
        system_prompt=_BIND_PLACEHOLDER,
        registry=_BIND_PLACEHOLDER,
        client=_BIND_PLACEHOLDER,
        session_key=_BIND_PLACEHOLDER,
        conn=_BIND_PLACEHOLDER,
        db_session_id=_BIND_PLACEHOLDER,
        existing_messages=_BIND_PLACEHOLDER,
        max_retries=_BIND_PLACEHOLDER,
        max_continuations=_BIND_PLACEHOLDER,
        compression_threshold=_BIND_PLACEHOLDER,
    )
    pre_model_call = getattr(value, "pre_model_call", None)
    if not callable(pre_model_call):
        return False
    inspect.signature(pre_model_call).bind(
        _BIND_PLACEHOLDER,
        _BIND_PLACEHOLDER,
    )
    return True


def _bind_session_creation(signature: inspect.Signature) -> None:
    signature.bind(_BIND_PLACEHOLDER, source="cli")


def _bind_session_message_read(signature: inspect.Signature) -> None:
    signature.bind(_BIND_PLACEHOLDER, _BIND_PLACEHOLDER)


def _compression_configuration_surface(value: object) -> bool:
    names = (
        "COMPRESSION_THRESHOLD",
        "PROTECT_FIRST",
        "KEEP_RECENT_TOOL_RESULTS",
        "TAIL_TOKEN_BUDGET",
    )
    values = [getattr(value, name, None) for name in names]
    return all(type(item) is int and item >= 0 for item in values) and values[0] > 0


def _token_usage_observation_surface(value: object) -> bool:
    fields = getattr(value, "__dataclass_fields__", {})
    return all(
        name in fields
        for name in ("prompt_tokens", "completion_tokens", "total_tokens")
    )


def _context_size_observation_surface(value: object) -> bool:
    fields = getattr(value, "__dataclass_fields__", {})
    return any(
        name in fields
        for name in ("message_count", "context_tokens", "context_size")
    )


def _compression_observation_surface(value: object) -> bool:
    fields = getattr(value, "__dataclass_fields__", {})
    return all(
        name in fields
        for name in ("compression_applied", "input_message_count", "output_message_count")
    )


def _tool_declaration_surface(
    value: object,
    *,
    expected_toolsets: frozenset[str],
    expected_names: frozenset[str] | None = None,
) -> bool:
    if not isinstance(value, tuple) or not value:
        return False
    names = {getattr(item, "name", None) for item in value}
    toolsets = {getattr(item, "toolset", None) for item in value}
    if not all(
        isinstance(getattr(item, "schema", None), Mapping)
        and isinstance(getattr(item, "name", None), str)
        and isinstance(getattr(item, "toolset", None), str)
        for item in value
    ):
        return False
    return toolsets == set(expected_toolsets) and (
        expected_names is None or names == set(expected_names)
    )


def _file_declaration_surface(value: object) -> bool:
    return _tool_declaration_surface(
        value,
        expected_toolsets=frozenset({"file"}),
    )


def _terminal_declaration_surface(value: object) -> bool:
    return _tool_declaration_surface(
        value,
        expected_toolsets=frozenset({"terminal"}),
    )


def _memory_declaration_surface(value: object) -> bool:
    return _tool_declaration_surface(
        value,
        expected_toolsets=frozenset({"memory"}),
        expected_names=frozenset({"memory"}),
    )


def _skill_read_declaration_surface(value: object) -> bool:
    if not isinstance(value, tuple):
        return False
    read_declarations = tuple(
        item for item in value if getattr(item, "toolset", None) == "skill_read"
    )
    return _tool_declaration_surface(
        read_declarations,
        expected_toolsets=frozenset({"skill_read"}),
        expected_names=frozenset({"skill_view", "skills_list"}),
    )


def _process_declaration_surface(value: object) -> bool:
    """Validate only the public process Tool declaration and schema."""
    return _tool_declaration_surface(
        value,
        expected_toolsets=frozenset({"terminal"}),
        expected_names=frozenset({"process"}),
    )


def _process_action_names(value: object) -> tuple[str, ...]:
    """Read the public ``process.action`` enum without importing handlers."""

    if not _process_declaration_surface(value):
        return ()
    declaration = value[0]
    schema = getattr(declaration, "schema", {})
    parameters = schema.get("parameters", {}) if isinstance(schema, Mapping) else {}
    properties = parameters.get("properties", {}) if isinstance(parameters, Mapping) else {}
    action = properties.get("action", {}) if isinstance(properties, Mapping) else {}
    enum = action.get("enum", ()) if isinstance(action, Mapping) else ()
    if not isinstance(action, Mapping) or action.get("type") != "string":
        return ()
    if not isinstance(enum, (tuple, list)):
        return ()
    names = tuple(item for item in enum if isinstance(item, str) and item)
    return names if len(names) == len(set(names)) else ()


def _process_status_surface(value: object) -> bool:
    """Validate only the public ProcessStatus enum surface."""

    if not isinstance(value, type) or not issubclass(value, Enum):
        return False
    members = getattr(value, "__members__", {})
    if not isinstance(members, Mapping) or not members:
        return False
    values = [getattr(item, "value", None) for item in members.values()]
    return all(isinstance(item, str) and item for item in values) and len(
        values
    ) == len(set(values))


def _process_status_names(value: object) -> tuple[str, ...]:
    """Read stable values from the public ProcessStatus enum only."""

    if not _process_status_surface(value):
        return ()
    members = getattr(value, "__members__", {})
    return tuple(item.value for item in members.values())


def _terminal_supports_background(value: object) -> bool:
    if not _terminal_declaration_surface(value):
        return False
    declaration = value[0]
    schema = getattr(declaration, "schema", {})
    parameters = schema.get("parameters", {}) if isinstance(schema, Mapping) else {}
    properties = parameters.get("properties", {}) if isinstance(parameters, Mapping) else {}
    background = properties.get("background") if isinstance(properties, Mapping) else None
    return isinstance(background, Mapping) and background.get("type") == "boolean"


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
    run_conversation = builder.check(
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
    memory_prompt_toggle = builder.check(
        "memory_prompt_toggle",
        "hermes.prompt",
        "build_system_prompt",
        signature_validator=_bind_prompt_memory_toggle,
        required=False,
    )
    memory_read = builder.check(
        "memory_read",
        "hermes.tools.memory",
        "read_memory_entries",
        signature_validator=_bind_memory_read,
        required=False,
    )
    memory_write = builder.check(
        "memory_write",
        "hermes.tools.memory",
        "mutate_memory_entries",
        signature_validator=_bind_memory_write,
        required=False,
    )
    user_profile_read = builder.check(
        "user_profile_read",
        "hermes.tools.memory",
        "read_memory_entries",
        signature_validator=_bind_memory_read,
        required=False,
    )
    user_profile_write = builder.check(
        "user_profile_write",
        "hermes.tools.memory",
        "mutate_memory_entries",
        signature_validator=_bind_memory_write,
        required=False,
    )
    memory_prompt_render = builder.check(
        "memory_prompt_render",
        "hermes.tools.memory",
        "render_memory_section",
        signature_validator=_bind_memory_render,
        required=False,
    )
    memory_declaration = builder.check(
        "memory_tool_declaration",
        "hermes.tool_declarations.memory",
        "TOOL_DECLARATIONS",
        _memory_declaration_surface,
        required=False,
    )
    memory_handler = builder.check(
        "memory_tool_handler",
        "hermes.tools.memory",
        "handle_memory",
        signature_validator=_bind_memory_handler,
        required=False,
    )
    memory_registration = builder.check(
        "memory_tool_registration",
        "hermes.tools.memory",
        "register",
        signature_validator=_bind_memory_register,
        required=False,
    )
    memory_tool_available = all(
        item is not None
        for item in (memory_declaration, memory_handler, memory_registration)
    )
    builder.derived_check(
        "memory_tool",
        available=memory_tool_available,
        public_object="memory declaration+handler+registration",
    )
    compression_available = builder.check(
        "compression_available",
        "hermes.conversation",
        "ConversationAgentLoop",
        _compression_runtime_surface,
        required=False,
    )
    compression_threshold_configuration = builder.check(
        "compression_threshold_configuration",
        "hermes.config",
        "<module>",
        _compression_configuration_surface,
        required=False,
    )
    compression_threshold_control = (
        compression_available is not None
        and compression_threshold_configuration is not None
    )
    builder.derived_check(
        "compression_threshold_control",
        available=compression_threshold_control,
        public_object="ConversationAgentLoop threshold+public compression configuration",
    )
    emergency_compression_disable = builder.check(
        "emergency_compression_disable",
        "hermes.config",
        "EMERGENCY_OVERFLOW_COMPRESSION_DISABLE_SUPPORTED",
        lambda value: value is True,
        required=False,
    )
    ranked_query = builder.check(
        "ranked_query",
        "hermes.tools.memory",
        "query_memory_entries",
        callable,
        required=False,
    )
    query_scores = builder.check(
        "query_scores",
        "hermes.tools.memory",
        "query_memory_entries",
        _has_parameters("include_scores"),
        required=False,
    )
    user_filtering = builder.check(
        "user_filtering",
        "hermes.tools.memory",
        "query_memory_entries",
        _has_parameters("user_id"),
        required=False,
    )
    session_filtering = builder.check(
        "session_filtering",
        "hermes.tools.memory",
        "query_memory_entries",
        _has_parameters("session_id"),
        required=False,
    )
    query_filters = builder.check(
        "query_filters",
        "hermes.tools.memory",
        "query_memory_entries",
        _has_parameters("filters"),
        required=False,
    )
    declared_strategies = builder.check(
        "declared_retrieval_strategies",
        "hermes.tools.memory",
        "SUPPORTED_RETRIEVAL_STRATEGIES",
        lambda value: isinstance(value, (tuple, list, frozenset))
        and all(
            isinstance(item, str)
            and item in {strategy.value for strategy in RetrievalStrategy}
            for item in value
        ),
        required=False,
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
    model_observation_view = builder.check(
        "token_usage_observation",
        "hermes.observability",
        "ModelCallObservationView",
        _token_usage_observation_surface,
        required=False,
    )
    builder.check(
        "context_size_observation",
        "hermes.observability",
        "ModelCallObservationView",
        _context_size_observation_surface,
        required=False,
    )
    compression_observation = builder.check(
        "compression_observation",
        "hermes.observability",
        "ModelCallObservationView",
        _compression_observation_surface,
        required=False,
    )
    builder.check(
        "database_initialization",
        "hermes.persistence.schema",
        "init_db",
        _has_parameters("db_path"),
    )
    session_creation = builder.check(
        "session_creation",
        "hermes.persistence.core",
        "create_session",
        signature_validator=_bind_session_creation,
    )
    session_message_read = builder.check(
        "session_message_read",
        "hermes.persistence.core",
        "get_session_messages",
        signature_validator=_bind_session_message_read,
        required=False,
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
    builder.check(
        "file_tool_declaration",
        "hermes.tool_declarations.file",
        "TOOL_DECLARATIONS",
        _file_declaration_surface,
    )
    terminal_declaration = builder.check(
        "terminal_tool_declaration",
        "hermes.tool_declarations.terminal",
        "TOOL_DECLARATIONS",
        _terminal_declaration_surface,
    )
    process_declaration = builder.check(
        "process_toolset",
        "hermes.tool_declarations.process",
        "TOOL_DECLARATIONS",
        _process_declaration_surface,
        required=False,
    )
    process_status_declaration = builder.check(
        "process_status_enum",
        "hermes.processes",
        "ProcessStatus",
        _process_status_surface,
        required=False,
    )
    # The handler is deliberately not inspected as a source of foreground
    # capability.  Public Process action support comes only from the schema.
    process_actions = _process_action_names(process_declaration)
    supported_process_statuses = (
        _process_status_names(process_status_declaration)
        if process_actions
        else ()
    )
    process_toolset = (
        getattr(process_declaration[0], "toolset", None)
        if process_actions and process_declaration
        else None
    )
    process_start_via_terminal = bool(process_actions) and _terminal_supports_background(
        terminal_declaration
    )
    for action, capability_name in (
        ("log", "process_log"),
        ("poll", "process_poll"),
        ("wait", "process_wait"),
        ("write", "process_write"),
        ("submit", "process_submit"),
        ("kill", "process_kill"),
        ("close", "process_close"),
        ("interrupt", "process_interrupt"),
    ):
        builder.derived_check(
            capability_name,
            available=action in process_actions,
            public_object=f"process.action={action}",
        )
    builder.derived_check(
        "process_start_via_terminal",
        available=process_start_via_terminal,
        public_object="terminal.background",
    )
    builder.derived_check(
        "process_toolset_actions",
        available=bool(process_actions),
        public_object="terminal.process.action enum",
    )
    builder.derived_check(
        "background_process_supported",
        available=(
            process_start_via_terminal
            and {"log", "wait", "kill"}.issubset(process_actions)
        ),
        public_object="terminal.background + process.action enum",
    )
    skill_read_declaration = builder.check(
        "skill_read_toolset",
        "hermes.tool_declarations.skill",
        "TOOL_DECLARATIONS",
        _skill_read_declaration_surface,
        required=False,
    )
    skill_view_tool = builder.check(
        "skill_view_tool",
        "hermes.tools.skill",
        "handle_skill_view",
        signature_validator=_bind_skill_handler,
        required=False,
    )
    skills_list_tool = builder.check(
        "skills_list_tool",
        "hermes.tools.skill",
        "handle_skill_list",
        signature_validator=_bind_skill_handler,
        required=False,
    )
    skill_read_registration = builder.check(
        "skill_read_tool_registration",
        "hermes.tools.skill",
        "register",
        signature_validator=_bind_skill_register,
        required=False,
    )

    # P5 is deliberately optional at probe level so a Subject without Review
    # support remains compatible with P0-P4.  These checks only inspect public
    # symbols/signatures; they never instantiate stores, create a database, or
    # obtain a Review claim/token.
    review_runtime = builder.check(
        "background_review_runtime",
        "hermes.review.runtime",
        "BackgroundReviewCoordinator",
        _background_review_coordinator_surface,
        signature_validator=_bind_background_review_coordinator,
        required=False,
    )
    review_runtime_config = builder.check(
        "background_review_runtime_config",
        "hermes.review.runtime",
        "BackgroundReviewConfig",
        signature_validator=_bind_background_review_config,
        required=False,
    )
    review_claim = builder.check(
        "review_claim_contract",
        "hermes.review.contracts",
        "ReviewClaim",
        _dataclass_fields_surface("kind", "session_id", "token", "payload"),
        required=False,
    )
    review_registry = builder.check(
        "review_driver_registry",
        "hermes.review.registry",
        "ReviewDriverRegistry",
        _review_driver_registry_surface,
        signature_validator=_bind_no_arguments,
        required=False,
    )
    review_loop = builder.check(
        "review_agent_loop",
        "hermes.review.loop",
        "ReviewAgentLoop",
        _review_agent_loop_surface,
        signature_validator=_bind_review_agent_loop,
        required=False,
    )
    review_loop_result = builder.check(
        "review_loop_result_contract",
        "hermes.agent_loop",
        "AgentLoopResult",
        _dataclass_fields_surface("ok", "status", "error_type"),
        required=False,
    )
    review_hook_registry = builder.check(
        "review_hook_registry",
        "hermes.hooks",
        "SyncHookRegistry",
        signature_validator=_bind_no_arguments,
        required=False,
    )
    review_observation_sink = builder.check(
        "review_observation_sink",
        "hermes.persistence.observation",
        "configure_sqlite_observation_sink",
        signature_validator=_bind_review_observation_sink,
        required=False,
    )
    review_policy = builder.check(
        "review_tool_policy",
        "hermes.tools",
        "<module>",
        _review_tool_policy_surface,
        required=False,
    )
    review_tool_resolution = builder.check(
        "review_tool_registry_resolution",
        "hermes.tools",
        "ToolRegistry",
        _review_tool_registry_surface,
        signature_validator=_bind_no_arguments,
        required=False,
    )
    review_tool_registration = builder.check(
        "review_tool_registration",
        "hermes.tools",
        "register_all",
        signature_validator=_bind_review_tool_registration,
        required=False,
    )
    memory_review_driver = builder.check(
        "memory_review_driver",
        "hermes.review.memory",
        "MemoryReviewDriver",
        _review_driver_surface,
        signature_validator=_bind_memory_review_driver,
        required=False,
    )
    skill_review_driver = builder.check(
        "skill_review_driver",
        "hermes.review.skill",
        "SkillReviewDriver",
        _review_driver_surface,
        signature_validator=_bind_skill_review_driver,
        required=False,
    )
    memory_review_store = builder.check(
        "memory_review_store",
        "hermes.review.memory_store",
        "MemoryReviewStore",
        _memory_review_store_surface,
        signature_validator=_bind_no_arguments,
        required=False,
    )
    skill_review_store = builder.check(
        "skill_review_store",
        "hermes.review.skill_store",
        "SkillReviewStore",
        _skill_review_store_surface,
        signature_validator=_bind_no_arguments,
        required=False,
    )
    review_run_spec = builder.check(
        "review_evidence_window",
        "hermes.review.contracts",
        "ReviewRunSpec",
        _dataclass_fields_surface(
            "messages",
            "system_prompt",
            "instruction",
            "tool_policy",
            "max_iterations",
            "tool_context",
        ),
        required=False,
    )
    foreground_review_event = builder.check(
        "review_foreground_event",
        "hermes.review.contracts",
        "ForegroundReviewEvent",
        _dataclass_fields_surface("session_id", "completed", "tool_batches"),
        signature_validator=_bind_foreground_review_event,
        required=False,
    )
    foreground_review_window = builder.check(
        "review_foreground_evidence_window",
        "hermes.persistence.core",
        "get_session_messages_in_id_range",
        signature_validator=_bind_session_message_range,
        required=False,
    )
    skill_service = builder.check(
        "review_state_snapshot",
        "hermes.skills",
        "SkillService",
        _skill_service_surface,
        signature_validator=_bind_no_arguments,
        required=False,
    )
    governance_revision = builder.check(
        "skill_governance_revision",
        "hermes.skills.governance",
        "SkillDescriptor",
        _dataclass_fields_surface(
            "skill_id",
            "name",
            "source",
            "managed_by",
            "pinned",
            "revision",
            "governance_revision",
        ),
        required=False,
    )
    review_executor = builder.check(
        "review_shutdown",
        "hermes.review.runtime",
        "BackgroundReviewExecutor",
        _background_review_executor_surface,
        signature_validator=_bind_background_review_executor,
        required=False,
    )
    any_review_driver = any(
        item is not None for item in (memory_review_driver, skill_review_driver)
    )
    builder.derived_check(
        "review_claim_validation",
        available=any_review_driver,
        public_object="public ReviewDriver claim validation",
    )
    builder.derived_check(
        "review_claim_completion",
        available=any_review_driver,
        public_object="public ReviewDriver completion",
    )
    builder.derived_check(
        "review_claim_failure",
        available=any_review_driver,
        public_object="public ReviewDriver failure release",
    )
    builder.derived_check(
        "memory_review_supported",
        available=all(
            item is not None
            for item in (memory_review_driver, memory_review_store, memory_read, memory_write)
        ),
        public_object="Memory Review Driver+Store+public Memory API",
    )
    builder.derived_check(
        "skill_review_supported",
        available=all(
            item is not None
            for item in (
                skill_review_driver,
                skill_review_store,
                skill_service,
                governance_revision,
            )
        ),
        public_object="Skill Review Driver+Store+public SkillService governance revision",
    )
    builder.derived_check(
        "duplicate_claim_rejection",
        available=any_review_driver,
        public_object="public ReviewDriver claim_is_valid lifecycle",
    )
    builder.derived_check(
        "review_outcome_observation",
        available=review_loop is not None,
        public_object="ReviewAgentLoop execution outcome",
    )
    # Current public claim validation does not bind a Skill governance revision.
    # Do not infer stale from a no-op or an unchanged snapshot.
    builder.derived_check(
        "stale_review_detection",
        available=False,
        public_object="governance-bound public ReviewClaim validation",
    )
    _ = (
        review_runtime,
        review_runtime_config,
        review_claim,
        review_registry,
        review_loop_result,
        review_hook_registry,
        review_observation_sink,
        review_policy,
        review_tool_resolution,
        review_tool_registration,
        governance_revision,
        review_executor,
        review_run_spec,
        foreground_review_event,
        foreground_review_window,
    )

    supported_review_kinds: list[ReviewKind] = []
    if all(
        item is not None
        for item in (memory_review_driver, memory_review_store, memory_read, memory_write)
    ):
        supported_review_kinds.append(ReviewKind.MEMORY)
    if all(
        item is not None
        for item in (
            skill_review_driver,
            skill_review_store,
            skill_service,
            governance_revision,
        )
    ):
        supported_review_kinds.append(ReviewKind.SKILL)
    builder.derived_check(
        "supported_review_kinds",
        available=bool(supported_review_kinds),
        public_object="complete public Review Driver/Store surfaces",
    )

    supported_memory_kinds: list[MemoryKind] = []
    if memory_read is not None and memory_write is not None:
        supported_memory_kinds.append(MemoryKind.LONG_TERM)
    if user_profile_read is not None and user_profile_write is not None:
        supported_memory_kinds.append(MemoryKind.USER_PROFILE)
    builder.derived_check(
        "supported_memory_kinds",
        available=bool(supported_memory_kinds),
        public_object="read/write target signatures",
    )

    supported_strategies: list[RetrievalStrategy] = []
    if memory_prompt_toggle is not None:
        supported_strategies.append(RetrievalStrategy.DISABLED)
    native_supported = all(
        item is not None
        for item in (
            memory_read,
            memory_prompt_render,
            memory_prompt_toggle,
        )
    )
    if native_supported:
        supported_strategies.insert(0, RetrievalStrategy.SUBJECT_NATIVE)
    if ranked_query is not None and declared_strategies is not None:
        declared_values = {str(item) for item in declared_strategies}
        for strategy in (
            RetrievalStrategy.DENSE,
            RetrievalStrategy.BM25,
            RetrievalStrategy.HYBRID,
        ):
            if strategy.value in declared_values:
                supported_strategies.append(strategy)
    builder.derived_check(
        "supported_retrieval_strategies",
        available=bool(supported_strategies),
        public_object="prompt toggle+ranked query declarations",
    )

    short_term_supported = all(
        item is not None
        for item in (run_conversation, session_creation, session_message_read)
    )
    builder.derived_check(
        "short_term_context",
        available=short_term_supported,
        public_object="run_conversation+public session message persistence",
    )
    session_isolation_supported = all(
        item is not None for item in (run_conversation, session_creation)
    )
    builder.derived_check(
        "session_context_isolation",
        available=session_isolation_supported,
        public_object="run_conversation session_id+create_session",
    )
    long_term_supported = native_supported
    builder.derived_check(
        "long_term_memory",
        available=long_term_supported,
        public_object="subject-native Memory prompt projection",
    )
    user_profile_supported = MemoryKind.USER_PROFILE in supported_memory_kinds
    builder.derived_check(
        "user_profile",
        available=user_profile_supported,
        public_object="public User Profile read/write projection",
    )

    supported_memory_modes: list[MemoryMode] = []
    if memory_prompt_toggle is not None and session_isolation_supported:
        supported_memory_modes.append(MemoryMode.NO_MEMORY)
    if (
        short_term_supported
        and session_isolation_supported
        and memory_prompt_toggle is not None
    ):
        supported_memory_modes.append(MemoryMode.SHORT_TERM_ONLY)
    if (
        long_term_supported
        and user_profile_supported
        and session_isolation_supported
    ):
        supported_memory_modes.append(MemoryMode.LONG_TERM_ONLY)
    if (
        short_term_supported
        and long_term_supported
        and user_profile_supported
        and session_isolation_supported
    ):
        supported_memory_modes.append(MemoryMode.SHORT_AND_LONG_TERM)
    supported_compression_modes = (
        [
            CompressionMode.THRESHOLD_DISABLED,
            CompressionMode.THRESHOLD_ENABLED,
        ]
        if compression_threshold_control
        else []
    )

    # Keep these local names intentionally referenced: their presence is recorded
    # by the individual checks and consumed by case preflight.
    _ = (
        query_scores,
        user_filtering,
        session_filtering,
        query_filters,
        memory_tool_available,
        model_observation_view,
    )
    missing = [
        item.name
        for item in builder.checks
        if item.required and not item.available
    ]
    fingerprint = canonical_sha256(
        {
            "protocol_version": CAPABILITY_PROTOCOL_VERSION,
            "public_api": builder.api_entries,
            "memory_projection": {
                "supported_kinds": [
                    item.value for item in supported_memory_kinds
                ],
                "supported_strategies": [
                    item.value for item in supported_strategies
                ],
                "provider": (
                    "prompt_context_injection" if native_supported else None
                ),
            },
            "background_review_projection": {
                "supported_kinds": [item.value for item in supported_review_kinds],
                "stale_review_detection": False,
            },
            "ablation_projection": {
                "memory_modes": [item.value for item in supported_memory_modes],
                "compression_modes": [
                    item.value for item in supported_compression_modes
                ],
                "compression_control": (
                    CompressionControl.THRESHOLD_CONFIGURATION.value
                    if supported_compression_modes
                    else CompressionControl.UNAVAILABLE.value
                ),
                "compression_threshold_control": compression_threshold_control,
                "compression_threshold_configuration": (
                    compression_threshold_configuration is not None
                ),
                "emergency_compression_disable": (
                    emergency_compression_disable is not None
                ),
                "compression_observation": compression_observation is not None,
            },
            "process_projection": {
                "toolset": process_toolset,
                "supported_actions": list(process_actions),
                "supported_statuses": list(supported_process_statuses),
                "start_via_terminal": process_start_via_terminal,
            },
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
        supported_memory_kinds=supported_memory_kinds,
        supported_retrieval_strategies=supported_strategies,
        supported_review_kinds=supported_review_kinds,
        memory_provider=("prompt_context_injection" if native_supported else None),
        supported_memory_modes=supported_memory_modes,
        supported_compression_modes=supported_compression_modes,
        compression_control=(
            CompressionControl.THRESHOLD_CONFIGURATION
            if supported_compression_modes
            else CompressionControl.UNAVAILABLE
        ),
        compression_configuration_paths=(
            [
                "compression.threshold",
                "compression.protect_first",
                "compression.keep_recent_tool_results",
                "compression.tail_token_budget",
            ]
            if supported_compression_modes
            else []
        ),
        compression_threshold_control=compression_threshold_control,
        compression_threshold_configuration=(
            compression_threshold_configuration is not None
        ),
        emergency_overflow_compression_disable_supported=(
            emergency_compression_disable is not None
        ),
        compression_observation_supported=compression_observation is not None,
        supported_process_actions=list(process_actions),
        supported_process_statuses=list(supported_process_statuses),
        process_toolset=process_toolset,
        process_start_via_terminal=process_start_via_terminal,
        process_log="log" in process_actions,
        process_poll="poll" in process_actions,
        process_wait="wait" in process_actions,
        process_write="write" in process_actions,
        process_submit="submit" in process_actions,
        process_kill="kill" in process_actions,
        process_close="close" in process_actions,
        process_interrupt="interrupt" in process_actions,
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
