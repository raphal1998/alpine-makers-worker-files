"""Native equipment UI: signed site calls and local read-only camera state.
No background retry of an action, no offline queue, no direct hardware command.
"""
import base64
import hashlib
import os
import re
import time
if __package__:
    from . import desktop_actions as desk
    from .desktop_actions import TaskError, v_object, v_session, v_text
    from . import desktop_library as library
    from .control_room import control_room
else:
    import desktop_actions as desk
    from desktop_actions import TaskError, v_object, v_session, v_text
    import desktop_library as library
    from control_room import control_room

PATH = "/api/worker-protocol/self/equipment"

def _call(agent, op, args, token):
    try:
        answer = agent.request(PATH, "POST", {"op": op, "args": args, "session_token": token})
    except Exception as error:
        raise library._site_error(error) from None
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        raise TaskError(str((answer or {}).get("error") or "Action refusée."), str((answer or {}).get("code") or ""))
    result = answer.get("response") or {}
    if op == "frame" and result.get("image"):
        data = base64.b64decode(result.pop("image"), validate=True)
        if len(data) > 8 * 1024 * 1024: raise TaskError("Image trop volumineuse.", "FRAME_TOO_LARGE")
        key = hashlib.sha256(str(args.get("equipment", "")).encode()).hexdigest()[:24]
        path = desk.runtime_folder(agent, "cache", "equipment") / (key + ".jpg")
        temporary = path.with_suffix(".part")
        temporary.write_bytes(data)
        os.replace(temporary, path)
        result["path"] = str(path)
        result["received_at"] = time.time()
    return result

def _request(agent, params):
    if not params.get("session_token") or not params.get("op"):
        raise ValueError("Connexion et opération requises.")
    op, args, token = params["op"], params.get("args", {}), params["session_token"]
    def work(task):
        if task.cancelled(): raise desk.TaskCancelled()
        if op == "reconfigure-camera":
            if args.get("confirmed") is not True: raise TaskError("Confirmation requise.", "CONFIRMATION_REQUIRED")
            # Continue jusqu'à la reconnexion même si la fenêtre est fermée entre les étapes.
            target = {"equipment": args.get("equipment"), "confirmed": True}
            _call(agent, "disconnect", target, token)
            try:
                result = _call(agent, "update", args, token)
            except Exception as error:
                try: _call(agent, "connect", target, token)
                except Exception as reconnect: raise TaskError(str(error) + " · Reconnexion impossible : " + str(reconnect), "RECONNECT_FAILED") from None
                raise
            _call(agent, "connect", target, token)
            result["message"] = "Configuration enregistrée ; reconnexion demandée au Worker."
            return library._finish(agent, result)
        return library._finish(agent, _call(agent, op, args, token))
    return desk.start_task(agent, "equipment", "Équipements : " + op, work)

def v_cameras(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 12 or any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", item) for item in value):
        raise ValueError("Liste de caméras invalide (12 au plus).")
    return list(dict.fromkeys(value))

def _frames(agent, params):
    if not params.get("session_token") or not params.get("cameras"):
        raise ValueError("Connexion et caméras requises.")
    def work(task):
        frames = []
        for identifier in params["cameras"]:
            if task.cancelled(): raise desk.TaskCancelled()
            try:
                frames.append({"id": identifier, **_call(agent, "frame", {"equipment": identifier}, params["session_token"])})
            except TaskError as error:
                if error.code in {"WORKER_EXE_SESSION_INVALID", "WORKER_EXE_SESSION_REQUIRED", "UNREACHABLE"}: raise
                frames.append({"id": identifier, "error": str(error), "code": error.code})
        return {"frames": frames}
    return desk.start_task(agent, "equipment", "Images des caméras", work)

DESKTOP_ACTIONS = {
    "request": {"handler": _request, "read": True, "params": {
        "session_token": v_session, "op": v_text(40, empty=False), "args": v_object(depth=12, keys=500)}},
    "local": {"handler": control_room, "read": True, "params": {}},
    "frames": {"handler": _frames, "read": True, "params": {"session_token": v_session, "cameras": v_cameras}},
}
