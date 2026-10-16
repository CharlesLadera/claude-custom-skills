"""Load-bearing: this test is what makes the scanner provably read-only and
keeps apply.py from ever deleting, sending, or drafting mail. Never weaken it
to make a change pass - if a change seems to need it, the change is wrong."""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
LIB = ROOT / "lib"
APPLY = ROOT / "apply" / "apply.py"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "apply"))
import scan  # noqa: E402
import apply as applier  # noqa: E402

WRITE_METHOD_NAMES = {"batchModify", "modify", "trash", "untrash", "delete", "batchDelete",
                      "create", "patch", "update", "insert", "import", "send"}
NEVER_IN_APPLY = {"batchDelete", "send", "insert", "import", "drafts"}
ALLOWED_WRITES = {
    ("gmail.users.labels", "create"),
    ("gmail.users.labels", "patch"),
    ("gmail.users.labels", "delete"),
    ("gmail.users.messages", "batchModify"),
    ("gmail.users.messages", "trash"),
    ("gmail.users.messages", "untrash"),
    ("gmail.users.settings.filters", "create"),
    ("gmail.users.settings.filters", "delete"),
}


def tree(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def strings(t):
    return {n.value for n in ast.walk(t) if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def callers_of(t, name):
    """Names of the functions containing a call to NAME (bare or attribute)."""
    found = []

    def visit(node, fn):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child.name)
                continue
            if isinstance(child, ast.Call):
                f = child.func
                if (isinstance(f, ast.Name) and f.id == name) or (isinstance(f, ast.Attribute) and f.attr == name):
                    found.append(fn)
            visit(child, fn)

    visit(t, "<module>")
    return found


def imports(t):
    out = set()
    for n in ast.walk(t):
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module.split(".")[0])
    return out


def py_files(d):
    return sorted(d.rglob("*.py"))


class TestScannerIsReadOnly(unittest.TestCase):
    def test_no_write_method_strings_in_scripts_or_lib(self):
        for f in py_files(SCRIPTS) + py_files(LIB):
            bad = strings(tree(f)) & WRITE_METHOD_NAMES
            self.assertEqual(bad, set(), "%s names write method(s) %s; writes belong in apply/apply.py"
                             % (f.name, sorted(bad)))

    def test_read_methods_are_exactly_the_allowlist(self):
        self.assertEqual(set(scan.READ_METHODS), {"list", "get", "getProfile"})

    def test_gws_read_is_the_only_caller_of_run_gws(self):
        for f in py_files(SCRIPTS):
            self.assertEqual(set(callers_of(tree(f), "run_gws")) - {"gws_read"}, set(), f.name)
        self.assertEqual(callers_of(tree(SCRIPTS / "scan.py"), "run_gws"), ["gws_read"])

    def test_gws_read_refuses_writes(self):
        ctx = scan.Ctx("gws", runner=lambda *a, **k: self.fail("gws was run"))
        for method in ("batchModify", "trash", "delete", "create", "patch"):
            with self.assertRaises(ValueError):
                scan.gws_read(ctx, "gmail.users.messages", method, {})


class TestApplyAllowlist(unittest.TestCase):
    def test_write_calls_are_exactly_the_allowlist(self):
        self.assertEqual(set(applier.WRITE_CALLS), ALLOWED_WRITES)

    def test_read_calls_are_reads(self):
        for path, method in applier.READ_CALLS:
            self.assertIn(method, {"list", "get", "getProfile"}, (path, method))

    def test_no_forbidden_strings(self):
        bad = strings(tree(APPLY)) & NEVER_IN_APPLY
        self.assertEqual(bad, set(), "apply.py names %s" % sorted(bad))

    def test_gws_call_is_the_only_caller_of_run_gws(self):
        self.assertEqual(callers_of(tree(APPLY), "run_gws"), ["gws_call"])


class TestProcessesConfinedToLib(unittest.TestCase):
    def test_only_lib_imports_subprocess(self):
        for f in py_files(SCRIPTS) + [APPLY]:
            self.assertNotIn("subprocess", imports(tree(f)), f.name)

    def test_no_shell_escapes(self):
        for f in py_files(SCRIPTS) + py_files(LIB) + [APPLY]:
            for name in ("system", "popen", "Popen", "spawnv", "execv"):
                self.assertEqual(callers_of(tree(f), name), [], "%s calls %s" % (f.name, name))


if __name__ == "__main__":
    unittest.main()
