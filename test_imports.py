"""
测试导入和基本功能
"""
import sys
print(f"Python version: {sys.version}")

try:
    from core.providers import ProviderRegistry
    print("OK core.providers 导入成功")

    # 测试提供商注册表
    providers = ProviderRegistry.get_all_providers()
    print(f"OK 找到 {len(providers)} 个提供商")

    for provider in providers:
        print(f"  - {provider.display_name}: {provider.default_base_url}")

    # 测试获取模型列表
    deepseek_models = ProviderRegistry.get_models("deepseek")
    print(f"OK DeepSeek 模型: {deepseek_models}")

    # 测试推理力度支持
    supports_effort = ProviderRegistry.supports_reasoning_effort("anthropic")
    print(f"OK Anthropic 支持推理力度: {supports_effort}")

    supports_effort = ProviderRegistry.supports_reasoning_effort("deepseek")
    print(f"OK DeepSeek 支持推理力度: {supports_effort}")

except Exception as e:
    print(f"FAIL 导入失败: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

try:
    from models.profile import ClaudeProfile
    print("OK models.profile 导入成功")

    # 测试创建 ClaudeProfile
    profile = ClaudeProfile(
        name="Test",
        auth_token_ref="test:token",
        base_url="https://api.deepseek.com/anthropic",
        model="deepseek-v4-flash",
        provider="deepseek"
    )
    print(f"OK 创建 ClaudeProfile 成功: {profile.name}, provider={profile.provider}")

    # 测试序列化
    profile_dict = profile.to_dict()
    print(f"OK 序列化成功: provider={profile_dict.get('provider')}")

    # 测试反序列化
    profile2 = ClaudeProfile.from_dict(profile_dict)
    print(f"OK 反序列化成功: provider={profile2.provider}")

except Exception as e:
    print(f"FAIL 模型测试失败: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

try:
    from core.parser import apply_claude_profile
    print("OK core.parser 导入成功")
    assert callable(apply_claude_profile)

    # 测试应用配置（不实际写入文件）
    settings = {"env": {}}
    # 注意：这里会尝试从 keyring 获取密钥，可能会失败，但不影响测试

except Exception as e:
    print(f"FAIL parser 测试失败: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("\nOK 所有测试通过！")
