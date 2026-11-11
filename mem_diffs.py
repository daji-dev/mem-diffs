
from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
import uuid

from hermes_constants import get_hermes_home

PLUGIN_ID = "mem-diffs"
FILE_MAX = 256 * 1024
TOTAL_MAX = 2 * 1024 * 1024
READ_BUDGET = 8 * 1024 * 1024
DIR_FILES_MAX = 200
LEDGER_ID_MAX = 512
ID_RE = re.compile(r"^[0-9a-f]{32}$")


# The plugin removes only the suffix that tool_execution adds.
_RESULT_SUFFIX_RE = re.compile(
    r', "inline_diff": "(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"'
    r', "mem_diff": "[0-9a-f]{32}"(?=\}\Z)'
)

_DIRECTIVE_RE = re.compile(r'\s*::(?:mem-diffs-review|mem-diff)\{id="[0-9a-f]{32}"\}(?:\s*(?=\Z))?')

_MODEL_DIRECTIVE_RE = re.compile(
    r'''\s*::(?:mem-diffs-review|mem-diff)\{(?:[^{}"']|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')*\}(?:\s*(?=\Z))?'''
)
TRACKED_TOOLS = ("memory", "skill_manage")

log = logging.getLogger(__name__)





def current_home() -> Path:
    return Path(get_hermes_home())


_profile_source = None


def bind_profile_source(source) -> None:
    global _profile_source
    _profile_source = source


_settings_source = None


def bind_settings_source(source):
    global _settings_source
    _settings_source = source


def current_notice_mode():

    try:
        if _settings_source is not None:
            mode = _settings_source("review_wording", "on")
        else:


            # REST requests use load_config when the plugin has not registered.
            from hermes_cli.config import load_config
            config = load_config()
            entry = config.get("plugins", {}).get("entries", {}).get(PLUGIN_ID, {})
            settings = entry.get("settings")
            if isinstance(settings, dict) and "review_wording" in settings:
                mode = settings["review_wording"]
            else:
                legacy = entry.get("config")
                mode = legacy.get("review_wording", "on") if isinstance(legacy, dict) else "on"
        return mode if isinstance(mode, str) and mode in ("on", "verbose") else "on"
    except Exception:
        return "on"


def current_review_popover():

    try:
        if _settings_source is not None:
            value = _settings_source("review_popover", False)
        else:

            from hermes_cli.config import load_config
            config = load_config()
            entry = config.get("plugins", {}).get("entries", {}).get(PLUGIN_ID, {})
            settings = entry.get("settings")
            if isinstance(settings, dict) and "review_popover" in settings:
                value = settings["review_popover"]
            else:
                legacy = entry.get("config")
                value = legacy.get("review_popover", False) if isinstance(legacy, dict) else False
        return value is True
    except Exception:
        return False


def _profile_from_home(home: Path) -> str:

    home = Path(home).resolve()
    root = (Path.home() / ".hermes").resolve()
    if home == root:
        return "default"
    if home.parent == root / "profiles" and home.name:
        return home.name
    return "custom"


def current_profile() -> str:

    try:
        name = _profile_source() if _profile_source is not None else ""
        if isinstance(name, str) and name.strip():
            return name
    except Exception:
        log.debug("mem-diffs: ctx.profile_name unavailable", exc_info=True)
    try:
        return _profile_from_home(current_home())
    except Exception:
        log.debug("mem-diffs: hermes home unresolved", exc_info=True)
        return "unknown"




class Snap:

    __slots__ = ("kind", "text", "reason", "sig")

    def __init__(self, kind, text=None, reason=None, sig=None):
        self.kind, self.text, self.reason, self.sig = kind, text, reason, sig

    @property
    def known(self):
        return self.kind == "absent" or self.sig is not None


class Budget:
    def __init__(self, remaining=READ_BUDGET):
        self.remaining = remaining


_OPEN_FLAGS = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
               | getattr(os, "O_NONBLOCK", 0))


def read_prefix(path, limit: int) -> tuple[bytes, bool, os.stat_result]:

    fd = os.open(path, _OPEN_FLAGS)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("special_file")
        chunks, got = [], 0
        # read_prefix reads one more byte to find files that become larger after the
        # file size check.
        while got <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - got))
            if not chunk:
                break
            chunks.append(chunk)
            got += len(chunk)
        return b"".join(chunks), got > limit, st
    finally:
        os.close(fd)


def read_file(path: Path, budget: Budget) -> Snap:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return Snap("absent")
    except OSError:
        return Snap("unavailable", reason="unreadable")
    if stat.S_ISLNK(st.st_mode):
        try:
            return Snap("unavailable", reason="symlink", sig=("link", os.readlink(path)))
        except OSError:
            return Snap("unavailable", reason="unreadable")
    if not stat.S_ISREG(st.st_mode):
        return Snap("unavailable", reason="special_file")
    limit = max(0, min(FILE_MAX, budget.remaining))
    if st.st_size > limit:
        return Snap("unavailable", reason="too_large", sig=("stat", st.st_size, st.st_mtime_ns))
    try:
        data, exceeded, fst = read_prefix(path, limit)
    except ValueError:
        return Snap("unavailable", reason="special_file")
    except FileNotFoundError:
        return Snap("absent")
    except OSError:
        return Snap("unavailable", reason="unreadable")
    budget.remaining -= len(data)
    if exceeded:
        return Snap("unavailable", reason="too_large", sig=("stat", fst.st_size, fst.st_mtime_ns))
    sig = ("sha", len(data), hashlib.sha256(data).hexdigest())
    if b"\x00" in data:
        return Snap("unavailable", reason="binary", sig=sig)
    try:
        return Snap("text", text=data.decode("utf-8"), sig=sig)
    except UnicodeDecodeError:
        return Snap("unavailable", reason="binary", sig=sig)


