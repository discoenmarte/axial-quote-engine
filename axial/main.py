"""`uv run uvicorn axial.main:app` - builds the app from environment variables."""

from .api.app import create_app, engine_from_env

app = create_app(engine_from_env())
