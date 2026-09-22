"""Model registry for the z.ai provider.

chat.z.ai does not accept a ``model`` field from us: the SPA picks the model in
its own UI and then stamps the wire id into the request body. So each
OpenAI-facing model id maps to

* ``label`` — the option text shown in the UI picker, and
* ``wire``  — the id the frontend sends upstream, readable from the selector
  button as ``model-selector-<wire>-button``.

This module deliberately has no Playwright dependency so the API layer can build
its model list without importing the browser driver.
"""

from __future__ import annotations

from dataclasses import dataclass


class ZaiModelError(Exception):
    """Raised when a requested model is not offered by the z.ai provider."""


@dataclass(frozen=True)
class ZaiModelSpec:
    """OpenAI-facing model id, its UI label, and the upstream wire id."""

    id: str
    name: str
    label: str
    wire: str


# Order matters: the first entry is the default advertised by the API.
ZAI_MODELS: dict[str, ZaiModelSpec] = {
    # GLM-5.3-Flash ships under an internal preview id upstream.
    "glm-5.3-flash": ZaiModelSpec(
        id="glm-5.3-flash",
        name="GLM-5.3-Flash",
        label="GLM-5.3-Flash",
        wire="x-preview-l",
    ),
    "glm-5.3": ZaiModelSpec(
        id="glm-5.3",
        name="GLM-5.3",
        label="GLM-5.3",
        wire="glm-5.3",
    ),
}

DEFAULT_ZAI_MODEL = next(iter(ZAI_MODELS))


def resolve_zai_model(model: str | None) -> ZaiModelSpec:
    """Return the spec for ``model``, falling back to the default when unset."""
    if not model:
        return ZAI_MODELS[DEFAULT_ZAI_MODEL]
    try:
        return ZAI_MODELS[model]
    except KeyError:
        supported = ", ".join(ZAI_MODELS)
        raise ZaiModelError(f"unsupported z.ai model {model!r}; supported: {supported}") from None


def openai_model_list() -> list[dict[str, str]]:
    """Build the OpenAI ``/v1/models`` entries for the z.ai provider."""
    return [{"id": spec.id, "name": spec.name, "owned_by": "zai", "model_type": "chat"} for spec in ZAI_MODELS.values()]
