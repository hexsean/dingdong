"""交互式引导程序。

支持增量配置：已有配置项显示为默认值，回车即可保留。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROVIDERS = [
    # --- Anthropic 原生 ---
    {"key": "anthropic",  "name": "Anthropic (Claude)", "type": "anthropic",
     "base_url": "", "model": "claude-sonnet-4-6"},
    # --- OpenAI 原生 ---
    {"key": "openai-com", "name": "OpenAI",             "type": "openai",
     "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    # --- OpenAI 兼容渠道 ---
    {"key": "deepseek",   "name": "DeepSeek",           "type": "openai",
     "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    {"key": "openrouter", "name": "OpenRouter",         "type": "openai",
     "base_url": "https://openrouter.ai/api/v1", "model": "openrouter/auto"},
    {"key": "siliconflow","name": "硅基流动",            "type": "openai",
     "base_url": "https://api.siliconflow.cn/v1", "model": "Qwen/Qwen3-8B"},
    {"key": "zhipu",      "name": "智谱 GLM",           "type": "openai",
     "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
    {"key": "moonshot",   "name": "Moonshot",           "type": "openai",
     "base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    {"key": "custom",     "name": "自定义（OpenAI 兼容）","type": "openai",
     "base_url": "", "model": ""},
]

_VISION_PROVIDERS = [
    {"name": "OpenAI (GPT-4o-mini)", "type": "openai",
     "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    {"name": "Claude (Sonnet)", "type": "anthropic",
     "base_url": "", "model": "claude-sonnet-4-6"},
    {"name": "自定义", "type": "openai",
     "base_url": "", "model": ""},
]

_TZ_OPTIONS = [
    ("Asia/Shanghai",      "北京/上海"),
    ("Asia/Tokyo",         "东京"),
    ("Asia/Singapore",     "新加坡"),
    ("America/New_York",   "纽约"),
    ("America/Los_Angeles","洛杉矶"),
    ("Europe/London",      "伦敦"),
]


def _ask(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    try:
        val = input(f"  {prompt}{hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  已取消。")
        sys.exit(1)
    return val or default


def _mask(val: str) -> str:
    if len(val) <= 8:
        return "***"
    return val[:4] + "***" + val[-4:]


def _load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        values[k.strip()] = v.strip()
    return values


def _write_env(path: Path, values: dict[str, str]) -> None:
    lines = [f"{k}={v}" for k, v in values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n  配置已保存到 {path}")


def _detect_provider(existing: dict[str, str]) -> dict | None:
    """从已有配置推断当前渠道。"""
    prov = existing.get("LLM_PROVIDER", "")
    if prov == "anthropic":
        for p in PROVIDERS:
            if p["type"] == "anthropic":
                return p
    elif prov == "openai":
        base_url = existing.get("OPENAI_BASE_URL", "")
        for p in PROVIDERS:
            if p["type"] == "openai" and p["base_url"] and p["base_url"] == base_url:
                return p
        if base_url:
            return PROVIDERS[-1]  # custom
    return None


def run_setup(data_dir: str = "./data") -> None:
    data = Path(data_dir).resolve()
    data.mkdir(parents=True, exist_ok=True)
    env_file = data / ".env"

    existing = _load_env(env_file)
    is_update = bool(existing)

    print()
    print("  叮咚 · 配置向导")
    print("  ================")
    if is_update:
        current_provider = _detect_provider(existing)
        current_name = current_provider["name"] if current_provider else "未知"
        print(f"  检测到已有配置（{current_name}），回车保留当前值。")
    print()

    env: dict[str, str] = dict(existing)

    # --- LLM ---
    print("  [1/4] 选择模型渠道\n")
    current = _detect_provider(existing)
    _GROUPS = {0: "原生", 2: "OpenAI 兼容"}
    for i, p in enumerate(PROVIDERS):
        if i in _GROUPS:
            print(f"    ── {_GROUPS[i]} ──")
        mark = " ←当前" if current and p["key"] == current["key"] else ""
        print(f"    {i + 1}. {p['name']}{mark}")
    print()

    if is_update:
        prompt = "输入编号（回车保留当前）"
    else:
        prompt = "输入编号"

    while True:
        try:
            raw = input(f"  {prompt}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  已取消。")
            sys.exit(1)
        if not raw and is_update:
            provider = _detect_provider(existing) or PROVIDERS[-1]
            break
        if raw.isdigit() and 1 <= int(raw) <= len(PROVIDERS):
            provider = PROVIDERS[int(raw) - 1]
            break
        print(f"  请输入 1-{len(PROVIDERS)}")

    if provider["type"] == "anthropic":
        env["LLM_PROVIDER"] = "anthropic"
        cur_key = existing.get("ANTHROPIC_API_KEY", "")
        if cur_key:
            print(f"  当前 API Key: {_mask(cur_key)}")
            env["ANTHROPIC_API_KEY"] = _ask("API Key（回车保留）", cur_key)
        else:
            env["ANTHROPIC_API_KEY"] = _ask("API Key")
        env["ANTHROPIC_MODEL"] = _ask("Model", existing.get("ANTHROPIC_MODEL", provider["model"]))
    else:
        env["LLM_PROVIDER"] = "openai"
        cur_key = existing.get("OPENAI_API_KEY", "")
        if cur_key:
            print(f"  当前 API Key: {_mask(cur_key)}")
            env["OPENAI_API_KEY"] = _ask("API Key（回车保留）", cur_key)
        else:
            env["OPENAI_API_KEY"] = _ask("API Key")
        if provider["key"] == "custom":
            env["OPENAI_BASE_URL"] = _ask("Base URL", existing.get("OPENAI_BASE_URL", ""))
            env["OPENAI_MODEL"] = _ask("Model", existing.get("OPENAI_MODEL", ""))
        else:
            env["OPENAI_BASE_URL"] = provider["base_url"]
            env["OPENAI_MODEL"] = _ask("Model", existing.get("OPENAI_MODEL", provider["model"]))
    print()

    # --- Vision ---
    print("  [2/4] 图片理解\n")
    print("    图片理解能力取决于所选模型。启动时自动检测主模型是否支持。")
    print("    也可配置独立视觉模型，仅在收到图片时调用。\n")
    has_vision_config = bool(existing.get("VISION_PROVIDER"))

    if has_vision_config:
        cur_vmodel = existing.get("VISION_MODEL", "")
        print(f"    当前视觉模型：{cur_vmodel}")
        action = _ask("操作：回车保留 / r 移除 / c 修改", "")
        if action.lower() == "r":
            for k in ("VISION_PROVIDER", "VISION_API_KEY", "VISION_BASE_URL", "VISION_MODEL"):
                env.pop(k, None)
            print("  已移除。")
        elif action.lower() == "c":
            _setup_vision(env, existing)
        else:
            print("  保留现有配置。")
    else:
        setup_v = _ask("配置独立视觉模型？(y/N)", "N")
        if setup_v.lower() in ("y", "yes"):
            _setup_vision(env, existing)
    print()

    # --- Search ---
    print("  [3/4] Exa 搜索（可选，回车跳过）")
    cur_exa = existing.get("EXA_API_KEY", "")
    if cur_exa:
        print(f"  当前 Exa Key: {_mask(cur_exa)}")
    exa_key = _ask("Exa API Key（回车保留）" if cur_exa else "Exa API Key", cur_exa)
    if exa_key:
        env["EXA_API_KEY"] = exa_key
    print()

    # --- Timezone ---
    print("  [4/4] 时区\n")
    cur_tz = existing.get("SCHEDULER_TZ", "")
    if cur_tz:
        print(f"    当前：{cur_tz}")
        change_tz = _ask("修改时区？(y/N)", "N")
        if change_tz.lower() not in ("y", "yes"):
            print("  保留现有时区。")
        else:
            _setup_timezone(env)
    else:
        _setup_timezone(env)

    env["DATA_DIR"] = str(data)
    _write_env(env_file, env)

    # --- WeChat Login ---
    print()
    session_path = data / "session.json"
    if session_path.exists():
        relogin = _ask("已有登录态，重新登录？(y/N)", "N")
        if relogin.lower() not in ("y", "yes"):
            print("\n  ✓ 配置已更新！重启生效：docker compose restart")
            print()
            return

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
        print("  配置已保存，稍后重新运行 setup 即可登录。")
        sys.exit(1)

    print()
    print("  ✓ 完成！启动：docker compose up -d")
    print()


def _setup_vision(env: dict[str, str], existing: dict[str, str]) -> None:
    print("    可选视觉模型：")
    for i, vp in enumerate(_VISION_PROVIDERS, 1):
        print(f"      {i}. {vp['name']}")
    print()

    v_raw = _ask("输入编号（回车跳过）")
    if v_raw.isdigit() and 1 <= int(v_raw) <= len(_VISION_PROVIDERS):
        vp = _VISION_PROVIDERS[int(v_raw) - 1]
        env["VISION_PROVIDER"] = vp["type"]
        cur_vkey = existing.get("VISION_API_KEY", "")
        if cur_vkey:
            print(f"  当前视觉模型 Key: {_mask(cur_vkey)}")
            env["VISION_API_KEY"] = _ask("视觉模型 API Key（回车保留）", cur_vkey)
        else:
            env["VISION_API_KEY"] = _ask("视觉模型 API Key")
        if vp["name"] == "自定义":
            env["VISION_BASE_URL"] = _ask("Base URL", existing.get("VISION_BASE_URL", ""))
            env["VISION_MODEL"] = _ask("Model", existing.get("VISION_MODEL", ""))
        else:
            if vp["base_url"]:
                env["VISION_BASE_URL"] = vp["base_url"]
            env["VISION_MODEL"] = _ask("Model", existing.get("VISION_MODEL", vp["model"]))


def _setup_timezone(env: dict[str, str]) -> None:
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
