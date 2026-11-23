"""Load-bearing: this test keeps the scanner read-only and keeps apply.py from ever
deleting a file, overwriting one by a move, or running a git verb outside its allowlist
(no push, reset, checkout, clean, rm). Never weaken it to make a change pass - if a
change seems to need it, the change is wrong."""
import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READ_ONLY = [os.path.join(ROOT, "lib", "vault.py"), os.path.join(ROOT, "scripts", "scan.py")]
APPLY = os.path.join(ROOT, "apply", "apply.py")

# Names are qualified when called on a module ("os.rename"), bare when called as a
# function ("open"), and ".name" when called as a method on some object. Path methods
# that only ever mean a filesystem write are matched as methods too.
OS_DELETE = {"os.remove", "os.unlink", "os.rmdir", "os.removedirs", "os.truncate", "shutil.rmtree",
             "shutil.move", ".unlink", ".rmdir", ".rmtree", "remove", "unlink", "rmdir", "rmtree"}
OS_WRITE = OS_DELETE | {"os.rename", "os.replace", "os.renames", "os.makedirs", "os.mkdir", "os.symlink",
                        "os.link", "os.chmod", "os.utime", "shutil.copy", "shutil.copy2",
                        "shutil.copyfile", "shutil.copytree", ".write_text", ".write_bytes",
                        ".symlink_to", ".touch", ".mkdir", ".rename"}
PROCESS = {"subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call",
           "subprocess.check_output", "os.system", "os.popen", "os.execv", "os.execvp", "os.spawnv"}
GIT_VERBS = ("rev-parse", "status", "add", "commit", "log")


def tree(path):
    with open(path, encoding="utf-8") as f:
        return ast.parse(f.read(), filename=path)


def qualname(f):
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        if isinstance(f.value, ast.Name) and f.value.id in ("os", "shutil", "subprocess"):
            return f.value.id + "." + f.attr
        return "." + f.attr
    return None


def calls(t):
    """(enclosing function, qualified name, call node) for every call."""
    out = []

    def visit(node, fn):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child.name)
                continue
            if isinstance(child, ast.Call):
                out.append((fn, qualname(child.func), child))
            visit(child, fn)

    visit(t, "<module>")
    return out


def imports(t):
    names = set()
    for n in ast.walk(t):
        if isinstance(n, ast.Import):
            names |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            names.add(n.module.split(".")[0])
    return names


def open_modes(t):
    modes = []
    for _, name, node in calls(t):
        if name == "open":
            mode = node.args[1] if len(node.args) > 1 else next(
                (k.value for k in node.keywords if k.arg == "mode"), None)
            modes.append(mode.value if isinstance(mode, ast.Constant) else ("r" if mode is None else "?"))
    return modes


class TestReadOnlyModules(unittest.TestCase):
    def test_no_writes_no_processes(self):
        for path in READ_ONLY:
            t = tree(path)
            self.assertFalse(imports(t) & {"subprocess", "shutil"}, path)
            bad = [(fn, n) for fn, n, _ in calls(t) if n in OS_WRITE | PROCESS]
            self.assertEqual(bad, [], path)
            self.assertTrue(all(m in ("r", "rb") for m in open_modes(t)), (path, open_modes(t)))


class TestApplyNeverDeletes(unittest.TestCase):
    def setUp(self):
        with open(APPLY, encoding="utf-8") as f:
            self.src = f.read()
        self.t = ast.parse(self.src)
        self.calls = calls(self.t)

    def test_no_delete_primitives(self):
        self.assertEqual([(fn, n) for fn, n, _ in self.calls if n in OS_DELETE], [])

    def test_writes_only_in_known_functions(self):
        allowed = {"os.rename": {"execute"}, "os.makedirs": {"execute", "do_import"},
                   "shutil.copy2": {"do_import"}}
        for fn, n, _ in self.calls:
            if n in OS_WRITE:
                self.assertIn(fn, allowed.get(n, set()), "%s() calls %s" % (fn, n))
        opens = [(fn, m) for (fn, n, _), m in zip([c for c in self.calls if c[1] == "open"], open_modes(self.t))]
        for fn, mode in opens:
            if mode != "r":
                self.assertEqual(fn, "execute", "%s() opens a file with mode %r" % (fn, mode))

    def test_rename_never_overwrites(self):
        fn = next(n for n in ast.walk(self.t) if isinstance(n, ast.FunctionDef) and n.name == "execute")
        self.assertIn("os.path.exists(d) and not os.path.samefile(s, d)", ast.get_source_segment(self.src, fn))

    def test_processes_only_through_git_run(self):
        for fn, n, _ in self.calls:
            if n in PROCESS:
                self.assertEqual(fn, "git_run", "%s() spawns via %s" % (fn, n))

    def test_git_verbs_allowlist_unchanged(self):
        for n in ast.walk(self.t):
            if isinstance(n, ast.Assign) and any(getattr(x, "id", None) == "GIT_VERBS" for x in n.targets):
                self.assertEqual(ast.literal_eval(n.value), GIT_VERBS)
                return
        self.fail("GIT_VERBS not found")

    def test_no_dangerous_git_words(self):
        strings = {n.value for n in ast.walk(self.t) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        for word in ("push", "reset", "checkout", "clean", "rm", "restore", "stash", "rebase", "--force"):
            self.assertNotIn(word, strings)


if __name__ == "__main__":
    unittest.main()
