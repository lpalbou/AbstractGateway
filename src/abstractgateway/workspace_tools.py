from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional


_DEFAULT_IGNORE_LINES: List[str] = [
    ".git/",
    ".hg/",
    ".svn/",
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    "node_modules/",
    "dist/",
    "build/",
    ".venv/",
    "venv/",
    "env/",
    ".env/",
    ".cursor/",
    "*.d/",
]


@dataclass(frozen=True)
class AbstractIgnoreRule:
    pattern: str
    negate: bool = False
    dir_only: bool = False
    anchored: bool = False


def _parse_rules(lines: Iterable[str]) -> List[AbstractIgnoreRule]:
    rules: List[AbstractIgnoreRule] = []
    for raw in lines:
        line = str(raw or "").strip()
        if not line or line.startswith("#"):
            continue
        negate = False
        if line.startswith("!"):
            negate = True
            line = line[1:].strip()
        if not line:
            continue
        dir_only = line.endswith("/")
        if dir_only:
            line = line[:-1].strip()
        anchored = line.startswith("/")
        if anchored:
            line = line[1:].strip()
        if not line:
            continue
        rules.append(AbstractIgnoreRule(pattern=line, negate=negate, dir_only=dir_only, anchored=anchored))
    return rules


def _find_nearest_abstractignore(start: Path) -> Optional[Path]:
    cur = start if start.is_dir() else start.parent
    cur = cur.resolve()
    for p in (cur, *cur.parents):
        candidate = p / ".abstractignore"
        if candidate.is_file():
            return candidate
    return None


class AbstractIgnore:
    def __init__(self, *, root: Path, rules: List[AbstractIgnoreRule], source: Optional[Path] = None):
        self.root = root.resolve()
        self.rules = list(rules)
        self.source = source.resolve() if isinstance(source, Path) else None

    @classmethod
    def for_path(cls, path: Path) -> "AbstractIgnore":
        start = path if path.is_dir() else path.parent
        ignore_file = _find_nearest_abstractignore(start)
        root = ignore_file.parent if ignore_file is not None else start
        file_rules: List[AbstractIgnoreRule] = []
        if ignore_file is not None:
            try:
                file_rules = _parse_rules(ignore_file.read_text(encoding="utf-8").splitlines())
            except Exception:
                file_rules = []
        rules = _parse_rules(_DEFAULT_IGNORE_LINES) + file_rules
        return cls(root=root, rules=rules, source=ignore_file)

    def _rel(self, path: Path) -> tuple[str, List[str]]:
        p = path.resolve()
        try:
            rel = p.relative_to(self.root)
            rel_str = rel.as_posix()
            parts = list(rel.parts)
        except Exception:
            rel_str = p.as_posix().lstrip("/")
            parts = list(p.parts)
        return rel_str, [str(x) for x in parts if str(x)]

    def is_ignored(self, path: Path, *, is_dir: Optional[bool] = None) -> bool:
        p = path.resolve()
        if is_dir is None:
            try:
                is_dir = p.is_dir()
            except Exception:
                is_dir = False

        rel_str, parts = self._rel(p)
        name = parts[-1] if parts else p.name
        dir_parts = parts if is_dir else parts[:-1]

        ignored = False
        for rule in self.rules:
            pat = rule.pattern
            if not pat:
                continue

            matched = False
            if rule.dir_only:
                for i in range(1, len(dir_parts) + 1):
                    prefix = "/".join(dir_parts[:i])
                    if fnmatch.fnmatchcase(prefix, pat) or fnmatch.fnmatchcase(dir_parts[i - 1], pat):
                        matched = True
                        break
                if not matched and is_dir:
                    matched = fnmatch.fnmatchcase(rel_str, pat) or fnmatch.fnmatchcase(name, pat)
            else:
                if rule.anchored or ("/" in pat):
                    matched = fnmatch.fnmatchcase(rel_str, pat)
                else:
                    matched = fnmatch.fnmatchcase(name, pat)

            if matched:
                ignored = not rule.negate

        return ignored


def _path_for_display(path: Path) -> str:
    try:
        return str(path.expanduser().absolute())
    except Exception:
        try:
            return str(path.expanduser().resolve())
        except Exception:
            return str(path)


