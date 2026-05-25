"""交互式引导程序。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROVIDERS = [
    {"key": "openrouter-free", "name": "OpenRouter (免费)", "type": "openai",
     "base_url": "https://openrouter.ai/api/v1", "model": "openrouter/free",
     "note": "注册免费无需信用卡 openrouter.ai"},
    {"key": "siliconflow", "name": "硅基流动 (注册送 2000 万 tokens)", "type": "openai",
     "base_url": "https://api.siliconflow.cn/v1", "model": "Qwen/Qwen3-8B",
     "note": "siliconflow.cn"},
    {"key": "zhipu", "name": "智谱 (注册送 2000 万 tokens)", "type": "openai",
     "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash",
     "note": "open.bigmodel.cn"},
    {"key": "moonshot", "name": "Moonshot / Kimi", "type": "openai",
     "base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k",
     "note": "platform.moonshot.cn"},
    {"key": "deepseek", "name": "DeepSeek", "type": "openai",
     "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
     "note": "platform.deepseek.com"},
    {"key": "anthropic", "name": "Anthropic Claude", "type": "anthropic",
     "base_url": "", "model": "claude-sonnet-4-6",
     "note": "console.anthropic.com"},
    {"key": "openai", "name": "OpenAI", "type": "openai",
     "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini",
     "note": "platform.openai.com"},
    {"key": "custom", "name": "自定义 (OpenAI 兼容)", "type": "openai",
     "base_url": "", "model": "",
     "note": "任何 OpenAI 兼容接口"},
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
    print("  [1/3] 选择模型供应商")
    print()
    for i, p in enumerate(PROVIDERS, 1):
        tag = " ★" if "免费" in p["name"] or "送" in p["name"] else ""
        print(f"    {i}. {p['name']}{tag}")
        print(f"       {p['note']}")
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

    # --- Search ---
    print("  [2/3] Exa 搜索（可选，回车跳过）")
    exa_key = _ask("Exa API Key")
    if exa_key:
        env["EXA_API_KEY"] = exa_key
    print()

    # --- Timezone ---
    print("  [3/3] 时区")
    env["SCHEDULER_TZ"] = _ask("时区", "Asia/Shanghai")
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
