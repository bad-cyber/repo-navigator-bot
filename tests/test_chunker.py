from src.services.chunker import MAX_CHUNK_CHARS, chunk_repo_file
from src.services.repo_loader import RepoFile


def test_python_ast_chunking_extracts_functions_and_lines() -> None:
    sample_py = (
        "import os\n\n"
        "def verify_domain_age(domain: str, min_days: int = 30) -> bool:\n"
        "    # Проверяем возраст домена для защиты от фишинга\n"
        "    if not domain:\n"
        "        return False\n"
        "    return len(domain) > 3 and min_days >= 30\n\n"
        "async def fetch_remote_rules(url: str) -> str:\n"
        "    cleaned = url.strip()\n"
        "    return cleaned\n"
    )
    rf = RepoFile(path="src/security.py", language="py", content=sample_py)
    chunks = chunk_repo_file(rf)

    assert len(chunks) == 2
    first_chunk, second_chunk = chunks
    assert "verify_domain_age" in first_chunk.content
    assert first_chunk.start_line == 3
    assert first_chunk.end_line == 7
    assert "fetch_remote_rules" in second_chunk.content
    assert second_chunk.start_line == 9


def test_python_class_ast_chunking() -> None:
    sample_py = (
        "class ShieldEngine:\n"
        "    def __init__(self, strict: bool = True) -> None:\n"
        "        self.strict = strict\n\n"
        "    def scan(self, host: str) -> bool:\n"
        "        return bool(host) and self.strict\n"
    )
    rf = RepoFile(path="src/engine.py", language="py", content=sample_py)
    chunks = chunk_repo_file(rf)

    assert len(chunks) == 1
    single = chunks.pop(0)
    assert "class ShieldEngine" in single.content
    assert single.start_line == 1
    assert single.end_line == 6
def test_javascript_ast_chunking() -> None:
    sample_js = (
        "function detectClickjacking(win) {\n"
        "    if (win.top !== win.self) {\n"
        "        return true;\n"
        "    }\n"
        "    return false;\n"
        "}\n"
    )
    rf = RepoFile(path="src/content/guard.js", language="js", content=sample_js)
    chunks = chunk_repo_file(rf)

    assert len(chunks) == 1
    first = chunks.pop(0)
    assert first.language == "js"
    assert "detectClickjacking" in first.content
    assert first.start_line == 1


def test_go_ast_chunking() -> None:
    sample_go = (
        "package main\n\n"
        "func CheckOrigin(origin string) bool {\n"
        "    if len(origin) == 0 {\n"
        "        return false\n"
        "    }\n"
        "    return true\n"
        "}\n"
    )
    rf = RepoFile(path="cmd/server.go", language="go", content=sample_go)
    chunks = chunk_repo_file(rf)

    assert len(chunks) == 1
    first = chunks.pop(0)
    assert "CheckOrigin" in first.content
    assert first.start_line == 3


def test_markdown_fallback_line_chunking_with_overlap() -> None:
    md_text = "\n".join(f"Строка документации номер {i} с полезным описанием." for i in range(1, 70))
    rf = RepoFile(path="README.md", language="md", content=md_text)
    chunks = chunk_repo_file(rf)

    assert len(chunks) >= 2
    first_chunk, second_chunk = chunks
    assert first_chunk.file_path == "README.md"
    assert first_chunk.start_line == 1
    # Проверяем перекрытие строк между соседними чанками
    assert second_chunk.start_line < first_chunk.end_line


def test_top_level_python_without_functions_uses_fallback() -> None:
    py_config = "\n".join(f"SETTING_KEY_{i} = 'value_{i}_configuration'" for i in range(1, 20))
    rf = RepoFile(path="src/constants.py", language="py", content=py_config)
    chunks = chunk_repo_file(rf)

    assert len(chunks) == 1
    first = chunks.pop(0)
    assert "SETTING_KEY_1" in first.content
    assert len(first.content) <= MAX_CHUNK_CHARS


def test_empty_and_tiny_files_return_no_chunks() -> None:
    assert chunk_repo_file(RepoFile(path="empty.py", language="py", content="")) == []
    assert chunk_repo_file(RepoFile(path="tiny.md", language="md", content="short")) == []
