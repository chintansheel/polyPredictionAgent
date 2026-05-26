"""FastAPI app: landing, auth, protected feed API, user-driven runs."""

from __future__ import annotations

import json
import os
import sys
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, field_validator, model_validator

# Repo root on path for agent imports.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent.selector import CATEGORY_ROTATION  # noqa: E402
from agent.tools import gamma  # noqa: E402
from agent.writer import (  # noqa: E402
    OutputContext,
    init_scorecard,
    load_feed,
    user_feed_path,
    user_scorecard_path,
)
from web import auth, db, jobs
from web.auth import (
    CurrentUser,
    ForgotPasswordBody,
    LoginBody,
    ResetPasswordBody,
    SignupBody,
)
from web.config import UI_DIR, WEB_RUNS_PER_USER_PER_DAY

app = FastAPI(title="Foretell")


@app.on_event("startup")
def startup() -> None:
    db.init_db()


def _html(name: str) -> FileResponse:
    path = UI_DIR / name
    if not path.is_file():
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return FileResponse(path, media_type="text/html")


@app.get("/")
def landing() -> FileResponse:
    return _html("landing.html")


@app.get("/ui/landing.html")
def legacy_landing() -> RedirectResponse:
    return RedirectResponse(url="/", status_code=302)


@app.get("/ui/signup.html")
def legacy_signup() -> RedirectResponse:
    return RedirectResponse(url="/signup", status_code=302)


@app.get("/ui/login.html")
def legacy_login() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=302)


@app.get("/ui/index.html")
def legacy_app() -> RedirectResponse:
    return RedirectResponse(url="/app", status_code=302)


@app.get("/signup")
def signup_page() -> FileResponse:
    return _html("signup.html")


@app.get("/login")
def login_page() -> FileResponse:
    return _html("login.html")


@app.get("/forgot-password")
def forgot_password_page() -> FileResponse:
    return _html("forgot-password.html")


@app.get("/reset-password")
def reset_password_page() -> FileResponse:
    return _html("reset-password.html")


@app.get("/app")
def app_page() -> FileResponse:
    return _html("index.html")


@app.get("/app/run")
def run_page() -> FileResponse:
    return _html("run.html")


@app.get("/ui/theme.css")
def theme_css() -> FileResponse:
    path = UI_DIR / "theme.css"
    if not path.is_file():
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return FileResponse(path, media_type="text/css")


@app.get("/ui/feed-render.js")
def feed_render_js() -> FileResponse:
    path = UI_DIR / "feed-render.js"
    if not path.is_file():
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return FileResponse(path, media_type="application/javascript")


@app.post("/api/auth/signup")
def api_signup(body: SignupBody, response: Response) -> dict:
    return auth.signup(body, response)


@app.post("/api/auth/login")
def api_login(body: LoginBody, response: Response) -> dict:
    return auth.login(body, response)


@app.post("/api/auth/logout")
def api_logout(response: Response) -> dict:
    return auth.logout(response)


@app.post("/api/auth/forgot-password")
def api_forgot_password(body: ForgotPasswordBody) -> dict:
    return auth.forgot_password(body)


@app.post("/api/auth/reset-password")
def api_reset_password(body: ResetPasswordBody) -> dict:
    return auth.reset_password(body)


@app.get("/api/auth/me")
def api_me(user: CurrentUser) -> dict:
    return {"name": user["name"], "email": user["email"]}


def _user_ctx(user: dict) -> OutputContext:
    return OutputContext.for_user(user["id"])


@app.get("/api/feed")
def api_feed(user: CurrentUser) -> JSONResponse:
    ctx = _user_ctx(user)
    feed_path = user_feed_path(user["id"])
    if not feed_path.is_file():
        return JSONResponse([])
    data = json.loads(feed_path.read_text(encoding="utf-8"))
    return JSONResponse(data)


@app.get("/api/scorecard")
def api_scorecard(user: CurrentUser) -> JSONResponse:
    ctx = _user_ctx(user)
    path = user_scorecard_path(user["id"])
    if not path.is_file():
        return JSONResponse(init_scorecard())
    data = json.loads(path.read_text(encoding="utf-8"))
    return JSONResponse(data)