def snapshot_dir(root: Path, budget: Budget) -> tuple[dict, bool]:

    files = {}
    if not root.is_dir():
        return files, False
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            if len(files) >= DIR_FILES_MAX:
                return files, True
            full = Path(dirpath) / name
            files[full.relative_to(root).as_posix()] = read_file(full, budget)
    return files, False




def _display(path: Path, home: Path) -> str:
    try:
        return path.resolve().relative_to(home.resolve()).as_posix()
    except (ValueError, OSError):
        pass
    with contextlib.suppress(ValueError, RuntimeError):
        return "~/" + path.resolve().relative_to(Path.home().resolve()).as_posix()
    return path.as_posix()


def locate_skill_dirs(names: set) -> set:

    # The host sets skill precedence and rejects name collisions and quarantined project
    # skills. Snapshots use the same lookup rules.
    from tools.skills_tool import locate_skill, skill_lookup_path_error, skill_search_dirs
    roots, _active = skill_search_dirs()
    found = set()
    for name in sorted(names):
        if skill_lookup_path_error(name):
            continue
        error, skill_dir, _skill_md = locate_skill(name, None, roots)
        if not error and skill_dir:
            found.add(Path(skill_dir))
    return found


def skill_names(args: dict) -> set:
    names = set()
    if isinstance(args.get("name"), str) and args["name"].strip():
        names.add(args["name"].strip())
    for op in args.get("operations") or []:
        if isinstance(op, dict) and isinstance(op.get("name"), str) and op["name"].strip():
            names.add(op["name"].strip())
    names |= {n.replace("\\", "/").rsplit("/", 1)[-1] for n in names}
    return names


class Plan:


    def __init__(self, home, tool_name, args, ids):
        self.home, self.tool_name, self.ids = home, tool_name, ids
        self.files: dict[Path, Snap] = {}
        self.dirs: dict[Path, dict] = {}
        self.dir_cut: dict[Path, bool] = {}
        self.names = set()
        self.budget = Budget()
        if tool_name == "memory":
            target = "user" if args.get("target") == "user" else "memory"
            for name in (["USER.md"] if target == "user" else ["MEMORY.md"]):
                path = home / "memories" / name
                self.files[path] = read_file(path, self.budget)
        else:
            self.names = skill_names(args)
            for d in locate_skill_dirs(self.names):
                self.dirs[d], self.dir_cut[d] = snapshot_dir(d, self.budget)

    def keys(self):
        return {str(p) for p in self.files} | {str(d) for d in self.dirs}




def _diff(path, before: Snap, after: Snap, budget: list):

    if before.kind == "absent" and after.kind == "absent":
        return None
    if before.known and after.known and before.kind == after.kind and before.sig == after.sig:
        return None
    status = ("added" if before.kind == "absent" else "deleted" if after.kind == "absent" else "modified")
    entry = {"path": path, "status": status, "reason": None, "diff": None, "additions": 0, "deletions": 0}
    unavailable = next((s for s in (before, after) if s.kind == "unavailable"), None)
    if unavailable is not None:
        entry.update(status="unavailable", reason=unavailable.reason)
        return entry
    old = (before.text or "").splitlines(keepends=True)
    new = (after.text or "").splitlines(keepends=True)
    lines = list(difflib.unified_diff(old, new, fromfile=f"a/{path}", tofile=f"b/{path}", n=3))
    text = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)
    entry["additions"] = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
    entry["deletions"] = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
    if len(text) > budget[0]:
        entry.update(reason="too_large", truncated=True)
        return entry
    budget[0] -= len(text)
    entry["diff"] = text
    return entry


NOT_COMPARED = Snap("unavailable", reason="not_compared")


def build_files(plan: Plan) -> tuple[list, bool, bool]:

    home, budget, out = plan.home, [TOTAL_MAX], []
    truncated = any(plan.dir_cut.values())
    after_budget = Budget()
    for path, before in plan.files.items():
        out.append(_diff(_display(path, home), before, read_file(path, after_budget), budget))
    after_dirs = set(plan.dirs) | locate_skill_dirs(plan.names) if plan.names else set(plan.dirs)
    for d in sorted(after_dirs, key=str):
        before_files, before_cut = plan.dirs.get(d, {}), plan.dir_cut.get(d, False)
        after_files, after_cut = snapshot_dir(d, after_budget)
        truncated |= after_cut
        for rel in sorted(set(before_files) | set(after_files)):

            # NOT_COMPARED prevents incorrect additions or deletions for files that a
            # scan did not read before it stopped.
            before = before_files.get(rel, NOT_COMPARED if before_cut else Snap("absent"))
            after = after_files.get(rel, NOT_COMPARED if after_cut else Snap("absent"))
            if before is NOT_COMPARED and after is NOT_COMPARED:
                continue
            out.append(_diff(_display(d / rel, home), before, after, budget))
    out = [f for f in out if f is not None]
    truncated |= any(f.pop("truncated", False) for f in out) or any(f["reason"] == "not_compared" for f in out)
    uncertain = any(f["reason"] == "unreadable" for f in out)
    return out, truncated, uncertain


