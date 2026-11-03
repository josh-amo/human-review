#!/usr/bin/env python3
"""Capture immutable review inputs and serve a human-authored reading guide."""

import argparse
import difflib
import hashlib
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit
import worktrees

PLUGIN = Path(__file__).resolve().parents[1]


def run(*args, cwd=None, check=True):
    result = subprocess.run(args, cwd=cwd, capture_output=True)
    if check and result.returncode:
        raise ValueError(result.stderr.decode(errors="replace").strip() or f"{args[0]} failed")
    return result


def git(repo, *args, check=True):
    return run("git", "-c", "filter.lfs.process=", "-c", "filter.lfs.required=false",
               *args, cwd=repo, check=check)


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, ensure_ascii=True, indent=2)
        handle.write("\n")
        temporary = handle.name
    os.replace(temporary, path)


def safe_path(path):
    value = PurePosixPath(path)
    if not path or value.is_absolute() or ".." in value.parts or "\x00" in path:
        raise ValueError(f"Unsafe repository path: {path!r}")
    return path


def changed_paths(data):
    fields = data.decode(errors="surrogateescape").split("\0")
    files = []
    while fields and fields[0]:
        status = fields.pop(0)
        old = safe_path(fields.pop(0))
        new = safe_path(fields.pop(0)) if status[0] in "RC" else old
        files.append((status, old, new))
    return files


def parse_hunks(diff):
    hunks, current = [], None
    old = new = 0
    for line in diff.splitlines():
        match = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if match:
            old, new = map(int, match.groups())
            current = {"header": line, "rows": []}
            hunks.append(current)
        elif current is not None and line.startswith((" ", "+", "-", "\\")):
            kind = {"+": "add", "-": "remove", " ": "context", "\\": "meta"}[line[0]]
            current["rows"].append({"kind": kind, "text": line[1:],
                                    "old": old if kind in ("remove", "context") else None,
                                    "new": new if kind in ("add", "context") else None})
            old += kind in ("remove", "context")
            new += kind in ("add", "context")
    return hunks


def blob(repo, revision, path):
    result = git(repo, "show", f"{revision}:{path}", check=False)
    if result.returncode:
        raise ValueError(f"Cannot read {revision}:{path}: {result.stderr.decode(errors='replace')}")
    return result.stdout


def working_bytes(repo, path):
    file = repo / path
    if file.is_symlink():
        return os.readlink(file).encode()
    return file.read_bytes()


