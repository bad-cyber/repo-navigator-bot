from dataclasses import dataclass

from tree_sitter_language_pack import get_parser

from src.services.repo_loader import RepoFile

# Маппинг расширений файлов на имена грамматик tree-sitter
EXT_TO_TS_LANG = {
    "py": "python",
    "js": "javascript",
    "jsx": "javascript",
    "ts": "typescript",
    "tsx": "tsx",
    "go": "go",
    "rs": "rust",
    "java": "java",
    "kt": "kotlin",
    "c": "c",
    "cpp": "cpp",
    "cs": "c_sharp",
    "php": "php",
    "rb": "ruby",
}

# Типы AST-узлов, представляющие целые функции, классы и методы
AST_TARGET_NODES = {
    "function_definition",
    "async_function_definition",
    "class_definition",
    "function_declaration",
    "class_declaration",
    "method_definition",
    "arrow_function",
    "function_item",
    "struct_item",
    "impl_item",
    "method_declaration",
}

MAX_CHUNK_CHARS = 1800
FALLBACK_LINES = 45
OVERLAP_LINES = 8


@dataclass
class CodeChunk:
    file_path: str
    language: str
    start_line: int
    end_line: int
    content: str
def _fallback_line_chunks(repo_file: RepoFile) -> list[CodeChunk]:
    """Построчное разбиение с перекрытием для Markdown, YAML, HTML и длинных файлов."""
    lines = repo_file.content.splitlines()
    if not lines:
        return []

    chunks: list[CodeChunk] = []
    step = max(1, FALLBACK_LINES - OVERLAP_LINES)

    for idx in range(0, len(lines), step):
        slice_lines = lines[idx : idx + FALLBACK_LINES]
        text = "\n".join(slice_lines).strip()
        if len(text) < 25:
            continue
        chunks.append(
            CodeChunk(
                file_path=repo_file.path,
                language=repo_file.language,
                start_line=idx + 1,
                end_line=idx + len(slice_lines),
                content=text[:MAX_CHUNK_CHARS],
            )
        )
    return chunks


def chunk_repo_file(repo_file: RepoFile) -> list[CodeChunk]:
    """Разбивает файл репозитория на чанки через AST (tree-sitter) или построчно."""
    ts_lang = EXT_TO_TS_LANG.get(repo_file.language)
    if not ts_lang:
        return _fallback_line_chunks(repo_file)

    try:
        parser = get_parser(ts_lang)
        source_bytes = repo_file.content.encode("utf-8")
        tree = parser.parse(source_bytes)
    except Exception:
        return _fallback_line_chunks(repo_file)

    ast_chunks: list[CodeChunk] = []
    stack = [tree.root_node]

    while stack:
        node = stack.pop()
        if node.type in AST_TARGET_NODES:
            snippet = source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="ignore").strip()
            if 30 <= len(snippet) <= MAX_CHUNK_CHARS:
                ast_chunks.append(
                    CodeChunk(
                        file_path=repo_file.path,
                        language=repo_file.language,
                        start_line=node.start_point.row + 1,
                        end_line=node.end_point.row + 1,
                        content=snippet,
                    )
                )
                continue
        stack.extend(reversed(node.children))

    # Если в файле только верхнеуровневый код/импорты без функций — используем построчный fallback
    return ast_chunks if ast_chunks else _fallback_line_chunks(repo_file)
