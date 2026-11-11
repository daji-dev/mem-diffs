
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import sys

_CORE = "hermes_mem_diffs_core"


def _core(*, reload=False):

    path = Path(__file__).resolve().with_name("mem_diffs.py")
    source = path.read_bytes()
    stamp = (str(path), hashlib.sha256(source).hexdigest())
    module = sys.modules.get(_CORE)
    if not reload and getattr(module, "_source_stamp", None) == stamp:
        return module
    spec = importlib.util.spec_from_file_location(_CORE, path)
    fresh = importlib.util.module_from_spec(spec)

    # _core compiles the bytes that supply the source stamp.
    exec(compile(source, str(path), "exec"), fresh.__dict__)
    fresh._source_stamp = stamp

    module = sys.modules.get(_CORE)
    if not reload and getattr(module, "_source_stamp", None) == stamp:
        return module
    sys.modules[_CORE] = fresh
    return fresh


def register(ctx):
    core = _core(reload=True)


    # The callback reads the active profile each time it runs.
    core.bind_profile_source(lambda: ctx.profile_name)
    core.bind_settings_source(lambda key, default: ctx.get_config(key, default))
    capture = core.MemDiffs()
    ctx.register_middleware("tool_execution", capture.tool_execution)
    ctx.register_middleware("llm_request", capture.llm_request)
    ctx.register_hook("pre_llm_call", capture.pre_llm_call)
    ctx.register_hook("transform_llm_output", capture.transform_llm_output)
    ctx.register_hook("on_session_delete", capture.on_session_delete)

    def data(params, *, session_id=None, **kwargs):
        if not isinstance(params, dict) or not isinstance(params.get("path"), str):
            return {"status": 400, "body": {"error": "bad_request"}}
        path = params["path"]
        if path != "settings":
            resource, separator, ident = path.partition("/")
            if resource not in ("receipts", "review-groups") or not separator or not core.ID_RE.fullmatch(ident):
                return {"status": 404, "body": {"error": "not_found"}}
            if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > core.LEDGER_ID_MAX:
                return {"status": 400, "body": {"error": "bad_request"}}
        status, body = core.api_get(path, {"session_id": session_id})
        return {"status": status, "body": body}

    ctx.register_rpc("data", data)
