import os

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from pydantic import SecretStr, ValidationError

from quality_platform.config.models import ModelSpec


class MissingCredentialError(ValueError):
    """The selected provider has no configured API credential."""


class ModelConfigurationError(ValueError):
    """The configured provider/model could not be initialized."""


_CREDENTIAL_KEYS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
}


def create_chat_model(spec: ModelSpec) -> BaseChatModel:
    provider = spec.provider
    key_name = _CREDENTIAL_KEYS.get(provider)
    if key_name is None:
        raise ModelConfigurationError(f"unsupported provider: {provider}")
    credential = os.getenv(key_name)
    if not credential:
        raise MissingCredentialError(f"missing {key_name} for provider {provider}")
    secret = SecretStr(credential)
    try:
        if provider == "openai":
            return ChatOpenAI(model=spec.model_id, api_key=secret)
        if provider == "anthropic":
            return ChatAnthropic.model_validate(
                {"model": spec.model_id, "anthropic_api_key": secret}
            )
        return ChatGoogleGenerativeAI(model=spec.model_id, api_key=secret)
    except (ValidationError, ValueError) as error:
        raise ModelConfigurationError(
            f"unable to initialize {provider} model {spec.model_id}"
        ) from error
