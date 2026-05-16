import subprocess

from core.git_manager import GitManager


def test_git_manager_ignores_parent_repository(tmp_path):
    parent_dir = tmp_path / "parent"
    parent_dir.mkdir()
    subprocess.run(["git", "init"], cwd=parent_dir, check=True, capture_output=True)

    project_dir = parent_dir / "nested-project"
    project_dir.mkdir()
    git_mgr = GitManager(project_dir)

    assert not git_mgr.is_git_repo()

    success, message = git_mgr.init_repo()
    assert success, message
    assert git_mgr.is_git_repo()
    assert (project_dir / ".git").exists()


def test_git_manager_snapshot_flow(tmp_path):
    project_dir = tmp_path / "repo"
    project_dir.mkdir()
    git_mgr = GitManager(project_dir)

    assert not git_mgr.is_git_repo()

    success, message = git_mgr.init_repo()
    assert success, message
    assert git_mgr.is_git_repo()

    test_file = project_dir / "test.txt"
    test_file.write_text("Hello, Git Manager!", encoding="utf-8")
    assert git_mgr.has_changes()

    success, result = git_mgr.create_snapshot(message="test snapshot", tag="test")
    assert success, result
    assert result != "没有需要提交的更改"
    assert not git_mgr.has_changes()

    test_file.write_text("Hello, Git Manager! (modified)", encoding="utf-8")
    success, second_result = git_mgr.create_snapshot(message="second snapshot", tag="test")
    assert success, second_result
    assert second_result != result

    commits = git_mgr.get_recent_commits(count=5)
    messages = [commit["message"] for commit in commits]
    assert "second snapshot" in messages
    assert "test snapshot" in messages

    success, no_change_result = git_mgr.create_snapshot(message="no change", tag="test")
    assert success
    assert no_change_result == "没有需要提交的更改"
