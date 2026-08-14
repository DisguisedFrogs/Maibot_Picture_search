"""markdown 结构分析（标题/分节）与 HTML→markdown 提取工具。"""

import re
import threading
import trafilatura

class MarkdownAnalyzer:
    """markdown 结构分析：一次构造、惰性缓存，避免同一调用重复 O(n) 切分。"""

    _HEADING_RE = re.compile(r"^#{1,6}\s")

    def __init__(self, md: str):
        self._md = md
        self._headings: list[str] | None = None
        self._sections: list[tuple[str, str]] | None = None
        self._lock = threading.Lock()

    def headings(self) -> list[str]:
        with self._lock:
            if self._headings is None:
                self._headings = [
                    line for line in self._md.splitlines() if self._HEADING_RE.match(line)
                ]
            return list(self._headings)

    def sections(self) -> list[tuple[str, str]]:
        with self._lock:
            if self._sections is None:
                self._sections = self._split_sections(self._md)
            return list(self._sections)

    @classmethod
    def _split_sections(cls, md: str) -> list[tuple[str, str]]:
        sections = []
        cur_heading = ""
        cur_lines = []
        for line in md.splitlines():
            if cls._HEADING_RE.match(line):
                body = "\n".join(cur_lines).strip()
                if body:
                    sections.append((cur_heading, body))
                cur_heading = line
                cur_lines = []
            else:
                cur_lines.append(line)
        body = "\n".join(cur_lines).strip()
        if body:
            sections.append((cur_heading, body))
        return sections


class PageProcessor:
    @staticmethod
    def extract_text(html: str) -> str:
        text = trafilatura.extract(html, output_format="markdown", include_formatting=True)
        if text is None:
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"[ \t\r\f\v]+", " ", text)
            text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
        return text

    @staticmethod
    def extract_title(html: str) -> str:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
        return "(无标题)"

    @staticmethod
    def truncate_md(md: str, max_chars: int) -> tuple[str, bool]:
        if len(md) <= max_chars:
            return md, False
        cuts = [max_chars]
        for m in re.finditer(r"\n\n", md):
            if m.end() <= max_chars:
                cuts.append(m.end())
        for m in re.finditer(r"```\n", md):
            if m.end() <= max_chars:
                cuts.append(m.end())
        return md[: max(cuts)].rstrip(), True

