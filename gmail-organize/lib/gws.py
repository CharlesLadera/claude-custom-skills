"""Method-agnostic runner for the gws CLI.

Shared by scripts/scan.py (reads only) and apply/apply.py (writes). This
module names no Gmail methods: each caller holds its own allowlist and wraps
run_gws in exactly one function (scan.gws_read, apply.gws_call), which
tests/test_safety.py enforces.
"""
import json
import os
import re
import shutil
import subprocess
import time

EXIT_API = 1
EXIT_AUTH = 2
EXIT_VALIDATION = 3
EXIT_DISCOVERY = 4
EXIT_NOT_FOUND = 127

# cmd.exe re-parses the arguments it hands to a .cmd/.bat shim: these
# characters change meaning there, and its command line stops at 8191.
CMD_METACHARS = frozenset("&|<>^%")
CMD_LINE_LIMIT = 8000
# Matched as whole words or exact Gmail phrases: message ids are hex, so a
# bare "404" substring can sit inside an id echoed in an unrelated error.
RATE_LIMIT_PATTERNS = (re.compile(r"\b429\b"), re.compile(r"ratelimitexceeded"),
                       re.compile(r"rate limit"), re.compile(r"quota exceeded"))
NOT_FOUND_PATTERNS = (re.compile(r"\b404\b"), re.compile(r"requested entity was not found"),
                      re.compile(r"invalid id value"))
# Gmail's quota is per minute per user: backoff must be able to outlast a
# full window (1+2+4+8+16+32 = 63 s) before a call counts as failed.
RETRIES = 6
NPM_EXE = os.path.join("node_modules", "@googleworkspace", "cli", "bin", "gws.exe")


class GwsError(Exception):
    def __init__(self, code, output, argv=()):
        super().__init__("gws exited %s: %s" % (code, output.strip()[-400:]))
        self.code = code
        self.output = output
        self.argv = list(argv)

    def _says(self, patterns):
        low = self.output.lower()
        return self.code == EXIT_API and any(p.search(low) for p in patterns)

    @property
    def rate_limited(self):
        return self._says(RATE_LIMIT_PATTERNS)

    @property
    def not_found(self):
        return self._says(NOT_FOUND_PATTERNS)


def is_cmd_shim(path):
    return path.lower().endswith((".cmd", ".bat"))


def resolve_gws(env=None, which=shutil.which, exists=os.path.exists):
    """Return the gws executable: $GMAIL_ORGANIZE_GWS, else gws on PATH,
    swapping an npm .cmd shim for the real gws.exe beside it."""
    env = os.environ if env is None else env
    if env.get("GMAIL_ORGANIZE_GWS"):
        return env["GMAIL_ORGANIZE_GWS"]
    found = which("gws")
    if not found:
        raise GwsError(EXIT_NOT_FOUND, "gws not found on PATH; install it or set GMAIL_ORGANIZE_GWS")
    if is_cmd_shim(found):
        exe = os.path.join(os.path.dirname(found), NPM_EXE)
        if exists(exe):
            return exe
    return found


def build_argv(exe, path, method, params, body=None):
    argv = [exe] + path.split(".") + [method, "--params", json.dumps(params)]
    if body is not None:
        argv += ["--json", json.dumps(body)]
    return argv


def check_cmd_shim(argv):
    """Refuse argv that cmd.exe would mangle on its way to a .cmd shim."""
    if not is_cmd_shim(argv[0]):
        return
    bad = sorted({c for a in argv[1:] for c in a if c in CMD_METACHARS})
    if bad:
        raise GwsError(EXIT_VALIDATION, "argument contains %s, which a .cmd shim would mangle; "
                       "set GMAIL_ORGANIZE_GWS to gws.exe" % " ".join(bad), argv)
    if sum(len(a) + 3 for a in argv) > CMD_LINE_LIMIT:
        raise GwsError(EXIT_VALIDATION, "command line exceeds %d characters for a .cmd shim; "
                       "set GMAIL_ORGANIZE_GWS to gws.exe" % CMD_LINE_LIMIT, argv)


def parse_output(text):
    """Parse gws stdout, skipping any status lines printed before the JSON."""
    text = (text or "").strip()
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    return json.loads(text[min(starts):]) if starts else {}


def run_gws(exe, path, method, params, body=None, runner=subprocess.run, sleep=time.sleep, retries=None):
    """Run one gws call and return its parsed JSON. Rate limits and quota
    errors are retried with exponential backoff (1s, 2s, ... 32s); any other
    failure raises GwsError."""
    retries = RETRIES if retries is None else retries
    argv = build_argv(exe, path, method, params, body)
    check_cmd_shim(argv)
    delay = 1.0
    for attempt in range(retries + 1):
        proc = runner(argv, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode == 0:
            return parse_output(proc.stdout)
        err = GwsError(proc.returncode, (proc.stderr or "") + (proc.stdout or ""), argv)
        if err.rate_limited and attempt < retries:
            sleep(delay)
            delay *= 2
            continue
        raise err
