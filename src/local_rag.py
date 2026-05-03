import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import List, Tuple, Optional

try:
    from tree_sitter import Language, Parser
    import tree_sitter_java as tsjava
    _JAVA_LANGUAGE = Language(tsjava.language())
    _TREE_SITTER_AVAILABLE = True
except Exception:
    _TREE_SITTER_AVAILABLE = False
    _JAVA_LANGUAGE = None


@dataclass
class SymbolEntry:
    name: str
    signature: str
    doc: str
    file_path: str
    line: int
    body: str = field(default="")
    embedding: Optional[List[float]] = field(default=None)


class LocalRAG:
    def __init__(self, repo_path: str, openai_client=None):
        self.repo_path = repo_path
        self.index: List[SymbolEntry] = []
        self.client = openai_client
        if _TREE_SITTER_AVAILABLE:
            self.parser = Parser(_JAVA_LANGUAGE)
        else:
            self.parser = None

    def build_index(self):
        self.index = []
        for root, _, files in os.walk(self.repo_path):
            for fname in files:
                if not fname.endswith(".java"):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, encoding="utf-8", errors="ignore") as f:
                        source = f.read()
                    self._extract_symbols(source, fpath)
                except Exception:
                    continue

        if self.client is not None:
            self._build_embeddings()

    def _build_embeddings(self):
        texts = [f"{e.signature} {e.doc}" for e in self.index]
        if not texts:
            return
        try:
            resp = self.client.embeddings.create(
                model="text-embedding-3-small",
                input=texts,
            )
            for entry, emb_data in zip(self.index, resp.data):
                entry.embedding = emb_data.embedding
        except Exception:
            pass  # fall back to TF-IDF silently

    def _extract_symbols(self, source: str, fpath: str):
        if self.parser is not None:
            tree = self.parser.parse(bytes(source, "utf-8"))
            lines = source.splitlines()
            self._walk_node(tree.root_node, lines, fpath, source)
        else:
            self._extract_symbols_regex(source, fpath)

    def _walk_node(self, node, lines: List[str], fpath: str, source: str):
        if node.type in ("method_declaration", "constructor_declaration"):
            name = ""
            params = ""
            return_type = ""
            for child in node.children:
                if child.type == "identifier" and not name:
                    name = child.text.decode("utf-8")
                elif child.type == "formal_parameters":
                    params = child.text.decode("utf-8")
                elif child.type in (
                    "type_identifier", "void_type", "integral_type",
                    "floating_point_type", "boolean_type", "array_type", "generic_type",
                ):
                    return_type = child.text.decode("utf-8")
            signature = f"{return_type} {name}{params}".strip()
            doc = self._get_preceding_doc(node, lines)
            rel_path = os.path.relpath(fpath, self.repo_path)
            body = source[node.start_byte:node.end_byte]
            self.index.append(SymbolEntry(
                name=name,
                signature=signature,
                doc=doc,
                file_path=rel_path,
                line=node.start_point[0] + 1,
                body=body,
            ))
            return  # skip recursing into method body

        for child in node.children:
            self._walk_node(child, lines, fpath, source)

    def _get_preceding_doc(self, node, lines: List[str]) -> str:
        start_line = node.start_point[0]  # 0-indexed
        if start_line == 0:
            return ""
        prev = lines[start_line - 1].strip()
        if not prev.endswith("*/"):
            return ""
        i = start_line - 1
        doc_lines = []
        while i >= 0:
            doc_lines.insert(0, lines[i].strip())
            stripped = lines[i].strip()
            if stripped.startswith("/**") or stripped.startswith("/*"):
                break
            i -= 1
        return " ".join(doc_lines)

    def _extract_symbols_regex(self, source: str, fpath: str):
        pattern = re.compile(
            r'/\*\*(.*?)\*/\s*'
            r'(?:(?:public|private|protected|static|final|abstract|synchronized)\s+)*'
            r'(\w+(?:<[^>]*>)?(?:\[\])*)\s+(\w+)\s*\(([^)]*)\)',
            re.DOTALL,
        )
        rel_path = os.path.relpath(fpath, self.repo_path)
        for m in pattern.finditer(source):
            doc = re.sub(r'\s*\*\s*', ' ', m.group(1)).strip()
            return_type = m.group(2)
            name = m.group(3)
            params = m.group(4)
            line = source[: m.start()].count("\n") + 1
            self.index.append(SymbolEntry(
                name=name,
                signature=f"{return_type} {name}({params})",
                doc=doc,
                file_path=rel_path,
                line=line,
                body="",  # regex mode can't capture body reliably
            ))

    def get_method_body(self, method_name: str, file_path: str = None) -> str:
        """Return the full source of the first method matching name, optionally filtered by file."""
        for entry in self.index:
            if entry.name != method_name:
                continue
            if file_path is not None:
                if file_path not in entry.file_path and entry.file_path not in file_path:
                    continue
            if entry.body:
                return entry.body
            return f"Body not captured for '{method_name}' (regex fallback mode)."
        return f"Method '{method_name}' not found in index."

    @staticmethod
    def _tokenize(text: str) -> Counter:
        tokens = re.findall(r'[a-zA-Z][a-zA-Z0-9]*', text)
        expanded = []
        for t in tokens:
            parts = re.sub(r'([A-Z])', r' \1', t).split()
            expanded.extend(p.lower() for p in parts)
        return Counter(expanded)

    @staticmethod
    def _cosine(a: List[float], b: List[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def _embed_query(self, text: str) -> Optional[List[float]]:
        if self.client is None:
            return None
        try:
            resp = self.client.embeddings.create(
                model="text-embedding-3-small",
                input=[text],
            )
            return resp.data[0].embedding
        except Exception:
            return None

    def _score(self, entry: SymbolEntry, query_tokens: Counter,
               query_embedding: Optional[List[float]] = None) -> float:
        if query_embedding is not None and entry.embedding is not None:
            return self._cosine(query_embedding, entry.embedding)
        text = f"{entry.name} {entry.signature} {entry.doc}"
        entry_tokens = self._tokenize(text)
        if not entry_tokens or not query_tokens:
            return 0.0
        intersection = sum((query_tokens & entry_tokens).values())
        union = sum((query_tokens | entry_tokens).values())
        return intersection / union if union > 0 else 0.0

    def query(self, buggy_code: str, top_k: int = 5) -> Tuple[str, List[SymbolEntry]]:
        if not self.index:
            return "No local symbols found in the repository.", []

        query_tokens = self._tokenize(buggy_code)
        query_embedding = self._embed_query(buggy_code)
        scored = sorted(
            self.index,
            key=lambda e: self._score(e, query_tokens, query_embedding),
            reverse=True,
        )
        top = scored[:top_k]

        lines = ["Relevant local symbols from the repository:"]
        for i, entry in enumerate(top, 1):
            lines.append(f"\n[{i}] {entry.file_path}:{entry.line}")
            lines.append(f"    Signature: {entry.signature}")
            if entry.doc:
                doc_preview = entry.doc[:200] + ("..." if len(entry.doc) > 200 else "")
                lines.append(f"    Doc: {doc_preview}")

        return "\n".join(lines), top
