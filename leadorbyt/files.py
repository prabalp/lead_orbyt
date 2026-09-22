"""Authenticated CSV downloads, and local-path <-> download-URL translation.

CSV-producing jobs (jobs.py/web_jobs.py/people_jobs.py/signal_jobs.py) write
under `config.OUTPUT_DIR/<user_id>/...` on THIS server's own disk, and keep
passing real filesystem `Path`s to each other internally (chaining into
enrichment, caching in store.py, etc.) -- that part is unchanged.

But this server is a remote multi-tenant HTTP MCP server, not a local
process sharing a filesystem with its caller. A server-local path is useless
to the calling client, so server.py translates every `result_path` it
returns to a client into a `download_url` here before it leaves the
process, via `download_url()`. `resolve_owned_path()` is the inverse, so a
client can hand a previously-returned download_url straight back into a
tool like `enrich_lead_list` and get the real path underneath (still
ownership-checked, exactly as the old direct-path contract was).
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from starlette.requests import Request
from starlette.responses import FileResponse, Response

from . import auth, config
from .errors import ErrorType, LeadOrbytError


def download_url(path: str | None) -> str | None:
    """A CSV's server filesystem path -> a URL the client can GET it from."""
    if not path:
        return None
    candidate = Path(path).expanduser().resolve()
    try:
        rel = candidate.relative_to(config.OUTPUT_DIR.resolve())
    except ValueError as exc:
        raise LeadOrbytError(ErrorType.INTERNAL, f"{path} is not under OUTPUT_DIR") from exc
    return f"{config.PUBLIC_URL}/files/{quote(str(rel), safe='/')}"


def resolve_owned_path(path_or_url: str, user_id: str) -> str:
    """A download_url (or a raw path, for direct/local callers) -> the real
    server path, only if it resolves under this user's own OUTPUT_DIR subtree."""
    parsed = urlparse(path_or_url)
    if parsed.path.startswith("/files/"):
        rel = unquote(parsed.path.removeprefix("/files/"))
        candidate = (config.OUTPUT_DIR / rel).resolve()
    else:
        candidate = Path(path_or_url).expanduser().resolve()

    allowed_root = (config.OUTPUT_DIR / user_id).resolve()
    try:
        candidate.relative_to(allowed_root)
    except ValueError as exc:
        raise LeadOrbytError(
            ErrorType.INVALID_INPUT,
            "lead list must be a CSV previously created for the authenticated user",
        ) from exc
    if candidate.suffix.lower() != ".csv" or not candidate.is_file():
        raise LeadOrbytError(ErrorType.NOT_FOUND, "lead-list CSV does not exist")
    return str(candidate)


async def _download(request: Request) -> Response:
    user_id = auth.require_user_id()
    rel_path = unquote(request.path_params["rel_path"])
    candidate = (config.OUTPUT_DIR / rel_path).resolve()
    allowed_root = (config.OUTPUT_DIR / user_id).resolve()
    try:
        candidate.relative_to(allowed_root)
    except ValueError:
        return Response("Not found", status_code=404)
    if candidate.suffix.lower() != ".csv" or not candidate.is_file():
        return Response("Not found", status_code=404)
    return FileResponse(candidate, media_type="text/csv", filename=candidate.name)


def register(mcp_server) -> None:
    mcp_server.custom_route("/files/{rel_path:path}", methods=["GET"])(_download)
