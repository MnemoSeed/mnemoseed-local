"""Retrieval-side cue extractor (PRD-03 FR-3.2 / FR-3.14).

Deterministic, offline, zero-LLM extraction of routing cues from recent
conversation text: entities, project, tools used, and a coarse intent signal.
The extractor emits the same ``Cues`` shape the capture funnel stamps at write
time (FR-1.6), so query-side cues compare directly with the chunk-side cues for
entity overlap and for the situational-weak-cue rerank weight.

Red lines respected:
- The verbatim channel is not touched: this module only reads text.
- No clocks, no randomness, no network: identical input always yields
  identical extracted cues.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from mnemoseed_local.schema.stamp import Cues

# --------------------------------------------------------------- intent enum


class Intent(StrEnum):
    """Coarse request intent, classified deterministically from markers."""

    DEBUG = "debug"
    IMPLEMENT = "implement"
    DECIDE = "decide"
    RECALL = "recall"
    OTHER = "other"


# --------------------------------------------------------------- output types


@dataclass(frozen=True)
class ExtractedCues:
    """One deterministic extraction pass: cues plus the intent signal."""

    cues: Cues
    intent: Intent
    intent_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class CueConfig:
    """Bounded tunables for the extractor."""

    entities_cap: int = 16
    tools_cap: int = 8
    cjk_run_min: int = 2
    cjk_run_max: int = 10


# ------------------------------------------------------- intent marker data
#
# Both languages are required (the project is bilingual). ASCII terms use the
# established ASCII-alnum lookaround boundary: Python's `\b` treats CJK
# ideographs as word letters, which would hide an English term glued to CJK
# ("这个bug"). CJK terms match on plain substring.

_DEBUG_EN = (
    "bug",
    "bugs",
    "error",
    "errors",
    "exception",
    "exceptions",
    "crash",
    "crashed",
    "crashing",
    "failed",
    "failure",
    "failing",
    "broken",
    "stuck",
    "debug",
    "debugging",
    "traceback",
    "segfault",
    "timeout",
    "flaky",
    "fix",
    "fixing",
    "weird",
    "wrong",
)
_DEBUG_ZH = (
    "报错",
    "错误",
    "异常",
    "崩溃",
    "挂了",
    "卡死",
    "卡住",
    "失败",
    "修复",
    "排查",
    "定位",
    "复现",
    "翻车",
    "闪退",
    "死机",
    "宕机",
)
_IMPLEMENT_EN = (
    "implement",
    "implementing",
    "implementation",
    "build",
    "building",
    "refactor",
    "refactoring",
    "write",
    "writing",
    "coding",
    "create",
    "creating",
    "add",
    "adding",
    "migrate",
    "migrating",
    "deploy",
    "deploying",
    "integrate",
    "integrating",
    "setup",
)
_IMPLEMENT_ZH = (
    "实现",
    "编写",
    "开发",
    "构建",
    "重构",
    "迁移",
    "部署",
    "集成",
    "搭建",
    "新增",
    "接入",
    "优化",
    "封装",
    "写个",
    "写",
)
_DECIDE_EN = (
    "decide",
    "decided",
    "decision",
    "choose",
    "chose",
    "chosen",
    "instead",
    "adopt",
    "adopting",
    "prefer",
    "preferred",
    "recommend",
    "recommended",
    "from now on",
    "never again",
    "go with",
    "switch to",
    "switching to",
)
_DECIDE_ZH = (
    "决定",
    "打算",
    "以后",
    "改为",
    "改成",
    "改用",
    "换成",
    "弃用",
    "采用",
    "一律",
    "统一用",
    "坚持",
    "就选",
    "从今往后",
    "定稿",
    "拍板",
)
_RECALL_EN = (
    "recall",
    "remember",
    "remind",
    "we discussed",
    "we talked",
    "we agreed",
    "as we said",
    "last time",
    "previous",
    "previously",
    "earlier",
    "before",
    "search",
    "searching",
)
_RECALL_ZH = (
    "记得",
    "上次",
    "之前",
    "以前",
    "讨论过",
    "说过",
    "约定过",
    "回想",
    "查找",
    "搜索",
    "之前说过",
    "我们讨论",
    "刚才提到",
    "前面提到",
    "当时",
)

# Priority order: debug > implement > decide > recall > other. Concrete action
# markers win over weaker conversational signals.
_INTENT_MARKERS: tuple[tuple[Intent, tuple[str, ...]], ...] = (
    (Intent.DEBUG, _DEBUG_EN + _DEBUG_ZH),
    (Intent.IMPLEMENT, _IMPLEMENT_EN + _IMPLEMENT_ZH),
    (Intent.DECIDE, _DECIDE_EN + _DECIDE_ZH),
    (Intent.RECALL, _RECALL_EN + _RECALL_ZH),
)

_TERM_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _term_pattern(term: str) -> re.Pattern[str]:
    pattern = _TERM_RE_CACHE.get(term)
    if pattern is None:
        if term.isascii():
            pattern = re.compile(rf"(?i)(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])")
        else:
            pattern = re.compile(re.escape(term))
        _TERM_RE_CACHE[term] = pattern
    return pattern


def _marker_hits(text: str, markers: tuple[str, ...]) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for term in markers:
        m = _term_pattern(term).search(text)
        if m is not None:
            hits.append((m.start(), term))
    hits.sort()
    return hits


# ------------------------------------------------------------- token sources


_BACKTICK = re.compile(r"`([^`\n]+)`")

_CAP_TOKEN = re.compile(r"(?<![A-Za-z0-9_])([A-Z][A-Za-z0-9_\-]+[a-z][A-Za-z0-9_\-]*)")

# Bare filename tokens with an extension, e.g. "scorer.py".
_BARE_FILE = re.compile(r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,8})(?![A-Za-z0-9_.-])")

_PATH_STRONG = re.compile(
    r"(?<![A-Za-z0-9_.\\/])("
    r"[A-Za-z]:[\\/][^\s]+"
    r"|/[^\s/][^\s]*"
    r"|~[\\/][^\s]+"
    r")"
)
_PATH_REL = re.compile(r"(?<![A-Za-z0-9_.\\/-])([A-Za-z0-9_.-]+[\\/][^\s]+)")

_NOUN_PROJECT = re.compile(
    r"(?<![A-Za-z0-9_])([A-Z][A-Za-z0-9_\-]+)\s+(?:repo|repository|仓库|项目|project)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)

_CAMEL = re.compile(r"^[a-z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*$")
_SNAKE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)+$")
_KEBAB = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")
_DOTTED = re.compile(r"^[a-z][A-Za-z0-9_]*\.[A-Za-z0-9_]+$")
_MCP_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*__[A-Za-z0-9_]+__[A-Za-z0-9_]+$")

# Common internet TLDs: a dotted token ending in one is a hostname, not a tool.
_TLDS = frozenset(
    {
        "com",
        "org",
        "net",
        "io",
        "dev",
        "ai",
        "app",
        "edu",
        "gov",
        "mil",
        "int",
        "info",
        "biz",
        "name",
        "me",
        "cc",
        "tv",
        "pro",
        "top",
        "xyz",
        "site",
        "tech",
        "store",
        "cloud",
        "online",
        "co.uk",
        "co",
        "uk",
        "us",
        "de",
        "fr",
        "jp",
        "cn",
        "au",
        "ca",
        "ru",
        "in",
        "br",
        "it",
        "es",
        "nl",
        "se",
        "no",
        "pl",
        "ch",
        "at",
        "be",
        "dk",
        "fi",
        "ie",
        "pt",
        "gr",
        "cz",
        "hu",
        "ro",
        "bg",
        "hr",
        "sk",
        "si",
        "lt",
        "lv",
        "ee",
        "il",
        "kr",
        "sg",
        "hk",
        "tw",
        "th",
        "vn",
        "ph",
        "my",
        "id",
        "nz",
        "mx",
        "ar",
        "cl",
        "za",
        "tr",
        "ua",
        "rs",
        "eg",
        "sa",
        "ae",
        "qa",
        "local",
        "example",
        "test",
        "internal",
        "localhost",
    }
)

_DOTTED_SCAN = re.compile(r"(?<![A-Za-z0-9_])([a-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)(?![A-Za-z0-9_])")
_MCP_SCAN = re.compile(
    r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9_]*__[A-Za-z0-9_]+__[A-Za-z0-9_]+)(?![A-Za-z0-9_])"
)

_CAP_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "this",
        "that",
        "these",
        "those",
        "we",
        "i",
        "you",
        "it",
        "he",
        "she",
        "they",
        "our",
        "my",
        "your",
        "etc",
        "via",
        "per",
        "vs",
        "and",
        "but",
        "for",
        "not",
        "are",
        "was",
        "were",
        "will",
        "would",
        "shall",
        "should",
        "could",
        "can",
        "may",
        "might",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "been",
        "am",
        "is",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "from",
        "into",
        "under",
        "over",
        "after",
        "before",
        "during",
        "while",
        "when",
        "where",
        "why",
        "how",
        "what",
        "which",
        "who",
        "whose",
        "if",
        "then",
        "than",
        "so",
        "same",
        "such",
        "too",
        "very",
        "just",
        "more",
        "most",
        "only",
        "also",
        "other",
        "another",
        "about",
        "above",
        "below",
        "next",
        "last",
        "first",
        "second",
        "new",
        "old",
        "big",
        "small",
    }
)

_PROSE_TAILS = frozenset(
    {
        "or",
        "and",
        "of",
        "to",
        "at",
        "in",
        "by",
        "for",
        "on",
        "no",
        "as",
        "via",
        "is",
        "it",
        "a",
        "an",
        "the",
        "per",
    }
)

_PROJECT_GENERIC = frozenset(
    {
        "src",
        "lib",
        "libs",
        "docs",
        "tests",
        "test",
        "build",
        "dist",
        "dist-newstyle",
        "target",
        "node_modules",
        "venv",
        ".venv",
        "env",
        "bin",
        "obj",
        "scripts",
        "assets",
        "public",
        "static",
        "data",
        "tmp",
        "temp",
        "cache",
        "media",
        "files",
        "packages",
        "modules",
        "services",
        "containers",
        "deploy",
        "config",
        "archive",
        "backup",
        "private",
        "personal",
        "projects",
        "project",
        "repos",
        "code",
        "work",
        "dev",
        "home",
        "root",
        "core",
        "app",
        "apps",
        "development",
        "users",
        ".git",
        ".github",
        ".claude",
        ".vscode",
        ".idea",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "issues",
        "pulls",
        "tree",
        "main",
        "master",
        "develop",
        "release",
        "hotfix",
        "feature",
    }
)

_FILE_EXTENSIONS = frozenset(
    {
        "py",
        "ts",
        "js",
        "jsx",
        "tsx",
        "md",
        "rst",
        "txt",
        "json",
        "yaml",
        "yml",
        "toml",
        "ini",
        "cfg",
        "lock",
        "csv",
        "tsv",
        "html",
        "htm",
        "css",
        "scss",
        "png",
        "jpg",
        "jpeg",
        "gif",
        "svg",
        "webp",
        "ico",
        "sh",
        "bat",
        "cmd",
        "ps1",
        "bash",
        "zsh",
        "fish",
        "c",
        "h",
        "cpp",
        "hpp",
        "cc",
        "cs",
        "java",
        "kt",
        "rs",
        "go",
        "rb",
        "php",
        "swift",
        "m",
        "sql",
        "ipynb",
        "so",
        "dll",
        "dylib",
        "exe",
        "bin",
        "o",
        "a",
        "jar",
        "class",
        "wasm",
        "deb",
        "rpm",
        "pdf",
        "zip",
        "tar",
        "gz",
        "xz",
        "bz2",
        "7z",
        "rar",
        "mp3",
        "wav",
        "mp4",
        "mov",
        "mkv",
        "avi",
        "log",
        "out",
        "tmp",
        "bak",
    }
)

# --------------------------------------------------------------- cjk nouns
#
# Chinese has no word boundaries, so a maximal CJK run is split at
# grammatical-function characters (的了这那...) and then glued/dropped at the
# ends. The result is a bounded, deterministic term candidate — no segmenter.

_CJK_GLUE_STR = "的了吗呢吧啊呀哦噢哈是都在也和与或而但并个把被给让对向从往到于这那之其及等们我你他她它咱自"
_CJK_GLUE = frozenset(_CJK_GLUE_STR)
_CJK_RUN = re.compile(r"[一-鿿]+")

_CJK_DROP_WORDS: tuple[str, ...] = (
    "我们",
    "你们",
    "他们",
    "咱们",
    "问题",
    "东西",
    "时候",
    "地方",
    "方式",
    "方法",
    "情况",
    "方面",
    "部分",
    "意思",
    "样子",
    "事情",
    "观点",
    "想法",
    "内容",
    "现在",
)


def _polish_cjk(piece: str) -> str:
    s = piece.strip(_CJK_GLUE_STR)
    if len(s) < 2:
        return s
    changed = True
    while changed:
        changed = False
        for word in _CJK_DROP_WORDS:
            if s == word:
                return ""
            if s.startswith(word):
                s = s[len(word) :]
                changed = True
                break
            if s.endswith(word):
                s = s[: -len(word)]
                changed = True
                break
    return s


def _cjk_entities(run: str, start: int, cfg: CueConfig) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    piece_start = start
    piece: list[str] = []
    for idx, ch in enumerate(run):
        if ch in _CJK_GLUE:
            if piece:
                _append_cjk_piece(out, piece_start, "".join(piece), cfg)
                piece = []
            piece_start = start + idx + 1
        else:
            if not piece:
                piece_start = start + idx
            piece.append(ch)
    if piece:
        _append_cjk_piece(out, piece_start, "".join(piece), cfg)
    return out


def _append_cjk_piece(out: list[tuple[int, str]], pos: int, piece: str, cfg: CueConfig) -> None:
    polished = _polish_cjk(piece)
    if cfg.cjk_run_min <= len(polished) <= cfg.cjk_run_max:
        out.append((pos, polished))


# ------------------------------------------------------------ path handling


_USER_ROOTS = frozenset({"user", "users", "home"})
_USER_DIRS = frozenset(
    {
        "desktop",
        "documents",
        "downloads",
        "pictures",
        "music",
        "videos",
        "appdata",
        "localappdata",
        "appdata.local",
        "roaming",
        "onedrive",
        "icloud",
        "library",
        "applications",
        "favorites",
        "contacts",
        "links",
        "public",
        "templates",
        "saved searches",
        "windows",
        "system32",
        "syswow64",
        "program files",
        "program files (x86)",
        "programdata",
        "drivers",
        "bin",
        "etc",
    }
)


def _path_segments(tok: str) -> list[str]:
    parts = [p for p in re.split(r"[\\/]", tok) if p]
    if len(parts) >= 2 and parts[0].endswith(":"):
        parts = parts[1:]
    return parts


def _resolve_project(segments: list[str]) -> str | None:
    rest = list(segments)
    for i, seg in enumerate(segments):
        low = seg.casefold()
        if low == "~":
            rest = segments[i + 1 :]
            break
        if low in _USER_ROOTS:
            # Drop the home-root marker and the following username: everything
            # under C:\Users\<name> is user-land, not a project.
            rest = segments[i + 2 :]
            break
    for seg in reversed(rest):
        low = seg.casefold()
        if "." in seg or seg.isdigit() or low in _PROJECT_GENERIC or low in _USER_DIRS:
            continue
        return seg
    return None


def _first_seg_is_hostname(first: str) -> bool:
    host, dot, tld = first.rpartition(".")
    return bool(host) and bool(dot) and tld.casefold() in _TLDS


def _url_repo(segments: list[str]) -> str | None:
    # URL shape host/org/repo/... — the repo is the segment right after the org.
    repo = segments[2] if len(segments) >= 3 else segments[1]
    low = repo.casefold()
    if "." in repo or repo.isdigit() or low in _PROJECT_GENERIC or low in _USER_DIRS or low in _PROSE_TAILS:
        return None
    return repo


def _chomp(tok: str) -> str:
    return tok.lstrip("([{<\"'`").rstrip(".,;:!?)]}>\"'`")


# ------------------------------------------------------------- tool shapes


def _is_tool_name(tok: str) -> bool:
    # Dotted names route through the extension/TLD guard so a backticked
    # filename and its bare spelling classify identically.
    return bool(
        _MCP_TOKEN.match(tok)
        or _CAMEL.match(tok)
        or _SNAKE.match(tok)
        or _KEBAB.match(tok)
        or (_DOTTED.match(tok) and _dotted_tool(tok))
    )


def _dotted_tool(tok: str) -> bool:
    last = tok.rsplit(".", 1)[-1]
    return not last.isdigit() and last.casefold() not in _FILE_EXTENSIONS and last.casefold() not in _TLDS


def _mask_backticks(text: str) -> str:
    chars = list(text)
    for m in _BACKTICK.finditer(text):
        for i in range(m.start(1), m.end(1)):
            chars[i] = " "
    return "".join(chars)


def _dedup_cap(candidates: list[tuple[int, str]], cap: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for _, tok in sorted(candidates):
        key = tok.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(tok)
        if len(result) >= cap:
            break
    return result


def _is_name_like(tok: str) -> bool:
    body = tok[1:]
    return (
        len(tok) >= 2
        and tok.casefold() not in _CAP_STOPWORDS
        and (re.search(r"[A-Z]", body) is not None or re.search(r"[\-_0-9]", tok) is not None)
    )


def _classify_intent(text: str) -> tuple[Intent, tuple[str, ...]]:
    for intent, markers in _INTENT_MARKERS:
        hits = _marker_hits(text, markers)
        if hits:
            return intent, tuple(term for _, term in hits)
    return Intent.OTHER, ()


# --------------------------------------------------------------- the extractor


class CueExtractor:
    """Deterministic retrieval cue extraction over recent conversation text."""

    def __init__(self, config: CueConfig | None = None) -> None:
        self._config = config if config is not None else CueConfig()

    @property
    def config(self) -> CueConfig:
        return self._config

    def extract(
        self,
        text: str,
        *,
        host: str | None = None,
        project: str | None = None,
        time_bucket: str | None = None,
    ) -> ExtractedCues:
        """Extract cues from ``text``; context params ride through as weak cues."""
        masked = _mask_backticks(text)
        entities: list[tuple[int, str]] = []
        tools: list[tuple[int, str]] = []
        projects: list[tuple[int, str]] = []

        # backticked identifiers (also the tool-name shapes inside backticks)
        for m in _BACKTICK.finditer(text):
            inner_start = m.start(1)
            inner = m.group(1)
            cursor = 0
            for raw in inner.split():
                idx = inner.find(raw, cursor)
                pos = inner_start + idx
                cursor = idx + len(raw)
                tok = _chomp(raw)
                if not tok or not any(ch.isalnum() for ch in tok) or tok.isdigit():
                    continue
                entities.append((pos, tok))
                if _is_tool_name(tok):
                    tools.append((pos, tok))

        # capitalized ASCII tokens
        for m in _CAP_TOKEN.finditer(masked):
            tok = m.group(1)
            if _is_name_like(tok):
                entities.append((m.start(), tok))

        # CJK noun runs
        for m in _CJK_RUN.finditer(masked):
            entities.extend(_cjk_entities(m.group(0), m.start(), self._config))

        # bare filenames
        for m in _BARE_FILE.finditer(masked):
            tok = m.group(1)
            if tok.rsplit(".", 1)[-1].casefold() in _FILE_EXTENSIONS:
                entities.append((m.start(), tok))

        # path tokens: basename entities + project mentions
        for strong, pattern in ((True, _PATH_STRONG), (False, _PATH_REL)):
            for m in pattern.finditer(masked):
                tok = _chomp(m.group(1))
                if len(tok) < 2 or ".." in tok:
                    continue
                segments = _path_segments(tok)
                if not segments:
                    continue
                if segments[-1].casefold() in _PROSE_TAILS:
                    continue
                if not segments[-1].isdigit():
                    entities.append((m.start(), segments[-1]))
                if len(segments) >= 2 and _first_seg_is_hostname(segments[0]):
                    repo = _url_repo(segments)
                    if repo is not None:
                        projects.append((m.start(), repo))
                elif strong or len(segments) == 2:
                    root = _resolve_project(segments)
                    if root is not None:
                        projects.append((m.start(), root))

        # explicit project nouns ("X repo / X 项目")
        for m in _NOUN_PROJECT.finditer(text):
            name = m.group(1)
            if name.casefold() not in _CAP_STOPWORDS:
                projects.append((m.start(), name))

        # tool-name mentions outside backticks (dotted and mcp__x__y shapes)
        for m in _DOTTED_SCAN.finditer(masked):
            tok = m.group(1)
            if _dotted_tool(tok):
                tools.append((m.start(), tok))
        for m in _MCP_SCAN.finditer(masked):
            tools.append((m.start(), m.group(1)))

        final_entities = _dedup_cap(entities, self._config.entities_cap)
        final_tools = _dedup_cap(tools, self._config.tools_cap)

        intent, reasons = _classify_intent(masked)

        # Most-recent text mention wins; the context param is the fallback.
        resolved_project = sorted(projects)[-1][1] if projects else None
        effective_project = resolved_project if resolved_project is not None else project

        cues = Cues(
            project=effective_project,
            host=host,
            tools_used=final_tools,
            time_bucket=time_bucket,
            entities=final_entities,
        )
        return ExtractedCues(cues=cues, intent=intent, intent_reasons=reasons)


def extract_cues(
    text: str,
    *,
    host: str | None = None,
    project: str | None = None,
    time_bucket: str | None = None,
    config: CueConfig | None = None,
) -> ExtractedCues:
    """Module-level convenience wrapper around ``CueExtractor``."""
    return CueExtractor(config).extract(
        text,
        host=host,
        project=project,
        time_bucket=time_bucket,
    )