def _title(kind, files, args):
    if kind == "memory":
        return "Updated " + ("USER.md" if args.get("target") == "user" else "MEMORY.md")
    names = sorted(skill_names(args)) or ["skill"]
    statuses = {f["status"] for f in files}
    verb = "Created" if statuses == {"added"} else "Deleted" if statuses == {"deleted"} else "Updated"
    shown = [n for n in names if "/" not in n][:2] or names[:2]
    return f"{verb} skill " + ", ".join(shown)


def _result_succeeded(result) -> bool:
    data = result
    if isinstance(result, (bytes, str)):
        try:
            data = json.loads(result)
        except (ValueError, TypeError):
            return False
    return isinstance(data, dict) and data.get("success") is True and not data.get("staged")






_NOTICE_DEFAULTS = (("action", "?"), ("target", "memory"), ("content", ""),
                    ("old_text", ""), ("name", ""), ("old_string", ""), ("new_string", ""))
_SKILL_VERBS = {"create": "created", "patch": "patched", "edit": "rewritten",
                "write_file": "written", "remove_file": "removed", "delete": "deleted"}


def _preview(text, limit):
    return text[:limit] + ("…" if len(text) > limit else "")


def _memory_notice(label, action, fields):
    formats = {"add": ("➕", "content", 120), "replace": ("✏️", "content", 120),
               "patch": ("✏️", "content", 120), "remove": ("➖", "old_text", 60)}
    glyph, field, limit = formats.get(action, (None, "", 0))
    text = (fields.get(field) or "") if glyph else ""
    return f"{label} {glyph} {_preview(text, limit)}" if text else None


def _skill_notice(data, detail, message):
    action, name = detail.get("action", ""), detail.get("name", "")
    change = data.get("_change") if isinstance(data.get("_change"), dict) else {}
    old = change.get("old", "") or detail.get("old_string", "")
    new = change.get("new", "") or detail.get("new_string", "")
    if action == "patch" and (old or new):
        old, new = (_preview(text, 80).replace("\n", " ") for text in (old, new))
        return f'📝 Skill \'{name}\' patched: "{old}" → "{new}"'
    verb = {"create": "created", "edit": "rewritten"}.get(action)
    if verb and change.get("description"):
        return f"📝 Skill '{name}' {verb}: {change['description']}"
    return f"📝 {message}" if message else f"Skill {action}"


def _notice_lines(data, detail, verbose):
    if data.get("staged"):
        return [data["message"]] if data.get("proposal_staged") and data.get("message") else []
    message = data.get("message", "")
    target = data.get("target", "") or detail.get("target", "")
    is_skill = detail.get("tool") == "skill_manage"
    if is_skill and "results" in data:
        results = data.get("results")
        if not data.get("operations_applied") or not isinstance(results, list):
            return []
        lines = []
        for result in results:
            if not isinstance(result, dict) or result.get("success") is not True:
                continue
            verb = _SKILL_VERBS.get(result.get("action") or "")
            if verb and result.get("name"):
                path = f" ({result['file_path']})" if result.get("file_path") else ""
                lines.append(f"Skill '{result['name']}' {verb}{path}")
        return lines
    lower = message.lower()
    if not verbose and ("created" in lower or "updated" in lower or
                        (is_skill and any(word in lower for word in ("patched", "deleted", "written")))):
        return [message]
    if not is_skill and not target:
        return []
    label = "Skill" if is_skill else {"memory": "Memory", "user": "User profile"}.get(target, target)
    if verbose:
        if is_skill:
            return [_skill_notice(data, detail, message)]
        ops = detail.get("operations")
        if isinstance(ops, list) and ops:
            return [line for op in ops if isinstance(op, dict)
                    if (line := _memory_notice(label, op.get("action", ""), op))]
        return [_memory_notice(label, detail.get("action", ""), detail) or f"{label} updated"]
    hit = any(word in lower for word in ("added", "replaced", "removed", "applied")) or (target and "add" in lower)
    return [f"{label} updated"] if hit else []


def capture_notice(tool_name, args, result):

    data = json.loads(result) if isinstance(result, (str, bytes)) else result
    if not isinstance(data, dict) or not data.get("success") or tool_name not in TRACKED_TOOLS:
        return {"on": [], "verbose": []}
    detail = {"tool": tool_name, "operations": args.get("operations") or [],
              **{key: args.get(key, default) for key, default in _NOTICE_DEFAULTS}}
    return {mode: _notice_lines(data, detail, mode == "verbose") for mode in ("on", "verbose")}





