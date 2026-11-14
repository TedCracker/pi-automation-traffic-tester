"""HTTP API for Settings -> Network. `register(app, st)` adds the routes (they sit behind the normal password gate, like every /api/ route):

  GET  /api/net/config        saved + live state: Wi-Fi mode, every Ethernet port (link, speed, IP, label), the port you are using, a pending revert
  POST /api/net/wifi/scan     networks in range (not possible while the Pi is its own access point)
  POST /api/net/plan          dry run in plain words: steps, warnings, affects_current_connection
  POST /api/net/apply         {"changes": {...}, "confirm": true}; 409 when it affects the connection in use and confirm is not true
  GET  /api/net/pending       the revert countdown (works after reconnecting) + the last result
  POST /api/net/keep | /api/net/revert
  POST /api/net/fallback      {"enabled": bool}  the emergency access point on/off

`st` is the AppState: only st.store.get_setting / set_setting are used (reads `pi_name`; writes net_wifi_mode, net_labels, net_fallback)."""
from __future__ import annotations

from typing import Optional

from fastapi import Body, HTTPException, Request
from fastapi.responses import JSONResponse

from ..update import netops
from . import client, plan as planmod, state as statemod

APPLY_WAIT_S = 25.0
HTTP_FOR = {"pending": 409, "busy": 409, "nm_unavailable": 503, "needs_service": 503, "unavailable": 503, "not_permitted": 503, "helper_failed": 503, "timer_failed": 503}


def _client_ip(request: Request) -> Optional[str]:
    return request.client.host if request.client else None


def _fail(res: dict) -> HTTPException:
    return HTTPException(HTTP_FOR.get(res.get("code"), 422), res.get("message") or "The network change did not work.")


def register(app, st) -> None:
    def setting(key, default=None):
        try:
            return st.store.get_setting(key, default)
        except Exception:
            return default

    def config_for(request: Request) -> dict:
        return statemod.build_config(setting, _client_ip(request))

    def validate(changes):
        try:
            return netops.normalize_changes(client.ctx(), changes)
        except netops.NetError as e:
            raise HTTPException(422, e.message)

    def helper(op: str, params: Optional[dict] = None, wait: float = 60.0) -> dict:
        try:
            return client.call(op, params or {}, wait)
        except client.NetClientError as e:
            raise HTTPException(e.status, e.message)

    def mirror() -> None:
        """Remember the result where the rest of the app can read it without the root-owned files."""
        try:
            pub = client.read_state()
            st.store.set_setting("net_wifi_mode", pub.get("wifi_mode"))
            st.store.set_setting("net_labels", pub.get("labels") or {})
            st.store.set_setting("net_fallback", bool((pub.get("fallback") or {}).get("enabled", True)))
        except Exception:
            pass

    @app.get("/api/net/config")
    def net_config(request: Request):
        return config_for(request)

    @app.post("/api/net/wifi/scan")
    def net_scan():
        res = helper("scan", {}, 45)
        if res.get("ok") is None:
            raise HTTPException(504, "The Wi-Fi scan is taking too long. Try again in a moment.")
        if not res.get("ok"):
            raise _fail(res)
        return {"networks": res.get("networks", [])}

    @app.post("/api/net/plan")
    def net_plan(request: Request, body: dict = Body(...)):
        changes = body.get("changes") or {}
        norm = validate(changes)
        cfg = config_for(request)
        return planmod.build_plan(norm, cfg, changes)

    @app.post("/api/net/apply")
    def net_apply(request: Request, body: dict = Body(...)):
        changes = body.get("changes") or {}
        norm = validate(changes)
        cfg = config_for(request)
        p = planmod.build_plan(norm, cfg, changes)
        if p["nothing_to_do"]:
            return {"ok": True, "state": "done", "message": "Nothing needed to change.", "plan": p}
        if p["needs_confirm"] and body.get("confirm") is not True:
            raise HTTPException(409, "This change affects the connection you are using (" + "; ".join(p["reasons"]) + ") Confirm it first: the Pi goes back to the old settings after "
                                f"{p['revert_seconds']} seconds unless you press Keep.")
        res = helper("apply", {"changes": changes, "auto_revert": bool(p["will_auto_revert"]), "revert_seconds": p["revert_seconds"], "summary": p["summary"]}, APPLY_WAIT_S)
        if res.get("ok") is None:                                        # still working (e.g. joining a Wi-Fi network): the page polls /api/net/pending
            return JSONResponse({"ok": None, "state": "working", "id": res.get("id"), "message": "Applying the change…", "plan": p}, status_code=202)
        if not res.get("ok"):
            raise _fail(res)
        mirror()
        return {"ok": True, "state": "applied", "message": res.get("message"), "pending": client.read_pending(), "warnings": res.get("warnings", []), "plan": p}

    @app.get("/api/net/pending")
    def net_pending():
        res = client.read_result()
        pub = client.read_state()
        return {"pending": client.read_pending(), "last_event": pub.get("last_event"), "result": {k: res.get(k) for k in ("id", "op", "ok", "code", "message", "time")} if res else None}

    @app.post("/api/net/keep")
    def net_keep():
        res = helper("keep", {}, 30)
        if not res.get("ok"):
            raise _fail(res)
        mirror()
        return {"ok": True, "message": res.get("message")}

    @app.post("/api/net/revert")
    def net_revert():
        res = helper("revert", {}, 90)
        if res.get("ok") is None:
            return JSONResponse({"ok": None, "message": "Restoring the previous settings…"}, status_code=202)
        if not res.get("ok"):
            raise _fail(res)
        mirror()
        return {"ok": True, "message": res.get("message")}

    @app.post("/api/net/fallback")
    def net_fallback(body: dict = Body(...)):
        en = body.get("enabled")
        if not isinstance(en, bool):
            raise HTTPException(422, "Say whether the emergency access point should be on or off.")
        res = helper("set_fallback", {"enabled": en}, 30)
        if not res.get("ok"):
            raise _fail(res)
        mirror()
        return {"ok": True, "enabled": en}
