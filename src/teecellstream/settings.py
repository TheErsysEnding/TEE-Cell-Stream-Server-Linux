"""User preferences, remembered between runs (the Windows server used the registry; this is a JSON file)."""

import json
import os
import threading

_CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "tee-cell-stream-server")
# TEE_CST_SETTINGS_PATH lets tests run against a throwaway file instead of the user's real preferences
SETTINGS_PATH = os.environ.get("TEE_CST_SETTINGS_PATH") or os.path.join(_CONFIG_DIR, "settings.json")

DEFAULTS = {
    "encoder": None,                       # encoder kind ("nvenc" | "vaapi" | "x264"); None = best available
    "loss_recovery": "intra",              # "intra" (intra refresh, default) | "keyframe"
    "video_kbps": 6000,                    # video bitrate; the PS3's decoder, not the link, is the limit
    "stream_size": "1280x720",             # what the PS3 gets; the larger sizes cost the SPU decoder roughly in proportion
    "entropy_coder": "cavlc",              # "cavlc" (cheap for the PS3 to decode) | "cabac" (the Windows original)
    "switch_display_mode": True,           # switch the desktop to the streaming resolution while streaming
    "swap_mouse_sticks": False,            # mouse mode: right stick moves the pointer
    "custom_commands": None,               # list of 4 {"kind","value","label"}; None = not yet seeded
    "screencast_restore_token": None,      # xdg-desktop-portal token so the share dialog is asked once
    "hide_notice_shown": False,            # "still running in the background" notification shown once
}


class Settings:
    def __init__(self, path: str = SETTINGS_PATH):
        self._path = path
        self._gate = threading.RLock()
        self._values = dict(DEFAULTS)
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                self._values.update(loaded)
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            temporary = self._path + ".tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(self._values, handle, indent=2, ensure_ascii=False)
            os.replace(temporary, self._path)
        except OSError:
            pass

    def get(self, key: str, default=None):
        with self._gate:
            value = self._values.get(key)
            return default if value is None else value

    def set(self, key: str, value) -> None:
        with self._gate:
            if self._values.get(key) == value and key in self._values:
                return
            self._values[key] = value
            self._save()


settings = Settings()
