from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel

from quality_platform.config.models import AppConfig, ModelSpec
from quality_platform.llm.model_factory import MissingCredentialError, create_chat_model
from quality_platform.llm.registry import AgentRole, ModelRegistry
from quality_platform.llm.structured import (
    ModelInvocationError,
    StructuredOutputUnsupported,
    StructuredOutputValidationError,
    generate_structured,
)
from quality_platform.llm.usage import capture_usage


class Answer(BaseModel):
    value: int


def registry_for(role: AgentRole, provider: str) -> ModelRegistry:
    models = {
        agent.value: {"provider": "openai", "model_id": f"configured-{agent.value}"}
        for agent in AgentRole
    }
    models[role.value] = {"provider": provider, "model_id": "configured-model"}
    config = AppConfig.model_validate(
        {"database_path": Path("qa.sqlite"), "workspace_root": Path("."), "agent_models": models}
    )
    return ModelRegistry(config)


class FakeStructuredModel:
    def __init__(self, result: object) -> None:
        self.result = result

    async def ainvoke(self, _messages: object) -> object:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeModel:
    def __init__(
        self,
        structured_result: object,
        fallback_result: str = '{"value": 7}',
        setup_error: Exception | None = None,
    ) -> None:
        self.structured_result = structured_result
        self.fallback_result = fallback_result
        self.setup_error = setup_error
        self.structured_calls = 0
        self.fallback_calls = 0

    def with_structured_output(self, _schema: type[BaseModel]) -> FakeStructuredModel:
        self.structured_calls += 1
        if self.setup_error is not None:
            raise self.setup_error
        return FakeStructuredModel(self.structured_result)

    async def ainvoke(self, _messages: object) -> AIMessage:
        self.fallback_calls += 1
        return AIMessage(content=self.fallback_result)


def test_missing_credential_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(MissingCredentialError, match="OPENAI_API_KEY"):
        create_chat_model(ModelSpec(provider="openai", model_id="configured-model"))


@pytest.mark.parametrize(
    ("provider", "credential", "expected_class"),
    [
        ("openai", "OPENAI_API_KEY", "ChatOpenAI"),
        ("anthropic", "ANTHROPIC_API_KEY", "ChatAnthropic"),
        ("google", "GOOGLE_API_KEY", "ChatGoogleGenerativeAI"),
    ],
)
def test_factory_uses_exact_configured_provider_without_live_call(
    monkeypatch: pytest.MonkeyPatch, provider: str, credential: str, expected_class: str
) -> None:
    monkeypatch.setenv(credential, "test-credential")
    spec = ModelSpec.model_validate({"provider": provider, "model_id": "configured-model"})
    model = create_chat_model(spec)
    assert type(model).__name__ == expected_class


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_error", [TimeoutError("provider timeout"), ValueError("unknown model ID")]
)
async def test_provider_failure_does_not_switch_to_another_model(provider_error: Exception) -> None:
    fake = FakeModel(provider_error)
    factory_calls: list[ModelSpec] = []

    def factory(spec: ModelSpec) -> FakeModel:
        factory_calls.append(spec)
        return fake

    with pytest.raises(ModelInvocationError) as caught:
        await generate_structured(
            AgentRole.ANALYST,
            [HumanMessage(content="Analyze")],
            Answer,
            registry=registry_for(AgentRole.ANALYST, "openai"),
            factory=factory,
        )
    assert len(factory_calls) == 1
    assert fake.fallback_calls == 0
    assert caught.value.__cause__ is provider_error


@pytest.mark.asyncio
async def test_provider_error_does_not_echo_sensitive_details() -> None:
    secret = "private-prompt-and-token"
    fake = FakeModel(TimeoutError(secret))
    with pytest.raises(ModelInvocationError) as caught:
        await generate_structured(
            AgentRole.ANALYST,
            [HumanMessage(content="Analyze")],
            Answer,
            registry=registry_for(AgentRole.ANALYST, "openai"),
            factory=lambda _spec: fake,
        )
    assert secret not in str(caught.value)


@pytest.mark.asyncio
async def test_invalid_structured_result_retries_same_model_with_json() -> None:
    fake = FakeModel({"value": "not-an-int"}, fallback_result='{"value": 7}')
    answer = await generate_structured(
        AgentRole.PLANNER,
        [HumanMessage(content="Plan")],
        Answer,
        registry=registry_for(AgentRole.PLANNER, "anthropic"),
        factory=lambda _spec: fake,
    )
    assert answer.value == 7
    assert fake.structured_calls == 1
    assert fake.fallback_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capability_error", [NotImplementedError("unsupported"), ValueError("unsupported method")]
)
async def test_unsupported_structured_output_is_typed_error(capability_error: Exception) -> None:
    fake = FakeModel({"value": 7}, setup_error=capability_error)
    with pytest.raises(StructuredOutputUnsupported):
        await generate_structured(
            AgentRole.EVALUATOR,
            [HumanMessage(content="Score")],
            Answer,
            registry=registry_for(AgentRole.EVALUATOR, "google"),
            factory=lambda _spec: fake,
        )
    assert fake.fallback_calls == 0


@pytest.mark.asyncio
async def test_invalid_json_from_same_model_is_typed_validation_error() -> None:
    fake = FakeModel({"value": "bad"}, fallback_result="not json")
    with pytest.raises(StructuredOutputValidationError):
        await generate_structured(
            AgentRole.PLANNER,
            [HumanMessage(content="Plan")],
            Answer,
            registry=registry_for(AgentRole.PLANNER, "anthropic"),
            factory=lambda _spec: fake,
        )
    assert fake.fallback_calls == 1


def test_usage_record_keeps_provider_and_model() -> None:
    spec = ModelSpec(provider="google", model_id="configured-model")
    message = AIMessage(
        content="ok", usage_metadata={"input_tokens": 11, "output_tokens": 4, "total_tokens": 15}
    )
    usage = capture_usage(AgentRole.PLANNER, spec, message)
    assert (usage.provider, usage.model_id, usage.input_tokens, usage.output_tokens) == (
        "google",
        "configured-model",
        11,
        4,
    )
