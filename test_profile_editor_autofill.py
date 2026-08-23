from core.api_config_parser import parse_api_config_text
from core.providers import ProviderRegistry
from ui.dialogs.profile_editor import CLAUDE_AUTH_SCHEME_LABELS, ProfileEditorDialog


class _Row:
    def __init__(self):
        self.visible = True

    def pack(self, **_kwargs):
        self.visible = True

    def pack_forget(self):
        self.visible = False


class _Field:
    def __init__(self, value=""):
        self.value = value
        self.state = "normal"
        self.values = []
        self.master = _Row()

    def get(self):
        return self.value

    def set(self, value):
        self.value = str(value)

    def delete(self, *_args):
        if self.state != "disabled":
            self.value = ""

    def insert(self, _index, value):
        if self.state != "disabled":
            self.value = str(value)

    def configure(self, **kwargs):
        if "state" in kwargs:
            self.state = kwargs["state"]
        if "values" in kwargs:
            self.values = list(kwargs["values"])


class _Button:
    def __init__(self):
        self.state = "normal"

    def configure(self, **kwargs):
        self.state = kwargs.get("state", self.state)


class _MaskedField:
    def __init__(self):
        self.value = ""
        self.entry = _Field()
        self.toggle_btn = _Button()
        self.master = _Row()

    def get(self):
        return self.value

    def set(self, value):
        # Match a disabled Tk entry closely enough to catch ordering bugs.
        if self.entry.state != "disabled":
            self.value = str(value)


class _Switch:
    def __init__(self, selected=False):
        self.selected = bool(selected)
        self.state = "normal"
        self.master = _Row()

    def get(self):
        return 1 if self.selected else 0

    def select(self):
        self.selected = True

    def deselect(self):
        self.selected = False

    def configure(self, **kwargs):
        self.state = kwargs.get("state", self.state)


def _base_dialog(profile_type: str):
    dialog = object.__new__(ProfileEditorDialog)
    dialog._profile_type = profile_type
    dialog._profile = None
    dialog._provider_note_label = None
    dialog._last_model_for_effort_options = None
    dialog._refresh_reasoning_effort_options = lambda *_args, **_kwargs: None
    dialog._show_error = lambda message: (_ for _ in ()).throw(AssertionError(message))
    dialog.statuses = []
    dialog._show_status = lambda message, color="muted": dialog.statuses.append((message, color))
    return dialog


def test_codex_autofill_disables_stale_openai_login_mode_before_writing_key():
    dialog = _base_dialog("codex")
    old_auth_switch = _Switch(selected=True)
    api_key = _MaskedField()
    env_key = _Field("OLD_API_KEY")
    dialog._fields = {
        "codex_provider": (_Field("Custom"), "combo"),
        "name": (_Field(), "entry"),
        "custom_base_url": (_Field(), "entry"),
        "api_key": (api_key, "masked"),
        "custom_name": (_Field(), "entry"),
        "custom_env_key": (env_key, "entry"),
        "custom_requires_openai_auth": (old_auth_switch, "switch"),
        "model": (_Field(), "combo"),
        "model_reasoning_effort": (_Field("high"), "combo"),
        "approval_policy": (_Field("never"), "combo"),
        "sandbox_mode": (_Field("danger-full-access"), "combo"),
    }
    parsed = parse_api_config_text(
        '''{
          "provider": {
            "openai": {
              "options": {
                "baseURL": "https://relay.example.test/v1/responses",
                "apiKey": "sk-editor-codex",
                "model": "gpt-relay"
              }
            }
          }
        }''',
        "codex",
    )

    ProfileEditorDialog._apply_parsed_api_config(dialog, parsed)

    assert old_auth_switch.get() == 0
    assert api_key.entry.state == "normal"
    assert api_key.get() == "sk-editor-codex"
    assert env_key.state == "normal"
    assert env_key.get() == "OPENAI_API_KEY"
    assert dialog._fields["custom_base_url"][0].get() == "https://relay.example.test/v1"
    assert dialog._fields["model"][0].get() == "gpt-relay"
    assert dialog._fields["codex_provider"][0].get() == ProviderRegistry.get_provider("custom").display_name
    assert dialog.statuses[-1][1] == "success"

    saved = []
    dialog._on_save = lambda data, profile: saved.append((data, profile))
    dialog.destroy = lambda: None
    ProfileEditorDialog._save(dialog)
    payload = saved[0][0]
    assert payload["custom_requires_openai_auth"] is False
    assert payload["api_key"] == "sk-editor-codex"
    assert payload["custom_env_key"] == "OPENAI_API_KEY"
    assert payload["custom_base_url"] == "https://relay.example.test/v1"


def test_claude_autofill_restores_sniffed_values_after_provider_defaults_reset():
    dialog = _base_dialog("claude")
    token = _MaskedField()
    dialog._fields = {
        "provider": (_Field("Custom"), "combo"),
        "name": (_Field(), "entry"),
        "base_url": (_Field(), "entry"),
        "auth_token": (token, "masked"),
        "auth_scheme": (_Field(), "combo"),
        "model": (_Field(), "combo"),
        "effort_level": (_Field("high"), "combo"),
        "permissions_mode": (_Field("default"), "combo"),
        "skip_dangerous_prompt": (_Switch(selected=False), "switch"),
        "custom_provider_name": (_Field(), "entry"),
    }
    parsed = parse_api_config_text(
        "PROVIDER_NAME=Fable Relay\n"
        "ANTHROPIC_BASE_URL=https://relay.example.test/v1/messages\n"
        "ANTHROPIC_API_KEY=sk-editor-claude\n"
        "ANTHROPIC_MODEL=claude-fable-test",
        "claude",
    )

    ProfileEditorDialog._apply_parsed_api_config(dialog, parsed)

    assert dialog._fields["provider"][0].get() == ProviderRegistry.get_provider("custom").display_name
    assert dialog._fields["base_url"][0].get() == "https://relay.example.test"
    assert token.get() == "sk-editor-claude"
    assert dialog._fields["auth_scheme"][0].get() == CLAUDE_AUTH_SCHEME_LABELS["api_key"]
    assert dialog._fields["model"][0].get() == "claude-fable-test"
    assert dialog._fields["custom_provider_name"][0].get() == "Fable Relay"
    assert dialog.statuses[-1][1] == "success"

    saved = []
    dialog._on_save = lambda data, profile: saved.append((data, profile))
    dialog.destroy = lambda: None
    ProfileEditorDialog._save(dialog)
    payload = saved[0][0]
    assert payload["auth_token"] == "sk-editor-claude"
    assert payload["auth_scheme"] == "api_key"
    assert payload["base_url"] == "https://relay.example.test"
