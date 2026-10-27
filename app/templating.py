"""Jinja2 environment shared by all routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.config import get_settings
from app.security.csrf import CSRF_FIELD

TEMPLATE_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.autoescape = True
templates.env.globals["csrf_field_name"] = CSRF_FIELD


def render(request: Request, name: str, context: dict[str, Any] | None = None, **kwargs: Any):
    settings = get_settings()
    user = getattr(request.state, "user", None)
    data: dict[str, Any] = {
        "request": request,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "user": user,
        "theme": (user.theme_override if user and user.theme_override else settings.default_theme),
        "language": (user.language if user else settings.default_language),
    }
    if context:
        data.update(context)
    return templates.TemplateResponse(request, name, data, **kwargs)