# Use receipt paths and names to find roots for deleted skills.
def _summary_files(receipt, files):

    paths = list(dict.fromkeys(f["path"] for f in files if f.get("path")))


    names = set()
    notice = receipt.get("notice")
    if isinstance(notice, dict):
        for lines in notice.values():
            for line in lines:
                names.update(n.rsplit("/", 1)[-1] for n in re.findall(r"Skill '([^']+)'", line))
    title = re.fullmatch(r"(?:Created|Updated|Deleted) skill (.+)", receipt["title"])
    if title:
        names.update(n.rsplit("/", 1)[-1] for n in title[1].split(", "))
    roots = {p.rsplit("/", 1)[0] for p in paths if p.endswith("/SKILL.md")}
    supporting = ("references", "templates", "scripts", "assets")
    for path in paths:
        if any(path.startswith(root + "/") for root in roots):
            continue
        parts = path.split("/")


        at = next((i for i, part in enumerate(parts[:-1])
                   if part in names and parts[i + 1] in supporting), None)
        if at is None:
            at = next((i for i in range(len(parts) - 2, -1, -1) if parts[i] in names), None)
        if at is not None:
            roots.add("/".join(parts[:at + 1]))
    result = []
    for path in paths:
        parts = path.split("/")
        if len(parts) >= 2 and parts[-2] == "memories" and parts[-1] in ("MEMORY.md", "USER.md"):
            shown = parts[-1]
        else:
            root = next((r for r in sorted(roots, key=len, reverse=True) if path.startswith(r + "/")), None)
            if root is not None:
                shown = path[len(root) + 1:]
            else:


                at = next((i for i, p in enumerate(parts[:-1]) if p in supporting), None)
                shown = "/".join(parts[at:]) if at is not None else path
        if shown not in result:
            result.append(shown)
    return result


def _with_summary_files(line, files):


    if not files or any(re.search(r"(?<![\w./-])" + re.escape(path) + r"(?![\w./-])", line) for path in files):
        return line
    return f"{line} ({', '.join(files)})"


DETAIL_KEYS = ("id", "title", "created_at", "file_count", "additions", "deletions",
               "uncertain", "concurrent", "truncated")


# Allow only one database-open worker, even after its caller times out.
_short_open_slot = threading.Lock()


