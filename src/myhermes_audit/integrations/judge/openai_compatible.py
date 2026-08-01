"""Official OpenAI SDK adapter for an OpenAI-compatible Judge endpoint."""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import math
import os
import time
from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationError,
    field_validator,
)
from myhermes_audit.contracts import (
    JudgeCriterionResult,
    JudgeRequest,
    JudgeResult,
)
from myhermes_audit.errors import (
    AuditError,
    JudgeConfigError,
    JudgeDependencyError,
    JudgeInvocationError,
    JudgeParseError,
    JudgeProtocolError,
    JudgeTimeoutError,
)
from myhermes_audit.integrations.judge.prompt_builder import build_judge_prompt
from myhermes_audit.security import (
    redact_text,
    sanitize_external_error,
    sensitive_environment_values,
)


_PROVIDER_NAME = "openai_compatible"
_MAX_OUTPUT_TOKENS = 2_000
JUDGE_ADAPTER_VERSION = "openai-compatible-v1"
_ShortWireText = Annotated[
    StrictStr,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1_000),
]


class _WireCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: StrictStr
    score: StrictInt | StrictFloat = Field(ge=0, le=1)
    reason: _ShortWireText

    @field_validator("score")
    @classmethod
    def validate_score(cls, value: int | float) -> int | float:
        if not math.isfinite(float(value)):
            raise ValueError("criterion score must be finite")
        return value


class _WireResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    criteria: list[_WireCriterion] = Field(min_length=1, max_length=5)
    summary: _ShortWireText


@dataclass(frozen=True, slots=True)
class _Invocation:
    response: _WireResponse
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


class OpenAICompatibleJudgeAdapter:
    def __init__(
        self,
        *,
        client: Any,
        model: str,
        sensitive_values: tuple[str, ...],
    ) -> None:
        self._client = client
        self._model = model
        self._sensitive_values = sensitive_values
        self._closed = False

    @classmethod
    def from_environment(cls) -> "OpenAICompatibleJudgeAdapter":
        try:
            openai = importlib.import_module("openai")
            installed_version = importlib.metadata.version("openai")
        except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
            raise JudgeDependencyError(
                "OpenAI SDK is unavailable; install my-hermes-audit[judge]"
            ) from exc
        try:
            major = int(installed_version.split(".", maxsplit=1)[0])
        except (TypeError, ValueError) as exc:
            raise JudgeDependencyError(
                "installed OpenAI SDK version cannot be identified"
            ) from exc
        if major != 2:
            raise JudgeDependencyError("OpenAI SDK major version 2 is required")
        model = _required_environment("AUDIT_JUDGE_MODEL")
        api_key = _required_environment("AUDIT_JUDGE_API_KEY")
        _validate_model_name(model)
        base_url = os.environ.get("AUDIT_JUDGE_BASE_URL") or None
        if base_url is not None:
            base_url = base_url.strip()
            _validate_base_url(base_url)
        timeout = _timeout_from_environment()
        arguments: dict[str, Any] = {
            "api_key": api_key,
            "timeout": timeout,
            "max_retries": 0,
        }
        if base_url is not None:
            arguments["base_url"] = base_url
        try:
            client = openai.OpenAI(**arguments)
        except Exception as exc:
            raise JudgeConfigError(
                "Judge client initialization failed",
                exception_type=type(exc).__name__,
            ) from exc
        return cls(
            client=client,
            model=model,
            sensitive_values=tuple(
                sorted(
                    {*sensitive_environment_values(os.environ), api_key},
                    key=len,
                    reverse=True,
                )
            ),
        )

    def evaluate(self, request: JudgeRequest) -> JudgeResult:
        started = time.perf_counter()
        retry_count = 0
        prompt = build_judge_prompt(
            request,
            sensitive_values=self._sensitive_values,
        )
        try:
            invocation = self._structured_call(prompt.system, prompt.user)
            _validate_criterion_protocol(request, invocation.response)
        except Exception as first_error:
            if not _should_retry(first_error):
                raise _map_error(first_error, self._sensitive_values, attempts=1)
            retry_count = 1
            repair_prompt = build_judge_prompt(
                request,
                sensitive_values=self._sensitive_values,
                repair=True,
            )
            try:
                invocation = self._json_fallback_call(
                    repair_prompt.system,
                    repair_prompt.user,
                )
                _validate_criterion_protocol(request, invocation.response)
                prompt = repair_prompt
            except Exception as second_error:
                raise _map_error(
                    second_error,
                    self._sensitive_values,
                    attempts=2,
                ) from second_error
        duration_ms = max(0, round((time.perf_counter() - started) * 1_000))
        return _build_result(
            request,
            invocation,
            model=self._model,
            duration_ms=duration_ms,
            retry_count=retry_count,
            prompt_metadata=prompt.metadata,
            sensitive_values=self._sensitive_values,
        )

    def _structured_call(self, system: str, user: str) -> _Invocation:
        response = self._client.responses.parse(
            model=self._model,
            input=[
                {"role": "developer", "content": system},
                {"role": "user", "content": user},
            ],
            text_format=_WireResponse,
            max_output_tokens=_MAX_OUTPUT_TOKENS,
        )
        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            raise JudgeInvocationError(
                "Judge refused or omitted the structured result",
                retryable=False,
            )
        try:
            wire = (
                parsed
                if isinstance(parsed, _WireResponse)
                else _WireResponse.model_validate(parsed)
            )
        except ValidationError as exc:
            raise JudgeParseError("Judge structured result is invalid") from exc
        return _Invocation(wire, *_responses_usage(response))

    def _json_fallback_call(self, system: str, user: str) -> _Invocation:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            max_tokens=_MAX_OUTPUT_TOKENS,
        )
        choices = getattr(response, "choices", None)
        if not choices:
            raise JudgeInvocationError(
                "Judge returned no completion choice",
                retryable=False,
            )
        message = choices[0].message
        if getattr(message, "refusal", None):
            raise JudgeInvocationError(
                "Judge explicitly refused the evaluation",
                retryable=False,
            )
        content = getattr(message, "content", None)
        if not isinstance(content, str):
            raise JudgeParseError("Judge JSON fallback returned no text")
        try:
            data = json.loads(
                content,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_unique_json_object,
            )
            wire = _WireResponse.model_validate(data)
        except (ValueError, ValidationError) as exc:
            raise JudgeParseError("Judge JSON fallback is invalid") from exc
        return _Invocation(wire, *_chat_usage(response))

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._client, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:
                raise JudgeInvocationError(
                    "Judge client shutdown failed: "
                    + sanitize_external_error(exc, self._sensitive_values),
                    retryable=False,
                ) from exc


