"""交互式引导程序。

支持增量配置：已有配置项显示为默认值，回车即可保留。
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

PROVIDERS = [
    # --- Anthropic 原生 ---
    {"key": "anthropic",  "name": "Anthropic",           "type": "anthropic",
     "base_url": "", "model": "claude-sonnet-4-6"},
    # --- OpenAI 原生 ---
    {"key": "openai-com", "name": "OpenAI",             "type": "openai",
     "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    # --- OpenAI 兼容渠道 ---
    {"key": "mimo-plan",  "name": "MiMo Token Plan（官方订阅）", "type": "openai",
     "base_url": "https://token-plan-cn.xiaomimimo.com/v1", "model": "mimo-v2.5-pro",
     "ask_base_url": True},
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


def _is_enabled(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


def _ask_secret(prompt: str, current: str = "") -> str:
    """敏感字段输入，已有值时仅显示脱敏版本。"""
    if current:
        print(f"  当前: {_mask(current)}")
        val = _ask(f"{prompt}（回车保留）")
        return val if val else current
    return _ask(prompt)


def _make_test_image_b64() -> str:
    import io, base64
    from PIL import Image
    img = Image.new("RGB", (16, 16), color="red")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode()


def _test_model(provider_type: str, api_key: str, model: str, base_url: str = "",
                *, test_vision: bool = False) -> tuple[bool, str]:
    """发送测试消息验证模型配置。test_vision=True 时额外发图片验证视觉能力。"""
    label = f"{model} (视觉)" if test_vision else model
    print(f"\n  测试 {label} ...", end=" ", flush=True)

    if test_vision:
        img_b64 = _make_test_image_b64()
        if provider_type == "anthropic":
            image_part = {"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg", "data": img_b64}}
        else:
            image_part = {"type": "image_url", "image_url": {
                "url": f"data:image/jpeg;base64,{img_b64}"}}
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "这张图片是什么颜色？只回复颜色名称。"},
            image_part,
        ]}]
    else:
        messages = [{"role": "user", "content": "Reply OK"}]

    try:
        if provider_type == "anthropic":
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            client.messages.create(model=model, max_tokens=16, messages=messages)
        else:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url=base_url or "https://api.openai.com/v1")
            client.chat.completions.create(model=model, max_tokens=16, messages=messages)
        print("✓ 通过")
        return True, ""
    except Exception as exc:
        print("✗ 失败")
        reason = _classify_error(exc)
        print(f"  原因: {reason}")
        return False, reason


def _classify_error(exc: Exception) -> str:
    name = type(exc).__name__
    msg = str(exc)[:200]
    code = getattr(exc, "status_code", None)
    if code == 401 or "auth" in msg.lower() or "unauthorized" in msg.lower():
        return "API Key 无效或已过期"
    if code == 403 or "permission" in msg.lower():
        return "无权限访问该模型"
    if code == 404 or "not found" in msg.lower() or "does not exist" in msg.lower():
        return f"模型不存在，请检查模型名称"
    if "image" in msg.lower() and ("format" in msg.lower() or "decode" in msg.lower()):
        return "图片格式不支持，该模型可能不具备视觉能力"
    if code == 429 or "rate" in msg.lower():
        return "触发频率限制，请稍后重试"
    if "billing" in msg.lower() or "quota" in msg.lower() or "insufficient" in msg.lower():
        return "账户余额不足或配额用尽"
    if "timeout" in msg.lower() or "timed out" in msg.lower():
        return "请求超时，请检查网络"
    if "connection" in msg.lower() or "resolve" in msg.lower():
        return f"无法连接到服务器，请检查 Base URL"
    return f"{name}: {msg}"


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


def _write_updater_env(path: Path, values: dict[str, str]) -> None:
    token = values.get("WATCHTOWER_HTTP_API_TOKEN", "").strip()
    if not token:
        return
    path.write_text(f"WATCHTOWER_HTTP_API_TOKEN={token}\n", encoding="utf-8")
    print(f"  Updater 配置已保存到 {path}")


def _request_restart(data: Path) -> None:
    from .restart import request_service_restart
    request_service_restart(data)


def _matches_provider_base_url(provider: dict, base_url: str) -> bool:
    normalized = base_url.rstrip("/")
    provider_base_url = provider.get("base_url", "").rstrip("/")
    if provider_base_url and normalized == provider_base_url:
        return True
    if provider.get("key") == "mimo-plan":
        return "token-plan-cn.xiaomimimo.com" in normalized
    return False


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
            if p["type"] == "openai" and _matches_provider_base_url(p, base_url):
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
    print("  [1/5] 选择模型渠道\n")
    current = _detect_provider(existing)
    for i, p in enumerate(PROVIDERS):
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

    while True:
        if provider["type"] == "anthropic":
            env["LLM_PROVIDER"] = "anthropic"
            env["ANTHROPIC_API_KEY"] = _ask_secret("API Key", existing.get("ANTHROPIC_API_KEY", ""))
            env["ANTHROPIC_MODEL"] = _ask("Model", existing.get("ANTHROPIC_MODEL", provider["model"]))
            ok, _ = _test_model("anthropic", env["ANTHROPIC_API_KEY"], env["ANTHROPIC_MODEL"])
        else:
            env["LLM_PROVIDER"] = "openai"
            env["OPENAI_API_KEY"] = _ask_secret("API Key", existing.get("OPENAI_API_KEY", ""))
            if provider["key"] == "custom":
                env["OPENAI_BASE_URL"] = _ask("Base URL", existing.get("OPENAI_BASE_URL", ""))
                env["OPENAI_MODEL"] = _ask("Model", existing.get("OPENAI_MODEL", ""))
            elif provider.get("ask_base_url"):
                base_default = (
                    existing.get("OPENAI_BASE_URL", provider["base_url"])
                    if current and current["key"] == provider["key"]
                    else provider["base_url"]
                )
                model_default = (
                    existing.get("OPENAI_MODEL", provider["model"])
                    if current and current["key"] == provider["key"]
                    else provider["model"]
                )
                env["OPENAI_BASE_URL"] = _ask("Base URL", base_default)
                env["OPENAI_MODEL"] = _ask("Model", model_default)
            else:
                env["OPENAI_BASE_URL"] = provider["base_url"]
                env["OPENAI_MODEL"] = _ask("Model", existing.get("OPENAI_MODEL", provider["model"]))
            ok, _ = _test_model("openai", env["OPENAI_API_KEY"], env["OPENAI_MODEL"], env.get("OPENAI_BASE_URL", ""))
        if ok:
            break
        retry = _ask("重新输入？(Y/n)", "Y")
        if retry.lower() in ("n", "no"):
            break
    print()

    # --- Vision ---
    print("  [2/5] 图片理解\n")
    print("    图片理解能力取决于所选模型。启动时自动检测主模型是否支持。")
    print("    也可配置独立视觉模型，仅在收到图片时调用。\n")
    has_vision_config = bool(existing.get("VISION_PROVIDER"))

    if has_vision_config:
        cur_vmodel = existing.get("VISION_MODEL", "")
        print(f"    当前视觉模型：{cur_vmodel}")

        ok, _ = _test_model(
            existing.get("VISION_PROVIDER", "openai"),
            existing.get("VISION_API_KEY", ""),
            cur_vmodel,
            existing.get("VISION_BASE_URL", ""),
            test_vision=True,
        )
        if ok:
            action = _ask("操作：回车保留 / r 移除 / c 修改", "")
        else:
            action = _ask("测试未通过，建议修改。操作：c 修改 / r 移除 / 回车强制保留", "c")

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
    print("  [3/5] Exa 搜索（可选，回车跳过）")
    exa_key = _ask_secret("Exa API Key", existing.get("EXA_API_KEY", ""))
    if exa_key:
        env["EXA_API_KEY"] = exa_key
    print()

    # --- Timezone ---
    print("  [4/5] 时区\n")
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

    print()
    print("  [5/5] 微信内更新\n")
    _setup_wechat_update(env, existing)

    env["DATA_DIR"] = str(data)
    _write_env(env_file, env)
    _write_updater_env(data / "updater.env", env)

    # --- WeChat Login ---
    print()
    session_path = data / "session.json"
    if session_path.exists():
        relogin = _ask("已有登录态，重新登录？(y/N)", "N")
        if relogin.lower() not in ("y", "yes"):
            _request_restart(data)
            print("\n  ✓ 配置已更新！主服务会自动重启生效。")
            _print_update_hint(env)
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

    _request_restart(data)
    print()
    print("  ✓ 完成！启动：docker compose up -d")
    _print_update_hint(env)
    print()


def _setup_vision(env: dict[str, str], existing: dict[str, str]) -> None:
    print("    选择视觉模型渠道（与主模型可以不同）：\n")
    for i, p in enumerate(PROVIDERS):
        print(f"      {i + 1}. {p['name']}")
    print()

    while True:
        v_raw = _ask("输入编号（回车跳过）")
        if not v_raw:
            return
        if v_raw.isdigit() and 1 <= int(v_raw) <= len(PROVIDERS):
            break
        print(f"  请输入 1-{len(PROVIDERS)}，或回车跳过")

    vp = PROVIDERS[int(v_raw) - 1]

    while True:
        env["VISION_PROVIDER"] = vp["type"]
        env["VISION_API_KEY"] = _ask_secret("视觉模型 API Key", existing.get("VISION_API_KEY", ""))
        if vp["key"] == "custom":
            env["VISION_BASE_URL"] = _ask("Base URL", existing.get("VISION_BASE_URL", ""))
            env["VISION_MODEL"] = _ask("Model", existing.get("VISION_MODEL", ""))
        elif vp.get("ask_base_url"):
            env["VISION_BASE_URL"] = _ask("Base URL", existing.get("VISION_BASE_URL", vp["base_url"]))
            env["VISION_MODEL"] = _ask("Model", existing.get("VISION_MODEL", vp["model"]))
        else:
            if vp["base_url"]:
                env["VISION_BASE_URL"] = vp["base_url"]
            env["VISION_MODEL"] = _ask("Model", existing.get("VISION_MODEL", vp["model"]))

        ok, _ = _test_model(
            vp["type"], env["VISION_API_KEY"],
            env["VISION_MODEL"], env.get("VISION_BASE_URL", ""),
            test_vision=True,
        )
        if ok:
            break
        retry = _ask("重新输入？(Y/n)", "Y")
        if retry.lower() in ("n", "no"):
            for k in ("VISION_PROVIDER", "VISION_API_KEY", "VISION_BASE_URL", "VISION_MODEL"):
                env.pop(k, None)
            print("  已跳过视觉模型配置。")
            return

    print("    视觉能力将在启动时自动检测。如检测不到，可在 .env 中设置 VISION_ENABLED=true 强制开启。")


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


def _setup_wechat_update(env: dict[str, str], existing: dict[str, str]) -> None:
    current_enabled = _is_enabled(existing.get("WECHAT_UPDATE_ENABLED", "true"))
    if current_enabled:
        print("    当前：已开启")
    else:
        print("    当前：未开启")
    print("    开启后有更新提醒时可直接回复「确认更新」；也可发「检查更新」手动检查。")
    print()

    default = "Y" if current_enabled else "N"
    enable = _ask("开启微信内更新？(y/N)" if default == "N" else "开启微信内更新？(Y/n)", default)
    token = existing.get("WATCHTOWER_HTTP_API_TOKEN", "").strip()
    if not token:
        token = secrets.token_urlsafe(32)
        print(f"  已生成 updater 令牌：{_mask(token)}")
    else:
        print(f"  保留 updater 令牌：{_mask(token)}")
    env["WATCHTOWER_HTTP_API_TOKEN"] = token

    if enable.lower() not in ("y", "yes"):
        env["WECHAT_UPDATE_ENABLED"] = "false"
        print("  已关闭微信内更新。")
        return

    env["WECHAT_UPDATE_ENABLED"] = "true"
    if existing.get("WATCHTOWER_URL"):
        env["WATCHTOWER_URL"] = existing["WATCHTOWER_URL"]
    print("  已开启微信内更新。")


def _print_update_hint(env: dict[str, str]) -> None:
    if not _is_enabled(env.get("WECHAT_UPDATE_ENABLED", "")):
        return
    print("  微信内更新已开启。有更新提醒时回复「确认更新」；也可发「检查更新」手动检查。")
