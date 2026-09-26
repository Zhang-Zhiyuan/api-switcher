"""Transfer one official login with optional password protection.

Export never changes a live login or a saved snapshot. Import creates a locally
encrypted account snapshot, never activates it or replaces different credentials.
The existing account switch transaction remains the sole activation mechanism.
An empty password explicitly exports an unencrypted, sensitive login package.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from config import paths
from core import auth_parser, parser, portable_migration, profile_manager, security, toml_parser
from core.atomic_io import atomic_write_text
from models.profile import ClaudeAccountProfile, CodexAccountProfile


ACCOUNT_FORMAT = "api-switcher-account-login"
ACCOUNT_VERSION = 1
ACCOUNT_EXTENSION = ".asxaccount"
MAX_ACCOUNT_FILE_BYTES = 2 * 1024 * 1024
MAX_ACCOUNT_PAYLOAD_BYTES = 1024 * 1024
_IDENTITY_KEYS = (
    "email", "user_email", "account_email", "userId", "user_id", "account_id",
    "sub", "name", "display_name", "displayName", "full_name", "nickname",
    "preferred_username", "username", "organizationUuid", "organization_id",
    "accountUuid", "accountId", "chatgpt_account_id",
)
_TOKEN_KEYS = ("access_token", "refresh_token", "id_token", "accessToken", "refreshToken", "idToken")
_CODEX_REFRESH_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt ][0-9]{2}:[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]{1,9})?(?:[Zz]|[+-][0-9]{2}:[0-5][0-9])"
)


@dataclass(frozen=True)
class AccountExportResult:
    path: Path
    profile_type: str
    account_name: str
    used_current_login: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class AccountImportResult:
    profile_type: str
    account_name: str
    created_new: bool
    warnings: tuple[str, ...] = ()


def _profile_type(value: object) -> str:
    if value not in ("claude", "codex"):
        raise ValueError("账号类型必须是 Claude 或 Codex")
    return str(value)


def _account_name(value: object, fallback: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return fallback
    # Names are metadata, never paths or imported secret-store references.
    cleaned = "".join(char for char in value if ord(char) >= 32 and ord(char) != 127)
    return cleaned.strip()[:160] or fallback


def _copy_identity(source: dict, destination: dict) -> None:
    for key in _IDENTITY_KEYS:
        value = source.get(key)
        if isinstance(value, str) and len(value) <= 1024:
            destination[key] = value


def _clean_credentials(profile_type: str, value: object) -> dict:
    """Allow only known official-login fields, not API/MCP/browser secrets."""
    if not isinstance(value, dict):
        raise ValueError("账号登录状态必须是有效的官方 OAuth 凭据")
    if profile_type == "codex":
        if str(value.get("auth_mode") or "").lower() in {"chatgptauthtokens", "chatgpt_auth_tokens"}:
            raise ValueError("这是外部程序管理的 Codex 临时登录，不能作为可刷新的账号迁移；请用 codex login 独立登录")
        value = auth_parser.normalize_codex_token_fields(value)
        raw_tokens = value.get("tokens")
        if not isinstance(raw_tokens, dict):
            raise ValueError("Codex 登录状态缺少官方登录 token，不能导入 API 配置")
        tokens = {}
        for key in _TOKEN_KEYS:
            token = raw_tokens.get(key)
            if token is not None:
                if not isinstance(token, str):
                    raise ValueError("Codex 登录 token 格式无效")
                tokens[key] = token
        if not any(tokens.get(key, "").strip() for key in ("access_token", "refresh_token", "accessToken", "refreshToken")):
            raise ValueError("Codex 登录状态没有可用的 access/refresh token")
        missing = [key for key in ("id_token", "access_token", "refresh_token") if not tokens.get(key, "").strip()]
        if missing:
            raise ValueError(
                "Codex 登录状态不完整（缺少 " + ", ".join(missing)
                + "），无法迁移可自动续期的官方账号。请在源电脑重新登录后导出当前登录。"
            )
        claims = auth_parser.codex_token_claims(tokens["id_token"])
        if not claims:
            raise ValueError("Codex id_token 格式无效，客户端无法读取；请重新登录后导出当前登录")
        # Codex deserializes these optional claim fields as strings. A JSON
        # payload that Python can decode is not necessarily readable by Codex.
        # Unknown extra claims remain untouched; this is not signature checking.
        for mapping, fields in (
            (claims, ("email",)),
            (claims.get("https://api.openai.com/auth"), ("chatgpt_account_id", "chatgpt_plan_type")),
        ):
            if mapping is None:
                continue
            if not isinstance(mapping, dict) or any(
                mapping.get(key) is not None and not isinstance(mapping[key], str) for key in fields
            ):
                raise ValueError("Codex id_token 账号字段类型无效，客户端无法读取；请重新导出当前登录")
        _copy_identity(raw_tokens, tokens)
        result = {"auth_mode": "chatgpt", "tokens": tokens}
        if value.get("last_refresh") is not None:
            from datetime import datetime

            refresh_time = value["last_refresh"]
            try:
                # datetime.fromisoformat also accepts compact/week dates and
                # second-granularity UTC offsets, which Codex's RFC3339 parser
                # rejects. Preserve the accepted original string and precision.
                if not isinstance(refresh_time, str) or not _CODEX_REFRESH_TIME.fullmatch(refresh_time):
                    raise ValueError
                parsed_refresh = datetime.fromisoformat(refresh_time.replace("Z", "+00:00").replace("z", "+00:00"))
                if parsed_refresh.tzinfo is None:
                    raise ValueError
            except (ValueError, OverflowError):
                raise ValueError("Codex last_refresh 格式无效，请重新导出当前登录") from None
            result["last_refresh"] = refresh_time
    else:
        raw_oauth = value.get("claudeAiOauth")
        if not isinstance(raw_oauth, dict):
            raise ValueError("Claude 登录状态缺少 claudeAiOauth，不能导入 API 配置")
        oauth = {}
        for key in ("accessToken", "refreshToken"):
            token = raw_oauth.get(key)
            if token is not None:
                if not isinstance(token, str):
                    raise ValueError("Claude 登录 token 格式无效")
                oauth[key] = token
        if not any(oauth.get(key, "").strip() for key in ("accessToken", "refreshToken")):
            raise ValueError("Claude 登录状态没有可用的 access/refresh token")
        for key in ("expiresAt", "subscriptionType", "rateLimitTier"):
            item = raw_oauth.get(key)
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("Claude 登录状态的过期时间或账号元数据无效")
            if item is None or type(item) in (str, int, float):
                if key in raw_oauth:
                    oauth[key] = item
        scopes = raw_oauth.get("scopes")
        if isinstance(scopes, list) and all(isinstance(item, str) for item in scopes):
            oauth["scopes"] = list(scopes)
        _copy_identity(raw_oauth, oauth)
        result = {"claudeAiOauth": oauth}
    _copy_identity(value, result)
    # Keep only account identification in optional metadata objects.
    for key in ("account", "oauthAccount"):
        if isinstance(value.get(key), dict):
            metadata = {}
            _copy_identity(value[key], metadata)
            if metadata:
                result[key] = metadata
    try:
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (UnicodeError, ValueError) as exc:
        raise ValueError("账号登录状态含无效字符或元数据") from exc
    if len(encoded) > MAX_ACCOUNT_PAYLOAD_BYTES:
        raise ValueError("账号登录状态过大，已拒绝处理")
    return result


def _login_warnings(profile_type: str, credentials: dict) -> tuple[str, ...]:
    if profile_type != "codex":
        return ()
    from datetime import datetime, timezone

    warnings = []
    tokens = credentials.get("tokens") or {}
    access_claims = auth_parser.codex_token_claims(tokens.get("access_token"))
    expiry = auth_parser._auth_timestamp(access_claims.get("exp"))
    if expiry and expiry <= datetime.now(timezone.utc).timestamp():
        warnings.append("访问令牌已过期，需要 Codex 使用刷新凭据续期；若刷新凭据已失效或已被另一台电脑使用，须重新登录后导出。")
    if not tokens.get("refresh_token"):
        warnings.append("登录包缺少刷新凭据，不能保证到另一台电脑后自动续期；建议在源电脑重新登录并导出当前登录。")
    return tuple(warnings)


def _read_current_raw(profile_type: str, *, require_official_mode: bool = True) -> dict:
    if profile_type == "codex":
        try:
            config = toml_parser.read_codex_config()
            store = profile_manager._codex_credentials_store(config)
        except (OSError, ValueError) as exc:
            raise ValueError("无法确认 Codex 凭据存储方式，请检查配置文件") from exc
        if store != "file":
            raise ValueError(
                "当前 Codex 凭据存储为 auto/keyring，不能确认 auth.json 是有效登录。"
                "请先让 Codex 使用 file 凭据存储并重新登录，或导出已保存的账号快照。"
            )
        credentials = auth_parser.read_codex_auth()
        if require_official_mode and profile_manager._codex_account_override_active(config, credentials):
            raise ValueError("Codex 当前正在使用 API 配置，请从已保存的官方账号卡片导出登录状态")
    else:
        credentials = parser.read_claude_credentials()
        if require_official_mode and profile_manager._claude_api_override_active(
            parser.read_claude_settings(), parser.read_claude_config(),
        ):
            raise ValueError("Claude 当前正在使用 API 配置，请从已保存的官方账号卡片导出登录状态")
    _clean_credentials(profile_type, credentials)
    return credentials


def _read_current(profile_type: str) -> dict:
    return _clean_credentials(profile_type, _read_current_raw(profile_type))


def _profiles(profile_type: str) -> list:
    return (profile_manager.list_codex_account_profiles() if profile_type == "codex"
            else profile_manager.list_claude_account_profiles())


def _saved_credentials(profile_type: str, profile) -> dict:
    value = (profile_manager.load_codex_account_auth(profile) if profile_type == "codex"
             else profile_manager.load_claude_account_credentials(profile))
    return _clean_credentials(profile_type, value)


def _same_login(profile_type: str, saved: dict, current: dict) -> bool:
    # Display names alone are not identity proof and must not select another login.
    # One user/email can own multiple Codex accounts/workspaces. Explicit IDs
    # take precedence over shared email/JWT claims when exporting a chosen card.
    if profile_manager._account_snapshot_identity_conflict(saved, current):
        return False
    prefix = f"{profile_type}-login"
    saved_ids = profile_manager._account_stable_identity_candidates_from_json(saved, prefix)
    current_ids = profile_manager._account_stable_identity_candidates_from_json(current, prefix)
    return bool(saved_ids & current_ids) if saved_ids and current_ids else saved == current


def _validate_output_path(path: Path) -> None:
    if path.suffix.lower() != ACCOUNT_EXTENSION:
        raise ValueError("账号登录包请使用 .asxaccount 扩展名")
    if path.exists() and not path.is_file():
        raise ValueError("账号导出路径必须是文件")
    profile_path = Path(profile_manager.PROFILES_FILE).resolve()
    protected_files = {
        profile_path, profile_path.with_suffix(".backup"),
        Path(parser.CLAUDE_CONFIG).resolve(),
        Path(paths.VSCODE_SETTINGS).resolve(),
        Path(paths.DATA_DIR_POINTER).resolve(), Path(paths.USER_DATA_DIR_POINTER).resolve(),
    }
    protected_directories = {
        Path(paths.SECRETS_DIR).resolve(),
        Path(parser.CLAUDE_CREDENTIALS).resolve().parent,
        Path(parser.CLAUDE_SETTINGS).resolve().parent,
        Path(auth_parser.CODEX_AUTH).resolve().parent,
        Path(toml_parser.CODEX_CONFIG).resolve().parent,
        Path(paths.CODEX_ENV).resolve().parent,
    }
    if path in protected_files or any(path == root or root in path.parents for root in protected_directories):
        raise ValueError("账号登录包不能覆盖或写入正在使用的账号、配置或密钥目录")


def export_account_login(
    output_path: str | Path,
    password: str,
    profile_type: str,
    account_name: str | None = None,
) -> AccountExportResult:
    """Export a current or saved official login without changing either."""
    profile_type = _profile_type(profile_type)
    if not isinstance(password, str):
        raise ValueError("账号迁移密码必须是文本；不设密码请留空")
    if password != "" and len(password) < 8:
        raise ValueError("设置账号迁移密码时至少需要 8 个字符，也可以留空")
    path = Path(output_path).expanduser().resolve()
    _validate_output_path(path)
    from core.switcher import _SWITCH_LOCK

    with _SWITCH_LOCK, profile_manager._STORE_CACHE_LOCK:
        used_current = account_name is None
        if account_name is None:
            credentials = _read_current(profile_type)
            preferred = profile_manager._account_preferred_name_from_json(credentials, f"{profile_type}-login")
            name = _account_name(preferred, f"{profile_type}-账号")
        else:
            selected = next((item for item in _profiles(profile_type) if item.name == account_name), None)
            if selected is None:
                raise ValueError("要导出的账号快照已不存在，请刷新后重试")
            name = selected.name
            credentials = _saved_credentials(profile_type, selected)
            try:
                current = _read_current(profile_type)
            except (OSError, ValueError):
                current = None
            if (current is not None and _same_login(profile_type, credentials, current)
                    and not (profile_type == "codex" and auth_parser.codex_auth_is_newer(credentials, current))):
                credentials = current
                used_current = True
        payload = {
            "payload_version": ACCOUNT_VERSION,
            "kind": ACCOUNT_FORMAT,
            "profile_type": profile_type,
            "account_name": name,
            "credentials": credentials,
        }
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_ACCOUNT_PAYLOAD_BYTES:
            raise ValueError("账号登录状态过大，已拒绝导出")
    bundle = portable_migration._encode_bundle_payload(payload, password)
    bundle["format"] = ACCOUNT_FORMAT
    serialized = json.dumps(bundle, ensure_ascii=False, indent=2)
    if len(serialized.encode("utf-8")) > MAX_ACCOUNT_FILE_BYTES:
        raise ValueError("账号登录包过大，已拒绝导出")
    _validate_output_path(path)
    atomic_write_text(path, serialized)
    return AccountExportResult(path, profile_type, name, used_current, _login_warnings(profile_type, credentials))


def _read_payload(path: Path, password: str) -> dict:
    if not path.is_file():
        raise ValueError("请选择有效的账号登录包文件")
    # Bound the actual read too: file size may change between stat and open.
    with path.open("rb") as handle:
        raw = handle.read(MAX_ACCOUNT_FILE_BYTES + 1)
    if len(raw) > MAX_ACCOUNT_FILE_BYTES:
        raise ValueError("账号登录包过大，已拒绝读取")
    try:
        bundle = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError("账号登录包格式损坏") from exc
    if not isinstance(bundle, dict) or bundle.get("format") != ACCOUNT_FORMAT:
        raise ValueError("不是独立账号登录包，请选择 .asxaccount 文件")
    if type(bundle.get("version")) is not int or bundle["version"] not in (
        ACCOUNT_VERSION, portable_migration.UNENCRYPTED_BUNDLE_VERSION,
    ):
        raise ValueError("账号登录包版本不受支持")
    if bundle.get("compression") != "zlib":
        raise ValueError("账号登录包压缩格式不受支持")
    if bundle["version"] == ACCOUNT_VERSION and password == "":
        raise ValueError("该账号登录包已加密，请输入迁移密码")
    # Reuse the strict encrypted/unencrypted dispatch and small size cap.
    # The dedicated inner kind also prevents confusing another package type.
    bundle["format"] = portable_migration.BUNDLE_FORMAT
    try:
        payload = portable_migration._decode_bundle_payload(bundle, password, max_payload_bytes=MAX_ACCOUNT_PAYLOAD_BYTES)
    except (TypeError, AttributeError, RecursionError) as exc:
        raise ValueError("账号登录包数据损坏") from exc
    except ValueError as exc:
        # Do not reflect untrusted format fields (or their content) into UI errors.
        if bundle["version"] == portable_migration.UNENCRYPTED_BUNDLE_VERSION:
            raise ValueError("未加密账号登录包已损坏、不受支持或超出安全限制") from exc
        raise ValueError("账号迁移密码错误，或登录包已损坏、超出安全限制") from exc
    if payload.get("kind") != ACCOUNT_FORMAT or type(payload.get("payload_version")) is not int:
        raise ValueError("账号登录包内容类型无效")
    return payload


def _new_profile(profile_type: str, preferred: str, credentials: dict, reserved_names: set[str]):
    """Reserve a local name/ref without overwriting orphaned or aliased secrets."""
    refs = profile_manager._store_secret_refs(profile_manager._load_store())
    name = profile_manager._unique_profile_name(reserved_names, preferred)
    prefix, suffix = ("codex-account:", ":auth_json") if profile_type == "codex" else ("claude-account:", ":credentials")
    while True:
        ref = f"{prefix}{name}{suffix}"
        if ref not in refs and security.get_secret_strict(ref) is None:
            break
        reserved_names.add(name)
        name = profile_manager._unique_profile_name(reserved_names, preferred)
    reserved_names.add(name)
    identity = profile_manager._identity_from_json(credentials, f"{profile_type}-login")
    created_at = profile_manager._now_iso()
    if profile_type == "codex":
        return CodexAccountProfile(name, ref, identity, created_at)
    return ClaudeAccountProfile(name, ref, identity, created_at)


def _save_new_accounts(profile_type: str, accounts: list[tuple[object, dict]]) -> None:
    """Commit the destination safety snapshot and imported snapshot together."""
    from core.switcher import _local_switch_transaction, _profile_store_transaction_paths

    refs = {
        (profile.auth_json_ref if profile_type == "codex" else profile.credentials_ref): None
        for profile, _credentials in accounts
    }
    # All refs have been checked absent under the store lock. The switch
    # transaction restores exact profile/backup bytes; secrets require their
    # own rollback because the OS keyring is not a file we may overwrite.
    with _local_switch_transaction(_profile_store_transaction_paths()):
        try:
            for profile, credentials in accounts:
                if profile_type == "codex":
                    profile_manager.save_codex_account_profile_with_auth(profile, credentials)
                else:
                    profile_manager.save_claude_account_profile_with_credentials(profile, credentials)
        except Exception as exc:
            errors = profile_manager._restore_secret_values(refs)
            if errors:
                raise RuntimeError("账号导入失败，且本机密钥回滚不完整；请检查密钥存储后再试") from exc
            raise


def import_account_login(
    input_path: str | Path,
    password: str,
    expected_type: str | None = None,
) -> AccountImportResult:
    """Save one login using this machine's secret store; never activate it."""
    if not isinstance(password, str):
        raise ValueError("账号迁移密码必须是文本；不设密码请留空")
    if expected_type is not None:
        expected_type = _profile_type(expected_type)
    payload = _read_payload(Path(input_path).expanduser().resolve(), password)
    profile_type = _profile_type(payload.get("profile_type"))
    if expected_type is not None and expected_type != profile_type:
        raise ValueError("账号登录包类型与当前页面不一致，请在对应的 Claude/Codex 页面导入")
    credentials = _clean_credentials(profile_type, payload.get("credentials"))
    warnings = _login_warnings(profile_type, credentials)
    name = _account_name(payload.get("account_name"), f"{profile_type}-账号")
    from core.switcher import _SWITCH_LOCK

    with _SWITCH_LOCK, profile_manager._STORE_CACHE_LOCK:
        existing = _profiles(profile_type)
        try:
            current_raw = _read_current_raw(profile_type, require_official_mode=False)
        except (OSError, ValueError):
            current_raw = None
        current = _clean_credentials(profile_type, current_raw) if current_raw is not None else None
        if profile_type == "codex":
            incoming = credentials
            candidates = [(current, None)] if current is not None else []
            for profile in existing:
                try:
                    candidates.append((_saved_credentials(profile_type, profile), profile))
                except (OSError, ValueError):
                    continue
            chosen_profile = None
            for candidate, profile in candidates:
                if (_same_login(profile_type, incoming, candidate)
                        and auth_parser.codex_auth_is_newer(candidate, credentials)):
                    credentials, chosen_profile = candidate, profile
            if credentials != incoming:
                warnings = (*_login_warnings(profile_type, credentials),
                            "登录包比本机同一账号的凭据更旧，已保留本机较新的完整登录状态，避免退回失效 token。")
                if chosen_profile is not None:
                    return AccountImportResult(profile_type, chosen_profile.name, False, warnings)
        # An unrelated live account cannot overwrite this exact snapshot when
        # switching later. Re-importing it must be idempotent, including parallel
        # import attempts. Same-identity/different-token bundles still require
        # separate snapshots so preserve-current cannot erase the imported pair.
        matches = (profile_manager._codex_account_matches_auth if profile_type == "codex"
                   else profile_manager._claude_account_matches_credentials)
        for profile in existing:
            try:
                saved = _saved_credentials(profile_type, profile)
            except (OSError, ValueError):
                continue
            # Use preserve-current's own identity matcher here. Its legacy
            # display-name fallback is broader than _same_login; absence of
            # strong IDs must not be mistaken for proof of different accounts.
            if saved == credentials and (current is None or current == credentials or not matches(profile, current_raw)):
                return AccountImportResult(profile_type, profile.name, False, warnings)
        accounts = []
        names = {item.name for item in existing}
        if current is not None and current != credentials:
            if not any(matches(profile, current_raw) for profile in existing):
                # The switcher preserves live tokens before activating a saved
                # account. Put a destination snapshot FIRST, so that preserving
                # same-identity live tokens cannot overwrite the imported one.
                preserved_name = _account_name(
                    profile_manager._account_preferred_name_from_json(current_raw, f"{profile_type}-login"),
                    f"{profile_type}-本机账号",
                )
                preserved = (profile_manager._normalize_codex_official_auth(current_raw)
                             if profile_type == "codex" else current_raw)
                safety_profile = _new_profile(profile_type, preserved_name, preserved, names)
                accounts.append((safety_profile, preserved))
        profile = _new_profile(profile_type, name, credentials, names)
        accounts.append((profile, credentials))
        _save_new_accounts(profile_type, accounts)
        name = profile.name
    return AccountImportResult(profile_type, name, True, warnings)
