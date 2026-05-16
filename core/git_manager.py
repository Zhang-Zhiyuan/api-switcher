"""
Git版本管理模块 - 自动为项目创建git快照
"""
import subprocess
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class GitManager:
    """Git版本管理器"""

    def __init__(self, project_path: Optional[Path] = None):
        """
        初始化Git管理器

        Args:
            project_path: 项目路径，如果为None则使用当前工作目录
        """
        self.project_path = project_path or Path.cwd()

    def _repo_root(self) -> Path | None:
        """返回当前目录所在的 Git 仓库根目录；不存在仓库时返回 None。"""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=self.project_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5
            )
            if result.returncode != 0:
                return None
            return Path(result.stdout.strip()).resolve()
        except Exception as e:
            logger.debug(f"检查git仓库失败: {e}")
            return None

    def _git_cwd(self) -> Path:
        """Git 命令执行目录：已有仓库用仓库根，新项目用项目目录。"""
        return self._repo_root() or self.project_path

    def _git_config_value(self, key: str, local_only: bool = False) -> str:
        """读取 Git 配置值（优先本仓库，必要时会读到全局配置）"""
        command = ["git", "config", "--get", key]
        if local_only:
            command = ["git", "config", "--local", "--get", key]
        result = subprocess.run(
            command,
            cwd=self._git_cwd(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def _set_local_git_config(self, key: str, value: str) -> None:
        """写入当前仓库的本地 Git 配置"""
        subprocess.run(
            ["git", "config", "--local", key, value],
            cwd=self._git_cwd(),
            capture_output=True,
            timeout=5,
            check=True
        )

    def _ensure_local_identity(self) -> None:
        """
        确保本地快照可以提交。

        已有仓库或已配置全局实名身份时保持不动；只有完全缺少身份时，才写入
        API Switcher 的本地兜底身份。
        """
        user_name = self._git_config_value("user.name")
        user_email = self._git_config_value("user.email")

        if not user_name:
            self._set_local_git_config("user.name", "API-Switcher-Auto")
        if not user_email:
            self._set_local_git_config("user.email", "auto@api-switcher.local")

    def _resolve_commit(self, commit_hash: str) -> Tuple[bool, str]:
        """解析 commit/tag/引用为具体 commit hash。"""
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"{commit_hash}^{{commit}}"],
            cwd=self._git_cwd(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5
        )
        if result.returncode != 0:
            return False, result.stderr.strip() or f"提交不存在: {commit_hash}"
        return True, result.stdout.strip()

    def _current_head(self) -> str | None:
        """获取当前 HEAD commit。空仓库返回 None。"""
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=self._git_cwd(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def is_git_repo(self) -> bool:
        """检查当前目录是否已有可用 Git 仓库（包含父级仓库）。"""
        return self._repo_root() is not None

    def init_repo(self) -> Tuple[bool, str]:
        """
        初始化git仓库

        Returns:
            (成功, 消息)
        """
        try:
            repo_root = self._repo_root()
            if repo_root:
                return True, f"已使用现有 Git 仓库: {repo_root}"

            # 初始化仓库
            result = subprocess.run(
                ["git", "init"],
                cwd=self.project_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10
            )

            if result.returncode != 0:
                return False, f"初始化失败: {result.stderr}"

            self._ensure_local_identity()

            # 创建.gitignore（如果不存在）
            gitignore_path = self.project_path / ".gitignore"
            if not gitignore_path.exists():
                default_ignores = [
                    "# Python",
                    "__pycache__/",
                    "*.py[cod]",
                    "*$py.class",
                    "*.so",
                    ".Python",
                    "build/",
                    "develop-eggs/",
                    "dist/",
                    "downloads/",
                    "eggs/",
                    ".eggs/",
                    "lib/",
                    "lib64/",
                    "parts/",
                    "sdist/",
                    "var/",
                    "wheels/",
                    "*.egg-info/",
                    ".installed.cfg",
                    "*.egg",
                    "",
                    "# Virtual environments",
                    "venv/",
                    "ENV/",
                    "env/",
                    ".venv/",
                    "",
                    "# IDE",
                    ".vscode/",
                    ".idea/",
                    "*.swp",
                    "*.swo",
                    "*~",
                    "",
                    "# OS",
                    ".DS_Store",
                    "Thumbs.db",
                    "",
                    "# Logs",
                    "*.log",
                    "logs/",
                    "",
                    "# Dependency caches / generated output",
                    "node_modules/",
                    ".next/",
                    ".nuxt/",
                    "target/",
                    ".cache/",
                    ".pytest_cache/",
                    ".ruff_cache/",
                    ".mypy_cache/",
                    "coverage/",
                    ".coverage",
                    "",
                    "# Local secrets",
                    ".env",
                    ".env.*",
                    "!.env.example",
                    "!.env.sample",
                ]
                gitignore_path.write_text("\n".join(default_ignores), encoding="utf-8")

            logger.info(f"Git仓库初始化成功: {self.project_path}")
            return True, "Git仓库初始化成功"

        except subprocess.TimeoutExpired:
            return False, "初始化超时"
        except Exception as e:
            logger.error(f"初始化git仓库失败: {e}")
            return False, f"初始化失败: {str(e)}"

    def has_changes(self) -> bool:
        """检查是否有未提交的更改"""
        try:
            # 检查工作区和暂存区
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self._git_cwd(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5
            )
            return bool(result.stdout.strip())
        except Exception as e:
            logger.debug(f"检查更改失败: {e}")
            return False

    def create_snapshot(self, message: Optional[str] = None, tag: str = "auto") -> Tuple[bool, str]:
        """
        创建git快照（自动add + commit）

        Args:
            message: 提交消息，如果为None则自动生成
            tag: 标签，用于区分不同类型的快照

        Returns:
            (成功, 消息/commit hash)
        """
        try:
            # 确保是git仓库
            if not self.is_git_repo():
                success, msg = self.init_repo()
                if not success:
                    return False, f"无法初始化git仓库: {msg}"

            # 检查是否有更改
            if not self.has_changes():
                return True, "没有需要提交的更改"

            # 添加所有更改
            subprocess.run(
                ["git", "add", "-A"],
                cwd=self._git_cwd(),
                capture_output=True,
                timeout=10,
                check=True
            )

            self._ensure_local_identity()

            # 生成提交消息
            if message is None:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                message = f"[{tag}] Auto snapshot at {timestamp}"

            # 提交
            result = subprocess.run(
                ["git", "commit", "-m", message],
                cwd=self._git_cwd(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10
            )

            if result.returncode != 0:
                # 可能是没有配置user.name/user.email
                if "user.name" in result.stderr or "user.email" in result.stderr:
                    self._ensure_local_identity()
                    # 重试提交
                    result = subprocess.run(
                        ["git", "commit", "-m", message],
                        cwd=self._git_cwd(),
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=10
                    )

            if result.returncode != 0:
                return False, f"提交失败: {result.stderr}"

            # 获取commit hash
            hash_result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=self._git_cwd(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5
            )
            commit_hash = hash_result.stdout.strip() if hash_result.returncode == 0 else "unknown"

            logger.info(f"创建快照成功: {commit_hash} - {message}")
            return True, commit_hash

        except subprocess.TimeoutExpired:
            return False, "操作超时"
        except subprocess.CalledProcessError as e:
            return False, f"Git命令失败: {e.stderr if hasattr(e, 'stderr') else str(e)}"
        except Exception as e:
            logger.error(f"创建快照失败: {e}")
            return False, f"创建快照失败: {str(e)}"

    def get_recent_commits(self, count: int = 10) -> list[dict]:
        """
        获取最近的提交记录

        Args:
            count: 获取的提交数量

        Returns:
            提交记录列表，每个记录包含 hash, message, author, date
        """
        try:
            if not self.is_git_repo():
                return []

            result = subprocess.run(
                ["git", "log", f"-{count}", "--pretty=format:%h|%s|%an|%ar"],
                cwd=self._git_cwd(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5
            )

            if result.returncode != 0:
                return []

            commits = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split("|", 3)
                if len(parts) == 4:
                    commits.append({
                        "hash": parts[0],
                        "message": parts[1],
                        "author": parts[2],
                        "date": parts[3]
                    })

            return commits

        except Exception as e:
            logger.debug(f"获取提交记录失败: {e}")
            return []

    def _create_safety_tag(self, commit_hash: str) -> Tuple[bool, str]:
        """为安全快照创建一个稳定可找回的标签"""
        base_tag = f"api-switcher-safety-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

        for index in range(100):
            tag_name = base_tag if index == 0 else f"{base_tag}-{index:02d}"
            result = subprocess.run(
                ["git", "tag", tag_name, commit_hash],
                cwd=self._git_cwd(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5
            )

            if result.returncode == 0:
                return True, tag_name

            if "already exists" not in result.stderr:
                return False, result.stderr.strip() or f"无法创建标签: {tag_name}"

        return False, "无法创建唯一的安全快照标签"

    def rollback_to_commit(
        self,
        commit_hash: str,
        hard: bool = False,
        create_safety_snapshot: bool = True
    ) -> Tuple[bool, str]:
        """
        回滚到指定提交

        Args:
            commit_hash: 提交hash
            hard: 是否硬回滚（丢弃所有更改）
            create_safety_snapshot: 回滚前是否自动保存当前未提交更改

        Returns:
            (成功, 消息)
        """
        try:
            if not self.is_git_repo():
                return False, "不是git仓库"

            target_success, target_commit = self._resolve_commit(commit_hash)
            if not target_success:
                return False, f"提交不存在: {commit_hash}"

            current_head = self._current_head()

            safety_tag = None
            if create_safety_snapshot and self.has_changes():
                snapshot_message = f"[rollback] Safety snapshot before reset to {commit_hash}"
                snapshot_success, snapshot_result = self.create_snapshot(
                    message=snapshot_message,
                    tag="rollback"
                )
                if not snapshot_success:
                    return False, f"回滚前安全快照失败: {snapshot_result}"
                if snapshot_result != "没有需要提交的更改":
                    tag_success, tag_result = self._create_safety_tag(snapshot_result)
                    if not tag_success:
                        return False, f"安全快照标签创建失败: {tag_result}"
                    safety_tag = tag_result
            elif create_safety_snapshot and current_head and current_head != target_commit:
                tag_success, tag_result = self._create_safety_tag(current_head)
                if not tag_success:
                    return False, f"回滚前版本标签创建失败: {tag_result}"
                safety_tag = tag_result

            # 回滚
            reset_type = "--hard" if hard else "--soft"
            result = subprocess.run(
                ["git", "reset", reset_type, target_commit],
                cwd=self._git_cwd(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10
            )

            if result.returncode != 0:
                return False, f"回滚失败: {result.stderr}"

            logger.info(f"回滚成功: {commit_hash} (hard={hard})")
            if safety_tag:
                return True, f"已回滚到 {commit_hash}（回滚前安全快照: {safety_tag}）"
            return True, f"已回滚到 {commit_hash}"

        except subprocess.TimeoutExpired:
            return False, "操作超时"
        except Exception as e:
            logger.error(f"回滚失败: {e}")
            return False, f"回滚失败: {str(e)}"


# 全局实例
_git_manager_cache = {}


def get_git_manager(project_path: Optional[Path] = None) -> GitManager:
    """
    获取GitManager实例（带缓存）

    Args:
        project_path: 项目路径

    Returns:
        GitManager实例
    """
    path = project_path or Path.cwd()
    path_str = str(path.resolve())

    if path_str not in _git_manager_cache:
        _git_manager_cache[path_str] = GitManager(path)

    return _git_manager_cache[path_str]
