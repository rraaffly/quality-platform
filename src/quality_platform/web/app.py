"""Same-origin HTTP adapter. Only the worker may mutate QA domain data."""

import difflib
import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.exc import NoResultFound
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from quality_platform.web.control import Conflict, Control, digest
from quality_platform.web.models import Command, Settings
from quality_platform.web.queries import (
    artifact_content,
    reader,
    source_content,
    version,
    workflow_view,
)
from quality_platform.web.worker import confined

BASE = Path(__file__).parent
MAX_BODY = 21 * 1024 * 1024
MAX_FILE = 5 * 1024 * 1024


def create_app(state: Path, *, public_origin: str | None = None, testing: bool = False) -> FastAPI:
    control = Control(state)
    paths = control.paths()
    database = Path(paths["database_path"])
    if public_origin:
        origin = urlsplit(public_origin)
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.path not in {"", "/"}
            or origin.query
            or origin.fragment
            or origin.username
        ):
            raise ValueError("Public origin must be an HTTPS origin without credentials or a path")
        public_origin = public_origin.rstrip("/")
    else:
        origin = None
    app = FastAPI(title="Quality Platform", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.control = control
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[
            "localhost",
            "127.0.0.1",
            "[::1]",
            *(["testserver"] if testing else []),
            *([str(origin.hostname)] if origin else []),
        ],
    )
    templates = Jinja2Templates(directory=BASE / "templates")
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

    @app.middleware("http")
    async def security(request: Request, call_next: Any) -> Response:
        session = control.session(request.cookies.get("qa_session"))
        request.state.session = session
        path = request.url.path
        public = path == "/login" or path.startswith("/static/")
        if not public and not (session and session["actor"]):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Authentication required"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("origin") not in {
                None,
                public_origin or str(request.base_url).rstrip("/"),
            }:
                return JSONResponse({"detail": "Origin rejected"}, status_code=403)
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > MAX_BODY:
                    return JSONResponse({"detail": "Request too large"}, status_code=413)
            request._body = bytes(raw)
            token = request.headers.get("x-csrf-token", "")
            if not token and "application/json" not in request.headers.get("content-type", ""):
                try:
                    token = str((await request.form()).get("csrf", ""))
                except (StarletteHTTPException, ValueError):
                    return JSONResponse({"detail": "Invalid form"}, status_code=422)
            if not session or not secrets.compare_digest(digest(token), session["csrf"]):
                return JSONResponse({"detail": "CSRF verification failed"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    def page(request: Request, template: str, **values: Any) -> Response:
        return templates.TemplateResponse(
            request=request,
            name=template,
            context={
                "csrf": request.cookies.get("qa_csrf", ""),
                "actor": (request.state.session or {}).get("actor"),
                "key": secrets.token_hex(16),
                "worker_healthy": control.healthy(),
                **values,
            },
        )

    def cookies(response: Response, token: str, csrf: str) -> Response:
        options: dict[str, Any] = {
            "secure": bool(public_origin),
            "httponly": True,
            "samesite": "strict",
            "max_age": 28800,
        }
        response.set_cookie("qa_session", token, **options)
        response.set_cookie("qa_csrf", csrf, **options)
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": error.detail}, status_code=error.status_code)
        response = page(request, "error.html", error=error.detail)
        response.status_code = error.status_code
        return response

    @app.exception_handler(Conflict)
    async def conflict_error(request: Request, error: Conflict) -> Response:
        return await http_error(
            request,
            HTTPException(409, "Reviewed version or command key changed. Reload before deciding."),
        )

    @app.exception_handler(ValidationError)
    async def validation_error(request: Request, error: ValidationError) -> Response:
        return await http_error(
            request,
            HTTPException(422, "Invalid input. Check required fields and supported values."),
        )

    @app.exception_handler(ValueError)
    async def value_error(request: Request, error: ValueError) -> Response:
        return await http_error(
            request, HTTPException(422, "Input or stored content failed validation.")
        )

    @app.exception_handler(NoResultFound)
    @app.exception_handler(KeyError)
    @app.exception_handler(FileNotFoundError)
    async def missing_error(request: Request, error: Exception) -> Response:
        return await http_error(request, HTTPException(404, "Resource not found or unavailable."))

    @app.exception_handler(PermissionError)
    async def permission_error(request: Request, error: Exception) -> Response:
        return await http_error(request, HTTPException(403, "Resource access or policy denied."))

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> Response:
        if request.state.session and request.state.session["actor"]:
            return RedirectResponse("/projects", status_code=303)
        csrf = request.cookies.get("qa_csrf", "")
        if request.state.session and secrets.compare_digest(
            digest(csrf), request.state.session["csrf"]
        ):
            # Reopening login (including an unauthenticated favicon redirect) must not
            # invalidate a form already open in this browser.
            return templates.TemplateResponse(
                request=request, name="login.html", context={"csrf": csrf}
            )
        if request.cookies.get("qa_session"):
            control.logout(request.cookies["qa_session"])
        token, csrf = control.new_session()
        response = templates.TemplateResponse(
            request=request, name="login.html", context={"csrf": csrf}
        )
        return cookies(response, token, csrf)

    @app.post("/login")
    async def login(request: Request) -> Response:
        form = await request.form()
        owner, password = str(form.get("owner", "")), str(form.get("password", ""))
        client = request.client.host if request.client else "local"
        if not control.login(client, owner, password):
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={
                    "csrf": request.cookies.get("qa_csrf"),
                    "error": "Login failed or temporarily rate-limited.",
                },
                status_code=401,
            )
        control.logout(request.cookies["qa_session"])
        token, csrf = control.new_session(owner)
        return cookies(RedirectResponse("/projects", status_code=303), token, csrf)

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        control.logout(request.cookies["qa_session"])
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie("qa_session")
        response.delete_cookie("qa_csrf")
        return response

    @app.get("/")
    async def index() -> Response:
        return RedirectResponse("/projects", status_code=303)

    @app.get("/projects", response_class=HTMLResponse)
    async def projects(request: Request) -> Response:
        with reader(database) as repo:
            items = [
                {"project": item, "workflows": repo.list_workflows(item.id)}
                for item in repo.list_projects()
            ]
        return page(request, "projects.html", projects=items, settings_id=control.settings_id())

    @app.get("/projects/{project}/workflows/{workflow}", response_class=HTMLResponse)
    async def workspace(request: Request, project: str, workflow: str) -> Response:
        with reader(database) as repo:
            view = workflow_view(repo, project, workflow)
            events = repo.list_decisions(project)
            snapshots = [
                json.loads(event.payload)
                for event in events
                if event.event_type == "workflow_config" and event.record_id == workflow
            ]
        return page(
            request,
            "workflow.html",
            view=view,
            project=project,
            workflow=workflow,
            snapshot=snapshots[-1] if snapshots else None,
        )

    @app.get("/api/projects/{project}/workflows/{workflow}")
    async def inspect_workflow(project: str, workflow: str) -> Any:
        with reader(database) as repo:
            return workflow_view(repo, project, workflow)

    async def accept(request: Request, values: dict[str, Any], key: str) -> Response:
        if not key or len(key) > 200:
            raise HTTPException(422, "Idempotency key is required")
        command = Command.model_validate(values)
        if command.kind == "import":
            raise HTTPException(422, "Use the source upload endpoint")
        if command.kind != "create_project":
            with reader(database) as repo:
                repo.read_project_record(command.project_id, command.project_id)
                if command.kind != "create_workflow":
                    current = workflow_view(repo, command.project_id, str(command.workflow_id))
                    if command.state_hash and command.state_hash != current["state_hash"]:
                        # An already accepted duplicate remains retrievable after the domain changed.
                        scope = json.dumps(
                            [
                                request.state.session["actor"],
                                command.kind,
                                command.project_id,
                                command.workflow_id,
                            ]
                        )
                        with control.connect() as db:
                            prior = db.execute(
                                "SELECT payload FROM jobs WHERE scope=? AND command_key=?",
                                (scope, key),
                            ).fetchone()
                        if not prior or json.loads(prior[0]) != command.model_dump(mode="json"):
                            raise Conflict("Reviewed state changed")
        job = control.enqueue(command.model_dump(mode="json"), key, request.state.session["actor"])
        return accepted(request, job)

    def accepted(request: Request, job: dict[str, Any]) -> Response:
        location = f"/jobs/{job['id']}"
        if request.headers.get("hx-request"):
            return HTMLResponse("", status_code=202, headers={"HX-Redirect": location})
        if "application/json" not in request.headers.get("content-type", ""):
            return RedirectResponse(location, status_code=303)
        return JSONResponse(
            {"job_id": job["id"], "status": job["status"], "status_url": f"/api/jobs/{job['id']}"},
            status_code=202,
            headers={"Location": location},
        )

    @app.post("/api/commands")
    async def api_command(request: Request) -> Response:
        return await accept(
            request, await request.json(), request.headers.get("idempotency-key", "")
        )

    @app.post("/commands")
    async def form_command(request: Request) -> Response:
        form = dict(await request.form())
        form.pop("csrf", None)
        key = str(form.pop("command_key", ""))
        for field in ("replacements", "merge_with_ids"):
            if field in form:
                form[field] = json.loads(str(form[field]))
        return await accept(request, form, key)

    @app.post("/api/projects/{project}/workflows/{workflow}/sources")
    async def upload(request: Request, project: str, workflow: str) -> Response:
        with reader(database) as repo:
            repo.read_workflow(project, workflow)
        form = await request.form(max_files=20, max_fields=10, max_part_size=MAX_FILE)
        key = request.headers.get("idempotency-key") or str(form.get("command_key", ""))
        if not key or len(key) > 200:
            raise HTTPException(422, "Idempotency key is required")
        files = form.getlist("files")
        if not files:
            raise HTTPException(422, "Choose Markdown files")
        prepared = []
        total = 0
        for file in files:
            if not isinstance(file, UploadFile):
                raise HTTPException(422, "Invalid upload")
            filename = file.filename or ""
            if (
                "/" in filename
                or "\\" in filename
                or not filename.lower().endswith(".md")
                or filename in {".md", "..md"}
            ):
                raise HTTPException(422, "Only Markdown filenames without paths are accepted")
            raw = await file.read(MAX_FILE + 1)
            total += len(raw)
            if len(raw) > MAX_FILE or total > 20 * 1024 * 1024:
                raise HTTPException(413, "Markdown upload exceeds size limit")
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(422, "Markdown must be UTF-8") from None
            if b"\x00" in raw:
                raise HTTPException(422, "Binary files are not Markdown")
            prepared.append((filename, raw, hashlib.sha256(raw).hexdigest()))
        directory = confined(control.state, control.state / "uploads")
        directory.mkdir(mode=0o700, exist_ok=True)
        for _, raw, identity in prepared:
            target = confined(control.state, directory / f"{identity}.md")
            if target.exists():
                if target.read_bytes() != raw:
                    raise Conflict("Staged upload changed")
            else:
                with target.open("xb") as stream:
                    stream.write(raw)
                target.chmod(0o600)
        command = Command(kind="import", project_id=project, workflow_id=workflow).model_dump(
            mode="json"
        )
        command["uploads"] = [{"name": name, "hash": identity} for name, _, identity in prepared]
        return accepted(request, control.enqueue(command, key, request.state.session["actor"]))

    def job_context(identity: str, actor: str) -> dict[str, Any]:
        job = control.job(identity)
        command = json.loads(job["payload"])
        if job["actor"] != actor:
            raise PermissionError("Job belongs to another actor")
        if command["kind"] != "create_project":
            with reader(database) as repo:
                repo.read_project_record(command["project_id"], command["project_id"])
                if command.get("workflow_id") and command["kind"] != "create_workflow":
                    repo.read_workflow(command["project_id"], command["workflow_id"])
        return {
            "id": job["id"],
            "status": job["status"],
            "error": job["error"],
            "result": json.loads(job["result"]) if job["result"] else None,
            "command": command,
            "created": job["created"],
            "started": job["started"],
            "finished": job["finished"],
        }

    @app.get("/api/jobs/{identity}")
    @app.get("/api/jobs/{identity}/events")
    async def inspect_job(request: Request, identity: str) -> Any:
        return {
            **job_context(identity, request.state.session["actor"]),
            "worker_healthy": control.healthy(),
        }

    @app.get("/jobs", response_class=HTMLResponse)
    async def jobs(request: Request) -> Response:
        return page(
            request,
            "jobs.html",
            jobs=[
                job_context(job["id"], request.state.session["actor"])
                for job in control.jobs()
                if job["actor"] == request.state.session["actor"]
            ],
        )

    @app.get("/jobs/{identity}", response_class=HTMLResponse)
    async def job_page(request: Request, identity: str) -> Response:
        job = job_context(identity, request.state.session["actor"])
        current_hash = ""
        if job["command"].get("workflow_id") and job["command"]["kind"] != "create_workflow":
            with reader(database) as repo:
                current_hash = workflow_view(
                    repo, job["command"]["project_id"], job["command"]["workflow_id"]
                )["state_hash"]
        diff = ""
        if job["command"]["kind"] == "revision" and job["result"]:
            draft = job["result"]
            diff = "".join(
                difflib.unified_diff(
                    json.dumps(draft["before"], indent=2).splitlines(True),
                    json.dumps(draft["after"], indent=2).splitlines(True),
                    fromfile="before",
                    tofile="proposed",
                )
            )
        template = "job_status.html" if request.headers.get("hx-request") else "job.html"
        return page(request, template, job=job, diff=diff, current_hash=current_hash)

    @app.post("/api/jobs/{identity}/retry")
    async def retry(request: Request, identity: str) -> Response:
        job = job_context(identity, request.state.session["actor"])
        form = await request.form()
        if form.get("confirm_cost") != "true":
            raise HTTPException(422, "Confirm retry; the provider may bill repeated calls")
        control.retry(identity)
        return accepted(request, control.job(job["id"]))

    @app.get(
        "/projects/{project}/workflows/{workflow}/artifacts/{artifact}/{number}",
        response_class=HTMLResponse,
    )
    async def artifact_page(
        request: Request, project: str, workflow: str, artifact: str, number: int
    ) -> Response:
        with reader(database) as repo:
            item = version(repo, project, workflow, artifact, number)
            content = artifact_content(repo, project, item)
            view = workflow_view(repo, project, workflow)
        with control.connect() as db:
            rejected = db.execute(
                """SELECT actor FROM jobs WHERE status='SUCCEEDED'
                AND json_extract(payload, '$.kind')='decide'
                AND json_extract(payload, '$.project_id')=?
                AND json_extract(payload, '$.workflow_id')=?
                AND json_extract(payload, '$.target_id')=?
                AND json_extract(result, '$.artifact_version')=?
                AND json_extract(result, '$.status')='REJECTED' LIMIT 1""",
                (project, workflow, artifact, number),
            ).fetchone()
        return page(
            request,
            "artifact.html",
            item=item,
            content=content,
            view=view,
            project=project,
            workflow=workflow,
            rejected_by=rejected["actor"] if rejected else None,
        )

    @app.get(
        "/api/projects/{project}/workflows/{workflow}/artifacts/{artifact}/versions/{number}/download"
    )
    async def download(
        project: str, workflow: str, artifact: str, number: int, format: str = "json"
    ) -> Response:
        with reader(database) as repo:
            item = version(repo, project, workflow, artifact, number)
            content = artifact_content(repo, project, item)
        if format not in content:
            raise HTTPException(422, "Unsupported artifact format")
        suffix = "json" if format == "json" else "md"
        return Response(
            content[format],
            media_type="application/json" if suffix == "json" else "text/markdown",
            headers={
                "Content-Disposition": f'attachment; filename="artifact-v{number}-{format}.{suffix}"'
            },
        )

    @app.get(
        "/projects/{project}/workflows/{workflow}/sources/{identity}", response_class=HTMLResponse
    )
    async def source_page(request: Request, project: str, workflow: str, identity: str) -> Response:
        with reader(database) as repo:
            text = source_content(repo, project, workflow, identity)
        return page(
            request,
            "source.html",
            source=text,
            identity=identity,
            project=project,
            workflow=workflow,
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request) -> Response:
        config = control.config()
        return page(
            request,
            "settings.html",
            settings=config,
            settings_id=control.settings_id(),
            credentials={
                provider: bool(os.getenv(key))
                for provider, key in {
                    "openai": "OPENAI_API_KEY",
                    "anthropic": "ANTHROPIC_API_KEY",
                    "google": "GOOGLE_API_KEY",
                }.items()
            },
        )

    @app.post("/api/settings")
    async def settings_save(request: Request) -> Response:
        key = request.headers.get("idempotency-key", "")
        if "application/json" in request.headers.get("content-type", ""):
            values = await request.json()
        else:
            form = await request.form()
            key = str(form.get("command_key", ""))
            values = {
                "agent_models": {
                    role: {
                        "provider": form.get(f"{role}_provider"),
                        "model_id": form.get(f"{role}_model_id"),
                    }
                    for role in ("orchestrator", "analyst", "planner", "evaluator")
                },
                "max_concurrency": form.get("max_concurrency"),
                "test_case_style": form.get("test_case_style"),
            }
        settings = Settings.model_validate(values)
        if not key or len(key) > 200:
            raise HTTPException(422, "Idempotency key is required")
        identity = control.save_settings(
            settings.model_dump(mode="json"), key=key, actor=request.state.session["actor"]
        )
        if "application/json" in request.headers.get("content-type", ""):
            return JSONResponse({"settings_id": identity}, status_code=201)
        return RedirectResponse("/settings", status_code=303)

    @app.get("/reports", response_class=HTMLResponse)
    async def reports(request: Request) -> Response:
        with control.connect() as db:
            entries = [dict(row) for row in db.execute("SELECT * FROM reports ORDER BY id")]
        with reader(database) as repo:
            for entry in entries:
                repo.read_project_record(entry["project_id"], entry["project_id"])
                entry["report"] = json.loads(entry["payload"])
        return page(request, "reports.html", reports=entries)

    return app
