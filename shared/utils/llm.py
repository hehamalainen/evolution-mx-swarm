"""LLM client utility for MX Swarm.

Provides a unified interface for LLM interactions with support for:
- Anthropic Claude
- OpenAI GPT
- Google Gemini
- Local models (ollama)
"""

import json
import os
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field


@dataclass
class Message:
    """Chat message."""
    role: str  # "system", "user", "assistant"
    content: str


@dataclass
class LLMResponse:
    """Response from LLM."""
    content: str
    model: str
    usage: dict = field(default_factory=dict)  # tokens used
    stop_reason: str | None = None


class LLMClient(ABC):
    """Abstract base class for LLM clients."""

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        max_tokens: int = 4096,
        temperature: float = 0.7,
        system: str | None = None,
    ) -> LLMResponse:
        """
        Send chat completion request.

        Args:
            messages: Conversation history
            max_tokens: Maximum tokens in response
            temperature: Sampling temperature
            system: System prompt

        Returns:
            LLMResponse with generated content
        """
        pass

    @abstractmethod
    def stream(
        self,
        messages: list[Message],
        max_tokens: int = 4096,
        temperature: float = 0.7,
        system: str | None = None,
    ) -> AsyncIterator[str]:
        """Stream response tokens."""
        pass


class AnthropicClient(LLMClient):
    """Anthropic Claude client."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-sonnet-4-20250514",
    ):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.model = model
        self._client = None

    async def _get_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError:
                raise ImportError("anthropic package required. Install with: pip install anthropic") from None
            self._client = anthropic.AsyncAnthropic(api_key=self.api_key)
        return self._client

    async def chat(
        self,
        messages: list[Message],
        max_tokens: int = 4096,
        temperature: float = 0.7,
        system: str | None = None,
    ) -> LLMResponse:
        client = await self._get_client()

        # Convert messages format
        api_messages = [{"role": m.role, "content": m.content} for m in messages]

        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": api_messages,
        }
        if system:
            kwargs["system"] = system

        response = await client.messages.create(**kwargs)

        return LLMResponse(
            content=response.content[0].text,
            model=response.model,
            usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
            stop_reason=response.stop_reason,
        )

    async def stream(
        self,
        messages: list[Message],
        max_tokens: int = 4096,
        temperature: float = 0.7,
        system: str | None = None,
    ) -> AsyncIterator[str]:
        client = await self._get_client()

        api_messages = [{"role": m.role, "content": m.content} for m in messages]

        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": api_messages,
        }
        if system:
            kwargs["system"] = system

        async with client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield text


class OpenAIClient(LLMClient):
    """OpenAI GPT client."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gpt-4-turbo-preview",
        base_url: str | None = None,
    ):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.model = model
        self.base_url = base_url
        self._client = None

    async def _get_client(self):
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError:
                raise ImportError("openai package required. Install with: pip install openai") from None
            kwargs = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = AsyncOpenAI(**kwargs)
        return self._client

    async def chat(
        self,
        messages: list[Message],
        max_tokens: int = 4096,
        temperature: float = 0.7,
        system: str | None = None,
    ) -> LLMResponse:
        client = await self._get_client()

        api_messages = []
        if system:
            api_messages.append({"role": "system", "content": system})
        api_messages.extend([{"role": m.role, "content": m.content} for m in messages])

        response = await client.chat.completions.create(
            model=self.model,
            messages=api_messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        choice = response.choices[0]
        return LLMResponse(
            content=choice.message.content,
            model=response.model,
            usage={
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
            },
            stop_reason=choice.finish_reason,
        )

    async def stream(
        self,
        messages: list[Message],
        max_tokens: int = 4096,
        temperature: float = 0.7,
        system: str | None = None,
    ) -> AsyncIterator[str]:
        client = await self._get_client()

        api_messages = []
        if system:
            api_messages.append({"role": "system", "content": system})
        api_messages.extend([{"role": m.role, "content": m.content} for m in messages])

        stream = await client.chat.completions.create(
            model=self.model,
            messages=api_messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=True,
        )

        async for chunk in stream:
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content


class GeminiClient(LLMClient):
    """Google Gemini client."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gemini-2.5-flash",
    ):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.model = model
        self._client = None

    async def _get_client(self):
        if self._client is None:
            try:
                import google.generativeai as genai
            except ImportError:
                raise ImportError("google-generativeai package required. Install with: pip install google-generativeai") from None
            genai.configure(api_key=self.api_key)
            
            # Note: system instruction should theoretically be passed here if required constantly, 
            # but we will pass it ad-hoc where possible or handle directly if the API demands.
            self._client = genai.GenerativeModel(self.model)
        return self._client

    async def chat(
        self,
        messages: list[Message],
        max_tokens: int = 4096,
        temperature: float = 0.7,
        system: str | None = None,
    ) -> LLMResponse:
        client = await self._get_client()

        # Convert roles: "assistant" -> "model"
        formatted_messages = []
        for m in messages:
            role = "model" if m.role == "assistant" else m.role
            # skip 'system' if it happens to be bleeding in, though usually it's passed via `system` kwarg
            if role == "system":
                continue
            formatted_messages.append({"role": role, "parts": [m.content]})
            
        kwargs = {
            "generation_config": {
                "max_output_tokens": max_tokens,
                "temperature": temperature,
            }
        }
        
        # In the modern SDK, system_instruction can be passed to generate_content or the model init.
        # It's safest to prepend to the first user message if we can't cleanly inject it at generation time.
        if system and formatted_messages:
            if formatted_messages[0]["role"] == "user":
                formatted_messages[0]["parts"][0] = f"System Instruction:\n{system}\n\nUser Request:\n" + formatted_messages[0]["parts"][0]
            else:
                formatted_messages.insert(0, {"role": "user", "parts": [f"System Instruction:\n{system}"]})
                formatted_messages.insert(1, {"role": "model", "parts": ["Understood."]})

        response = await client.generate_content_async(
            contents=formatted_messages,
            **kwargs
        )

        return LLMResponse(
            content=response.text,
            model=self.model,
            usage={
                "input_tokens": response.usage_metadata.prompt_token_count if hasattr(response, "usage_metadata") else 0,
                "output_tokens": response.usage_metadata.candidates_token_count if hasattr(response, "usage_metadata") else 0,
            },
            stop_reason="stop",
        )

    async def stream(
        self,
        messages: list[Message],
        max_tokens: int = 4096,
        temperature: float = 0.7,
        system: str | None = None,
    ) -> AsyncIterator[str]:
        client = await self._get_client()

        formatted_messages = []
        for m in messages:
            role = "model" if m.role == "assistant" else m.role
            if role == "system":
                continue
            formatted_messages.append({"role": role, "parts": [m.content]})
            
        kwargs = {
            "generation_config": {
                "max_output_tokens": max_tokens,
                "temperature": temperature,
            }
        }
        
        if system and formatted_messages:
            if formatted_messages[0]["role"] == "user":
                formatted_messages[0]["parts"][0] = f"System Instruction:\n{system}\n\nUser Request:\n" + formatted_messages[0]["parts"][0]
            else:
                formatted_messages.insert(0, {"role": "user", "parts": [f"System Instruction:\n{system}"]})
                formatted_messages.insert(1, {"role": "model", "parts": ["Understood."]})

        response = await client.generate_content_async(
            contents=formatted_messages,
            stream=True,
            **kwargs
        )

        async for chunk in response:
            if chunk.text:
                yield chunk.text


class OllamaClient(LLMClient):
    """Ollama local model client."""

    def __init__(
        self,
        model: str = "llama3",
        base_url: str = "http://localhost:11434",
    ):
        self.model = model
        self.base_url = base_url

    async def chat(
        self,
        messages: list[Message],
        max_tokens: int = 4096,
        temperature: float = 0.7,
        system: str | None = None,
    ) -> LLMResponse:
        try:
            import httpx
        except ImportError:
            raise ImportError("httpx package required. Install with: pip install httpx")

        api_messages = []
        if system:
            api_messages.append({"role": "system", "content": system})
        api_messages.extend([{"role": m.role, "content": m.content} for m in messages])

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": api_messages,
                    "stream": False,
                    "options": {
                        "num_predict": max_tokens,
                        "temperature": temperature,
                    },
                },
                timeout=600,  # 10 min for local models
            )
            data = response.json()

        return LLMResponse(
            content=data["message"]["content"],
            model=self.model,
            usage={
                "input_tokens": data.get("prompt_eval_count", 0),
                "output_tokens": data.get("eval_count", 0),
            },
            stop_reason="stop",
        )

    async def stream(
        self,
        messages: list[Message],
        max_tokens: int = 4096,
        temperature: float = 0.7,
        system: str | None = None,
    ) -> AsyncIterator[str]:
        try:
            import httpx
        except ImportError:
            raise ImportError("httpx package required. Install with: pip install httpx")

        api_messages = []
        if system:
            api_messages.append({"role": "system", "content": system})
        api_messages.extend([{"role": m.role, "content": m.content} for m in messages])

        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": api_messages,
                    "stream": True,
                    "options": {
                        "num_predict": max_tokens,
                        "temperature": temperature,
                    },
                },
                timeout=600,  # 10 min for local models
            ) as response:
                async for line in response.aiter_lines():
                    if line:
                        data = json.loads(line)
                        if "message" in data and "content" in data["message"]:
                            yield data["message"]["content"]


def create_llm_client(config: dict) -> LLMClient:
    """
    Factory function to create LLM client based on config.

    Config example:
    {
        "provider": "anthropic",  # or "openai", "ollama"
        "model": "claude-sonnet-4-20250514",
        "api_key": "...",  # optional, will use env var
        "base_url": "..."  # optional, for custom endpoints
    }
    """
    provider = config.get("provider", "anthropic")
    model = config.get("model")
    api_key = config.get("api_key")
    base_url = config.get("base_url")

    if provider == "anthropic":
        kwargs = {}
        if api_key:
            kwargs["api_key"] = api_key
        if model:
            kwargs["model"] = model
        return AnthropicClient(**kwargs)

    elif provider == "openai":
        kwargs = {}
        if api_key:
            kwargs["api_key"] = api_key
        if model:
            kwargs["model"] = model
        if base_url:
            kwargs["base_url"] = base_url
        return OpenAIClient(**kwargs)

    elif provider == "gemini":
        kwargs = {}
        if api_key:
            kwargs["api_key"] = api_key
        if model:
            kwargs["model"] = model
        return GeminiClient(**kwargs)

    elif provider == "ollama":
        kwargs = {}
        if model:
            kwargs["model"] = model
        if base_url:
            kwargs["base_url"] = base_url
        return OllamaClient(**kwargs)

    else:
        raise ValueError(f"Unknown LLM provider: {provider}")
