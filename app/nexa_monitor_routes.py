"""Admin routes for NEXA Phase 4 read-only health monitoring and manual actions."""
from urllib.parse import urlencode

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .db import get_db
from .services.nexa_monitor import (
    action_transition, dashboard_data, get_monitor_state, monitor_cycle,
)


def _redirect(kind: str, message: str):
    return RedirectResponse(
        "/integrations/nexa/monitor?" + urlencode({kind: message}),
        status_code=303,
    )


def register_nexa_monitor_routes(app, auth, render):
    @app.get("/integrations/nexa/monitor", response_class=HTMLResponse)
    def nexa_monitor_page(
        request: Request, days: int = 30, error: str | None = None,
        notice: str | None = None, db: Session = Depends(get_db),
    ):
        gate = auth(request)
        if gate:
            return gate
        days = days if days in (30, 90) else 30
        context = dashboard_data(db, days=days)
        db.commit()  # Initialise monitoring policy if this is the first visit.
        response = render(request, "nexa_monitor.html", error=error,
                          notice=notice, **context)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/integrations/nexa/monitor/settings")
    def nexa_monitor_settings(
        request: Request,
        expiry_warning_days: int = Form(30),
        stale_after_hours: int = Form(48),
        low_credit_threshold: int = Form(12),
        poll_interval_hours: int = Form(6),
        monitor_enabled: str = Form(""),
        notifications_enabled: str = Form(""),
        db: Session = Depends(get_db),
    ):
        gate = auth(request)
        if gate:
            return gate
        if (not 1 <= expiry_warning_days <= 180
                or not 1 <= stale_after_hours <= 720
                or not 0 <= low_credit_threshold <= 100000
                or not 1 <= poll_interval_hours <= 168):
            return _redirect("error", "Invalid monitoring settings range")
        state = get_monitor_state(db)
        state.enabled = monitor_enabled == "yes"
        state.notifications_enabled = notifications_enabled == "yes"
        state.expiry_warning_days = expiry_warning_days
        state.stale_after_hours = stale_after_hours
        state.low_credit_threshold = low_credit_threshold
        state.poll_interval_hours = poll_interval_hours
        db.commit()
        return _redirect("notice", "Read-only NEXA monitoring preferences saved")

    @app.post("/integrations/nexa/monitor/refresh")
    def nexa_monitor_refresh(request: Request):
        gate = auth(request)
        if gate:
            return gate
        # Synchronous, bounded read-only refresh: one catalog GET and at most
        # four account GETs. No NEXA write methods are used by this route.
        try:
            result = monitor_cycle(force=True)
        except Exception:
            return _redirect("error", "Read-only NEXA scan failed; inspect server logs")
        return _redirect(
            "notice",
            f"Read-only scan completed; checked {result['checked']} accounts and "
            f"evaluated {result.get('findings', 0)} action conditions",
        )

    @app.post("/integrations/nexa/monitor/actions/{action_id}/{operation}")
    def nexa_monitor_action(
        request: Request, action_id: int, operation: str,
        note: str = Form(""), db: Session = Depends(get_db),
    ):
        gate = auth(request)
        if gate:
            return gate
        if operation not in ("ack", "resolve", "reopen"):
            return _redirect("error", "Unknown monitoring action")
        if not action_transition(db, action_id, operation, note=note):
            return _redirect("error", "The action was not found or cannot be changed to that status")
        return _redirect("notice", f"Action #{action_id} marked {operation}")
