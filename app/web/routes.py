"""HTTP routes: the upload form and the upload endpoint.

Never log activation codes, confirmation codes, or file contents.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from app.core.models import Branding

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
TEMPLATES_DIR: Path = Path(__file__).parent / "templates"

router = APIRouter()
templates = Jinja2Templates(directory=TEMPLATES_DIR)


def get_branding() -> Branding:
    """Dependency that supplies the default branding (placeholder logo plus settings).

    Tests can override it.
    """
    raise NotImplementedError


async def read_capped(file: UploadFile, limit: int = MAX_UPLOAD_BYTES) -> bytes:
    """Read the upload into memory in chunks, aborting with 413 once ``limit`` is exceeded."""
    raise NotImplementedError


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """GET /: render the upload form."""
    return templates.TemplateResponse(request, "index.html")


@router.post("/upload")
async def upload(
    request: Request,
    file: UploadFile,
    branding: Annotated[Branding, Depends(get_branding)],
) -> Response:
    """POST /upload: validate and convert an uploaded CSV/XLSX.

    Rejects requests whose Content-Length exceeds ``MAX_UPLOAD_BYTES`` (413)
    and extensions other than .csv/.xlsx (415), then reads with
    ``read_capped``. On success it returns ``application/zip``. On validation
    errors it returns the ``_results.html`` partial for HTMX to swap in.
    """
    raise NotImplementedError
