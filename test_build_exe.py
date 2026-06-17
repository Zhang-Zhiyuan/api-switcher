import ast
from pathlib import Path

import build_exe


def _lazy_core_targets(*roots: Path) -> set[str]:
    targets: set[str] = set()
    for root in roots:
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if name not in {"LazyModule", "LazyAttribute"}:
                    continue
                target = node.args[0]
                if isinstance(target, ast.Constant) and isinstance(target.value, str) and target.value.startswith("core."):
                    targets.add(target.value)
    return targets


def test_project_hidden_imports_include_core_sync_manager():
    assert "core.sync_manager" in build_exe._project_hidden_imports()


def test_project_hidden_imports_cover_lazy_core_targets():
    hidden_imports = set(build_exe._project_hidden_imports())
    lazy_targets = _lazy_core_targets(Path("core"), Path("ui"))

    assert lazy_targets
    assert lazy_targets <= hidden_imports


def test_create_spec_includes_project_core_hidden_imports(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "sync_manager.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(build_exe, "SPEC_PATH", tmp_path / "API切换器.spec")

    build_exe.create_spec_file()

    assert "core.sync_manager" in build_exe.SPEC_PATH.read_text(encoding="utf-8")
