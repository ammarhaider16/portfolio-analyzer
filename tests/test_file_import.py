"""Portfolio files preserve cash and remain drafts until the owner saves."""

import base64
import re
from io import BytesIO
from datetime import datetime, timezone
import openpyxl
from fastapi.testclient import TestClient
from betafolio.imports import parse_holdings_file
from betafolio.web import create_app


def test_excel_upload_returns_a_draft_and_never_saves(tmp_path):
    book = openpyxl.Workbook()
    book.active.append(["Ticker", "Price"])
    book.active.append(["AAPL", 200])
    book.active.append(["CASH", 1500])
    content = BytesIO()
    book.save(content)
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8000") as client:
        token = re.search(r'name="csrf-token" content="([^"]+)"', client.get("/").text).group(1)
        response = client.post(
            "/api/import/file",
            json={
                "filename": "holdings.xlsx",
                "content_base64": base64.b64encode(content.getvalue()).decode(),
                "as_of": datetime.now(timezone.utc).isoformat(),
            },
            headers={"x-csrf-token": token},
        )
        assert response.status_code == 200, response.text
        draft = response.json()
        assert draft["cash"] == "1500" and draft["positions"][0]["quantity"] is None
        assert draft["positions"][0]["price"] == "200"
        assert client.get("/api/status").json()["snapshot"] is None


def test_file_import_rejects_corrupt_workbooks_and_unsupported_extensions():
    import pytest

    for filename in ("holdings.xlsx", "holdings.pdf", "holdings.xlsm"):
        with pytest.raises(ValueError):
            parse_holdings_file(b"invalid", filename, datetime.now(timezone.utc))
