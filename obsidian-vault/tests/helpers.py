import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for sub in ("lib", "scripts", "apply"):
    sys.path.insert(0, os.path.join(ROOT, sub))

RULES = """# CLAUDE.md

```json obsidian-vault-rules
{
  "naming": {"pattern": "^[a-z0-9][a-z0-9.\\\\-]*$",
             "exempt": ["CLAUDE.md", "Home.md", "00 - Inbox/**", "Reports/**"]},
  "frontmatter": {"required": ["tags", "created|date"], "exempt": ["CLAUDE.md", "00 - Inbox/**"]},
  "orphans": {"exempt": ["CLAUDE.md", "Home.md", "00 - Inbox/**"]},
  "lintIgnore": ["Reports/**"]
}
```
"""


def make_vault(files, git=True):
    """Create a temp vault from {relpath: text}. Returns its absolute path."""
    root = tempfile.mkdtemp(prefix="vault-test-")
    files = dict(files)
    files.setdefault("CLAUDE.md", RULES)
    for rel, text in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    if git:
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t")
        for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
            subprocess.run(["git", "-C", root] + cmd, check=True, env=env, capture_output=True)
    return root


def git_env():
    os.environ.setdefault("GIT_AUTHOR_NAME", "t")
    os.environ.setdefault("GIT_AUTHOR_EMAIL", "t@t")
    os.environ.setdefault("GIT_COMMITTER_NAME", "t")
    os.environ.setdefault("GIT_COMMITTER_EMAIL", "t@t")


def read(root, rel):
    with open(os.path.join(root, rel), encoding="utf-8") as f:
        return f.read()


def write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


def git_log(root):
    return subprocess.run(["git", "-C", root, "log", "--format=%s"], capture_output=True, text=True).stdout


def can_symlink():
    """False on Windows without Developer Mode or admin rights, where os.symlink raises."""
    d = tempfile.mkdtemp()
    try:
        os.symlink(os.path.join(d, "target"), os.path.join(d, "link"))
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False


SYMLINKS = can_symlink()
