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

    def is_git_repo(self) -> bool:
        """检查项目目录本身是否是git仓库根目录"""
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
                return False
            repo_root = Path(result.stdout.strip()).resolve()
            return repo_root == self.project_path.resolve()
        except Exception as e:
            logger.debug(f"检查git仓库失败: {e}")
            return False

    def init_repo(self) -> Tuple[bool, str]:
        """
        初始化git仓库

        Returns:
            (成功, 消息)
        """
        try:
            if self.is_git_repo():
                return True, "已经是git仓库"

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
                cwd=self.project_path,
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
                cwd=self.project_path,
                capture_output=True,
                timeout=10,
                check=True
            )

            # 生成提交消息
            if message is None:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                message = f"[{tag}] Auto snapshot at {timestamp}"

            # 提交
            result = subprocess.run(
                ["git", "commit", "-m", message],
                cwd=self.project_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10
            )

            if result.returncode != 0:
                # 可能是没有配置user.name/user.email
                if "user.name" in result.stderr or "user.email" in result.stderr:
                    # 设置默认配置
                    subprocess.run(
                        ["git", "config", "user.name", "API-Switcher-Auto"],
                        cwd=self.project_path,
                        capture_output=True,
                        timeout=5
                    )
                    subprocess.run(
                        ["git", "config", "user.email", "auto@api-switcher.local"],
                        cwd=self.project_path,
                        capture_output=True,
                        timeout=5
                    )
                    # 重试提交
                    result = subprocess.run(
                        ["git", "commit", "-m", message],
                        cwd=self.project_path,
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
                cwd=self.project_path,
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
                cwd=self.project_path,
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

    def rollback_to_commit(self, commit_hash: str, hard: bool = False) -> Tuple[bool, str]:
        """
        回滚到指定提交

        Args:
            commit_hash: 提交hash
            hard: 是否硬回滚（丢弃所有更改）

        Returns:
            (成功, 消息)
        """
        try:
            if not self.is_git_repo():
                return False, "不是git仓库"

            # 检查commit是否存在
            check_result = subprocess.run(
                ["git", "cat-file", "-t", commit_hash],
                cwd=self.project_path,
                capture_output=True,
                timeout=5
            )

            if check_result.returncode != 0:
                return False, f"提交不存在: {commit_hash}"

            # 回滚
            reset_type = "--hard" if hard else "--soft"
            result = subprocess.run(
                ["git", "reset", reset_type, commit_hash],
                cwd=self.project_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10
            )

            if result.returncode != 0:
                return False, f"回滚失败: {result.stderr}"

            logger.info(f"回滚成功: {commit_hash} (hard={hard})")
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
