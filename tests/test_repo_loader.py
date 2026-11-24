import io
import zipfile
from unittest.mock import AsyncMock, patch

import pytest

from src.services.repo_loader import _is_relevant_path, download_and_extract_repo


def _build_zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.mark.parametrize(
    ("rel_path", "expected"),
    [
        ("src/main.py", True),
        ("src/content/guard.js", True),
        ("README.md", True),
        ("node_modules/pkg/index.js", False),
        (".git/config", False),
        ("__pycache__/main.cpython-311.pyc", False),
        ("dist/bundle.min.js", False),
        ("assets/styles.min.css", False),
        ("assets/logo.png", False),
    ],
)
def test_is_relevant_path_filtering(rel_path: str, expected: bool) -> None:
    assert _is_relevant_path(rel_path) is expected


@pytest.mark.asyncio
async def test_download_and_extract_repo_extracts_valid_files() -> None:
    zip_data = _build_zip_bytes(
        {
            "repo-main/src/app.py": b"def run():\n    return True\n",
            "repo-main/node_modules/lib.js": b"console.log(1);",
            "repo-main/broken.py": b"\xff\xfe\xfd",
            "repo-main/empty.py": b"",
        }
    )

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.content = zip_data

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("src.services.repo_loader.httpx.AsyncClient", return_value=mock_client):
        files, resolved_branch = await download_and_extract_repo("bad-cyber", "demo")

    assert resolved_branch == "main"
    assert len(files) == 1
    extracted = files.pop(0)
    assert extracted.path == "src/app.py"
    assert extracted.language == "py"
    assert "def run" in extracted.content
@pytest.mark.asyncio
async def test_download_and_extract_repo_falls_back_to_master_branch() -> None:
    zip_data = _build_zip_bytes({"repo-master/main.go": b"package main\n"})

    resp_404 = AsyncMock(status_code=404, content=b"Not Found")
    resp_200 = AsyncMock(status_code=200, content=zip_data)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=[resp_404, resp_200])
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("src.services.repo_loader.httpx.AsyncClient", return_value=mock_client):
        files, resolved_branch = await download_and_extract_repo("bad-cyber", "legacy-repo")

    assert resolved_branch == "master"
    assert len(files) == 1
    assert files.pop(0).path == "main.go"


@pytest.mark.asyncio
async def test_download_and_extract_repo_custom_branch() -> None:
    zip_data = _build_zip_bytes({"repo-dev/src/feature.py": b"x = 1\n"})
    resp_200 = AsyncMock(status_code=200, content=zip_data)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=resp_200)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("src.services.repo_loader.httpx.AsyncClient", return_value=mock_client):
        files, resolved_branch = await download_and_extract_repo("bad-cyber", "demo", branch="dev")

    assert resolved_branch == "dev"
    assert len(files) == 1
    mock_client.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_download_and_extract_repo_raises_on_missing_branch() -> None:
    resp_404 = AsyncMock(status_code=404, content=b"Not Found")
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=resp_404)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("src.services.repo_loader.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(ValueError, match="ветка 'nonexistent' не существует"):
            await download_and_extract_repo("bad-cyber", "demo", branch="nonexistent")


@pytest.mark.asyncio
async def test_download_and_extract_repo_enforces_zip_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.services.repo_loader.settings.max_repo_zip_mb", 1)
    oversized_content = b"x" * (1024 * 1024 + 100)
    mock_resp = AsyncMock(status_code=200, content=oversized_content)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("src.services.repo_loader.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(ValueError, match="превышает лимит"):
            await download_and_extract_repo("bad-cyber", "huge-repo")
