"""Regression checks for data directory selection and switching helpers."""
import os
import tempfile
from pathlib import Path

from config import paths


def main() -> None:
    original_env = os.environ.get(paths.ENV_DATA_DIR)
    original_portable_env = os.environ.get(paths.ENV_PORTABLE)
    original_app_dir = paths.APP_DIR
    original_storage_dir = paths.STORAGE_DIR
    original_pointer = paths.DATA_DIR_POINTER
    original_user_pointer = paths.USER_DATA_DIR_POINTER
    original_marker = paths.PORTABLE_MARKER

    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir = root / "app"
            app_dir.mkdir()

            os.environ.pop(paths.ENV_DATA_DIR, None)
            os.environ.pop(paths.ENV_PORTABLE, None)
            paths.APP_DIR = app_dir
            paths.DATA_DIR_POINTER = app_dir / paths.DATA_DIR_POINTER_FILE
            paths.USER_DATA_DIR_POINTER = app_dir / "user" / paths.DATA_DIR_POINTER_FILE
            paths.PORTABLE_MARKER = app_dir / paths.PORTABLE_MARKER_FILE

            paths.DATA_DIR_POINTER.write_text("relative-data", encoding="utf-8")
            selected, source = paths._select_storage_dir()
            assert source == paths.DATA_DIR_POINTER_FILE, (selected, source)
            assert selected == (app_dir / "relative-data").resolve(), selected

            paths.DATA_DIR_POINTER.unlink()
            paths.PORTABLE_MARKER.write_text("portable", encoding="utf-8")
            selected, source = paths._select_storage_dir()
            assert source == "portable", (selected, source)
            assert selected == app_dir / paths.PORTABLE_DATA_DIR_NAME, selected

            env_dir = root / "env-data"
            os.environ[paths.ENV_DATA_DIR] = str(env_dir)
            selected, source = paths._select_storage_dir()
            assert source == paths.ENV_DATA_DIR, (selected, source)
            assert selected == env_dir.resolve(), selected

            current = root / "current-data"
            target = root / "target-data"
            paths.STORAGE_DIR = current
            current.mkdir()
            (current / "profiles.json").write_text("profiles", encoding="utf-8")
            (current / "browser_profiles").mkdir()
            (current / "browser_profiles" / "profile.txt").write_text("browser", encoding="utf-8")
            copied = paths.copy_storage_to(target)
            assert "profiles.json" in copied, copied
            assert (target / "profiles.json").read_text(encoding="utf-8") == "profiles"
            assert (target / "browser_profiles" / "profile.txt").read_text(encoding="utf-8") == "browser"
            assert paths.copy_storage_to(current) == []
            try:
                paths.copy_storage_to(current / "nested")
            except ValueError as e:
                assert "当前数据目录内部" in str(e)
            else:
                raise AssertionError("Nested target should fail")
            try:
                paths.copy_storage_to(current.parent)
            except ValueError as e:
                assert "上级目录" in str(e)
            else:
                raise AssertionError("Parent target should fail")

            paths.DATA_DIR_POINTER = app_dir / paths.DATA_DIR_POINTER_FILE
            copied = paths.write_data_dir_pointer(target, copy_current=True)
            assert paths.DATA_DIR_POINTER.read_text(encoding="utf-8") == str(target.resolve())
            assert copied == [], copied

    finally:
        if original_env is None:
            os.environ.pop(paths.ENV_DATA_DIR, None)
        else:
            os.environ[paths.ENV_DATA_DIR] = original_env
        if original_portable_env is None:
            os.environ.pop(paths.ENV_PORTABLE, None)
        else:
            os.environ[paths.ENV_PORTABLE] = original_portable_env
        paths.APP_DIR = original_app_dir
        paths.STORAGE_DIR = original_storage_dir
        paths.DATA_DIR_POINTER = original_pointer
        paths.USER_DATA_DIR_POINTER = original_user_pointer
        paths.PORTABLE_MARKER = original_marker

    print("OK storage path checks passed")


if __name__ == "__main__":
    main()
