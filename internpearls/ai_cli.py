# internpearls/ai_cli.py
"""Agent-CLI backends: detection, probes, and streaming subprocess runs.

No aqt imports: callers hand in paths and callbacks, so this stays testable
with a fake CLI script. The security posture lives in build_argv: no backend
ever gets write, shell, or edit tools; thorough mode allowlists web tools
only; claude additionally gets Read scoped to the scratch dir when images
are attached (that is how it views them).
"""
import os
import shutil
import subprocess
import threading
import time

from .ai_logic import parse_stream_event

BACKENDS = {
    "claude": {"label": "Claude Code", "exe": "claude",
               "subscription": "Claude Pro or Max"},
    "codex": {"label": "Codex CLI", "exe": "codex",
              "subscription": "ChatGPT Plus or Pro"},
    "agy": {"label": "Antigravity CLI", "exe": "agy",
            "subscription": "Google account (free tier, throttled)"},
}
_COMMON_DIRS = ("/opt/homebrew/bin", "/usr/local/bin",
                os.path.expanduser("~/.local/bin"),
                os.path.expanduser("~/.npm-global/bin"))
_TIMEOUTS = {"quick": 120, "thorough": 360}
_MAX_TURNS = {"quick": 1, "thorough": 15}
# Image-input support per backend. agy's headless mode accepts text blocks
# only (documented); flips to a live probe if Google adds media there.
_IMAGE_CAPABLE = {"claude": True, "codex": True, "agy": False}


class GenerationError(RuntimeError):
    pass


class GenerationCancelled(RuntimeError):
    pass


def image_capable(kind):
    return _IMAGE_CAPABLE.get(kind, False)


def find_cli(kind, override=""):
    if override:
        return override if (os.path.isfile(override)
                            and os.access(override, os.X_OK)) else None
    exe = BACKENDS[kind]["exe"]
    found = shutil.which(exe)
    if found:
        return found
    for d in _COMMON_DIRS:
        p = os.path.join(d, exe)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def probe(kind, path):
    try:
        r = subprocess.run([path, "--version"], capture_output=True,
                           text=True, timeout=10)
    except Exception as e:
        return {"ok": False, "detail": str(e)}
    out = (r.stdout or r.stderr or "").strip().splitlines()
    return {"ok": r.returncode == 0,
            "detail": out[0] if out else f"exit {r.returncode}"}


def build_argv(kind, path, mode, scratch, image_paths):
    """Returns (argv, prompt_via_stdin). The prompt always goes via stdin:
    codex exec freezes reading stdin when the prompt is an argument, and one
    consistent channel keeps the runner simple."""
    if kind == "claude":
        argv = [path, "-p", "--output-format", "stream-json", "--verbose",
                "--max-turns", str(_MAX_TURNS[mode]),
                "--setting-sources", ""]
        tools = []
        if mode == "thorough":
            tools += ["WebSearch", "WebFetch"]
        if image_paths:
            tools.append("Read")
            argv += ["--add-dir", scratch]
        # --tools is an allowlist of what's even available to the model, not just
        # what's auto-approved: naming this small a set here (never Bash/Edit/
        # Write/NotebookEdit/Task) is what makes "worst case: bad card text"
        # true regardless of anything the model tries to do.
        tool_list = ",".join(tools)
        argv += ["--tools", tool_list, "--allowedTools", tool_list]
        return argv, True
    if kind == "codex":
        argv = [path, "exec", "--json", "--sandbox", "read-only",
                "--skip-git-repo-check", "-C", scratch]
        for p in image_paths:
            argv += ["--image", p]
        return argv, True
    if kind == "agy":
        argv = [path, "-p", "--output-format", "stream-json"]
        return argv, True
    raise ValueError(kind)


def _run_argv(argv, kind, prompt, on_event=None, cancel=None, timeout=120,
              cwd=None):
    start = time.monotonic()
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, cwd=cwd,
                                start_new_session=True)
    except OSError as e:
        raise GenerationError(f"could not start the assistant: {e}") from e
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except OSError:
        pass
    result, tokens, rate_limits = None, 0, None

    lines = []
    done = threading.Event()

    def _reader():
        for line in proc.stdout:
            lines.append(line)
        done.set()

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    seen = 0
    err_text = ""
    try:
        while True:
            while seen < len(lines):
                evt = parse_stream_event(kind, lines[seen])
                seen += 1
                if not evt:
                    continue
                if evt["type"] == "result":
                    result = evt["text"]
                    tokens = max(tokens, evt.get("tokens", 0))
                elif evt["type"] == "usage":
                    tokens = max(tokens, evt["tokens"])
                elif evt["type"] == "rate_limits":
                    rate_limits = evt
                if on_event:
                    on_event(evt)
            if done.is_set() and proc.poll() is not None:
                break
            if cancel and cancel():
                _kill(proc)
                raise GenerationCancelled("cancelled")
            if time.monotonic() - start > timeout:
                _kill(proc)
                raise GenerationError(
                    "the assistant timed out; try Quick draft or a shorter source")
            time.sleep(0.05)
        # stderr is read here, before the finally block below closes it, so
        # a failure message survives the cleanup.
        err_text = (proc.stderr.read() or "").strip()
    finally:
        # A killed process still has to be waited on, or it leaks as a zombie;
        # the reader thread and the pipes need to be cleaned up too, or a
        # generation loop leaks file descriptors one run at a time.
        proc.wait(timeout=5)
        t.join(timeout=2)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except Exception:
                pass
    if proc.returncode != 0:
        raise GenerationError(err_text or f"assistant exited {proc.returncode}")
    if not result:
        raise GenerationError("the assistant produced no usable reply")
    return {"text": result, "tokens": tokens, "rate_limits": rate_limits,
            "duration_s": round(time.monotonic() - start, 1)}


def _kill(proc):
    import signal
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_generation(kind, path, prompt, mode, scratch, image_paths=(),
                   on_event=None, cancel=None, timeout=None):
    argv, _ = build_argv(kind, path, mode, scratch, list(image_paths))
    return _run_argv(argv, kind, prompt, on_event=on_event, cancel=cancel,
                     timeout=timeout or _TIMEOUTS[mode], cwd=scratch)
