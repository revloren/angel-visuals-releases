#!/usr/bin/env python3
"""
filetrack — ищет и показывает, куда и как были перемещены или удалены файлы.

Режимы работы:

  snapshot DIR [-o FILE]       Сохранить снимок папки (путь, размер, время, SHA-256).
  diff SNAPSHOT [DIR|SNAPSHOT2] Сравнить снимок с текущим состоянием папки
                                (или с другим снимком) и показать перемещения,
                                переименования, удаления, добавления и изменения.
  watch DIR [-i SEC]           Следить за папкой в реальном времени и печатать
                                события по мере их появления.
  git [PATH] [-n N]            Показать по истории git, какие файлы были
                                перемещены/переименованы/удалены, в каком коммите,
                                кем и когда.
  find NAME                    Найти в истории git всё, что происходило с файлом
                                по имени (куда переехал, когда удалён, последнее
                                содержимое можно восстановить).

Перемещение определяется по совпадению содержимого (SHA-256), поэтому
работает даже если файл одновременно переместили и переименовали.
Если содержимое совпадает лишь частично (переехал и чуть изменился),
в режиме git используется встроенное в git определение похожести.

Только стандартная библиотека Python 3.8+.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SNAPSHOT_VERSION = 1
DEFAULT_IGNORE = [".git", "__pycache__", ".DS_Store", "*.filetrack.json"]

# ---------------------------------------------------------------- вывод ----

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


KIND_STYLE = {
    "moved":    ("➜ ПЕРЕМЕЩЁН",      "36"),
    "renamed":  ("✎ ПЕРЕИМЕНОВАН",   "34"),
    "copied":   ("⧉ СКОПИРОВАН",     "35"),
    "deleted":  ("✖ УДАЛЁН",         "31"),
    "added":    ("✚ ДОБАВЛЕН",       "32"),
    "modified": ("● ИЗМЕНЁН",        "33"),
}


def label(kind: str) -> str:
    text, color = KIND_STYLE[kind]
    return c(text, color + ";1")


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} Б"


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


# -------------------------------------------------------------- снимки ----

@dataclass
class FileInfo:
    size: int
    mtime: float
    sha256: str


def is_ignored(rel: str, patterns: list[str]) -> bool:
    parts = rel.split("/")
    return any(
        fnmatch.fnmatch(part, pat) for pat in patterns for part in parts
    ) or any(fnmatch.fnmatch(rel, pat) for pat in patterns)


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scan(root: Path, ignore: list[str], cache: dict[str, FileInfo] | None = None) -> dict[str, FileInfo]:
    """Обойти папку и посчитать хеши. cache позволяет не перехешировать
    файлы, у которых не поменялись размер и время изменения."""
    result: dict[str, FileInfo] = {}
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = [d for d in dirnames if not is_ignored(rel_dir + d, ignore)]
        for name in filenames:
            rel = rel_dir + name
            if is_ignored(rel, ignore):
                continue
            full = Path(dirpath) / name
            try:
                st = full.stat()
                if not full.is_file():
                    continue
                old = cache.get(rel) if cache else None
                if old and old.size == st.st_size and old.mtime == st.st_mtime:
                    digest = old.sha256
                else:
                    digest = hash_file(full)
            except (OSError, PermissionError):
                continue  # файл исчез во время обхода или нет доступа
            result[rel] = FileInfo(st.st_size, st.st_mtime, digest)
    return result


def save_snapshot(root: Path, files: dict[str, FileInfo], out: Path) -> None:
    data = {
        "version": SNAPSHOT_VERSION,
        "root": str(root.resolve()),
        "created": time.time(),
        "files": {p: [i.size, i.mtime, i.sha256] for p, i in sorted(files.items())},
    }
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def load_snapshot(path: Path) -> tuple[dict, dict[str, FileInfo]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != SNAPSHOT_VERSION:
        sys.exit(f"Неподдерживаемая версия снимка: {path}")
    files = {p: FileInfo(*v) for p, v in data["files"].items()}
    return data, files


# ----------------------------------------------------------- сравнение ----

@dataclass
class Change:
    kind: str
    path: str               # старый путь (для added — новый)
    new_path: str | None = None
    size: int = 0
    note: str = ""
    extra: list[str] = field(default_factory=list)


def classify_move(old: str, new: str) -> str:
    """renamed — та же папка, другое имя; moved — другая папка."""
    return "renamed" if os.path.dirname(old) == os.path.dirname(new) else "moved"


def compare(before: dict[str, FileInfo], after: dict[str, FileInfo]) -> list[Change]:
    changes: list[Change] = []
    gone = {p: i for p, i in before.items() if p not in after}
    new = {p: i for p, i in after.items() if p not in before}

    for p in sorted(set(before) & set(after)):
        if before[p].sha256 != after[p].sha256:
            changes.append(Change("modified", p, size=after[p].size,
                                  note=f"{human_size(before[p].size)} → {human_size(after[p].size)}"))

    # индекс новых файлов по хешу
    new_by_hash: dict[str, list[str]] = defaultdict(list)
    for p, i in new.items():
        new_by_hash[i.sha256].append(p)
    # все текущие файлы по хешу — чтобы находить копии, оставшиеся на месте
    after_by_hash: dict[str, list[str]] = defaultdict(list)
    for p, i in after.items():
        after_by_hash[i.sha256].append(p)

    matched_new: set[str] = set()
    for old_path in sorted(gone):
        info = gone[old_path]
        candidates = [p for p in new_by_hash.get(info.sha256, []) if p not in matched_new]
        if candidates:
            # предпочитаем кандидата с тем же именем файла, затем — из той же папки
            base = os.path.basename(old_path)
            candidates.sort(key=lambda p: (os.path.basename(p) != base,
                                           os.path.dirname(p) != os.path.dirname(old_path), p))
            target = candidates[0]
            matched_new.add(target)
            ch = Change(classify_move(old_path, target), old_path, target, size=info.size)
            if base == os.path.basename(target):
                ch.note = "имя не изменилось"
            elif ch.kind == "moved":
                ch.note = "перемещён и переименован"
            changes.append(ch)
        else:
            ch = Change("deleted", old_path, size=info.size)
            same = [p for p in after_by_hash.get(info.sha256, [])]
            if same:
                ch.note = "но такое же содержимое осталось в:"
                ch.extra = same
            changes.append(ch)

    for p in sorted(new):
        if p in matched_new:
            continue
        info = new[p]
        twins = [q for q in after_by_hash[info.sha256] if q != p and q in before]
        if twins:
            changes.append(Change("copied", twins[0], p, size=info.size, note="копия существующего файла"))
        else:
            changes.append(Change("added", p, size=info.size))

    order = ["moved", "renamed", "deleted", "copied", "modified", "added"]
    changes.sort(key=lambda ch: (order.index(ch.kind), ch.path))
    return changes


def print_changes(changes: list[Change], only: set[str] | None = None, as_json: bool = False) -> None:
    if only:
        changes = [ch for ch in changes if ch.kind in only]
    if as_json:
        print(json.dumps([ch.__dict__ for ch in changes], ensure_ascii=False, indent=2))
        return
    if not changes:
        print(c("Изменений не найдено.", "2"))
        return
    for ch in changes:
        if ch.new_path:
            line = f"{label(ch.kind)}  {ch.path}\n           {c('→', '1')} {c(ch.new_path, '1')}"
        else:
            line = f"{label(ch.kind)}  {ch.path}"
        meta = [human_size(ch.size)] if ch.kind != "modified" else []
        if ch.note:
            meta.append(ch.note)
        if meta:
            line += c(f"  ({', '.join(meta)})", "2")
        print(line)
        for e in ch.extra:
            print(f"           • {e}")
    counts = defaultdict(int)
    for ch in changes:
        counts[ch.kind] += 1
    summary = ", ".join(f"{KIND_STYLE[k][0].split(' ', 1)[1].lower()}: {v}"
                        for k, v in counts.items())
    print(c(f"\nИтого — {summary}", "2"))


# ------------------------------------------------------------ команды ----

def default_snapshot_path(root: Path) -> Path:
    return root / ".filetrack.json"


def cmd_snapshot(args) -> None:
    root = Path(args.dir)
    if not root.is_dir():
        sys.exit(f"Нет такой папки: {root}")
    out = Path(args.output) if args.output else default_snapshot_path(root)
    ignore = DEFAULT_IGNORE + [out.name] + args.ignore
    files = scan(root, ignore)
    save_snapshot(root, files, out)
    total = sum(i.size for i in files.values())
    print(f"Снимок сохранён: {out}  ({len(files)} файлов, {human_size(total)})")


def cmd_diff(args) -> None:
    meta, before = load_snapshot(Path(args.snapshot))
    target = Path(args.target) if args.target else Path(meta["root"])
    if target.is_file():
        meta2, after = load_snapshot(target)
        print(c(f"Снимок {fmt_time(meta['created'])}  →  снимок {fmt_time(meta2['created'])}\n", "2"))
    else:
        if not target.is_dir():
            sys.exit(f"Нет такой папки: {target}")
        ignore = DEFAULT_IGNORE + [Path(args.snapshot).name] + args.ignore
        after = scan(target, ignore, cache=before)
        print(c(f"Снимок {fmt_time(meta['created'])}  →  сейчас ({target.resolve()})\n", "2"))
    changes = compare(before, after)
    print_changes(changes, set(args.only) if args.only else None, args.json)
    if args.update and not target.is_file():
        save_snapshot(target, after, Path(args.snapshot))
        print(c("Снимок обновлён до текущего состояния.", "2"))


def cmd_watch(args) -> None:
    root = Path(args.dir)
    if not root.is_dir():
        sys.exit(f"Нет такой папки: {root}")
    ignore = DEFAULT_IGNORE + args.ignore
    state = scan(root, ignore)
    print(c(f"Слежу за {root.resolve()} ({len(state)} файлов). Ctrl+C — выход.\n", "2"))
    try:
        while True:
            time.sleep(args.interval)
            current = scan(root, ignore, cache=state)
            changes = compare(state, current)
            if changes:
                print(c(f"[{datetime.now():%H:%M:%S}]", "2"))
                print_changes(changes)
                print()
            state = current
    except KeyboardInterrupt:
        print("\nОстановлено.")


def git(*cmd: str, cwd: str = ".") -> str:
    try:
        return subprocess.run(["git", *cmd], cwd=cwd, check=True,
                              capture_output=True, text=True, encoding="utf-8").stdout
    except FileNotFoundError:
        sys.exit("git не найден в PATH.")
    except subprocess.CalledProcessError as e:
        sys.exit(f"Ошибка git: {e.stderr.strip() or e}")


GIT_KIND = {"R": "renamed", "C": "copied", "D": "deleted", "A": "added", "M": "modified"}
SEP = "\x1e"


def git_history(repo: str, path: str | None, limit: int | None, kinds: str) -> list[dict]:
    fmt = f"{SEP}%H%x00%an%x00%at%x00%s"
    cmd = ["log", "--name-status", "-M", "-C", f"--diff-filter={kinds}",
           f"--format={fmt}", "-z"]
    if limit:
        cmd.append(f"-n{limit}")
    if path:
        cmd += ["--", path]
    out = git(*cmd, cwd=repo)
    commits = []
    for block in out.split(SEP)[1:]:
        fields = block.split("\x00")
        sha, author, ts, subject = fields[:4]
        rest = [f.strip("\n") for f in fields[4:]]
        rest = [f for f in rest if f != ""]
        entries, i = [], 0
        while i < len(rest):
            status = rest[i]
            code = status[0]
            if code in "RC":
                entries.append({"kind": GIT_KIND[code], "score": int(status[1:] or 100),
                                "path": rest[i + 1], "new_path": rest[i + 2]})
                i += 3
            else:
                entries.append({"kind": GIT_KIND.get(code, "modified"), "path": rest[i + 1]})
                i += 2
        commits.append({"sha": sha, "author": author, "time": int(ts),
                        "subject": subject, "changes": entries})
    return commits


def print_git_entry(e: dict) -> None:
    kind = e["kind"]
    if kind == "renamed":
        kind = classify_move(e["path"], e["new_path"])
    if e.get("new_path"):
        sim = e.get("score", 100)
        note = "" if sim == 100 else c(f"  (совпадение {sim}%)", "2")
        print(f"  {label(kind)}  {e['path']}\n             {c('→', '1')} {c(e['new_path'], '1')}{note}")
    else:
        print(f"  {label(kind)}  {e['path']}")


def cmd_git(args) -> None:
    kinds = "RD" + ("C" if args.copies else "") + ("A" if args.all else "")
    commits = git_history(args.repo, args.path, args.limit, kinds)
    if args.json:
        print(json.dumps(commits, ensure_ascii=False, indent=2))
        return
    if not commits:
        print(c("В истории нет перемещений и удалений.", "2"))
        return
    for cm in commits:
        print(f"{c(cm['sha'][:8], '33')}  {fmt_time(cm['time'])}  {c(cm['author'], '1')}  {cm['subject']}")
        for e in cm["changes"]:
            print_git_entry(e)
        print()


def cmd_find(args) -> None:
    """Проследить путь файла по истории: где он был, куда переехал, где он сейчас."""
    commits = git_history(args.repo, None, None, "ARDC")
    needle = args.name.lower()

    def matches(p: str) -> bool:
        return needle in p.lower() or fnmatch.fnmatch(p.lower(), needle) \
            or fnmatch.fnmatch(os.path.basename(p).lower(), needle)

    # идём от старых коммитов к новым, собирая цепочки путей
    chains: dict[str, list[tuple[dict, dict]]] = {}   # текущий путь -> события
    for cm in reversed(commits):
        for e in cm["changes"]:
            p, np = e["path"], e.get("new_path")
            if np and p in chains:
                if e["kind"] == "copied":
                    chains[np] = chains[p] + [(cm, e)]
                else:
                    chains[np] = chains.pop(p) + [(cm, e)]
            elif matches(p) or (np and matches(np)):
                key = np or p
                chains.setdefault(key, []).append((cm, e))
            if e["kind"] == "deleted" and p in chains and chains[p][-1][1] is not e:
                chains[p].append((cm, e))

    if not chains:
        print(c(f"В истории git нет файлов, похожих на «{args.name}».", "2"))
        return
    tracked = set(git("ls-files", cwd=args.repo).splitlines())
    for current, events in chains.items():
        alive = current in tracked
        status = c("существует сейчас", "32") if alive else c("сейчас отсутствует", "31")
        print(f"{c(current, '1')} — {status}")
        for cm, e in events:
            print(f"  {c(cm['sha'][:8], '33')} {fmt_time(cm['time'])} {cm['author']}: {cm['subject']}")
            print_git_entry(e)
        last_cm, last_e = events[-1]
        if last_e["kind"] == "deleted":
            print(c(f"  Восстановить:  git checkout {last_cm['sha'][:8]}^ -- \"{last_e['path']}\"", "2"))
        print()


# ---------------------------------------------------------------- main ----

def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="filetrack",
        description="Ищет и показывает, куда и как были перемещены или удалены файлы.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  filetrack snapshot ~/Projects            # запомнить состояние\n"
            "  filetrack diff ~/Projects/.filetrack.json  # что изменилось с тех пор\n"
            "  filetrack watch ./releases               # следить в реальном времени\n"
            "  filetrack git                            # перемещения/удаления в истории git\n"
            "  filetrack find launcher.jar              # что случилось с конкретным файлом\n"
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("snapshot", help="сохранить снимок папки")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("-o", "--output", help="куда сохранить (по умолчанию DIR/.filetrack.json)")
    s.add_argument("--ignore", action="append", default=[], metavar="GLOB")
    s.set_defaults(func=cmd_snapshot)

    d = sub.add_parser("diff", help="сравнить снимок с папкой или другим снимком")
    d.add_argument("snapshot", nargs="?", default=".filetrack.json")
    d.add_argument("target", nargs="?", help="папка или второй снимок (по умолчанию — папка из снимка)")
    d.add_argument("--only", nargs="+", choices=list(KIND_STYLE), help="показать только эти типы")
    d.add_argument("--ignore", action="append", default=[], metavar="GLOB")
    d.add_argument("--json", action="store_true", help="вывод в JSON")
    d.add_argument("-u", "--update", action="store_true", help="после сравнения обновить снимок")
    d.set_defaults(func=cmd_diff)

    w = sub.add_parser("watch", help="следить за папкой в реальном времени")
    w.add_argument("dir", nargs="?", default=".")
    w.add_argument("-i", "--interval", type=float, default=2.0, help="интервал опроса, сек")
    w.add_argument("--ignore", action="append", default=[], metavar="GLOB")
    w.set_defaults(func=cmd_watch)

    g = sub.add_parser("git", help="перемещения и удаления по истории git")
    g.add_argument("path", nargs="?", help="ограничить путём/папкой")
    g.add_argument("-C", "--repo", default=".", help="путь к репозиторию")
    g.add_argument("-n", "--limit", type=int, help="сколько последних коммитов смотреть")
    g.add_argument("--copies", action="store_true", help="показывать и копирования")
    g.add_argument("--all", action="store_true", help="показывать и добавления")
    g.add_argument("--json", action="store_true", help="вывод в JSON")
    g.set_defaults(func=cmd_git)

    f = sub.add_parser("find", help="проследить судьбу файла по истории git")
    f.add_argument("name", help="имя, часть пути или маска (например *.jar)")
    f.add_argument("-C", "--repo", default=".", help="путь к репозиторию")
    f.set_defaults(func=cmd_find)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
