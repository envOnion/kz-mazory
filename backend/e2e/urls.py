"""Production routes plus the built frontend, served by the local test process."""

from pathlib import Path
from django.http import FileResponse, Http404
from django.urls import re_path
from mazory_backend.urls import urlpatterns

DIST = Path(__file__).resolve().parents[2] / "frontend/dist"


def frontend(request, path=""):
    candidate = (DIST / path).resolve()
    if not candidate.is_relative_to(DIST.resolve()):
        raise Http404()
    if not candidate.is_file():
        candidate = DIST / "index.html"
    return FileResponse(candidate.open("rb"))


urlpatterns = [*urlpatterns, re_path(r"^(?P<path>.*)$", frontend)]