def file_record(repo, out, status, old, path, base, head, patch, context=False):
    result = {"path": path, "oldPath": old, "status": status, "diff": patch,
              "hunks": parse_hunks(patch), "context": context}
    for side, content in (("base", base), ("head", head)):
        text = None
        if content is not None:
            try:
                if b"\0" not in content:
                    text = content.decode("utf-8")
            except UnicodeDecodeError:
                pass
        result[side] = {"exists": content is not None,
                        "hash": digest(content) if content is not None else None,
                        "text": text}
        if text is not None:
            target = out / "sources" / side / safe_path(old if side == "base" else path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    result["fingerprint"] = digest(json.dumps([result["base"]["hash"], result["head"]["hash"], patch]))
    return result


def capture(args):
    repo = Path(git(args.repo, "rev-parse", "--show-toplevel").stdout.decode().strip()).resolve()
    metadata = None
    if args.pr:
        metadata = json.loads(run("gh", "pr", "view", args.pr, "--json",
                                  "title,body,url,number,baseRefName,headRefName,baseRefOid,headRefOid",
                                  cwd=repo).stdout)
        base, head = metadata["baseRefOid"], metadata["headRefOid"]
        for revision in (base, head):
            if git(repo, "cat-file", "-e", f"{revision}^{{commit}}", check=False).returncode:
                git(repo, "fetch", "--no-tags", "--no-write-fetch-head", "origin", revision)
        mode = "pr"
        identity = metadata["url"]
        title = metadata["title"]
    elif args.branch:
        head = git(repo, "rev-parse", "--verify", f"{args.branch}^{{commit}}").stdout.decode().strip()
        base = git(repo, "rev-parse", "--verify", f"{args.base}^{{commit}}").stdout.decode().strip()
        mode, identity, title = "branch", f"{repo}:{args.base}...{args.branch}", f"{args.branch} against {args.base}"
    else:
        head = git(repo, "rev-parse", "HEAD").stdout.decode().strip()
        base = head
        mode, identity, title = args.mode, f"{repo}:working-tree:{args.mode}", f"Working tree · {args.mode}"
    out = Path(args.output).expanduser().resolve() if args.output else Path.home() / ".codex" / "human-review" / digest(identity)[:16]
    out.mkdir(parents=True, exist_ok=True)
    if mode in ("pr", "branch"):
        comparison_base = git(repo, "merge-base", base, head).stdout.decode().strip()
        comparison = [comparison_base, head]
    else:
        comparison_base = base
        comparison = {"all": ["HEAD"], "staged": ["--cached", "HEAD"], "unstaged": []}[mode]
        if git(repo, "ls-files", "-u").stdout:
            raise ValueError("Resolve index conflicts before capturing a working-tree review.")
    before = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--find-renames", *comparison).stdout
    paths = changed_paths(git(repo, "diff", "--no-ext-diff", "--no-textconv", "--find-renames",
                              "--name-status", "-z", *comparison).stdout)
    untracked = []
    if mode in ("all", "unstaged"):
        untracked = [safe_path(p) for p in git(repo, "ls-files", "--others", "--exclude-standard", "-z").stdout.decode().split("\0") if p]
    files = []
    for status, old, path in paths:
        previous = None if status[0] == "A" else blob(repo, "" if mode == "unstaged" else comparison_base, old)
        current = None if status[0] == "D" else (blob(repo, head, path) if mode in ("pr", "branch") else
                                                  blob(repo, "", path) if mode == "staged" else working_bytes(repo, path))
        patch = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--find-renames", "--unified=5",
                    *comparison, "--", old, path).stdout.decode(errors="replace")
        files.append(file_record(repo, out, status, old, path, previous, current, patch))
    for path in untracked:
        current = working_bytes(repo, path)
        text = current.decode(errors="replace") if b"\0" not in current else ""
        patch = "".join(difflib.unified_diff([], text.splitlines(keepends=True), fromfile="/dev/null", tofile=f"b/{path}", n=5))
        files.append(file_record(repo, out, "?", path, path, None, current, patch))
    after = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--find-renames", *comparison).stdout
    if before != after or any(working_bytes(repo, f["path"]) != (f["head"]["text"] or "").encode()
                              for f in files if f["status"] == "?" and f["head"]["text"] is not None):
        raise ValueError("Files changed during capture. Capture again for a consistent snapshot.")
    if mode == "pr":
        latest = json.loads(run("gh", "pr", "view", args.pr, "--json", "baseRefOid,headRefOid", cwd=repo).stdout)
        if latest != {k: metadata[k] for k in latest}:
            raise ValueError("The PR changed during capture. Capture again.")
    snapshot = {"version": 1, "id": digest(identity)[:16], "title": title, "repo": str(repo),
                "mode": mode, "base": comparison_base, "targetBase": base, "head": head,
                "metadata": metadata, "files": files, "contextFiles": []}
    snapshot["baseLabel"] = "index snapshot" if mode == "unstaged" else comparison_base[:12]
    snapshot["headLabel"] = ("index snapshot" if mode == "staged" else "working-tree snapshot") if mode in ("all", "staged", "unstaged") else head[:12]
    snapshot["fingerprint"] = digest(json.dumps([f["fingerprint"] for f in files]))
    write_json(out / "snapshot.json", snapshot)
    print(out)


def add_context(args):
    out = Path(args.review).resolve()
    data = json.loads((out / "snapshot.json").read_text())
    path = safe_path(args.path)
    if any(f["path"] == path for f in data["files"] + data["contextFiles"]):
        return
    if data["mode"] in ("all", "staged", "unstaged"):
        raise ValueError("For working-tree reviews, cite background from already captured files; external context cannot be reconstructed immutably.")
    content = blob(data["repo"], data["head"], path)
    data["contextFiles"].append(file_record(Path(data["repo"]), out, "context", path, path, content, content, "", True))
    write_json(out / "snapshot.json", data)


