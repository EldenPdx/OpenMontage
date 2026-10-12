"""Central Backlot factory; authenticated business access is registered in code."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backlot import projects_api
from production.api.security import install_boundary
from production.auth.config import AuthConfig
from production.contracts import ContractViolation


def create_app(*, studio_repository=None, studio_config=None, studio_environment=None,
               auth_config=None, auth_store=None, auth_authority=None, auth_environment=None) -> FastAPI:
    try:
        config = auth_config or AuthConfig.load(auth_environment)
        mode = config.mode
    except ContractViolation:
        config, mode = None, "unavailable"
    runtime = None
    if mode == "authenticated":
        try:
            from production.auth.routes import AuthRuntime
            runtime = AuthRuntime(config, store=auth_store, authority=auth_authority)
        except Exception:
            mode = "unavailable"

    @asynccontextmanager
    async def lifespan(app):
        if runtime is not None:
            try:
                await runtime.startup(projects_api.PROJECTS_DIR)
            except Exception:
                app.state.auth_unavailable = True
        async with projects_api.lifespan(app):
            yield

    app = FastAPI(title="Backlot", docs_url=None, redoc_url=None,
                  openapi_url="/openapi.json" if mode == "local" else None, lifespan=lifespan)
    app.state.auth_mode, app.state.auth_runtime = mode, runtime
    app.state.auth_unavailable = mode == "unavailable"
    app.state.auth_policies = {"studio": None, "projects": None, "organizations": None}
    from production.api.tasks import mount_studio
    mount_studio(app, repository=studio_repository, config=studio_config, environment=studio_environment,
                 projects_dir=projects_api.PROJECTS_DIR)
    projects_api.mount_projects(app)
    if projects_api.UI_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=projects_api.UI_DIR), name="ui")
    if runtime is not None:
        from production.auth.routes import mount_auth
        mount_auth(app, runtime)
    elif mode == "local":
        @app.get("/api/auth/me")
        async def local_identity():
            return {"mode": "local"}

    @app.get("/api/health")
    async def health():
        return {"ok": True, "app": "backlot"}

    install_boundary(app, config=config, projects_dir=projects_api.PROJECTS_DIR)
    return app


app = create_app()
