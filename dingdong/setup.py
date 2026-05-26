"""交互式引导程序。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROVIDERS = [
    {"key": "openrouter-free", "name": "OpenRouter",   "tag": "免费",         "type": "openai",
     "base_url": "https://openrouter.ai/api/v1", "model": "openrouter/free", "vision": False},
    {"key": "siliconflow",     "name": "硅基流动",      "tag": "免费额度",     "type": "openai",
     "base_url": "https://api.siliconflow.cn/v1", "model": "Qwen/Qwen3-8B", "vision": False},
    {"key": "zhipu",           "name": "智谱 GLM",      "tag": "免费额度",     "type": "openai",
     "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash", "vision": False},
    {"key": "deepseek",        "name": "DeepSeek",      "tag": "",            "type": "openai",
     "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "vision": False},
    {"key": "moonshot",        "name": "Moonshot",      "tag": "",            "type": "openai",
     "base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k", "vision": False},
    {"key": "anthropic",       "name": "Claude",        "tag": "支持图片",    "type": "anthropic",
     "base_url": "", "model": "claude-sonnet-4-6", "vision": True},
    {"key": "openai",          "name": "OpenAI",        "tag": "支持图片",    "type": "openai",
     "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "vision": True},
    {"key": "custom",          "name": "自定义",         "tag": "",            "type": "openai",
     "base_url": "", "model": "", "vision": False},
]


def _ask(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    try:
        val = input(f"  {prompt}{hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  已取消。")
        sys.exit(1)
    return val or default


def _write_env(path: Path, values: dict[str, str]) -> None:
    lines = [f"{k}={v}" for k, v in values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n  配置已保存到 {path}")


def run_setup(data_dir: str = "./data") -> None:
    data = Path(data_dir).resolve()
    data.mkdir(parents=True, exist_ok=True)
    env_file = data / ".env"

    print()
    print("  叮咚 · 配置向导")
    print("  ================")
    print()

    env: dict[str, str] = {}

    # --- LLM ---
    print("  [1/4] 选择模型供应商\n")
    for i, p in enumerate(PROVIDERS, 1):
        tag = f"  ({p['tag']})" if p["tag"] else ""
        print(f"    {i}. {p['name']}{tag}")
    print()

    while True:
        try:
            raw = input("  输入编号: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  已取消。")
            sys.exit(1)
        if raw.isdigit() and 1 <= int(raw) <= len(PROVIDERS):
            provider = PROVIDERS[int(raw) - 1]
            break
        print(f"  请输入 1-{len(PROVIDERS)}")

    if provider["type"] == "anthropic":
        env["LLM_PROVIDER"] = "anthropic"
        env["ANTHROPIC_API_KEY"] = _ask("API Key")
        env["ANTHROPIC_MODEL"] = _ask("Model", provider["model"])
    else:
        env["LLM_PROVIDER"] = "openai"
        env["OPENAI_API_KEY"] = _ask("API Key")
        if provider["key"] == "custom":
            env["OPENAI_BASE_URL"] = _ask("Base URL")
            env["OPENAI_MODEL"] = _ask("Model")
        else:
            env["OPENAI_BASE_URL"] = provider["base_url"]
            env["OPENAI_MODEL"] = _ask("Model", provider["model"])
    print()

    # --- Vision ---
    print("  [2/4] 图片理解\n")
    if provider.get("vision"):
        print(f"    {provider['name']} 已支持图片理解，无需额外配置。\n")
    else:
        print(f"    {provider['name']} 不支持图片。可选配一个视觉模型，发图时自动调用。")
        print("    支持的视觉模型：Claude Sonnet、GPT-4o、GPT-4o-mini、Qwen-VL 等")
        print("    不需要可直接回车跳过。\n")

        _VISION_PROVIDERS = [
            {"name": "OpenAI (GPT-4o-mini)", "type": "openai",
             "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
            {"name": "Claude (Sonnet)", "type": "anthropic",
             "base_url": "", "model": "claude-sonnet-4-6"},
            {"name": "自定义", "type": "openai",
             "base_url": "", "model": ""},
        ]
        print("    可选视觉模型：")
        for i, vp in enumerate(_VISION_PROVIDERS, 1):
            print(f"      {i}. {vp['name']}")
        print()

        v_raw = _ask("输入编号（回车跳过）")
        if v_raw.isdigit() and 1 <= int(v_raw) <= len(_VISION_PROVIDERS):
            vp = _VISION_PROVIDERS[int(v_raw) - 1]
            env["VISION_PROVIDER"] = vp["type"]
            env["VISION_API_KEY"] = _ask("视觉模型 API Key")
            if vp["name"] == "自定义":
                env["VISION_BASE_URL"] = _ask("Base URL")
                env["VISION_MODEL"] = _ask("Model")
            else:
                if vp["base_url"]:
                    env["VISION_BASE_URL"] = vp["base_url"]
                env["VISION_MODEL"] = _ask("Model", vp["model"])
    print()

    # --- Search ---
    print("  [3/4] Exa 搜索（可选，回车跳过）")
    exa_key = _ask("Exa API Key")
    if exa_key:
        env["EXA_API_KEY"] = exa_key
    print()

    # --- Timezone ---
    print("  [4/4] 时区\n")
    _TZ_OPTIONS = [
        ("Asia/Shanghai",     "北京/上海"),
        ("Asia/Tokyo",        "东京"),
        ("Asia/Singapore",    "新加坡"),
        ("America/New_York",  "纽约"),
        ("America/Los_Angeles", "洛杉矶"),
        ("Europe/London",     "伦敦"),
    ]
    for i, (tz_id, label) in enumerate(_TZ_OPTIONS, 1):
        print(f"    {i}. {label} ({tz_id})")
    print(f"    0. 自定义")
    print()
    while True:
        raw_tz = _ask("输入编号", "1")
        if raw_tz.isdigit():
            idx = int(raw_tz)
            if idx == 0:
                custom_tz = _ask("IANA 时区（如 Asia/Hong_Kong）")
                try:
                    from zoneinfo import ZoneInfo
                    ZoneInfo(custom_tz)
                    env["SCHEDULER_TZ"] = custom_tz
                    break
                except (KeyError, Exception):
                    print(f"  无效时区：{custom_tz}，请重新输入")
                    continue
            if 1 <= idx <= len(_TZ_OPTIONS):
                env["SCHEDULER_TZ"] = _TZ_OPTIONS[idx - 1][0]
                break
        print(f"  请输入 0-{len(_TZ_OPTIONS)}")
    env["DATA_DIR"] = str(data)

    _write_env(env_file, env)

    # --- WeChat Login ---
    print()
    print("  微信登录")
    os.environ.update(env)

    from .config import load_config
    from .ilink import ILinkClient
    from .login import ensure_login

    cfg = load_config()

    def factory(token: str | None) -> ILinkClient:
        return ILinkClient(bot_token=token, long_poll_timeout_ms=cfg.long_poll_timeout_ms)

    try:
        ensure_login(
            session_path=cfg.session_path,
            qrcode_png_path=cfg.qrcode_png_path,
            client_factory=factory,
        )
    except Exception as exc:
        print(f"\n  登录失败：{exc}")
        print("  稍后重新运行 setup 即可。")
        sys.exit(1)

    print()
    print("  ✓ 完成！启动：docker compose up -d")
    print()
