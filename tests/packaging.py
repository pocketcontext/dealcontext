#!/usr/bin/env python3
"""Verify bundled resources and the standalone launcher contract."""
from importlib.resources import files
from pathlib import Path
import re
import subprocess
import os
import shlex
import tempfile
import shutil

ROOT = Path(__file__).resolve().parents[1]
launcher = ROOT / "skills/dealcontext/dealcontext"
assert files("dealcontext_client").joinpath("schema.json").read_bytes() == (ROOT / "skills/dealcontext/references/schema.json").read_bytes()
assert re.search(r"dealcontext\.git@[0-9a-f]{40}", launcher.read_text())
assert launcher.stat().st_mode & 0o111
with tempfile.TemporaryDirectory(prefix="dealcontext-launcher-") as directory:
    copied = Path(directory) / "dealcontext"
    shutil.copy2(launcher, copied)
    # Exercise the real uv shebang with the source package overriding the released
    # dependency until publication. The maintained launcher's pin stays unchanged.
    content = copied.read_text()
    first, rest = content.split("\n", 1)
    assert first == "#!/usr/bin/env -S uv run --script"
    copied.write_text(first + " --with " + shlex.quote(str(ROOT)) + "\n" + rest)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('DEALCONTEXT_', 'OBSERVECONTEXT_', 'POCKETCONTEXT_'))}
    env.update(UV_CACHE_DIR=str(Path(directory) / 'uv-cache'))
    for arguments, expected in ((["--help"], "demo-contacts"),
                                (["demo-contacts", "--help"], "sync"),
                                (["demo-contacts", "sync", "--help"], "--dry-run"),
                                (["demo-contacts", "status", "--help"], "--source")):
        result = subprocess.run([str(copied), *arguments], cwd=directory, env=env,
                                capture_output=True, text=True, timeout=180)
        assert result.returncode == 0, result.stderr
        assert expected in result.stdout
    result = subprocess.run([str(copied), "demo-contacts", "sync"], cwd=directory,
                            env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 2, result.stderr
    assert '--dry-run' in result.stderr
print("PASS: packaged schema and copied uv launcher with local package override")