def _build_result(
    request: JudgeRequest,
    invocation: _Invocation,
    *,
    model: str,
    duration_ms: int,
    retry_count: int,
    prompt_metadata: dict[str, object],
    sensitive_values: tuple[str, ...],
) -> JudgeResult:
    weight_by_name = {
        criterion.name: float(criterion.weight) for criterion in request.criteria
    }
    total_weight = sum(weight_by_name.values())
    overall = sum(
        float(item.score) * weight_by_name[item.name]
        for item in invocation.response.criteria
    ) / total_weight
    overall = min(1.0, max(0.0, float(overall)))
    passed = (
        (request.minimum_score is None or overall >= float(request.minimum_score))
        and (request.maximum_score is None or overall <= float(request.maximum_score))
    )
    criterion_results = [
        JudgeCriterionResult(
            name=item.name,
            score=item.score,
            passed=None,
            reason=redact_text(item.reason, sensitive_values),
            evidence=[],
        )
        for item in invocation.response.criteria
    ]
    return JudgeResult(
        judge_id=request.judge_id,
        judge_model=model,
        judge_provider=_PROVIDER_NAME,
        prompt_version=request.prompt_version,
        overall_score=overall,
        passed=passed,
        criteria=criterion_results,
        summary=redact_text(invocation.response.summary, sensitive_values),
        duration_ms=duration_ms,
        prompt_tokens=invocation.prompt_tokens,
        completion_tokens=invocation.completion_tokens,
        total_tokens=invocation.total_tokens,
        retry_count=retry_count,
        raw_response_artifact=None,
        metadata={
            "aggregation": "local_weighted_mean",
            "adapter_version": JUDGE_ADAPTER_VERSION,
            "prompt": prompt_metadata,
        },
    )


def _validate_criterion_protocol(
    request: JudgeRequest,
    response: _WireResponse,
) -> None:
    expected = [item.name for item in request.criteria]
    actual = [item.name for item in response.criteria]
    if actual != expected:
        raise JudgeProtocolError(
            "Judge criteria must exactly match the request in order"
        )


