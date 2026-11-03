"""Ownership, session leases, and conservative teardown of review checkouts."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

OWNER_FILE = "human-review-owner.json"


def git(repo, *args):
    result = subprocess.run(
        ["git", "-c", "filter.lfs.process=", "-c", "filter.lfs.required=false", *args],
        cwd=repo, capture_output=True,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_LFS_SKIP_SMUDGE": "1"},
    )
    if result.returncode:
        raise ValueError(result.stderr.decode(errors="replace").strip() or "Git operation failed")
    return result.stdout


def write_registry(out, data):
    with tempfile.NamedTemporaryFile(mode="w", dir=out, delete=False) as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
        temporary = handle.name
    os.replace(temporary, out / "worktrees.json")


def read_registry(out):
    path = out / "worktrees.json"
    return json.loads(path.read_text()) if path.exists() else {"version": 1, "current": None, "entries": []}


@contextmanager
def lifecycle(out):
    out.mkdir(parents=True, exist_ok=True)
    with (out / "worktrees.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


class Session:
    def __init__(self, out):
        self.handle = (out / "session.lock").open("a")
        fcntl.flock(self.handle, fcntl.LOCK_SH)

    def close(self):
        if not self.handle.closed:
            self.handle.close()


@contextmanager
def inactive_session(out):
    with (out / "session.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another review viewer is active; finish or stop it first.") from None
        yield


def now():
    return datetime.now(timezone.utc).isoformat()


def common_dir(repo):
    return str(Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip()).resolve())


def worktree_list(repo):
    records = []
    for chunk in git(repo, "worktree", "list", "--porcelain", "-z").decode().split("\0\0"):
        record = {}
        for field in chunk.split("\0"):
            if field:
                key, _, value = field.partition(" ")
                record[key] = value
        if "worktree" in record:
            records.append(record)
    return records


def status(repo, ignored=False):
    args = ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"]
    if ignored:
        args.append("--ignored=matching")
    return git(repo, *args)


def git_dir(repo):
    return Path(git(repo, "rev-parse", "--absolute-git-dir").decode().strip()).resolve()


def ensure_checkout(snapshot, out):
    out = Path(out).resolve()
    if snapshot["mode"] != "pr":
        return {"path": snapshot["repo"], "owned": False, "kind": "local"}
    with lifecycle(out):
        registry = read_registry(out)
        repo = snapshot["repo"]
        if not Path(repo).is_dir():
            repo = next((e["common"] for e in registry["entries"] if Path(e["common"]).is_dir()), repo)
        head = snapshot["head"]
        common = common_dir(repo)
        candidates = worktree_list(repo)
        candidates.sort(key=lambda r: r["worktree"] != registry["current"])
        for candidate in candidates:
            path = Path(candidate["worktree"])
            if candidate.get("HEAD") != head or "locked" in candidate or "prunable" in candidate or "bare" in candidate:
                continue
            try:
                if path.is_symlink() or not path.is_dir() or status(path):
                    continue
                marker_path = git_dir(path) / OWNER_FILE
                existing = next((e for e in registry["entries"] if e["path"] == str(path.resolve()) and not e.get("removedAt")), None)
                if marker_path.exists():
                    marker = json.loads(marker_path.read_text())
                    if existing is None or marker.get("review") != str(out) or marker.get("token") != existing.get("token"):
                        continue
                elif existing and existing["owned"]:
                    continue
            except (ValueError, OSError):
                continue
            entry = existing or {"path": str(path.resolve()), "repo": repo, "common": common,
                                 "head": head, "owned": False, "createdAt": now()}
            if existing is None:
                registry["entries"].append(entry)
            entry["lastOpenedAt"] = now()
            registry["current"] = entry["path"]
            write_registry(out, registry)
            return {**entry, "kind": "managed" if entry["owned"] else "reused"}
        path = out / f"checkout-{head[:12]}-{uuid.uuid4().hex[:8]}"
        entry = {"path": str(path), "repo": repo, "common": common, "head": head,
                 "owned": True, "createdAt": now(), "lastOpenedAt": now(), "token": uuid.uuid4().hex,
                 "initializing": True}
        registry["entries"].append(entry)
        write_registry(out, registry)
        git(repo, "-c", "submodule.recurse=false", "-c", "core.hooksPath=/dev/null",
            "worktree", "add", "--detach", str(path), head)
        marker = {"review": str(out), "head": head, "token": entry["token"]}
        (git_dir(path) / OWNER_FILE).write_text(json.dumps(marker) + "\n")
        entry.pop("initializing")
        registry["current"] = str(path)
        write_registry(out, registry)
        return {**entry, "kind": "managed"}


def current_checkout(snapshot, out):
    if snapshot["mode"] != "pr":
        return Path(snapshot["repo"])
    registry = read_registry(out)
    entry = next((e for e in registry["entries"] if e["path"] == registry["current"] and not e.get("removedAt")), None)
    if entry is None or entry["head"] != snapshot["head"]:
        return None
    path = Path(entry["path"])
    try:
        if git(path, "rev-parse", "HEAD").decode().strip() == snapshot["head"]:
            return path
    except (OSError, ValueError):
        pass
    return None


def touch(out):
    with lifecycle(out):
        registry = read_registry(out)
        for entry in registry["entries"]:
            if entry["path"] == registry["current"]:
                entry["lastOpenedAt"] = now()
                write_registry(out, registry)
                break


def check_removal(entry, out):
    path = Path(entry["path"])
    if not entry["owned"]:
        raise ValueError("Reused checkout belongs to you; Human Review never removes it.")
    if path.is_symlink() or path.parent.resolve() != out or not path.name.startswith("checkout-"):
        raise ValueError("Checkout is outside this review's managed directory.")
    if not path.exists():
        raise ValueError("Checkout is missing; no filesystem cleanup will be attempted.")
    if not (path / ".git").is_file():
        raise ValueError("Not a linked worktree.")
    if common_dir(path) != entry["common"]:
        raise ValueError("Repository identity changed.")
    marker = json.loads((git_dir(path) / OWNER_FILE).read_text())
    if marker != {"review": str(out), "head": entry["head"], "token": entry["token"]}:
        raise ValueError("Ownership marker does not match the registry.")
    registered = next((r for r in worktree_list(path) if Path(r["worktree"]).resolve() == path), None)
    if registered is None or "locked" in registered or "prunable" in registered:
        raise ValueError("Worktree registration is missing, locked, or prunable.")
    if "branch" in registered or registered.get("HEAD") != entry["head"]:
        raise ValueError("Worktree moved from its pinned revision or now has a branch; preserve local work.")
    history = set(git(path, "reflog", "show", "--format=%H", "HEAD").decode().split())
    if history - {entry["head"]}:
        raise ValueError("Worktree has other revisions in its HEAD history; preserve possible local commits.")
    if status(path, ignored=True):
        raise ValueError("Tracked edits, untracked files, or ignored files are present.")
    return path


def cleanup_review(out, apply=False, minimum_days=0):
    out = Path(out).resolve()
    result = []
    with lifecycle(out):
        registry = read_registry(out)
        try:
            with inactive_session(out):
                for entry in registry["entries"]:
                    if entry.get("removedAt"):
                        continue
                    item = {"review": str(out), "path": entry["path"], "owned": entry["owned"],
                            "lastOpenedAt": entry["lastOpenedAt"], "eligible": False, "removed": False}
                    try:
                        path = check_removal(entry, out)
                        age = (datetime.now(timezone.utc) - datetime.fromisoformat(entry["lastOpenedAt"])).total_seconds() / 86400
                        if age < minimum_days:
                            raise ValueError(f"Opened within the last {minimum_days} days.")
                        item["eligible"] = True
                        item["reason"] = "Clean, unchanged, plugin-owned checkout; guide and notes are retained."
                        if apply:
                            if any((path / name).exists() for name in ("MODULE.bazel", "WORKSPACE", "WORKSPACE.bazel")):
                                cleaned = subprocess.run(["bazel", "clean", "--expunge", "--async"], cwd=path, capture_output=True)
                                if cleaned.returncode:
                                    raise ValueError("Bazel cleanup failed: " + cleaned.stderr.decode(errors="replace").strip())
                            check_removal(entry, out)
                            git(entry["common"], "worktree", "remove", str(path))
                            entry["removedAt"] = now()
                            if registry["current"] == entry["path"]:
                                registry["current"] = None
                            write_registry(out, registry)
                            item["removed"] = True
                    except (ValueError, OSError) as error:
                        item["eligible"] = False
                        item["reason"] = str(error)
                    result.append(item)
        except ValueError as error:
            result = [{"review": str(out), "path": e["path"], "owned": e["owned"],
                       "lastOpenedAt": e["lastOpenedAt"], "eligible": False, "removed": False, "reason": str(error)}
                      for e in registry["entries"] if not e.get("removedAt")]
    return result


def cleanup_unused(root, apply=False):
    results = []
    for registry in sorted(Path(root).expanduser().resolve().glob("*/worktrees.json")):
        try:
            results.extend(cleanup_review(registry.parent, apply=apply, minimum_days=14))
        except (ValueError, OSError) as error:
            results.append({"review": str(registry.parent), "eligible": False, "removed": False, "reason": str(error)})
    return results
