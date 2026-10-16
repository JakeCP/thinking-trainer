"""The web app. Server-rendered forms; a few lines of JS for math preview."""

import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from app import scheduling
from app.config import Settings, load_settings
from app.db import make_engine, make_sessionmaker
from app.grading import grade, save_photo
from app.llm import Llm
from app.migrate import upgrade
from app.models import Attempt, Lens, Problem, ProblemLens
from app.render import render

log = logging.getLogger("web")
HERE = Path(__file__).parent
PUBLIC_PATHS = ("/login", "/static/", "/healthz")


def now() -> datetime:
    return datetime.now(timezone.utc)


def create_app(settings: Settings | None = None, llm: Llm | None = None, Session_: sessionmaker | None = None) -> FastAPI:
    settings = settings or load_settings()
    if Session_ is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        upgrade(settings.database_url)
        Session_ = make_sessionmaker(make_engine(settings.database_url))
    llm = llm or Llm(settings.prompts_dir)
    if not settings.app_password:
        log.warning("APP_PASSWORD is not set: no login required. Fine on localhost, not on a public host.")

    app = FastAPI(title="Thinking Trainer", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["md"] = render
    templates.env.globals["rating_labels"] = scheduling.RATING_LABELS

    def db():
        session = Session_()
        try:
            yield session
        finally:
            session.close()

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        if settings.app_password and not request.url.path.startswith(PUBLIC_PATHS) and not request.session.get("authed"):
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)

    # Added after the auth middleware so it wraps it (Starlette runs the last-added outermost).
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, max_age=60 * 60 * 24 * 90, same_site="lax", https_only=False)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    def page(request: Request, name: str, db_: Session, **ctx) -> HTMLResponse:
        review_count = db_.scalar(select(func.count()).select_from(Problem).where(Problem.status == "needs_review")) or 0
        return templates.TemplateResponse(
            request, name, {"review_count": review_count, "auth": bool(settings.app_password), "llm_enabled": settings.llm_enabled, **ctx}
        )

    # --- login ------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login")
    def login(request: Request, password: str = Form(...)):
        if settings.app_password and secrets.compare_digest(password.encode(), settings.app_password.encode()):
            request.session["authed"] = True
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "login.html", {"error": "Wrong password."}, status_code=401)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # --- practice -----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, db_: Session = Depends(db)):
        counts = scheduling.queue_counts(db_, now(), settings.tz, settings.new_per_day)
        by_status = dict(db_.execute(select(Problem.status, func.count()).group_by(Problem.status)).all())
        return page(request, "home.html", db_, counts=counts, by_status=by_status, pending=scheduling.unrated_attempt(db_))

    @app.get("/practice")
    def practice(request: Request, db_: Session = Depends(db)):
        if pending := scheduling.unrated_attempt(db_):
            return RedirectResponse(_after_submit(pending), status_code=303)
        problem = scheduling.next_problem(db_, now(), settings.tz, settings.new_per_day)
        if problem is None:
            counts = scheduling.queue_counts(db_, now(), settings.tz, settings.new_per_day)
            return page(request, "done.html", db_, counts=counts)
        return RedirectResponse(f"/problems/{problem.id}", status_code=303)

    @app.get("/problems/{problem_id}", response_class=HTMLResponse)
    def show_problem(problem_id: int, request: Request, db_: Session = Depends(db)):
        problem = db_.get(Problem, problem_id)
        if problem is None or problem.status != "ok":
            raise HTTPException(404)
        return page(request, "problem.html", db_, problem=problem, shown_at=now().isoformat())

    @app.post("/problems/{problem_id}/attempt")
    async def submit(
        problem_id: int,
        answer: str = Form(""),
        confidence: str = Form(""),
        shown_at: str = Form(""),
        photos: list[UploadFile] = File(default=[]),
        db_: Session = Depends(db),
    ):
        problem = db_.get(Problem, problem_id)
        if problem is None or problem.status != "ok":
            raise HTTPException(404)
        names = []
        for photo in photos:
            data = await photo.read()
            if data:
                names.append(save_photo(data, settings.uploads_dir))
        if not answer.strip() and not names:
            raise HTTPException(400, "Write an answer or attach a photo.")
        conf = int(confidence) if confidence.strip().isdigit() and 0 <= int(confidence) <= 100 else None
        attempt = Attempt(problem_id=problem.id, answer_text=answer, photo_paths=names, confidence=conf, shown_at=_parse_time(shown_at))
        db_.add(attempt)
        db_.commit()
        return RedirectResponse(_after_submit(attempt), status_code=303)

    def _after_submit(attempt: Attempt) -> str:
        """Claude grades when there's a key; otherwise you grade yourself on the attempt page."""
        needs_llm_grade = settings.llm_enabled and not attempt.verdict and not attempt.grading_error
        return f"/attempts/{attempt.id}" + ("/grade" if needs_llm_grade else "")

    @app.get("/attempts/{attempt_id}/grade", response_class=HTMLResponse)
    def grading_page(attempt_id: int, request: Request, db_: Session = Depends(db)):
        """Interstitial: shows "grading…" while the browser POSTs to start the grade."""
        attempt = _attempt(db_, attempt_id)
        if attempt.verdict or not settings.llm_enabled:
            return RedirectResponse(f"/attempts/{attempt.id}", status_code=303)
        return page(request, "grading.html", db_, attempt=attempt)

    @app.post("/attempts/{attempt_id}/grade")
    def run_grade(attempt_id: int, db_: Session = Depends(db)):
        attempt = _attempt(db_, attempt_id)
        if not attempt.verdict and settings.llm_enabled:
            grade(db_, llm, attempt, settings.uploads_dir)
        return RedirectResponse(f"/attempts/{attempt.id}", status_code=303)

    @app.post("/attempts/{attempt_id}/self-grade")
    def self_grade(attempt_id: int, verdict: str = Form(...), db_: Session = Depends(db)):
        if verdict not in ("correct", "partially_correct", "incorrect"):
            raise HTTPException(400)
        attempt = _attempt(db_, attempt_id)
        if not attempt.verdict:
            attempt.verdict, attempt.verdict_source = verdict, "self"
            db_.commit()
        return RedirectResponse(f"/attempts/{attempt.id}", status_code=303)

    @app.get("/attempts/{attempt_id}", response_class=HTMLResponse)
    def show_attempt(attempt_id: int, request: Request, db_: Session = Depends(db)):
        attempt = _attempt(db_, attempt_id)
        lenses = [pl for pl in attempt.problem.lenses]
        return page(
            request,
            "attempt.html",
            db_,
            attempt=attempt,
            problem=attempt.problem,
            lenses=lenses,
            suggested=scheduling.suggested_rating(attempt.verdict),
            self_grading=not attempt.verdict and not attempt.grading_error and not settings.llm_enabled,
        )

    @app.post("/attempts/{attempt_id}/rate")
    def rate(attempt_id: int, rating: int = Form(...), db_: Session = Depends(db)):
        if rating not in (1, 2, 3, 4):
            raise HTTPException(400)
        attempt = _attempt(db_, attempt_id)
        if attempt.rated_at is None:
            t = now()
            scheduling.review(db_, attempt.problem, rating, t)
            attempt.rating, attempt.rated_at = rating, t
            db_.commit()
        return RedirectResponse("/practice", status_code=303)

    @app.get("/uploads/{name}")
    def upload(name: str):
        path = (settings.uploads_dir / name).resolve()
        if path.parent != settings.uploads_dir.resolve() or not path.exists():
            raise HTTPException(404)
        return FileResponse(path)

    # --- review -------------------------------------------------------------

    @app.get("/review", response_class=HTMLResponse)
    def review_list(request: Request, status: str = "needs_review", db_: Session = Depends(db)):
        problems = list(
            db_.scalars(
                select(Problem)
                .where(Problem.status == status)
                .options(selectinload(Problem.lenses).selectinload(ProblemLens.lens))
                .order_by(Problem.id)
            )
        )
        counts = dict(db_.execute(select(Problem.status, func.count()).group_by(Problem.status)).all())
        return page(request, "review_list.html", db_, problems=problems, status=status, counts=counts)

    @app.get("/review/{problem_id}", response_class=HTMLResponse)
    def review_edit(problem_id: int, request: Request, db_: Session = Depends(db)):
        problem = db_.get(Problem, problem_id)
        if problem is None:
            raise HTTPException(404)
        return page(request, "review_edit.html", db_, problem=problem)

    @app.post("/review/{problem_id}")
    def review_save(
        problem_id: int,
        action: str = Form(...),
        statement_served: str = Form(""),
        solution: str = Form(""),
        db_: Session = Depends(db),
    ):
        problem = db_.get(Problem, problem_id)
        if problem is None:
            raise HTTPException(404)
        served = statement_served.replace("\r\n", "\n")
        # Only store an override when it differs from what was extracted.
        problem.statement_served = served if served.strip() and served != problem.statement_original else None
        problem.solution = solution.replace("\r\n", "\n")
        if action == "approve":
            if not problem.statement.strip() or not problem.solution.strip():
                raise HTTPException(400, "A problem needs a statement and a solution before it can be approved.")
            problem.status, problem.reviewed_at = "ok", now()
        elif action == "reject":
            problem.status, problem.reviewed_at = "rejected", now()
        elif action == "reopen":
            problem.status, problem.reviewed_at = "needs_review", None
        db_.commit()
        if action == "save":
            return RedirectResponse(f"/review/{problem.id}", status_code=303)
        nxt = db_.scalar(select(Problem.id).where(Problem.status == "needs_review", Problem.id > problem.id).order_by(Problem.id).limit(1))
        return RedirectResponse(f"/review/{nxt}" if nxt else "/review", status_code=303)

    # --- desk work ----------------------------------------------------------

    @app.get("/desk", response_class=HTMLResponse)
    def desk(request: Request, db_: Session = Depends(db)):
        lenses = list(db_.scalars(select(Lens).order_by(Lens.id)))
        items = list(db_.scalars(select(Problem).where(Problem.status == "desk").options(selectinload(Problem.lenses))))
        by_lens = {lens.id: [] for lens in lenses}
        for item in items:
            for pl in item.lenses:
                by_lens.setdefault(pl.lens_id, []).append((item, pl))
        return page(request, "desk.html", db_, lenses=lenses, by_lens=by_lens)

    @app.post("/desk/{problem_id}")
    def desk_toggle(problem_id: int, db_: Session = Depends(db)):
        item = db_.get(Problem, problem_id)
        if item is None or item.status != "desk":
            raise HTTPException(404)
        item.desk_done_at = None if item.desk_done_at else now()
        db_.commit()
        return RedirectResponse(f"/desk#item-{item.id}", status_code=303)

    return app


def _attempt(db_: Session, attempt_id: int) -> Attempt:
    attempt = db_.get(Attempt, attempt_id)
    if attempt is None:
        raise HTTPException(404)
    return attempt


def _parse_time(value: str) -> datetime | None:
    try:
        t = datetime.fromisoformat(value)
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def app_factory() -> FastAPI:
    """For uvicorn --factory."""
    logging.basicConfig(level=logging.INFO)
    return create_app()
