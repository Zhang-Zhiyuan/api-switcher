LOCAL_PROXY_AI_SERVICES = (
    {
        "id": "openai",
        "label": "OpenAI / Codex",
        "targets": (
            "chatgpt.com",
            "openai.com",
            "oaistatic.com",
            "oaiusercontent.com",
            "auth0.openai.com",
        ),
        "health_check_url": "https://api.openai.com/v1/models",
        "health_check_expected_status": "200/401",
    },
    {
        "id": "claude",
        "label": "Claude Code",
        "targets": ("anthropic.com", "claude.ai"),
        "health_check_url": "https://api.anthropic.com/v1/models",
        # Unauthenticated probes may receive an authentication/version challenge,
        # but a blocked/limited/missing endpoint must not keep a fallback alive.
        "health_check_expected_status": "200/400/401",
    },
    {
        "id": "google_ai",
        "label": "Google AI / Gemini",
        "targets": (
            "gemini.google.com",
            "generativelanguage.googleapis.com",
            "oauth2.googleapis.com",
            "www.googleapis.com",
            "aiplatform.googleapis.com",
            "cloudcode-pa.googleapis.com",
            "aistudio.google.com",
            "ai.google.dev",
            "makersuite.google.com",
        ),
        "health_check_url": "https://generativelanguage.googleapis.com/v1beta/models",
        # Google's credential challenge can legitimately be HTTP 403. The core
        # cannot inspect its JSON body: this remains transport evidence only.
        "health_check_expected_status": "200/400/401/403",
    },
)


LOCAL_PROXY_BUILTIN_SITES = (
    {
        "id": "youtube",
        "label": "YouTube",
        "health_check_url": "https://www.youtube.com/generate_204",
        "health_check_expected_status": "204",
        "targets": (
            "youtube.com",
            "youtube-nocookie.com",
            "youtu.be",
            "ytimg.com",
            "googlevideo.com",
            "ggpht.com",
            "youtubei.googleapis.com",
            "youtube.googleapis.com",
        ),
    },
    {
        "id": "google",
        "label": "Google 搜索/账号",
        "health_check_url": "https://www.google.com/generate_204",
        "health_check_expected_status": "204",
        "targets": ("google.com", "gstatic.com", "googleapis.com", "googleusercontent.com"),
    },
    {
        "id": "github",
        "label": "GitHub",
        "health_check_url": "https://github.com/robots.txt",
        "health_check_expected_status": "200",
        "targets": ("github.com", "githubusercontent.com", "githubassets.com", "github.io"),
    },
    {
        "id": "huggingface",
        "label": "Hugging Face",
        "health_check_url": "https://huggingface.co/robots.txt",
        "health_check_expected_status": "200",
        "targets": ("huggingface.co", "hf.co"),
    },
    {
        "id": "x_twitter",
        "label": "X / Twitter",
        "health_check_url": "https://x.com/robots.txt",
        "health_check_expected_status": "200",
        "targets": ("x.com", "twitter.com", "twimg.com", "t.co"),
    },
    {
        "id": "reddit",
        "label": "Reddit",
        "health_check_url": "https://www.reddit.com/robots.txt",
        "health_check_expected_status": "200",
        "targets": ("reddit.com", "redd.it", "redditstatic.com", "redditmedia.com"),
    },
    {
        "id": "discord",
        "label": "Discord",
        "health_check_url": "https://discord.com/api/v10/gateway",
        "health_check_expected_status": "200",
        "targets": ("discord.com", "discordapp.com", "discord.gg", "discordcdn.com"),
    },
    {
        "id": "telegram",
        "label": "Telegram",
        "health_check_url": "https://telegram.org/",
        "health_check_expected_status": "200",
        "targets": ("telegram.org", "t.me", "tdesktop.com"),
    },
)

LOCAL_PROXY_BUILTIN_SITE_IDS = {str(item["id"]) for item in LOCAL_PROXY_BUILTIN_SITES}
LOCAL_PROXY_AI_SERVICE_IDS = {str(item["id"]) for item in LOCAL_PROXY_AI_SERVICES}
LOCAL_PROXY_CUSTOM_ROUTE_ID = "custom"
LOCAL_PROXY_SERVICE_ROUTE_IDS = (
    LOCAL_PROXY_AI_SERVICE_IDS
    | LOCAL_PROXY_BUILTIN_SITE_IDS
    | {LOCAL_PROXY_CUSTOM_ROUTE_ID}
)
