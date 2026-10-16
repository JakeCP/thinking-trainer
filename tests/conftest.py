"""Shared fixtures. Nothing here touches the network or the real API."""

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fpdf import FPDF

from app.config import ROOT, load_settings
from app.db import make_engine, make_sessionmaker
from app.llm import Llm, RawResult
from app.migrate import upgrade


class FakeTransport:
    """Returns queued JSON per prompt name and records what was sent."""

    def __init__(self):
        self.responses: dict[str, list] = {}
        self.sent: list[dict] = []

    def queue(self, prompt_name: str, payload, stop_reason: str = "end_turn") -> None:
        self.responses.setdefault(prompt_name, []).append((payload, stop_reason))

    def send(self, *, prompt, content, schema) -> RawResult:
        self.sent.append({"prompt": prompt, "content": content, "schema": schema})
        if not self.responses.get(prompt.name):
            raise AssertionError(f"no fake response queued for {prompt.name}")
        payload, stop = self.responses[prompt.name].pop(0)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return RawResult(text=text, stop_reason=stop, served_model=prompt.model, input_tokens=100, output_tokens=50)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    s = replace(load_settings(), llm_enabled=True)  # most tests run with the (fake) grader on
    s.data_dir.mkdir(parents=True)
    return s


@pytest.fixture
def Session_(settings):
    upgrade(settings.database_url)
    return make_sessionmaker(make_engine(settings.database_url))


@pytest.fixture
def db(Session_):
    with Session_() as session:
        yield session


@pytest.fixture
def transport():
    return FakeTransport()


@pytest.fixture
def llm(transport):
    return Llm(ROOT / "prompts", transport)


def make_pdf(path: Path, paragraphs: list[str]) -> bytes:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=11)
    for p in paragraphs:
        pdf.multi_cell(0, 6, p)
        pdf.ln(3)
    pdf.output(str(path))
    return path.read_bytes()


@pytest.fixture
def mock_http():
    """An httpx client whose responses come from a dict url → (status, bytes, content-type)."""
    routes: dict[str, tuple[int, bytes, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        status, body, ctype = routes.get(str(request.url), (404, b"not found", "text/plain"))
        return httpx.Response(status, content=body, headers={"content-type": ctype})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    client.routes = routes
    yield client
    client.close()