@app.get("/api/categories")
def api_categories(user: CurrentUser) -> JSONResponse:
    return JSONResponse({"categories": list(CATEGORY_ROTATION)})


@app.get("/api/markets/search")
def api_markets_search(
    user: CurrentUser,
    q: str = Query(..., min_length=1, max_length=200),
) -> JSONResponse:
    try:
        markets = gamma.search_markets(q.strip(), limit_per_type=10)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Market search failed: {exc}") from exc
    preview = [
        {
            "id": m.get("id"),
            "question": m.get("question"),
            "probability_now": m.get("probability_now"),
            "volume_24hr": m.get("volume_24hr"),
            "category": m.get("category"),
            "polymarket_url": m.get("polymarket_url"),
        }
        for m in markets[:5]
    ]
    return JSONResponse({"query": q.strip(), "markets": preview})


class RunRequest(BaseModel):
    mode: Literal["fresh", "reanalysis"]
    topic: Optional[str] = None
    category: Optional[str] = None
    market_id: Optional[str] = None
    parent_run_id: Optional[str] = None
    force: bool = False

    @field_validator("topic", "category", "market_id", "parent_run_id")
    @classmethod
    def strip_optional(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @model_validator(mode="after")
    def validate_inputs(self) -> RunRequest:
        if self.mode == "fresh":
            if not any([self.topic, self.category, self.market_id]):
                raise ValueError(
                    "Fresh run requires at least one of: topic, category, market_id"
                )
            if self.category and self.category not in CATEGORY_ROTATION:
                raise ValueError(
                    f"category must be one of: {', '.join(CATEGORY_ROTATION)}"
                )
            if self.parent_run_id:
                raise ValueError("parent_run_id is only valid for reanalysis mode")
        elif self.mode == "reanalysis":
            if not (self.parent_run_id or self.market_id):
                raise ValueError(
                    "Reanalysis requires parent_run_id or market_id"
                )
        if self.topic and len(self.topic) > 200:
            raise ValueError("topic must be at most 200 characters")
        if self.market_id and len(self.market_id) > 128:
            raise ValueError("market_id must be at most 128 characters")
        return self


@app.post("/api/runs")
def api_create_run(body: RunRequest, user: CurrentUser) -> JSONResponse:
    if db.user_has_active_job(user["id"]):
        raise HTTPException(
            status_code=429,
            detail="You already have a run in progress. Wait for it to finish.",
        )
    if db.count_jobs_today_for_user(user["id"]) >= WEB_RUNS_PER_USER_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail=f"Daily run limit reached ({WEB_RUNS_PER_USER_PER_DAY} per day).",
        )

    ctx = _user_ctx(user)

    if body.mode == "reanalysis":
        if body.parent_run_id:
            prior = load_feed(ctx)
            if not any(c.get("id") == body.parent_run_id for c in prior):
                raise HTTPException(
                    status_code=404,
                    detail="Prior run not found in your feed",
                )
        elif body.market_id:
            from agent.writer import most_recent_card_for_market

            if not most_recent_card_for_market(body.market_id, ctx):
                raise HTTPException(
                    status_code=404,
                    detail="No prior analysis for this market in your feed",
                )

    try:
        job = db.create_job(
            user_id=user["id"],
            run_mode=body.mode,
            topic=body.topic,
            category=body.category,
            market_id=body.market_id,
            parent_run_id=body.parent_run_id,
            force=body.force,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    jobs.enqueue_job(job["id"])
    return JSONResponse(
        jobs.job_response_for_user(job, user["id"]),
        status_code=202,
    )


@app.get("/api/runs")
def api_list_runs(user: CurrentUser, limit: int = Query(20, ge=1, le=50)) -> JSONResponse:
    job_list = db.list_jobs_for_user(user["id"], limit=limit)
    return JSONResponse(
        [jobs.job_response_for_user(j, user["id"]) for j in job_list]
    )


@app.get("/api/runs/{job_id}")
def api_get_run(job_id: str, user: CurrentUser) -> JSONResponse:
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Forbidden")
    return JSONResponse(jobs.job_response_for_user(job, user["id"]))