def validate_guide(snapshot, guide):
    if guide.get("snapshot") != snapshot["fingerprint"]:
        raise ValueError("Guide snapshot fingerprint does not match captured input. Regenerate the guide.")
    files = {f["path"]: f for f in snapshot["files"] + snapshot["contextFiles"]}
    sections = guide.get("sections", [])
    ids = [s["id"] for s in sections]
    if not sections or len(ids) != len(set(ids)):
        raise ValueError("The guide needs sections with unique stable ids.")
    covered = set()
    for section in sections:
        for path in section.get("files", []):
            if path not in files:
                raise ValueError(f"Unknown file: {path}")
            covered.add(path)
        for block in section.get("blocks", []):
            kind = block.get("type")
            if kind not in ("text", "background", "flow", "diff", "source"):
                raise ValueError(f"Unknown block type: {kind}")
            refs = block.get("refs", [])
            if block.get("source"):
                refs = refs + [block["source"]]
            if kind in ("diff", "source"):
                refs = refs + [block]
            if kind == "flow":
                refs = refs + [n["ref"] for n in block["nodes"] if "ref" in n]
            for ref in refs:
                file = files.get(ref["path"])
                side = ref.get("side", "head")
                if file is None or side not in ("base", "head"):
                    raise ValueError(f"Unknown source reference: {ref}")
                content = file[side]["text"]
                start, end = ref.get("start", 1), ref.get("end", ref.get("start", 1))
                if content is None or not 1 <= start <= end <= len(content.splitlines()):
                    raise ValueError(f"Source reference outside snapshot: {ref}")
                if kind == "diff" and ref is block:
                    if not any(row["kind"] in ("add", "remove") and row.get("new" if side == "head" else "old") is not None
                               and start <= row["new" if side == "head" else "old"] <= end
                               for h in file["hunks"] for row in h["rows"]):
                        raise ValueError(f"Diff excerpt contains no changed lines: {ref}")
                    for annotation in block.get("annotations", []):
                        field = "new" if side == "head" else "old"
                        if not start <= annotation["line"] <= end or not any(
                            row[field] == annotation["line"] for h in file["hunks"] for row in h["rows"]
                        ):
                            raise ValueError("Annotation is outside its excerpt.")
    missing = {f["path"] for f in snapshot["files"]} - covered
    if missing:
        raise ValueError(f"Changed files absent from section map: {sorted(missing)}")


def load_review(out):
    snapshot = json.loads((out / "snapshot.json").read_text())
    guide = json.loads((out / "guide.json").read_text())
    validate_guide(snapshot, guide)
    for section in guide["sections"]:
        referenced = set(section.get("files", []))
        def collect_paths(value):
            if isinstance(value, dict):
                if "path" in value:
                    referenced.add(value["path"])
                for child in value.values():
                    collect_paths(child)
            elif isinstance(value, list):
                for child in value:
                    collect_paths(child)
        collect_paths(section["blocks"])
        fingerprints = [f["fingerprint"] for f in snapshot["files"] + snapshot["contextFiles"] if f["path"] in referenced]
        section["fingerprint"] = digest(json.dumps([section, fingerprints], sort_keys=True))
    return snapshot, guide


def editor_target(snapshot, out, path, side, line, use_checkout=True):
    file = next((f for f in snapshot["files"] + snapshot["contextFiles"] if f["path"] == path), None)
    if file is None or side not in ("base", "head") or file[side]["text"] is None:
        raise ValueError("No text source at this revision.")
    if not 1 <= line <= max(1, len(file[side]["text"].splitlines())):
        raise ValueError("Line outside snapshot.")
    name = file["oldPath"] if side == "base" else path
    root = worktrees.current_checkout(snapshot, out) if use_checkout else None
    if snapshot["mode"] == "pr" and side == "base":
        root = None
    checkout = root / name if root is not None else None
    matches = False
    try:
        matches = checkout is not None and not checkout.is_symlink() and digest(checkout.read_bytes()) == file[side]["hash"]
    except OSError:
        pass
    target = checkout if matches else out / "sources" / side / name
    return {"url": f"vscode://file{quote(str(target), safe='/')}:{line}:1", "checkout": matches,
            "path": str(target), "label": ("Open review worktree in VS Code" if snapshot["mode"] == "pr" else "Open checkout in VS Code") if matches else f"Open {side} snapshot in VS Code"}


