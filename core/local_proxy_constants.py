LOCAL_PROXY_BUILTIN_SITES = (
    {
        "id": "youtube",
        "label": "YouTube",
        "targets": ("youtube.com", "youtu.be", "ytimg.com", "googlevideo.com"),
    },
    {
        "id": "google",
        "label": "Google 搜索/账号",
        "targets": ("google.com", "gstatic.com", "googleapis.com", "googleusercontent.com"),
    },
    {
        "id": "github",
        "label": "GitHub",
        "targets": ("github.com", "githubusercontent.com", "githubassets.com", "github.io"),
    },
    {
        "id": "huggingface",
        "label": "Hugging Face",
        "targets": ("huggingface.co", "hf.co"),
    },
    {
        "id": "x_twitter",
        "label": "X / Twitter",
        "targets": ("x.com", "twitter.com", "twimg.com", "t.co"),
    },
    {
        "id": "reddit",
        "label": "Reddit",
        "targets": ("reddit.com", "redd.it", "redditstatic.com", "redditmedia.com"),
    },
    {
        "id": "discord",
        "label": "Discord",
        "targets": ("discord.com", "discordapp.com", "discord.gg", "discordcdn.com"),
    },
    {
        "id": "telegram",
        "label": "Telegram",
        "targets": ("telegram.org", "t.me", "tdesktop.com"),
    },
)

LOCAL_PROXY_BUILTIN_SITE_IDS = {str(item["id"]) for item in LOCAL_PROXY_BUILTIN_SITES}
