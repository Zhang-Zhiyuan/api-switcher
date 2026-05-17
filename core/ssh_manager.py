import logging
import errno
import posixpath
import time
import uuid
from pathlib import Path
import paramiko
from core import security
from models.profile import SSHProfile

logger = logging.getLogger(__name__)


class SSHManager:
    """Manages SSH connections and remote file operations with retry and timeout mechanisms."""

    def __init__(self):
        self._clients: dict[str, paramiko.SSHClient] = {}
        self._client_signatures: dict[str, tuple] = {}

    @staticmethod
    def _connection_signature(profile: SSHProfile) -> tuple:
        """Return fields that identify the remote endpoint and auth material."""
        return (
            str(profile.host or "").strip(),
            str(profile.port or "").strip(),
            str(profile.username or "").strip(),
            str(profile.auth_type or "").strip(),
            str(profile.private_key_path or "").strip(),
            str(profile.private_key_passphrase_ref or "").strip(),
            str(profile.password_ref or "").strip(),
        )

    def connect(self, profile: SSHProfile, timeout: int = 10, max_retries: int = 3) -> paramiko.SSHClient:
        """Establish SSH connection with retry mechanism."""
        signature = self._connection_signature(profile)

        # Check if already connected
        if profile.name in self._clients:
            client = self._clients[profile.name]
            try:
                transport = client.get_transport()
                if (
                    transport
                    and transport.is_active()
                    and self._client_signatures.get(profile.name) == signature
                ):
                    logger.debug(f"Reusing existing connection to {profile.host}")
                    return client
                logger.debug(f"Cached SSH connection for {profile.name} is stale, reconnecting")
            except Exception as e:
                logger.debug(f"Existing connection invalid: {e}")
            # Clean up invalid or stale connection
            try:
                client.close()
            except Exception:
                pass
            del self._clients[profile.name]
            self._client_signatures.pop(profile.name, None)

        # Validate profile
        if not profile.host or not profile.host.strip():
            raise ValueError("SSH 主机地址不能为空")
        if not profile.username or not profile.username.strip():
            raise ValueError("SSH 用户名不能为空")
        if profile.port <= 0 or profile.port > 65535:
            raise ValueError(f"无效的端口号: {profile.port}")

        # Prepare connection parameters
        connect_kwargs = {
            "hostname": profile.host.strip(),
            "port": profile.port,
            "username": profile.username.strip(),
            "timeout": timeout,
            "banner_timeout": timeout,
            "auth_timeout": timeout,
        }

        # Handle authentication
        if profile.auth_type == "key":
            if not profile.private_key_path:
                raise ValueError("密钥认证需要指定私钥路径")

            key_path = Path(profile.private_key_path).expanduser()
            if not key_path.exists():
                raise FileNotFoundError(f"私钥文件不存在: {key_path}")
            if not key_path.is_file():
                raise ValueError(f"私钥路径不是文件: {key_path}")

            passphrase = None
            if profile.private_key_passphrase_ref:
                try:
                    passphrase = security.get_secret(profile.private_key_passphrase_ref)
                except Exception as e:
                    logger.warning(f"Failed to retrieve key passphrase: {e}")

            connect_kwargs["key_filename"] = str(key_path)
            if passphrase:
                connect_kwargs["passphrase"] = passphrase

        elif profile.auth_type == "password":
            if not profile.password_ref:
                raise ValueError("密码认证需要指定密码")

            try:
                password = security.get_secret(profile.password_ref)
                if not password:
                    raise ValueError("密码为空")
                connect_kwargs["password"] = password
            except Exception as e:
                raise ValueError(f"无法获取密码: {e}") from e
        else:
            raise ValueError(f"不支持的认证类型: {profile.auth_type}")

        # Retry connection with exponential backoff
        last_error = None
        for attempt in range(max_retries):
            try:
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

                logger.info(f"Connecting to {profile.host}:{profile.port} (attempt {attempt + 1}/{max_retries})")
                client.connect(**connect_kwargs)

                # Verify connection is working
                transport = client.get_transport()
                if not transport or not transport.is_active():
                    raise RuntimeError("连接建立后立即失效")

                self._clients[profile.name] = client
                self._client_signatures[profile.name] = signature
                logger.info(f"Successfully connected to {profile.host} as {profile.username}")
                return client

            except paramiko.AuthenticationException as e:
                # Don't retry authentication failures
                logger.error(f"Authentication failed: {e}")
                raise RuntimeError(f"认证失败: {e}") from e

            except paramiko.SSHException as e:
                last_error = e
                logger.warning(f"SSH error on attempt {attempt + 1}: {e}")
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                    logger.info(f"Retrying in {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    raise RuntimeError(f"SSH 连接失败 (已重试 {max_retries} 次): {e}") from e

            except Exception as e:
                last_error = e
                logger.error(f"Connection error on attempt {attempt + 1}: {e}")
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    time.sleep(wait_time)
                else:
                    raise RuntimeError(f"连接失败: {e}") from e

        # Should not reach here, but just in case
        raise RuntimeError(f"连接失败: {last_error}")

    def disconnect(self, name: str):
        """Disconnect from a server with error handling."""
        if name in self._clients:
            try:
                self._clients[name].close()
                logger.info(f"Disconnected from {name}")
            except Exception as e:
                logger.warning(f"Error closing connection to {name}: {e}")
            finally:
                del self._clients[name]
                self._client_signatures.pop(name, None)

    def disconnect_all(self):
        """Disconnect all clients."""
        for name in list(self._clients.keys()):
            self.disconnect(name)

    def is_connected(self, name: str) -> bool:
        """Check if connected to a server."""
        if name not in self._clients:
            return False
        try:
            transport = self._clients[name].get_transport()
            return transport is not None and transport.is_active()
        except Exception:
            return False

    @staticmethod
    def _is_not_found_error(error: BaseException) -> bool:
        return (
            isinstance(error, FileNotFoundError)
            or getattr(error, "errno", None) == errno.ENOENT
            or "No such file" in str(error)
        )

    @staticmethod
    def _normalize_remote_path(path: str) -> str:
        text = str(path or "").strip().replace("\\", "/")
        if not text:
            raise ValueError("文件路径不能为空")
        return posixpath.normpath(text)

    @staticmethod
    def _sftp_operation_unsupported(error: BaseException) -> bool:
        text = str(error).lower()
        return (
            getattr(error, "errno", None) in {errno.ENOSYS, errno.EOPNOTSUPP}
            or "unsupported" in text
            or "not implemented" in text
        )

    def _replace_remote_file(self, sftp, source: str, target: str) -> None:
        """Atomically replace a remote file when the server supports it."""
        posix_rename = getattr(sftp, "posix_rename", None)
        if callable(posix_rename):
            try:
                posix_rename(source, target)
                return
            except Exception as e:
                if not self._sftp_operation_unsupported(e):
                    raise
                logger.debug(f"SFTP posix_rename unsupported, falling back to rename: {e}")

        try:
            sftp.rename(source, target)
        except Exception:
            # Older SFTP servers often refuse rename-over-existing. This fallback
            # keeps compatibility when posix_rename is unavailable.
            try:
                sftp.remove(target)
            except Exception:
                pass
            sftp.rename(source, target)

    def read_remote_file(self, client: paramiko.SSHClient, path: str, timeout: int = 30) -> str | None:
        """Read a file from the remote server with timeout."""
        path = self._normalize_remote_path(path)

        sftp = None
        try:
            sftp = client.open_sftp()
            sftp.get_channel().settimeout(timeout)

            with sftp.open(path, "rb") as f:
                raw = f.read()
                content = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else str(raw)
                logger.debug(f"Read {len(content)} bytes from {path}")
                return content

        except IOError as e:
            if self._is_not_found_error(e):
                logger.info(f"Remote file not found: {path}")
                return None
            logger.error(f"IO error reading {path}: {e}")
            raise RuntimeError(f"读取远程文件失败: {e}") from e
        except Exception as e:
            if self._is_not_found_error(e):
                logger.info(f"Remote file not found: {path}")
                return None
            logger.error(f"Error reading remote file {path}: {e}")
            raise RuntimeError(f"读取远程文件失败: {e}") from e
        finally:
            if sftp:
                try:
                    sftp.close()
                except Exception:
                    pass

    def write_remote_file(
        self,
        client: paramiko.SSHClient,
        path: str,
        content: str,
        timeout: int = 30,
        file_mode: int | None = None,
    ):
        """Write a file to the remote server with atomic operation."""
        path = self._normalize_remote_path(path)
        if content is None:
            raise ValueError("文件内容不能为 None")

        sftp = None
        temp_path = f"{path}.tmp.{uuid.uuid4().hex}"

        try:
            sftp = client.open_sftp()
            sftp.get_channel().settimeout(timeout)

            # Ensure directory exists
            remote_dir = posixpath.dirname(path)
            if remote_dir:
                self._ensure_remote_dir(sftp, remote_dir)

            # Write to temporary file
            with sftp.open(temp_path, "wb") as f:
                f.write(content.encode("utf-8"))

            if file_mode is not None:
                try:
                    sftp.chmod(temp_path, file_mode)
                except Exception as e:
                    logger.warning(f"Failed to chmod temporary remote file {temp_path}: {e}")

            self._replace_remote_file(sftp, temp_path, path)

            if file_mode is not None:
                try:
                    sftp.chmod(path, file_mode)
                except Exception as e:
                    logger.warning(f"Failed to chmod remote file {path}: {e}")

            logger.info(f"Wrote {len(content)} bytes to {path}")

        except Exception as e:
            # Clean up temp file on error
            if sftp:
                try:
                    sftp.remove(temp_path)
                except Exception:
                    pass
            logger.error(f"Error writing remote file {path}: {e}")
            raise RuntimeError(f"写入远程文件失败: {e}") from e
        finally:
            if sftp:
                try:
                    sftp.close()
                except Exception:
                    pass

    def execute_command(self, client: paramiko.SSHClient, cmd: str, timeout: int = 30) -> tuple[str, str]:
        """Execute a command on the remote server with timeout."""
        if not cmd or not cmd.strip():
            raise ValueError("命令不能为空")

        try:
            logger.debug(f"Executing command: {cmd}")
            stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)

            stdout_data = stdout.read().decode("utf-8", errors="replace")
            stderr_data = stderr.read().decode("utf-8", errors="replace")

            exit_status = stdout.channel.recv_exit_status()
            logger.debug(f"Command exit status: {exit_status}")

            return stdout_data, stderr_data

        except Exception as e:
            logger.error(f"Error executing command: {e}")
            raise RuntimeError(f"执行远程命令失败: {e}") from e

    def execute_command_with_status(
        self,
        client: paramiko.SSHClient,
        cmd: str,
        timeout: int = 30,
        input_data: str | None = None,
        log_command: bool = True,
        get_pty: bool = False,
    ) -> tuple[int, str, str]:
        """Execute a command and return (exit_status, stdout, stderr)."""
        if not cmd or not cmd.strip():
            raise ValueError("命令不能为空")

        try:
            logger.debug(f"Executing command: {cmd if log_command else '[redacted]'}")
            stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout, get_pty=get_pty)
            if input_data is not None:
                stdin.write(input_data)
                stdin.flush()
            try:
                stdin.channel.shutdown_write()
            except Exception:
                pass

            stdout_data = stdout.read().decode("utf-8", errors="replace")
            stderr_data = stderr.read().decode("utf-8", errors="replace")

            exit_status = stdout.channel.recv_exit_status()
            logger.debug(f"Command exit status: {exit_status}")

            return exit_status, stdout_data, stderr_data

        except Exception as e:
            logger.error(f"Error executing command: {e}")
            raise RuntimeError(f"执行远程命令失败: {e}") from e

    def test_connection(self, profile: SSHProfile) -> tuple[bool, str]:
        """Test SSH connection with comprehensive validation."""
        try:
            # Attempt connection
            client = self.connect(profile, timeout=10, max_retries=2)

            # Execute test command
            stdout, stderr = self.execute_command(client, "echo 'Connection OK'", timeout=5)

            if "Connection OK" in stdout:
                return True, f"连接成功: {profile.host}:{profile.port}"
            else:
                return False, f"连接测试失败: {stderr or '未知错误'}"

        except Exception as e:
            logger.error(f"Connection test failed: {e}")
            return False, f"连接失败: {e}"

    def _ensure_remote_dir(self, sftp, path: str):
        """Ensure remote directory exists with error handling."""
        path = self._normalize_remote_path(path)
        if not path or path in {".", "/"}:
            return

        parts = [p for p in path.split("/") if p and p != "."]
        current = "/" if path.startswith("/") else ""

        for part in parts:
            current = posixpath.join(current, part) if current else part

            try:
                sftp.stat(current)
            except OSError as e:
                if not self._is_not_found_error(e):
                    raise RuntimeError(f"无法访问远程目录 {current}: {e}") from e
                try:
                    sftp.mkdir(current)
                    try:
                        sftp.chmod(current, 0o700)
                    except Exception:
                        pass
                    logger.debug(f"Created remote directory: {current}")
                except Exception as e:
                    logger.error(f"Failed to create directory {current}: {e}")
                    raise RuntimeError(f"无法创建远程目录 {current}: {e}") from e


# Global instance
ssh_manager = SSHManager()
