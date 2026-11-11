
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import sys

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

_CORE = "hermes_mem_diffs_core"


def _core():

    path = Path(__file__).resolve().parents[1] / "mem_diffs.py"
    source = path.read_bytes()
    stamp = (str(path), hashlib.sha256(source).hexdigest())
    module = sys.modules.get(_CORE)
    if getattr(module, "_source_stamp", None) == stamp:
        return module
    spec = importlib.util.spec_from_file_location(_CORE, path)
    fresh = importlib.util.module_from_spec(spec)

    exec(compile(source, str(path), "exec"), fresh.__dict__)
    fresh._source_stamp = stamp

    module = sys.modules.get(_CORE)
    if getattr(module, "_source_stamp", None) == stamp:
        return module
    sys.modules[_CORE] = fresh
    return fresh


router = APIRouter()


def _reply(result):
    status, body = result
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


@router.get("/{path:path}")
def get(path: str, request: Request):


    # Check for source changes on each request because the host mounts this router once.
    return _reply(_core().api_get(path, request.query_params))
