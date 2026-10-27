"""Jinja2 environment shared by all routes.

The translation function is passed into the context rather than installed on
the environment, so two concurrent requests in different languages cannot
affect each other.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.config import get_settings
from app.security.csrf import CSRF_FIELD
from app.translations import SUPPORTED_LANGUAGES, gettext_for, normalise

TEMPLATE_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.autoescape = True
templates.env.globals["csrf_field_name"] = CSRF_FIELD
templates.env.globals["languages"] = SUPPORTED_LANGUAGES


def language_for(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if user is not None and user.language:
        return normalise(user.language)
    header = request.headers.get("accept-language", "")
    first = header.split(",")[0] if header else ""
    return normalise(first)


def theme_for(request: Request, festival: Any | None = None) -> str:
    settings = get_settings()
    user = getattr(request.state, "user", None)
    if user is not None and user.theme_override:
        return user.theme_override
    if festival is not None and getattr(festival, "theme", None):
        return festival.theme
    return settings.default_theme


def render(request: Request, name: str, context: dict[str, Any] | None = None, **kwargs: Any):
    context = context or {}
    language = language_for(request)
    data: dict[str, Any] = {
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "user": getattr(request.state, "user", None),
        "language": language,
        "_": gettext_for(language),
        "theme": theme_for(request, context.get("festival")),
    }
    data.update(context)
    return templates.TemplateResponse(request, name, data, **kwargs)