def serve(args):
    out = Path(args.review).expanduser().resolve()
    snapshot, guide = load_review(out)
    lock = threading.Lock()
    session = worktrees.Session(out) if snapshot["mode"] == "pr" else None
    try:
        if session:
            worktrees.ensure_checkout(snapshot, out)
    except (ValueError, OSError):
        session.close()
        raise

    def checkout_info():
        registry = worktrees.read_registry(out)
        entry = next((e for e in registry["entries"] if e["path"] == registry["current"] and not e.get("removedAt")), None)
        return {"pr": snapshot["mode"] == "pr", "active": session is not None and not session.handle.closed,
                "path": entry["path"] if entry else None, "owned": bool(entry and entry["owned"])}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, status, data, content_type="application/json"):
            raw = json.dumps(data).encode() if content_type == "application/json" else data
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.headers.get("Host") not in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"):
                return self.send(403, {"error": "Local host only"})
            url = urlsplit(self.path)
            try:
                if url.path == "/api/review":
                    if session is not None and not session.handle.closed:
                        worktrees.touch(out)
                    return self.send(200, {"snapshot": snapshot, "guide": guide})
                if url.path == "/api/checkout":
                    return self.send(200, checkout_info())
                if url.path == "/api/cleanup-plan":
                    return self.send(200, worktrees.cleanup_unused(out.parent))
                if url.path == "/api/state":
                    with lock:
                        state = json.loads((out / "state.json").read_text()) if (out / "state.json").exists() else {}
                    return self.send(200, state)
                if url.path == "/api/editor":
                    params = parse_qs(url.query)
                    target = editor_target(snapshot, out, params["path"][0], params.get("side", ["head"])[0], int(params.get("line", ["1"])[0]),
                                           use_checkout=snapshot["mode"] != "pr" or not session.handle.closed)
                    return self.send(200, target)
                asset = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}.get(url.path)
                if asset:
                    content_type = mimetypes.guess_type(asset)[0] or "application/octet-stream"
                    return self.send(200, (PLUGIN / "assets" / asset).read_bytes(), content_type)
                return self.send(404, {"error": "Not found"})
            except (ValueError, KeyError, OSError) as error:
                self.send(400, {"error": str(error)})

        def do_POST(self):
            nonlocal session
            origin = self.headers.get("Origin")
            expected = f"http://{self.headers.get('Host')}"
            if origin != expected or self.headers.get("Sec-Fetch-Site") == "cross-site":
                return self.send(403, {"error": "Same-origin requests only"})
            if self.path not in ("/api/state", "/api/finish", "/api/checkout", "/api/cleanup") or self.headers.get("Content-Type") != "application/json":
                return self.send(400, {"error": "Expected JSON state"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 262144:
                    raise ValueError("State is empty or too large")
                state = json.loads(self.rfile.read(size))
                if not isinstance(state, dict):
                    raise ValueError("State must be an object")
                if self.path != "/api/state":
                    if state.get("confirm") is not True:
                        raise ValueError("Explicit confirmation is required")
                    with lock:
                        if self.path == "/api/finish":
                            if session:
                                session.close()
                            results = worktrees.cleanup_review(out, apply=True)
                        elif self.path == "/api/cleanup":
                            results = worktrees.cleanup_unused(out.parent, apply=True)
                        else:
                            if snapshot["mode"] != "pr":
                                raise ValueError("Local reviews use the existing checkout")
                            if session is None or session.handle.closed:
                                session = worktrees.Session(out)
                            try:
                                worktrees.ensure_checkout(snapshot, out)
                            except (OSError, ValueError):
                                session.close()
                                raise
                            results = []
                    return self.send(200, {"checkout": checkout_info(), "results": results})
                with lock:
                    write_json(out / "state.json", state)
                self.send(200, {"saved": True})
            except (ValueError, OSError) as error:
                self.send(400, {"error": str(error)})

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if session:
            session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture_parser = commands.add_parser("capture")
    capture_parser.add_argument("--repo", default=".")
    sources = capture_parser.add_mutually_exclusive_group()
    sources.add_argument("--pr", help="GitHub PR URL or number")
    sources.add_argument("--branch")
    capture_parser.add_argument("--base", help="Comparison branch (required with --branch)")
    capture_parser.add_argument("--mode", choices=("all", "staged", "unstaged"), default="all")
    capture_parser.add_argument("--output")
    capture_parser.set_defaults(action=capture)
    context_parser = commands.add_parser("context")
    context_parser.add_argument("review")
    context_parser.add_argument("path")
    context_parser.set_defaults(action=add_context)
    validate_parser = commands.add_parser("validate")
    validate_parser.add_argument("review")
    validate_parser.set_defaults(action=lambda args: (load_review(Path(args.review)), print("Guide validated")))
    serve_parser = commands.add_parser("serve")
    serve_parser.add_argument("review")
    serve_parser.add_argument("--port", type=int, default=0)
    serve_parser.set_defaults(action=serve)
    checkout_parser = commands.add_parser("checkout", help="Create or reuse the exact PR checkout")
    checkout_parser.add_argument("review")
    checkout_parser.set_defaults(action=lambda args: print(json.dumps(worktrees.ensure_checkout(
        json.loads((Path(args.review).resolve() / "snapshot.json").read_text()), Path(args.review).resolve()), indent=2)))
    cleanup_parser = commands.add_parser("cleanup", help="Preview cleanup; --apply explicitly removes eligible plugin-owned worktrees")
    cleanup_parser.add_argument("--review", help="One review, including recently opened worktrees")
    cleanup_parser.add_argument("--root", default=str(Path.home() / ".codex/human-review"))
    cleanup_parser.add_argument("--apply", action="store_true")
    cleanup_parser.set_defaults(action=lambda args: print(json.dumps(
        worktrees.cleanup_review(Path(args.review).expanduser().resolve(), apply=args.apply) if args.review
        else worktrees.cleanup_unused(args.root, apply=args.apply), indent=2)))
    args = parser.parse_args()
    if args.command == "capture" and args.branch and not args.base:
        parser.error("--branch requires --base; choose the intended comparison, including the parent of a stack")
    try:
        args.action(args)
    except (ValueError, OSError, json.JSONDecodeError) as error:
        print(f"human-review: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
