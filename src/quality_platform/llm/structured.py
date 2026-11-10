import json
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.messages import BaseMessage, SystemMessage
from pydantic import BaseModel, ValidationError

from quality_platform.config.models import ModelSpec
from quality_platform.llm.model_factory import create_chat_model
from quality_platform.llm.registry import AgentRole, ModelRegistry


class StructuredOutputUnsupported(RuntimeError):
    """The selected model cannot provide structured output."""


class StructuredOutputValidationError(RuntimeError):
    """The selected model returned data that does not match the schema."""


class ModelInvocationError(RuntimeError):
    """The selected provider failed; no fallback provider was used."""


async def generate_structured[T: BaseModel](
    role: AgentRole,
    messages: Sequence[BaseMessage],
    schema: type[T],
    *,
    registry: ModelRegistry,
    factory: Callable[[ModelSpec], Any] = create_chat_model,
) -> T:
    spec = registry.get(role)
    model = factory(spec)
    try:
        structured = model.with_structured_output(schema)
    except (NotImplementedError, ValueError) as error:
        raise StructuredOutputUnsupported(
            f"structured output unsupported by {spec.provider}:{spec.model_id}"
        ) from error
    except Exception as error:
        raise ModelInvocationError(f"{spec.provider}:{spec.model_id} setup failed") from error
    try:
        result = await structured.ainvoke(list(messages))
    except ValidationError:
        result = None
    except Exception as error:
        raise ModelInvocationError(f"{spec.provider}:{spec.model_id} invocation failed") from error
    if result is not None:
        try:
            return schema.model_validate(result)
        except ValidationError:
            pass

    instruction = SystemMessage(
        content="Return only a JSON object matching this schema: "
        + json.dumps(schema.model_json_schema(), sort_keys=True)
    )
    try:
        response = await model.ainvoke([instruction, *messages])
    except Exception as error:
        raise ModelInvocationError(
            f"{spec.provider}:{spec.model_id} validation retry failed"
        ) from error
    if not isinstance(response.content, str):
        raise StructuredOutputValidationError("model response was not JSON text")
    try:
        return schema.model_validate_json(response.content)
    except ValidationError as error:
        raise StructuredOutputValidationError(
            f"{spec.provider}:{spec.model_id} response failed schema validation"
        ) from error
