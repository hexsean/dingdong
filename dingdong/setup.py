"""交互式引导程序。

``dingdong setup`` 调用此模块，用 input() 引导用户填写配置并扫码登录。
不依赖任何 TUI 框架。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _ask(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    val = input(f"  {prompt}{hint}: ").strip()
    return val or default


def _ask_choice(prompt: str, options: list[str]) -> str:
    opts = " / ".join(f"{i+1}={o}" for i, o in enumerate(options))
    while True:
        raw = input(f"  {prompt} ({opts}): ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        if raw in options:
            return raw
        print(f"  请输入 1-{len(options)}")


def _write_env(path: Path, values: dict[str, str]) -> None:
    lines: list[str] = []
    for k, v in values.items():
        lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n  配置已保存到 {path}")


def run_setup(data_dir: str = "./data", env_path: str = ".env") -> None:
    env_file = Path(env_path).resolve()
    data = Path(data_dir).resolve()
    data.mkdir(parents=True, exist_ok=True)

    print()
    print("  ╔══════════════════════════════════╗")
    print("  ║  叮咚 - 微信定时任务助手 · 配置  ║")
    print("  ╚══════════════════════════════════╝")
    print()

    env: dict[str, str] = {}

    # --- LLM ---
    print("  [1/4] LLM 配置")
    provider = _ask_choice("模型供应商", ["anthropic", "openai"])
    env["LLM_PROVIDER"] = provider

    if provider == "anthropic":
        env["ANTHROPIC_API_KEY"] = _ask("API Key")
        env["ANTHROPIC_MODEL"] = _ask("Model", "claude-sonnet-4-6")
    else:
        env["OPENAI_API_KEY"] = _ask("API Key")
        env["OPENAI_BASE_URL"] = _ask("Base URL", "https://api.openai.com/v1")
        env["OPENAI_MODEL"] = _ask("Model", "gpt-4o-mini")
    print()

    # --- Search ---
    print("  [2/4] 搜索（可选）")
    exa_key = _ask("Exa API Key（回车跳过）")
    if exa_key:
        env["EXA_API_KEY"] = exa_key
    print()

    # --- Timezone ---
    print("  [3/4] 时区")
    env["SCHEDULER_TZ"] = _ask("时区", "Asia/Shanghai")
    env["DATA_DIR"] = str(data)
    print()

    # --- Write .env ---
    _write_env(env_file, env)

    # --- WeChat Login ---
    print()
    print("  [4/4] 微信登录")

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
        print("  你可以稍后运行 `dingdong setup` 重试。")
        sys.exit(1)

    print()
    print("  ✓ 配置完成！")
    print()
    print("  启动方式：")
    print("    python -m dingdong start")
    print("    docker compose up -d")
    print()
