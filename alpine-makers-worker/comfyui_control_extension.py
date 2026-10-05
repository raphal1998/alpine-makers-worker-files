"""Token-protected targeted cancellation for managed older ComfyUI engines.

Installed as custom_nodes/alpine_worker_control/__init__.py; no model nodes.
The queue lock covers both the identity check and the interruption signal.
"""
import hmac
import os
import re
from pathlib import Path

NODE_CLASS_MAPPINGS = {}


def cancel_prompt(queue, prompt_id, interrupt):
    if not isinstance(prompt_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", prompt_id):
        raise ValueError("Invalid prompt identity")
    with queue.mutex:
        active = any(len(entry) > 1 and entry[1] == prompt_id for entry in queue.currently_running.values())
        if active:
            interrupt()
            return {"ok": True, "cancel_requested": True, "engine_stop_confirmed": False}
        removed = queue.delete_queue_item(lambda entry: len(entry) > 1 and entry[1] == prompt_id)
        return {"ok": True, "cancel_requested": bool(removed), "engine_stop_confirmed": True}


def register_routes(server, web, interrupt):
    @server.routes.post("/alpine-worker/jobs/{prompt_id}/cancel")
    async def cancel(request):
        try:
            token_path = os.environ.get("ALPINE_WORKER_CONTROL_TOKEN_FILE", "")
            token = Path(token_path).read_text(encoding="utf-8").strip() if token_path else ""
        except (OSError, UnicodeError):
            token = ""
        supplied = str(request.headers.get("X-Alpine-Worker-Token", ""))
        if len(token) < 32 or not hmac.compare_digest(token, supplied):
            return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)
        try:
            result = cancel_prompt(server.prompt_queue, request.match_info["prompt_id"], interrupt)
        except ValueError:
            return web.json_response({"ok": False, "error": "Invalid prompt identity"}, status=400)
        return web.json_response(result)


# Importing the helper for unit tests must not require ComfyUI/PyTorch.
if __name__ != "worker_agent.comfyui_control_extension":
    from aiohttp import web
    import nodes
    from server import PromptServer
    register_routes(PromptServer.instance, web, nodes.interrupt_processing)
