import subprocess

from core.git_manager import GitManager


def test_git_manager_uses_existing_parent_repository_without_touching_remote(tmp_path):
    parent_dir = tmp_path / "parent"
    parent_dir.mkdir()
    subprocess.run(["git", "init"], cwd=parent_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "--local", "user.name", "Real User"], cwd=parent_dir, check=True)
    subprocess.run(["git", "config", "--local", "user.email", "real@example.com"], cwd=parent_dir, check=True)
    subprocess.run(["git", "remote", "add", "origin", "git@example.com:real/project.git"], cwd=parent_dir, check=True)

    project_dir = parent_dir / "nested-project"
    project_dir.mkdir()
    git_mgr = GitManager(project_dir)

    assert git_mgr.is_git_repo()

    success, message = git_mgr.init_repo()
    assert success, message
    assert git_mgr.is_git_repo()
    assert not (project_dir / ".git").exists()

    test_file = project_dir / "test.txt"
    test_file.write_text("tracked from nested project", encoding="utf-8")
    success, commit_hash = git_mgr.create_snapshot(message="nested snapshot", tag="test")
    assert success, commit_hash

    assert subprocess.run(
        ["git", "config", "--local", "user.name"],
        cwd=parent_dir,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip() == "Real User"
    assert subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=parent_dir,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip() == "git@example.com:real/project.git"
    assert "nested snapshot" in [
        commit["message"] for commit in git_mgr.get_recent_commits(count=1)
    ]


def test_git_manager_init_repo_sets_fallback_identity_and_gitignore_when_no_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "missing-global.gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    project_dir = tmp_path / "repo"
    project_dir.mkdir()
    git_mgr = GitManager(project_dir)

    assert not git_mgr.is_git_repo()

    success, message = git_mgr.init_repo()
    assert success, message
    assert git_mgr.is_git_repo()
    assert (project_dir / ".git").exists()
    assert subprocess.run(
        ["git", "config", "--local", "user.name"],
        cwd=project_dir,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip() == "API-Switcher-Auto"
    gitignore = (project_dir / ".gitignore").read_text(encoding="utf-8")
    assert "node_modules/" in gitignore
    assert ".env.*" in gitignore


def test_git_manager_init_repo_uses_existing_global_identity_without_local_override(tmp_path, monkeypatch):
    global_config = tmp_path / "global.gitconfig"
    subprocess.run(
        ["git", "config", "--file", str(global_config), "user.name", "Verified User"],
        check=True,
    )
    subprocess.run(
        ["git", "config", "--file", str(global_config), "user.email", "verified@example.com"],
        check=True,
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))

    project_dir = tmp_path / "repo"
    project_dir.mkdir()
    git_mgr = GitManager(project_dir)

    success, message = git_mgr.init_repo()
    assert success, message
    assert subprocess.run(
        ["git", "config", "user.name"],
        cwd=project_dir,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip() == "Verified User"
    local_name = subprocess.run(
        ["git", "config", "--local", "--get", "user.name"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert local_name.returncode != 0


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
    assert commits[0]["full_hash"]
    assert commits[0]["changed_files"] >= 1

    success, no_change_result = git_mgr.create_snapshot(message="no change", tag="test")
    assert success
    assert no_change_result == "没有需要提交的更改"


def test_git_manager_auto_snapshot_history_and_diff(tmp_path):
    project_dir = tmp_path / "repo"
    project_dir.mkdir()
    git_mgr = GitManager(project_dir)

    success, message = git_mgr.init_repo()
    assert success, message

    snapshot_file = project_dir / "snapshot.txt"
    snapshot_file.write_text("v1", encoding="utf-8")
    success, commit_hash = git_mgr.create_snapshot(
        message="[git-snapshot] 2026-05-21 10:00:00",
        tag="git-snapshot",
    )
    assert success, commit_hash

    commits = git_mgr.get_recent_commits(count=5, auto_only=True)
    assert len(commits) == 1
    assert commits[0]["auto_snapshot"] is True
    assert commits[0]["changed_files"] >= 1

    ok, diff_stat = git_mgr.get_commit_diff(commits[0]["full_hash"], stat_only=True)
    assert ok, diff_stat
    assert "snapshot.txt" in diff_stat


def test_git_manager_hard_rollback_preserves_uncommitted_changes_with_safety_tag(tmp_path):
    project_dir = tmp_path / "repo"
    project_dir.mkdir()
    git_mgr = GitManager(project_dir)

    success, message = git_mgr.init_repo()
    assert success, message

    test_file = project_dir / "test.txt"
    test_file.write_text("v1", encoding="utf-8")
    success, first_hash = git_mgr.create_snapshot(message="first snapshot", tag="test")
    assert success, first_hash

    test_file.write_text("v2", encoding="utf-8")
    success, second_hash = git_mgr.create_snapshot(message="second snapshot", tag="test")
    assert success, second_hash
    assert second_hash != first_hash

    test_file.write_text("uncommitted work", encoding="utf-8")
    success, rollback_message = git_mgr.rollback_to_commit(first_hash, hard=True)

    assert success, rollback_message
    assert "回滚前安全快照" in rollback_message
    assert test_file.read_text(encoding="utf-8") == "v1"

    tag_result = subprocess.run(
        ["git", "tag", "--list", "api-switcher-safety-*"],
        cwd=project_dir,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    safety_tags = [tag for tag in tag_result.stdout.splitlines() if tag]
    assert len(safety_tags) == 1

    show_result = subprocess.run(
        ["git", "show", f"{safety_tags[0]}:test.txt"],
        cwd=project_dir,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert show_result.stdout == "uncommitted work"


def test_git_manager_hard_rollback_tags_previous_head_without_uncommitted_changes(tmp_path):
    project_dir = tmp_path / "repo"
    project_dir.mkdir()
    git_mgr = GitManager(project_dir)

    success, message = git_mgr.init_repo()
    assert success, message

    test_file = project_dir / "test.txt"
    test_file.write_text("v1", encoding="utf-8")
    success, first_hash = git_mgr.create_snapshot(message="first snapshot", tag="test")
    assert success, first_hash

    test_file.write_text("v2", encoding="utf-8")
    success, second_hash = git_mgr.create_snapshot(message="second snapshot", tag="test")
    assert success, second_hash

    success, rollback_message = git_mgr.rollback_to_commit(first_hash, hard=True)

    assert success, rollback_message
    assert "回滚前安全快照" in rollback_message
    assert test_file.read_text(encoding="utf-8") == "v1"

    tag_result = subprocess.run(
        ["git", "tag", "--list", "api-switcher-safety-*"],
        cwd=project_dir,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    safety_tags = [tag for tag in tag_result.stdout.splitlines() if tag]
    assert len(safety_tags) == 1

    tag_hash = subprocess.run(
        ["git", "rev-parse", "--short", safety_tags[0]],
        cwd=project_dir,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    assert tag_hash == second_hash

    show_result = subprocess.run(
        ["git", "show", f"{safety_tags[0]}:test.txt"],
        cwd=project_dir,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert show_result.stdout == "v2"