class Store:
    def __init__(self, home=None):
        self.home = Path(home) if home is not None else current_home()
        self.path = self.home / "plugin-data" / PLUGIN_ID / "receipts.sqlite3"

    def _open(self, deadline) -> sqlite3.Connection:
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from plugins.plugin_storage import plugin_data_dir, plugin_db

        # Set the home override here because new threads do not inherit it.
        token = set_hermes_home_override(self.home)
        try:
            directory = plugin_data_dir(PLUGIN_ID)
            os.chmod(directory.parent, 0o700)
            os.chmod(directory, 0o700)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            os.close(fd)
            os.chmod(self.path, 0o600)
            attempt = 0
            while True:
                if attempt and time.monotonic() >= deadline:
                    raise sqlite3.OperationalError("database locked: safe opener timeout")
                try:
                    return plugin_db(PLUGIN_ID, "receipts.sqlite3")
                except sqlite3.OperationalError as exc:
                    attempt += 1
                    remaining = deadline - time.monotonic()
                    if str(exc).lower() != "database is locked" or attempt >= 20 or remaining <= 0:
                        raise
                    # WAL startup can fail immediately under competing cold opens,
                    # even though subsequent transaction locking uses busy_timeout.
                    time.sleep(min(0.01 * attempt, remaining))
        finally:
            reset_hermes_home_override(token)

    def _open_short(self, deadline) -> sqlite3.Connection:
        # plugin_db has no timeout parameter, so a worker lets the caller stop waiting.
        # Its connections can be passed between threads.
        if not _short_open_slot.acquire(blocking=False):
            raise sqlite3.OperationalError("database locked: safe opener still busy")
        condition = threading.Condition()
        state = {"done": False, "abandoned": False, "db": None, "error": None}

        def open_worker():
            db, error = None, None
            try:
                try:
                    db = self._open(deadline)
                except BaseException as exc:
                    error = exc
                with condition:
                    if not state["abandoned"]:
                        state.update(done=True, db=db, error=error)
                        db = None  # Ownership transfers to the waiting caller.
                        condition.notify()
            finally:
                try:
                    if db is not None:
                        db.close()
                finally:
                    _short_open_slot.release()

        worker = threading.Thread(target=open_worker, name="mem-diffs-db-open", daemon=True)
        try:
            worker.start()
        except BaseException:
            _short_open_slot.release()
            raise
        with condition:
            if not condition.wait_for(lambda: state["done"], timeout=max(0, deadline - time.monotonic())):
                state["abandoned"] = True
                raise sqlite3.OperationalError("database locked: safe opener timeout")
            if state["error"] is not None:
                raise state["error"]
            return state["db"]

    @contextlib.contextmanager
    def connect(self, create=False, *, write=False, timeout=10.0):
        if not self.path.exists() and not create:
            yield None
            return
        deadline = time.monotonic() + max(0, timeout)
        db = self._open_short(deadline) if timeout < 5.0 else self._open(deadline)
        try:
            db.isolation_level = None
            db.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
            if db.execute("PRAGMA user_version").fetchone()[0] < 5:
                db.execute("BEGIN IMMEDIATE")
                self._migrate(db)
                db.commit()
            for sidecar in (f"{self.path}-wal", f"{self.path}-shm"):
                with contextlib.suppress(FileNotFoundError):
                    os.chmod(sidecar, 0o600)
            # INSERT OR IGNORE needs a writer lock even when the row already exists.
            db.execute("BEGIN IMMEDIATE" if create or write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _migrate(db):



        db.execute("""CREATE TABLE IF NOT EXISTS receipts (
            id TEXT PRIMARY KEY, session TEXT NOT NULL, turn TEXT NOT NULL, call_id TEXT NOT NULL,
            created REAL NOT NULL, inline_turn TEXT, acknowledged REAL,
            summary TEXT NOT NULL, files TEXT NOT NULL)""")
        db.execute("CREATE INDEX IF NOT EXISTS receipts_session ON receipts(session, created)")
        db.execute("""CREATE TABLE IF NOT EXISTS review_turns (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
            session TEXT NOT NULL, turn TEXT NOT NULL, created REAL NOT NULL,
            foreground INTEGER NOT NULL DEFAULT 0, completed REAL,
            receipt_count INTEGER NOT NULL DEFAULT 0, gap INTEGER NOT NULL DEFAULT 0,
            UNIQUE(session, turn))""")
        db.execute("""CREATE TABLE IF NOT EXISTS review_members (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, receipt_id TEXT UNIQUE NOT NULL,
            group_id TEXT NOT NULL, session TEXT NOT NULL, turn TEXT NOT NULL,
            created REAL NOT NULL)""")
        db.execute("CREATE INDEX IF NOT EXISTS review_members_group ON review_members(group_id, created)")

        db.execute("""CREATE TABLE IF NOT EXISTS review_ledger (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL,
            id TEXT NOT NULL, started REAL NOT NULL, truncated INTEGER NOT NULL DEFAULT 0)""")
        db.execute("INSERT OR IGNORE INTO review_ledger(singleton,version,id,started) VALUES (1,1,?,?)",
                   (uuid.uuid4().hex, time.time()))


        db.execute("UPDATE review_ledger SET version=2")
        db.execute("CREATE TABLE IF NOT EXISTS deleted_sessions (session TEXT PRIMARY KEY)")
        columns = {r[1] for r in db.execute("PRAGMA table_info(review_turns)")}
        if "announced_turn" not in columns:
            db.execute("ALTER TABLE review_turns ADD COLUMN announced_turn TEXT")
        db.execute("PRAGMA user_version=5")

    def add(self, receipt: dict, files: list):
        summary = {k: receipt[k] for k in DETAIL_KEYS}
        if "notice" in receipt:
            summary["notice"] = receipt["notice"]
        with self.connect(create=True) as db:
            if self._deleted(db, receipt["session_id"]):
                return
            db.execute("INSERT INTO receipts (id,session,turn,call_id,created,summary,files) VALUES (?,?,?,?,?,?,?)", (
                receipt["id"], receipt["session_id"], receipt["turn_id"], receipt["tool_call_id"],
                receipt["created_at"], json.dumps(summary), json.dumps(files)))
            # The savepoint keeps the receipt when the plugin rolls back review
            # metadata.
            db.execute("SAVEPOINT review_metadata")
            try:
                if receipt["session_id"] and receipt["turn_id"]:
                    group = self._review_turn(db, session_scope(receipt["session_id"]), receipt["turn_id"],
                                              receipt["session_id"], receipt["created_at"])
                    if group is not None:
                        db.execute("UPDATE review_turns SET receipt_count=receipt_count+1 WHERE id=?", (group[0],))
                        if group[1] is not None:
                            db.execute("UPDATE review_turns SET gap=1 WHERE id=?", (group[0],))
                        else:
                            db.execute("INSERT INTO review_members(receipt_id,group_id,session,turn,created) VALUES (?,?,?,?,?)",
                                       (receipt["id"], group[0], receipt["session_id"], receipt["turn_id"], receipt["created_at"]))
            except Exception:
                db.execute("ROLLBACK TO review_metadata")
                log.warning("mem-diffs: receipt saved without lifecycle metadata", exc_info=True)
            finally:
                db.execute("RELEASE review_metadata")
        return True

    @staticmethod
    def _deleted(db, session):
        return db.execute("SELECT 1 FROM deleted_sessions WHERE session=?", (session,)).fetchone() is not None

    def delete_sessions(self, sessions):



        # Tombstones stop receipt inserts from captures that complete after session
        # deletion.
        with self.connect(create=True, timeout=0.1) as db:
            for session in dict.fromkeys(sessions):
                db.execute("INSERT OR IGNORE INTO deleted_sessions(session) VALUES (?)", (session,))
                db.execute("DELETE FROM receipts WHERE session=?", (session,))
                db.execute("DELETE FROM review_members WHERE session=?", (session,))
                db.execute("DELETE FROM review_turns WHERE session=?", (session,))

    def _review_turn(self, db, sessions, turn, owner, created):
        if (not all(isinstance(i, str) and 0 < len(i) <= LEDGER_ID_MAX for i in (owner, turn))
                or self._deleted(db, owner)):
            return None
        marks = ",".join("?" * len(sessions))
        rows = db.execute(f"SELECT id, completed FROM review_turns WHERE session IN ({marks}) AND turn=? "
                          "ORDER BY seq", (*sessions, turn)).fetchall()
        # If lineage gaps put one turn in different groups, _review_turn sets gap for
        # each group.
        if len(rows) > 1:

            db.executemany("UPDATE review_turns SET gap=1 WHERE id=?", [(r[0],) for r in rows])
            return None
        if rows:
            return rows[0]
        gid = uuid.uuid4().hex
        db.execute("INSERT INTO review_turns(id,session,turn,created) VALUES (?,?,?,?)",
                   (gid, owner, turn, created))
        return gid, None

    def mark_foreground(self, sessions, session, turn):
        if not session or not turn:
            return
        with self.connect(create=True) as db:
            if self._deleted(db, session):
                return
            self._review_turn(db, sessions, turn, session, time.time())

            marks = ",".join("?" * len(sessions))
            db.execute(f"UPDATE review_turns SET foreground=1 WHERE session IN ({marks}) AND turn=?",
                       (*sessions, turn))

    def complete_review(self, sessions, turn, *, owner=None):
        if not sessions or not turn:
            return
        with self.connect(write=True) as db:
            if db is None or (owner is not None and self._deleted(db, owner)):
                return
            marks = ",".join("?" * len(sessions))
            competing = db.execute(f"SELECT id FROM review_turns WHERE session IN ({marks}) AND turn=?",
                                   (*sessions, turn)).fetchall()
            if len(competing) > 1:
                db.executemany("UPDATE review_turns SET gap=1 WHERE id=?", competing)
            db.execute(f"UPDATE review_turns SET completed=? WHERE session IN ({marks}) AND turn=? "
                       "AND completed IS NULL", (time.time(), *sessions, turn))

    def review_group(self, group_id, sessions, mode="on"):

        if not sessions or not isinstance(group_id, str) or not ID_RE.fullmatch(group_id):
            return None
        with self.connect() as db:
            if db is None:
                raise ValueError("ledger unavailable")
            marks = ",".join("?" * len(sessions))
            row = db.execute(f"SELECT session,turn,completed,receipt_count,gap FROM review_turns "
                             f"WHERE id=? AND session IN ({marks}) AND completed IS NOT NULL AND foreground=0 "
                             "AND NOT EXISTS (SELECT 1 FROM deleted_sessions d WHERE d.session=review_turns.session)",
                             (group_id, *sessions)).fetchone()
            if row is None:
                return None
            session, turn, completed, count, gap = row
            observations = db.execute(f"SELECT foreground FROM review_turns WHERE session IN ({marks}) AND turn=?",
                                      (*sessions, turn)).fetchall()
            if any(fg for (fg,) in observations):
                return None
            if db.execute("SELECT 1 FROM review_members m JOIN deleted_sessions d ON d.session=m.session "
                          "WHERE m.group_id=?", (group_id,)).fetchone():
                return None
            members = db.execute("SELECT m.receipt_id,r.summary,r.files FROM review_members m "
                                 "LEFT JOIN receipts r ON r.id=m.receipt_id AND r.session=m.session AND r.turn=m.turn "
                                 f"WHERE m.group_id=? AND m.session IN ({marks}) ORDER BY m.created,m.seq",
                                 (group_id, *sessions)).fetchall()
            if not count:
                return None
            receipts = [{**json.loads(raw), "files": json.loads(files)}
                        for _, raw, files in members if raw is not None]
            missing = [rid for rid, raw, _ in members if raw is None]
            lines, summaries = [], []
            for receipt in receipts:
                files = _summary_files(receipt, receipt["files"])
                notice = receipt.get("notice")
                if isinstance(notice, dict) and mode in notice:
                    lines.extend(notice[mode])
                else:
                    lines.append(_with_summary_files(receipt["title"], files))
                summaries.append({"id": receipt["id"], "title": receipt["title"], "files": files})

            return {"id": group_id, "completed_at": completed,
                    "state": "unavailable" if gap or len(observations) != 1 or count != len(members) or missing else "available",
                    "summary": " · ".join(dict.fromkeys(lines)),
                    "receipt_ids": [rid for rid, _, _ in members], "missing_receipt_ids": missing,
                    "receipts": summaries}

    def get(self, receipt_id, sessions):
        sessions = [s for s in sessions if s]
        if not isinstance(receipt_id, str) or not ID_RE.fullmatch(receipt_id) or not sessions:
            return None
        with self.connect() as db:
            if db is None:
                return None
            marks = ",".join("?" * len(sessions))
            row = db.execute(f"SELECT summary, files FROM receipts WHERE id=? "
                             f"AND session IN ({marks})", (receipt_id, *sessions)).fetchone()
        if not row:
            return None
        summary = json.loads(row[0])
        detail = {k: summary[k] for k in DETAIL_KEYS}
        detail["files"] = json.loads(row[1])
        return detail

    # announce_reviews gives claims to one turn in a write transaction. Transforms that
    # run again for that turn use the same claims.
    def announce_reviews(self, sessions, turn, *, owner):

        if not sessions or not turn:
            return []
        marks = ",".join("?" * len(sessions))
        with self.connect(write=True) as db:
            if db is None or self._deleted(db, owner):
                return []
            if not db.execute(f"SELECT 1 FROM review_turns WHERE session IN ({marks}) AND turn=? AND foreground=1",
                              (*sessions, turn)).fetchone():
                return []
            rows = db.execute(
                f"SELECT g.id FROM review_turns g WHERE g.session IN ({marks}) "
                "AND g.foreground=0 AND g.completed IS NOT NULL "
                "AND (g.announced_turn IS NULL OR g.announced_turn=?) "
                "AND NOT EXISTS (SELECT 1 FROM deleted_sessions d WHERE d.session=g.session) "
                f"AND NOT EXISTS (SELECT 1 FROM review_turns f WHERE f.session IN ({marks}) "
                "AND f.turn=g.turn AND f.foreground=1) "
                "AND NOT EXISTS (SELECT 1 FROM review_members m JOIN deleted_sessions d ON d.session=m.session WHERE m.group_id=g.id) "
                "AND EXISTS (SELECT 1 FROM review_members m JOIN receipts r ON r.id=m.receipt_id "
                f"AND r.session=m.session AND r.turn=m.turn WHERE m.group_id=g.id AND m.session IN ({marks})) "
                "ORDER BY g.completed,g.seq", (*sessions, turn, *sessions, *sessions)).fetchall()
            ids = [gid for (gid,) in rows if ID_RE.fullmatch(gid)]
            db.executemany("UPDATE review_turns SET announced_turn=? WHERE id=?", [(turn, gid) for gid in ids])
            return ids




class MemDiffs:


    def __init__(self):
        self._lock = threading.Lock()
        self._captures: dict[str, tuple[list, set]] = {}

    # _enter finds concurrent captures that use the same key only in this MemDiffs
    # instance.
    def _enter(self, token, keys):

        with self._lock:
            mine = [False]
            for flag, other_keys in self._captures.values():
                if other_keys & keys:
                    flag[0] = mine[0] = True
            self._captures[token] = (mine, keys)

    def _exit(self, token) -> bool:
        with self._lock:
            return self._captures.pop(token, ([False], set()))[0][0]

    def tool_execution(self, **kw):
        next_call, args = kw["next_call"], kw.get("args")
        if not callable(next_call):


            raise TypeError("mem-diffs requires in-process plugins (next_call is not callable)")
        plan = token = None
        if kw.get("tool_name") in TRACKED_TOOLS and kw.get("session_id") and isinstance(args, dict):
            try:


                home = current_home()
                with Store(home).connect(create=True):
                    pass
                plan = Plan(home, kw["tool_name"], args, {
                    "session_id": kw.get("session_id") or "", "turn_id": kw.get("turn_id") or "",
                    "tool_call_id": kw.get("tool_call_id") or ""})
                token = uuid.uuid4().hex
                self._enter(token, plan.keys())
            except Exception:
                log.warning("mem-diffs: before-snapshot failed; this call will have no receipt", exc_info=True)
                plan = None
        try:
            result = next_call(args)
        finally:
            concurrent = self._exit(token) if token is not None else False
        if plan is not None:
            try:
                captured = self._record(plan, args, result, concurrent)


                if captured is not None:
                    receipt, files = captured
                    if isinstance(result, str) and result.endswith("}") and isinstance(json.loads(result), dict):
                        diff = "".join(f["diff"] or "" for f in files)
                        if diff and len(diff) <= TOTAL_MAX:
                            # The tool renderer uses these fields for display.
                            # llm_request removes the added fields from model requests.
                            result = (result[:-1] + ', "inline_diff": ' + json.dumps(diff)
                                      + ', "mem_diff": "' + receipt["id"] + '"}')
            except Exception:
                log.warning("mem-diffs: could not record receipt", exc_info=True)
        return result

    def _record(self, plan: Plan, args, result, concurrent):
        if not _result_succeeded(result):
            return
        files, truncated, uncertain = build_files(plan)
        if not any(f["reason"] != "not_compared" for f in files):
            return
        kind = "memory" if plan.tool_name == "memory" else "skill"
        receipt = {
            "id": uuid.uuid4().hex, "created_at": time.time(),
            "title": _title(kind, files, args), "file_count": len(files),
            "notice": capture_notice(plan.tool_name, args, result),
            "additions": sum(f["additions"] for f in files), "deletions": sum(f["deletions"] for f in files),
            "truncated": truncated, "uncertain": uncertain, "concurrent": concurrent, **plan.ids}
        if Store(plan.home).add(receipt, files):
            return receipt, files

    def llm_request(self, request, **_):

        def strip(value, assistant_text=False):
            if isinstance(value, str):
                restored = _RESULT_SUFFIX_RE.sub("", value)
                return _DIRECTIVE_RE.sub("", restored) if assistant_text else restored
            if isinstance(value, dict):
                marked = ("inline_diff" in value and isinstance(value.get("mem_diff"), str)
                          and ID_RE.fullmatch(value["mem_diff"]) is not None)
                out = {}
                changed = bool(marked)
                for key, child in value.items():
                    if marked and key in ("inline_diff", "mem_diff"):
                        continue


                    # strip removes mem-diff and mem-diffs-review directives only from
                    # assistant content and text blocks. Tool arguments, reasoning
                    # blocks, and other roles keep these directives.
                    text = ((key == "content" and value.get("role") == "assistant")
                            or (key == "text" and assistant_text
                                and value.get("type") in (None, "text", "output_text")))
                    out[key] = strip(child, text)
                    changed |= out[key] is not child
                return out if changed else value
            if isinstance(value, (list, tuple)):
                out = [strip(child, assistant_text) for child in value]
                if any(new is not old for new, old in zip(out, value)):
                    return tuple(out) if isinstance(value, tuple) else out
            return value

        restored = strip(request)
        return {"request": restored} if restored is not request else None

    def on_session_delete(self, session_id="", reason="", profile=None, deleted_ids=None, **_):


        # The host also sends archive events through the deletion hook.
        if reason == "archive":
            return None
        try:
            sessions = [sid for sid in [session_id, *(deleted_ids or [])] if isinstance(sid, str) and sid]
            if sessions:
                Store(current_home()).delete_sessions(sessions)
        except Exception:
            log.warning("mem-diffs: session cleanup failed", exc_info=True)
        return None

    # pre_llm_call records foreground turns. The host does not call this hook for
    # detached review forks.
    def pre_llm_call(self, session_id="", turn_id="", **_):

        try:
            if session_id and turn_id:
                Store(current_home()).mark_foreground(session_scope(session_id), session_id, turn_id)
        except Exception:
            log.warning("mem-diffs: foreground observation failed", exc_info=True)
        return None

    def transform_llm_output(self, response_text="", session_id="", turn_id="", **_):



        cleaned = _MODEL_DIRECTIVE_RE.sub("", response_text)


        # The host ignores an empty string from a transform. One space replaces a reply
        # that contains only model directives.
        if cleaned != response_text and not cleaned:
            cleaned = " "
        sanitized = cleaned if cleaned != response_text else None
        try:
            if not session_id or not turn_id:
                return sanitized
            store, sessions = Store(current_home()), session_scope(session_id)
            try:
                store.complete_review(sessions, turn_id, owner=session_id)
            except Exception:
                log.warning("mem-diffs: review completion failed", exc_info=True)
            if not cleaned:
                return sanitized
            paragraphs = []
            try:


                reviews = store.announce_reviews(session_scope(session_id, strict=True), turn_id, owner=session_id)
                paragraphs.extend(f'::mem-diffs-review{{id="{gid}"}}' for gid in reviews)
            except Exception:
                log.warning("mem-diffs: review announcement failed", exc_info=True)
            result = cleaned.rstrip() + "\n\n" + "\n\n".join(paragraphs) if paragraphs else cleaned
            return result if result != response_text else None
        except Exception:
            log.warning("mem-diffs: directive attach failed", exc_info=True)
            return sanitized




def _open_session_db(home: Path):
    path = home / "state.db"
    if not path.exists():
        return None
    try:
        from hermes_state import SessionDB
        return SessionDB(db_path=path, read_only=True)
    except Exception:
        log.debug("mem-diffs: session db unavailable", exc_info=True)
        return None


# session_scope follows only parents with end_reason='compression'. On lookup errors,
# strict=True gives an empty scope. strict=False keeps the IDs found before the error.
def session_scope(session_id: str, db="auto", *, strict=False) -> list:

    ids = [session_id]
    if not session_id:
        return ids
    opened = db == "auto"
    if opened:
        db = _open_session_db(current_home())
    if db is None:
        return [] if strict else ids
    try:
        if strict and not db.get_session(session_id):
            return []
        for sid in db.get_compression_chain(session_id) or []:
            if sid not in ids:
                ids.append(sid)
        current = db.get_session(session_id)
        for _ in range(32):
            parent_id = (current or {}).get("parent_session_id")
            parent = db.get_session(parent_id) if parent_id else None
            if strict and parent_id and parent is None:
                return []
            if not parent or parent.get("end_reason") != "compression" or parent_id in ids:
                break
            ids.insert(0, parent_id)
            current = parent
    except Exception:
        if strict:
            ids = []
        log.debug("mem-diffs: lineage lookup failed", exc_info=True)
    finally:
        if opened:
            with contextlib.suppress(Exception):
                db.close()
    return ids




def api_get(path, query_params):

    if path == "settings":
        return 200, {"profile": current_profile(), "settings": {
            "review_popover": current_review_popover(), "review_wording": current_notice_mode()}}
    resource, separator, ident = path.partition("/")
    if separator and ident and "/" not in ident:
        if resource == "receipts":
            return api_detail(ident, query_params.get("session_id", ""))
        if resource == "review-groups":
            return api_review_group(ident, query_params.get("session_id", ""))
    return 404, {"error": "not_found"}


def api_review_group(group_id, session_id):
    profile = current_profile()
    if (not isinstance(session_id, str) or not session_id.strip() or len(session_id) > LEDGER_ID_MAX
            or not isinstance(group_id, str) or not ID_RE.fullmatch(group_id)):
        return 400, {"error": "bad_request", "profile": profile}
    sessions = session_scope(session_id, strict=True)
    if not sessions:
        return 503, {"error": "history_unavailable", "profile": profile}
    try:
        group = Store(current_home()).review_group(group_id, sessions, current_notice_mode())
    except Exception:
        log.debug("mem-diffs: review ledger unavailable", exc_info=True)
        return 503, {"error": "ledger_unavailable", "profile": profile}
    if group is None:
        return 404, {"error": "not_found", "profile": profile}
    return 200, {"profile": profile, "group": group}


def api_detail(receipt_id, session_id):
    if (not isinstance(session_id, str) or not session_id.strip()
            or not isinstance(receipt_id, str) or not ID_RE.fullmatch(receipt_id)):
        return 400, {"error": "bad_request"}
    receipt = Store(current_home()).get(receipt_id, session_scope(session_id))
    if receipt is None:
        return 404, {"error": "not_found", "profile": current_profile()}
    return 200, {"profile": current_profile(), "receipt": receipt}