def read_file(
    file_path: str,
    *,
    should_read_entire_file: Optional[bool] = None,
    start_line: int = 1,
    end_line: Optional[int] = None,
) -> str:
    try:
        path = Path(file_path).expanduser()
        display_path = _path_for_display(path)

        ignore = AbstractIgnore.for_path(path)
        if ignore.is_ignored(path, is_dir=False):
            return f"Error: File '{display_path}' is ignored by .abstractignore policy"

        if not path.exists():
            return f"Error: File '{display_path}' does not exist"
        if not path.is_file():
            return f"Error: '{display_path}' is not a file"

        max_lines_per_call = 2000

        try:
            inferred_start = int(start_line or 1)
        except Exception:
            inferred_start = 1
        if should_read_entire_file is True:
            read_entire = True
        elif should_read_entire_file is False:
            read_entire = False
        else:
            read_entire = end_line is None and inferred_start == 1

        with open(path, "r", encoding="utf-8") as f:
            if read_entire:
                raw_lines: list[str] = []
                for idx, line in enumerate(f, 1):
                    if idx > max_lines_per_call:
                        preview_lines = raw_lines[: min(len(raw_lines), 60)]
                        num_width = max(1, len(str(len(preview_lines) or 1)))
                        preview = "\n".join([f"{i:>{num_width}}: {line}" for i, line in enumerate(preview_lines, 1)])
                        return (
                            f"Refused: File '{display_path}' is too large to read entirely "
                            f"(> {max_lines_per_call} lines).\n"
                            "Next step: use search_files(...) to find the relevant line number(s), "
                            "then call read_file with start_line/end_line for a smaller range."
                            + ("\n\nPreview (first 60 lines):\n\n" + preview if preview_lines else "")
                        )
                    raw_lines.append(line.rstrip("\r\n"))

                line_count = len(raw_lines)
                num_width = max(1, len(str(line_count or 1)))
                numbered = "\n".join([f"{i:>{num_width}}: {line}" for i, line in enumerate(raw_lines, 1)])
                return f"File: {display_path} ({line_count} lines)\n\n{numbered}"

            try:
                start_line = int(start_line or 1)
            except Exception:
                start_line = 1
            if start_line < 1:
                return f"Error: start_line must be >= 1 (got {start_line})"

            end_line_value = None
            if end_line is not None:
                try:
                    end_line_value = int(end_line)
                except Exception:
                    return f"Error: end_line must be an integer (got {end_line})"
                if end_line_value < 1:
                    return f"Error: end_line must be >= 1 (got {end_line_value})"

            if end_line_value is not None and start_line > end_line_value:
                return f"Error: start_line ({start_line}) cannot be greater than end_line ({end_line_value})"

            if end_line_value is not None:
                requested_lines = end_line_value - start_line + 1
                if requested_lines > max_lines_per_call:
                    return (
                        f"Refused: Requested range would return {requested_lines} lines "
                        f"(> {max_lines_per_call} lines).\n"
                        "Next step: request a smaller range by narrowing end_line, "
                        "or use search_files(...) to target the exact region."
                    )

            selected_lines: list[tuple[int, str]] = []
            last_line_seen = 0
            for line_no, line in enumerate(f, 1):
                last_line_seen = line_no
                if line_no < start_line:
                    continue
                if end_line_value is not None and line_no > end_line_value:
                    break
                selected_lines.append((line_no, line.rstrip("\r\n")))
                if len(selected_lines) > max_lines_per_call:
                    return (
                        f"Refused: Requested range is too large to return in one call "
                        f"(> {max_lines_per_call} lines).\n"
                        "Next step: specify a smaller end_line, "
                        "or split the read into multiple smaller ranges."
                    )

            if last_line_seen < start_line:
                return f"Error: Start line {start_line} exceeds file length ({last_line_seen} lines)"

            end_width = selected_lines[-1][0] if selected_lines else start_line
            num_width = max(1, len(str(end_width)))
            result_lines = [f"{line_no:>{num_width}}: {text}" for line_no, text in selected_lines]
            header = f"File: {display_path} ({len(selected_lines)} lines)"
            return header + "\n\n" + "\n".join(result_lines)
    except UnicodeDecodeError:
        return f"Error: Cannot read '{_path_for_display(Path(file_path).expanduser())}' - file appears to be binary"
    except FileNotFoundError:
        return f"Error: File not found: {_path_for_display(Path(file_path).expanduser())}"
    except PermissionError:
        return f"Error: Permission denied reading file: {_path_for_display(Path(file_path).expanduser())}"
    except Exception as e:
        return f"Error reading file: {e}"