def _responses_usage(response: Any) -> tuple[int | None, int | None, int | None]:
    usage = getattr(response, "usage", None)
    prompt = _optional_nonnegative_int(getattr(usage, "input_tokens", None))
    completion = _optional_nonnegative_int(
        getattr(usage, "output_tokens", None)
    )
    total = _optional_nonnegative_int(getattr(usage, "total_tokens", None))
    return prompt, completion, _normalized_total(prompt, completion, total)


def _chat_usage(response: Any) -> tuple[int | None, int | None, int | None]:
    usage = getattr(response, "usage", None)
    prompt = _optional_nonnegative_int(getattr(usage, "prompt_tokens", None))
    completion = _optional_nonnegative_int(
        getattr(usage, "completion_tokens", None)
    )
    total = _optional_nonnegative_int(getattr(usage, "total_tokens", None))
    return prompt, completion, _normalized_total(prompt, completion, total)


def _normalized_total(
    prompt: int | None,
    completion: int | None,
    total: int | None,
) -> int | None:
    if prompt is not None and completion is not None:
        return prompt + completion
    return total


def _optional_nonnegative_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _should_retry(error: BaseException) -> bool:
    if isinstance(error, (JudgeParseError, JudgeProtocolError, ValidationError)):
        return True
    if isinstance(error, AuditError):
        return error.details.get("retryable") is True
    status = getattr(error, "status_code", None)
    if status in {408, 409, 429} or (type(status) is int and status >= 500):
        return True
    name = type(error).__name__.lower()
    if "timeout" in name or "connection" in name or "ratelimit" in name:
        return True
    if status == 400:
        message = str(error).lower()
        return any(
            marker in message
            for marker in ("response_format", "json_schema", "structured output")
        )
    return isinstance(error, (AttributeError, TypeError, NotImplementedError))


def _map_error(
    error: BaseException,
    sensitive_values: tuple[str, ...],
    *,
    attempts: int,
) -> AuditError:
    if isinstance(error, (JudgeConfigError, JudgeDependencyError)):
        return error
    if isinstance(error, JudgeProtocolError):
        return JudgeProtocolError(error.message, attempts=attempts, retryable=False)
    if isinstance(error, JudgeParseError):
        return JudgeParseError(error.message, attempts=attempts, retryable=False)
    if isinstance(error, ValidationError):
        return JudgeParseError(
            "Judge structured result is invalid",
            attempts=attempts,
            retryable=False,
        )
    if isinstance(error, JudgeTimeoutError):
        return JudgeTimeoutError(attempts=attempts, retryable=True)
    if isinstance(error, JudgeInvocationError):
        return JudgeInvocationError(
            error.message,
            attempts=attempts,
            retryable=error.details.get("retryable") is True,
        )
    status = getattr(error, "status_code", None)
    name = type(error).__name__.lower()
    if "timeout" in name or status == 408:
        return JudgeTimeoutError(attempts=attempts, retryable=True)
    safe_message = sanitize_external_error(error, sensitive_values)
    return JudgeInvocationError(
        f"Judge provider request failed: {safe_message}",
        status_code=status if type(status) is int else None,
        attempts=attempts,
        retryable=_should_retry(error),
    )


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise JudgeConfigError(
            f"missing required Judge environment variable {name}",
            field=name,
        )
    return value.strip()


def _timeout_from_environment() -> float:
    raw = os.environ.get("AUDIT_JUDGE_TIMEOUT_SECONDS", "60")
    try:
        value = float(raw)
    except ValueError as exc:
        raise JudgeConfigError(
            "AUDIT_JUDGE_TIMEOUT_SECONDS must be a positive finite number",
            field="AUDIT_JUDGE_TIMEOUT_SECONDS",
        ) from exc
    if not math.isfinite(value) or value <= 0 or value > 600:
        raise JudgeConfigError(
            "AUDIT_JUDGE_TIMEOUT_SECONDS must be greater than 0 and at most 600",
            field="AUDIT_JUDGE_TIMEOUT_SECONDS",
        )
    return value


def _validate_model_name(value: str) -> None:
    if len(value) > 256 or any(ord(character) < 32 for character in value):
        raise JudgeConfigError(
            "AUDIT_JUDGE_MODEL is invalid",
            field="AUDIT_JUDGE_MODEL",
        )


def _validate_base_url(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise JudgeConfigError(
            "AUDIT_JUDGE_BASE_URL must be an HTTP(S) URL without credentials or query",
            field="AUDIT_JUDGE_BASE_URL",
        )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


__all__ = ("JUDGE_ADAPTER_VERSION", "OpenAICompatibleJudgeAdapter")
