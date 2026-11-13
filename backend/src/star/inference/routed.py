from __future__ import annotations

from collections.abc import Mapping

from star.inference.provider import (
    LocalInferenceProvider,
    LocalModelInventoryItem,
    StructuredInferenceRequest,
    StructuredInferenceResult,
)


class RoutedLocalInferenceProvider:
    """Route explicitly named models to isolated local runtimes.

    Photo review models stay on Ollama while the text-only adviser can use a
    native MLX server. A failed optional runtime never hides the healthy models
    served by the other runtime.
    """

    def __init__(
        self,
        *,
        default: LocalInferenceProvider,
        model_routes: Mapping[str, LocalInferenceProvider],
    ) -> None:
        self._default = default
        self._model_routes = dict(model_routes)

    def _provider(self, model_key: str) -> LocalInferenceProvider:
        return self._model_routes.get(model_key, self._default)

    async def health(self) -> bool:
        return await self._default.health()

    async def inventory(self) -> list[LocalModelInventoryItem]:
        providers = {id(self._default): self._default}
        providers.update({id(provider): provider for provider in self._model_routes.values()})
        items: list[LocalModelInventoryItem] = []
        for provider in providers.values():
            items.extend(await provider.inventory())
        by_model: dict[str, LocalModelInventoryItem] = {}
        for item in items:
            by_model[item.model_key] = item
        return list(by_model.values())

    async def warmup(self, model_key: str, *, system_prompt: str | None = None) -> None:
        await self._provider(model_key).warmup(model_key, system_prompt=system_prompt)

    async def unload(self, model_key: str) -> None:
        await self._provider(model_key).unload(model_key)

    async def infer(self, request: StructuredInferenceRequest) -> StructuredInferenceResult:
        return await self._provider(request.model_key).infer(request)

    async def close(self) -> None:
        providers = {id(self._default): self._default}
        providers.update({id(provider): provider for provider in self._model_routes.values()})
        for provider in providers.values():
            await provider.close()