def skim_files(
    paths: list[str],
    *,
    target_percent: float = 8.0,
    head_lines: int = 25,
    tail_lines: int = 25,
) -> str:
    max_output_lines_per_file = 200
    max_chars_per_excerpt = 240

    def _coerce_int(value: object, default: int, *, min_value: int = 0) -> int:
        try:
            i = int(value)
        except Exception:
            i = int(default)
        if i < min_value:
            i = min_value
        return i

    def _coerce_float(value: object, default: float) -> float:
        try:
            return float(value)
        except Exception:
            return float(default)

    def _pick_evenly_spaced(items: list[int], k: int) -> list[int]:
        if k <= 0 or not items:
            return []
        if k >= len(items):
            return list(items)
        if k == 1:
            return [items[len(items) // 2]]
        out: list[int] = []
        n = len(items) - 1
        for i in range(k):
            idx = int(round(i * n / (k - 1)))
            out.append(items[idx])
        seen: set[int] = set()
        uniq: list[int] = []
        for x in out:
            if x in seen:
                continue
            seen.add(x)
            uniq.append(x)
        return uniq

    def _is_heading_line(text: str) -> bool:
        s = str(text or "").strip()
        return bool(s) and bool(re.match(r"^#{1,6}\s+\S", s))

    def _is_structure_marker(line: str) -> bool:
        stripped = str(line or "").strip()
        if not stripped:
            return False
        if _is_heading_line(stripped):
            return True
        if re.match(r"^[-=]{3,}\s*$", stripped):
            return True
        if re.match(r"^([-*+]\s+|\d+\.\s+|\[(?: |x|X)\]\s+)\S", stripped):
            return True
        if stripped.startswith("|") or stripped.startswith(">"):
            return True
        if stripped.startswith("```") or stripped.startswith("~~~"):
            return True
        if re.match(r"^(class|def)\s+\w+", stripped):
            return True
        if stripped.startswith("@") and len(stripped) <= 120:
            return True
        letters = re.sub(r"[^A-Za-z]+", "", stripped)
        if letters and letters.isupper() and len(letters) >= 8 and len(stripped.split()) <= 8:
            return True
        if stripped.endswith(":") and len(stripped) <= 120:
            return True
        return False

    sentence_end_re = re.compile(r"([.!?])(\s+|$)")

    def _first_sentence(text: str) -> str:
        s = " ".join(str(text or "").strip().split())
        if not s:
            return ""
        m = sentence_end_re.search(s)
        if not m:
            return s
        return s[: m.end(1)].strip()

    def _truncate(text: str, limit: int) -> str:
        s = str(text or "").strip()
        if limit <= 0 or len(s) <= limit:
            return s
        return s[: max(1, int(limit) - 1)].rstrip() + "..."

    requested_paths = [str(p or "").strip() for p in (paths or []) if str(p or "").strip()]
    if not requested_paths:
        return (
            "Error: 'paths' is required (provide one or more file paths).\n"
            'Example: {"paths": ["docs/architecture.md", "README.md"], "target_percent": 8.0}'
        )

    pct = _coerce_float(target_percent, 8.0)
    if pct <= 0:
        pct = 8.0
    pct = max(1.0, min(25.0, pct))
    head_lines = _coerce_int(head_lines, 25, min_value=0)
    tail_lines = _coerce_int(tail_lines, 25, min_value=0)

    out_blocks: list[str] = []
    for raw_path in requested_paths:
        path = Path(raw_path).expanduser()
        display_path = _path_for_display(path)
        input_line = f"Input: {raw_path}" if raw_path and not path.is_absolute() else ""
        header_prefix = f"File: {display_path}" + (f"\n{input_line}" if input_line else "")

        ignore = AbstractIgnore.for_path(path)
        if ignore.is_ignored(path, is_dir=False):
            out_blocks.append(f"{header_prefix}\n\nError: File is ignored by .abstractignore policy")
            continue
        if not path.exists():
            out_blocks.append(f"{header_prefix}\n\nError: File does not exist")
            continue
        if not path.is_file():
            out_blocks.append(f"{header_prefix}\n\nError: Path is not a file")
            continue

        total_lines = 0
        marker_lines: list[int] = []
        paragraph_starts: list[int] = []
        heading_lines: set[int] = set()
        heading_followup: dict[int, int] = {}
        pending_headings: list[int] = []
        prev_blank = True

        try:
            with open(path, "r", encoding="utf-8") as f:
                for line_no, line in enumerate(f, 1):
                    total_lines = line_no
                    text = line.rstrip("\r\n")
                    stripped = text.strip()
                    blank = not stripped
                    if prev_blank and not blank:
                        paragraph_starts.append(line_no)
                    prev_blank = blank

                    if _is_heading_line(stripped):
                        heading_lines.add(line_no)
                        pending_headings.append(line_no)
                    elif pending_headings and not blank:
                        for h in pending_headings:
                            heading_followup.setdefault(h, line_no)
                        pending_headings.clear()

                    if len(marker_lines) < 20000 and _is_structure_marker(text):
                        marker_lines.append(line_no)
        except UnicodeDecodeError:
            out_blocks.append(f"{header_prefix}\n\nError: File appears to be binary (cannot decode as UTF-8)")
            continue
        except PermissionError:
            out_blocks.append(f"{header_prefix}\n\nError: Permission denied")
            continue
        except Exception as e:
            out_blocks.append(f"{header_prefix}\n\nError: Failed to read file: {e}")
            continue

        if total_lines <= 0:
            header = f"File: {display_path} (0 lines)"
            if input_line:
                header += "\n" + input_line
            out_blocks.append(header + "\n\n(empty)")
            continue

        target_lines = int((total_lines * pct) / 100.0 + 0.9999)
        budget = max(20, target_lines)
        budget = min(budget, max_output_lines_per_file)

        max_bookends = max(2, int(round(budget * 0.6)))
        bookend_budget = min(head_lines + tail_lines, max_bookends)
        if bookend_budget <= 0:
            bookend_budget = min(2, budget)
        head_take = min(head_lines, (bookend_budget + 1) // 2)
        tail_take = min(tail_lines, bookend_budget - head_take)
        if tail_take <= 0 and total_lines > head_take:
            tail_take = 1
            if head_take + tail_take > bookend_budget and head_take > 1:
                head_take -= 1

        head_range = set(range(1, min(total_lines, head_take) + 1))
        tail_start = max(1, total_lines - tail_take + 1)
        tail_range = set(range(tail_start, total_lines + 1))

        selected: set[int] = set()
        selected |= head_range
        selected |= tail_range

        middle_start = max(1, (max(head_range) + 1) if head_range else 1)
        middle_end = min(total_lines, (min(tail_range) - 1) if tail_range else total_lines)
        remaining_budget = max(0, budget - len(selected))

        if remaining_budget > 0 and middle_start <= middle_end:
            markers_mid = sorted({ln for ln in marker_lines if middle_start <= ln <= middle_end})
            paras_mid = sorted({ln for ln in paragraph_starts if middle_start <= ln <= middle_end})
            marker_budget = max(0, min(int(round(remaining_budget * 0.4)), remaining_budget))
            chosen_markers = _pick_evenly_spaced(markers_mid, marker_budget) if marker_budget else []

            for ln in chosen_markers:
                selected.add(ln)

            if len(selected) < budget:
                for ln in chosen_markers:
                    if len(selected) >= budget:
                        break
                    nxt = ln + 1
                    if middle_start <= nxt <= middle_end:
                        selected.add(nxt)

            remaining_after_markers = max(0, budget - len(selected))
            if remaining_after_markers > 0:
                if paras_mid:
                    for ln in _pick_evenly_spaced(paras_mid, remaining_after_markers):
                        selected.add(ln)
                else:
                    span = max(1, middle_end - middle_start + 1)
                    step = max(1, int(round(span / max(1, remaining_after_markers))))
                    for ln in range(middle_start, middle_end + 1, step):
                        if len(selected) >= budget:
                            break
                        selected.add(ln)

        for ln in list(selected):
            if ln not in heading_lines:
                continue
            follow = heading_followup.get(ln)
            if follow is not None and 1 <= follow <= total_lines:
                selected.add(follow)

        if len(selected) > max_output_lines_per_file:
            mandatory = set(head_range) | set(tail_range)
            for ln in list(selected):
                if ln not in heading_lines:
                    continue
                mandatory.add(ln)
                follow = heading_followup.get(ln)
                if follow is not None and 1 <= follow <= total_lines:
                    mandatory.add(follow)

            if len(mandatory) >= max_output_lines_per_file:
                selected = set(_pick_evenly_spaced(sorted(mandatory), max_output_lines_per_file))
            else:
                picked = _pick_evenly_spaced(sorted(selected - mandatory), max_output_lines_per_file - len(mandatory))
                selected = set(picked) | mandatory

        selected_sorted = sorted(selected)
        num_width = max(1, len(str(total_lines)))

        excerpts: Dict[int, str] = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line_no, line in enumerate(f, 1):
                    if line_no not in selected:
                        continue
                    raw_line = line.rstrip("\r\n")
                    stripped = raw_line.strip()
                    if not stripped:
                        continue
                    excerpt = stripped if _is_structure_marker(raw_line) else _first_sentence(raw_line)
                    excerpts[line_no] = _truncate(excerpt, max_chars_per_excerpt)
                    if len(excerpts) >= max_output_lines_per_file:
                        break
        except UnicodeDecodeError:
            out_blocks.append(f"{header_prefix}\n\nError: File appears to be binary (cannot decode as UTF-8)")
            continue
        except Exception as e:
            out_blocks.append(f"{header_prefix}\n\nError: Failed to read file: {e}")
            continue

        rendered_lines = [f"{ln:>{num_width}}: {text}" for ln in selected_sorted if (text := excerpts.get(ln))]
        header = f"File: {display_path} ({total_lines} lines) - skim {len(rendered_lines)} lines (target {pct:.1f}%)"
        if input_line:
            header += "\n" + input_line
        if rendered_lines:
            out_blocks.append(header + "\n\n" + "\n".join(rendered_lines))
        else:
            out_blocks.append(header + "\n\n(no non-empty excerpts selected)")

    return "\n\n---\n\n".join(out_blocks)


__all__ = ["AbstractIgnore", "AbstractIgnoreRule", "read_file", "skim_files"]
