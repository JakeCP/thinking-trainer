"""Fetch each source URL once, cache the bytes, and keep a text layer for verification."""

import hashlib
import io
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SourceDoc

USER_AGENT = "thinking-trainer/0.1 (personal study tool)"


def normalize_text(text: str) -> str:
    """Undo the usual PDF text-layer damage: ligatures, hyphenated line breaks."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)
    return text


def pdf_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    return normalize_text("\n".join(page.extract_text() or "" for page in reader.pages))


def html_text(data: bytes) -> str:
    soup = BeautifulSoup(data, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "svg"]):
        tag.decompose()
    main = soup.find("main") or soup.body or soup
    return normalize_text(main.get_text("\n", strip=True))


def sniff_kind(data: bytes, content_type: str) -> str:
    if data[:5] == b"%PDF-" or "pdf" in content_type:
        return "pdf"
    return "html"


def fetch(db: Session, url: str, cache_dir: Path, *, refetch: bool = False, client: httpx.Client | None = None) -> SourceDoc:
    """Return the SourceDoc for url, fetching it if it isn't cached. Failures are recorded, not raised."""
    doc = db.scalar(select(SourceDoc).where(SourceDoc.url == url))
    if doc is None:
        doc = SourceDoc(url=url)
        db.add(doc)
    elif doc.raw_path and not doc.error and not refetch and Path(doc.raw_path).exists():
        return doc

    doc.fetched_at = datetime.now(timezone.utc)
    doc.error = None
    try:
        owns_client = client is None
        client = client or httpx.Client(follow_redirects=True, timeout=60, headers={"User-Agent": USER_AGENT})
        try:
            resp = client.get(url)
        finally:
            if owns_client:
                client.close()
        doc.http_status = resp.status_code
        if resp.status_code != 200:
            doc.error = f"HTTP {resp.status_code}"
            db.commit()
            return doc
        data = resp.content
        doc.kind = sniff_kind(data, resp.headers.get("content-type", ""))
        if url.lower().endswith(".pdf") and doc.kind != "pdf":
            # A .pdf link answering with HTML is usually a login wall or an error page.
            doc.error = "expected a PDF, got HTML"
    except httpx.HTTPError as e:
        doc.error = f"{type(e).__name__}: {e}"
        db.commit()
        return doc

    doc.sha256 = hashlib.sha256(data).hexdigest()
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{doc.sha256}.{doc.kind}"
    path.write_bytes(data)
    doc.raw_path = str(path)
    try:
        doc.text_layer = pdf_text(data) if doc.kind == "pdf" else html_text(data)
    except Exception as e:  # a malformed PDF still goes to the extractor; verification will flag it
        doc.text_layer = ""
        doc.error = doc.error or f"text layer: {type(e).__name__}: {e}"
    doc.has_text_layer = len(re.findall(r"[A-Za-z]{3,}", doc.text_layer)) >= 50
    db.commit()
    return doc
