"""交互式引导程序。

支持增量配置：已有配置项显示为默认值，回车即可保留。
使用 simple-term-menu 提供上下键选择，不支持时自动降级为数字输入。
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

PROVIDERS = [
    {"key": "anthropic",  "name": "Anthropic",           "type": "anthropic",
     "base_url": "", "model": "claude-sonnet-4-6"},
    {"key": "openai-com", "name": "OpenAI",              "type": "openai",
     "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    {"key": "mimo-plan",  "name": "MiMo",                "type": "openai",
     "base_url": "https://token-plan-cn.xiaomimimo.com/v1", "model": "mimo-v2.5-pro",
     "vision_model": "mimo-v2.5"},
    {"key": "deepseek",   "name": "DeepSeek",            "type": "openai",
     "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    {"key": "openrouter", "name": "OpenRouter",          "type": "openai",
     "base_url": "https://openrouter.ai/api/v1", "model": "openrouter/auto"},
    {"key": "siliconflow","name": "硅基流动",             "type": "openai",
     "base_url": "https://api.siliconflow.cn/v1", "model": "Qwen/Qwen3-8B"},
    {"key": "zhipu",      "name": "智谱 GLM",            "type": "openai",
     "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
    {"key": "moonshot",   "name": "Moonshot",            "type": "openai",
     "base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    {"key": "custom",     "name": "自定义 OpenAI 兼容",  "type": "openai",
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


# ── UI helpers ──────────────────────────────────────────────


def _section(title: str) -> None:
    width = 44
    line = "─" * width
    print(f"\n  ── {title} {line[:width - len(title) - 1]}\n")


def _select(title: str, options: list[str], default: int = 0) -> int:
    """Arrow-key selection with fallback to number input."""
    try:
        from simple_term_menu import TerminalMenu
        menu = TerminalMenu(
            options,
            title=f"\n  {title}\n",
            cursor_index=default,
            menu_cursor="  ▸ ",
            menu_cursor_style=("fg_cyan", "bold"),
            menu_highlight_style=("fg_cyan", "bold"),
        )
        idx = menu.show()
        return idx if idx is not None else default
    except Exception:
        for i, opt in enumerate(options):
            mark = " ←当前" if i == default else ""
            print(f"    {i + 1}. {opt}{mark}")
        print()
        while True:
            raw = _ask("输入编号", str(default + 1))
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return int(raw) - 1
            print(f"  请输入 1-{len(options)}")


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


def _is_xiaomi_mimo_base_url(base_url: str) -> bool:
    return "xiaomimimo.com" in base_url.lower()


def _ask_secret(prompt: str, current: str = "") -> str:
    if current:
        print(f"  当前: {_mask(current)}")
        val = _ask(f"{prompt}（回车保留）")
        return val if val else current
    return _ask(prompt)


# ── Model testing ───────────────────────────────────────────


def _make_test_image_b64() -> str:
    import io, base64
    from PIL import Image
    img = Image.new("RGB", (16, 16), color="red")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode()


def _test_model(provider_type: str, api_key: str, model: str, base_url: str = "",
                *, test_vision: bool = False) -> tuple[bool, str]:
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
            is_mimo = _is_xiaomi_mimo_base_url(base_url)
            client_kwargs = {"api_key": api_key, "base_url": base_url or "https://api.openai.com/v1"}
            if is_mimo:
                client_kwargs["default_headers"] = {"api-key": api_key}
            client = OpenAI(**client_kwargs)
            request_kwargs = {"model": model, "messages": messages}
            if is_mimo:
                request_kwargs["extra_body"] = {"max_completion_tokens": 16}
            else:
                request_kwargs["max_tokens"] = 16
            client.chat.completions.create(**request_kwargs)
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
    if "not supported model" in msg.lower():
        return "模型不存在或当前订阅不支持；MiMo 模型名需使用小写，如 mimo-v2.5-pro"
    if code == 404 or "not found" in msg.lower() or "does not exist" in msg.lower():
        return "模型不存在，请检查模型名称"
    if "image" in msg.lower() and ("format" in msg.lower() or "decode" in msg.lower()):
        return "图片格式不支持，该模型可能不具备视觉能力"
    if code == 429 or "rate" in msg.lower():
        return "触发频率限制，请稍后重试"
    if "billing" in msg.lower() or "quota" in msg.lower() or "insufficient" in msg.lower():
        return "账户余额不足或配额用尽"
    if "timeout" in msg.lower() or "timed out" in msg.lower():
        return "请求超时，请检查网络"
    if "connection" in msg.lower() or "resolve" in msg.lower():
        return "无法连接到服务器，请检查 Base URL"
    return f"{name}: {msg}"


# ── Env file ops ────────────────────────────────────────────


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


# ── Provider detection (for existing config) ────────────────


def _matches_provider_base_url(provider: dict, base_url: str) -> bool:
    normalized = base_url.rstrip("/")
    provider_base_url = provider.get("base_url", "").rstrip("/")
    if provider_base_url and normalized == provider_base_url:
        return True
    if provider.get("key") == "mimo-plan":
        return "token-plan-" in normalized and _is_xiaomi_mimo_base_url(normalized)
    return False


def _detect_provider(existing: dict[str, str]) -> dict | None:
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
            return PROVIDERS[-1]
    return None


def _detect_provider_index(existing: dict[str, str]) -> int:
    prov = _detect_provider(existing)
    if prov is None:
        return 0
    for i, p in enumerate(PROVIDERS):
        if p["key"] == prov["key"]:
            return i
    return 0


# ── Context length detection ────────────────────────────────


def _query_context_length(model_name: str, data_dir: Path | None = None) -> int | None:
    try:
        from .models import fetch_model_info
        info = fetch_model_info(model_name, data_dir=data_dir)
        return info.context_length if info.context_length else None
    except Exception:
        return None


# ── Main setup ──────────────────────────────────────────────


def run_setup(data_dir: str = "./data") -> None:
    data = Path(data_dir).resolve()
    data.mkdir(parents=True, exist_ok=True)
    env_file = data / ".env"

    existing = _load_env(env_file)
    is_update = bool(existing)

    print()
    print("  ┌─────────────────────────────────────┐")
    print("  │        叮咚 · 配置向导               │")
    print("  └─────────────────────────────────────┘")
    if is_update:
        current_provider = _detect_provider(existing)
        current_name = current_provider["name"] if current_provider else "未知"
        print(f"\n  检测到已有配置（{current_name}），回车保留当前值。")

    env: dict[str, str] = dict(existing)

    # ── [1/6] 模型配置 ──

    _section("[1/6] 模型配置")

    default_idx = _detect_provider_index(existing) if is_update else 0
    options = []
    for p in PROVIDERS:
        options.append(p["name"])
    chosen_idx = _select("选择模型渠道", options, default=default_idx)
    provider = PROVIDERS[chosen_idx]

    while True:
        api_key, model, base_url = _configure_provider(provider, env, existing)
        ok, _ = _test_model(
            provider["type"], api_key, model,
            base_url if provider["type"] == "openai" else "",
        )
        if ok:
            _apply_provider_config(env, provider, api_key, model, base_url)
            break
        print("  继续保存后，机器人可能无法正常回复。")
        retry = _ask("是否重新输入？(Y/n)", "Y")
        if retry.lower() in ("n", "no"):
            _apply_provider_config(env, provider, api_key, model, base_url)
            print("  已保存未通过测试的模型配置。")
            break

    # context length
    _setup_context_length(env, existing, model, data_dir=data)

    # ── [2/6] 视觉模型 ──

    _section("[2/6] 视觉模型")
    _setup_vision(env, existing, provider, model)

    # ── [3/6] Exa 搜索 ──

    _section("[3/6] Exa 搜索（可选）")
    exa_key = _ask_secret("Exa API Key", existing.get("EXA_API_KEY", ""))
    if exa_key:
        env["EXA_API_KEY"] = exa_key

    # ── [4/6] 时区 ──

    _section("[4/6] 时区")
    cur_tz = existing.get("SCHEDULER_TZ", "")
    if cur_tz:
        print(f"  当前：{cur_tz}")
        change_tz = _ask("修改时区？(y/N)", "N")
        if change_tz.lower() in ("y", "yes"):
            _setup_timezone(env)
        else:
            print("  保留现有时区。")
    else:
        _setup_timezone(env)

    # ── [5/6] 管理台密码 ──

    _section("[5/6] 管理台")
    _setup_admin_password(env, existing)

    # ── [6/6] 微信内更新 ──

    _section("[6/6] 微信内更新")
    _setup_wechat_update(env, existing)

    # ── 保存 & 登录 ──

    env["DATA_DIR"] = str(data)
    _write_env(env_file, env)
    _write_updater_env(data / "updater.env", env)

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


# ── Provider configuration ──────────────────────────────────


def _configure_provider(provider: dict, env: dict, existing: dict
                        ) -> tuple[str, str, str]:
    """Gather credentials and model for a provider. Returns (api_key, model, base_url)."""
    current = _detect_provider(existing)

    if provider["type"] == "anthropic":
        api_key = _ask_secret("API Key", existing.get("ANTHROPIC_API_KEY", ""))
        model = _ask("Model", existing.get("ANTHROPIC_MODEL", provider["model"]))
        return api_key, model, ""

    # openai-compatible
    api_key = _ask_secret("API Key", existing.get("OPENAI_API_KEY", ""))

    if provider["key"] == "custom":
        base_url = _ask("Base URL", existing.get("OPENAI_BASE_URL", ""))
        model = _ask("Model", existing.get("OPENAI_MODEL", ""))
    else:
        base_url = provider["base_url"]
        model_default = (
            existing.get("OPENAI_MODEL", provider["model"])
            if current and current["key"] == provider["key"]
            else provider["model"]
        )
        model = _ask("Model", model_default)

    return api_key, model, base_url


def _apply_provider_config(env: dict, provider: dict, api_key: str, model: str, base_url: str) -> None:
    if provider["type"] == "anthropic":
        env["LLM_PROVIDER"] = "anthropic"
        env["ANTHROPIC_API_KEY"] = api_key
        env["ANTHROPIC_MODEL"] = model
    else:
        env["LLM_PROVIDER"] = "openai"
        env["OPENAI_API_KEY"] = api_key
        env["OPENAI_MODEL"] = model
        env["OPENAI_BASE_URL"] = base_url


# ── Context length ──────────────────────────────────────────


def _setup_context_length(env: dict, existing: dict, model: str, data_dir: Path | None = None) -> None:
    print()
    cur = existing.get("CONTEXT_LENGTH", "")

    print("  检测模型上下文长度 ...", end=" ", flush=True)
    detected = _query_context_length(model, data_dir=data_dir)
    if detected:
        print(f"{detected:,} tokens")
        default = str(detected)
        if cur and cur != str(detected):
            print(f"  当前配置：{cur}，已更新为检测值")
    else:
        print("未检测到")
        default = cur if cur else ""

    val = _ask("上下文长度（tokens，回车接受默认值，输入 0 清除）", default)
    cleaned = val.replace(",", "").strip() if val else ""
    if cleaned and cleaned.isdigit() and int(cleaned) > 0:
        env["CONTEXT_LENGTH"] = cleaned
    elif cleaned == "0" or (not val and not default):
        env.pop("CONTEXT_LENGTH", None)


# ── Vision configuration ────────────────────────────────────

_VISION_KEYS = ("VISION_PROVIDER", "VISION_API_KEY", "VISION_BASE_URL", "VISION_MODEL")


def _clear_vision_config(env: dict) -> None:
    for k in _VISION_KEYS:
        env.pop(k, None)


def _setup_vision(env: dict, existing: dict, main_provider: dict, main_model: str) -> None:
    print("  图片理解能力取决于所选模型。启动时自动检测主模型是否支持。")
    print("  也可配置独立视觉模型，仅在收到图片时调用。\n")

    has_vision_config = bool(existing.get("VISION_PROVIDER"))

    if has_vision_config:
        cur_vmodel = existing.get("VISION_MODEL", "")
        print(f"  当前视觉模型：{cur_vmodel}")
        ok, _ = _test_model(
            existing.get("VISION_PROVIDER", "openai"),
            existing.get("VISION_API_KEY", ""),
            cur_vmodel,
            existing.get("VISION_BASE_URL", ""),
            test_vision=True,
        )
        if ok:
            actions = ["keep", "main", "separate", "remove"]
            options = ["保留现有配置", "与主模型相同", "配置独立视觉模型", "移除视觉模型"]
            idx = _select("视觉模型配置", options, default=0)
        else:
            print("  当前视觉模型测试未通过。")
            actions = ["main", "separate", "remove"]
            options = ["与主模型相同", "配置独立视觉模型", "移除视觉模型"]
            idx = _select("视觉模型配置", options, default=0)

        action = actions[idx]
        if action == "keep":
            print("  保留现有配置。")
        elif action == "main":
            _clear_vision_config(env)
            print("  已切换为主模型，视觉能力将在启动时自动检测。")
        elif action == "separate":
            _configure_separate_vision(env, existing)
        elif action == "remove":
            _clear_vision_config(env)
            print("  已移除视觉模型。")
    else:
        options = [f"与主模型相同（{main_model}）", "配置独立视觉模型"]
        choice = _select("视觉模型", options, default=0)
        if choice == 0:
            print("  视觉能力将在启动时自动检测。")
        else:
            _configure_separate_vision(env, existing)


def _configure_separate_vision(env: dict, existing: dict) -> None:
    options = [p["name"] for p in PROVIDERS]
    v_idx = _select("选择视觉模型渠道", options, default=0)
    vp = PROVIDERS[v_idx]

    while True:
        env["VISION_PROVIDER"] = vp["type"]
        env["VISION_API_KEY"] = _ask_secret("视觉模型 API Key", existing.get("VISION_API_KEY", ""))
        vision_default = vp.get("vision_model", vp["model"])

        if vp["key"] == "custom":
            env["VISION_BASE_URL"] = _ask("Base URL", existing.get("VISION_BASE_URL", ""))
            env["VISION_MODEL"] = _ask("Model", existing.get("VISION_MODEL", ""))
        else:
            if vp["base_url"]:
                env["VISION_BASE_URL"] = vp["base_url"]
            else:
                env.pop("VISION_BASE_URL", None)
            env["VISION_MODEL"] = _ask("Model", existing.get("VISION_MODEL", vision_default))

        ok, _ = _test_model(
            vp["type"], env["VISION_API_KEY"],
            env["VISION_MODEL"], env.get("VISION_BASE_URL", ""),
            test_vision=True,
        )
        if ok:
            break
        retry = _ask("是否重新输入？(Y/n)", "Y")
        if retry.lower() in ("n", "no"):
            _clear_vision_config(env)
            print("  已跳过视觉模型配置。")
            return


# ── Timezone ────────────────────────────────────────────────


def _setup_timezone(env: dict) -> None:
    tz_options = [f"{label} ({tz_id})" for tz_id, label in _TZ_OPTIONS]
    tz_options.append("自定义")

    idx = _select("选择时区", tz_options, default=0)
    if idx < len(_TZ_OPTIONS):
        env["SCHEDULER_TZ"] = _TZ_OPTIONS[idx][0]
    else:
        while True:
            custom_tz = _ask("IANA 时区（如 Asia/Hong_Kong）")
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(custom_tz)
                env["SCHEDULER_TZ"] = custom_tz
                break
            except (KeyError, Exception):
                print(f"  无效时区：{custom_tz}，请重新输入")


# ── WeChat update ───────────────────────────────────────────


def _setup_wechat_update(env: dict, existing: dict) -> None:
    current_enabled = _is_enabled(existing.get("WECHAT_UPDATE_ENABLED", "true"))
    if current_enabled:
        print("  当前：已开启")
    else:
        print("  当前：未开启")
    print("  开启后有更新提醒时可直接回复「确认更新」；也可发「检查更新」手动检查。")
    print()

    options = ["开启", "关闭"]
    default = 0 if current_enabled else 1
    choice = _select("微信内更新", options, default=default)

    token = existing.get("WATCHTOWER_HTTP_API_TOKEN", "").strip()
    if not token:
        token = secrets.token_urlsafe(32)
        print(f"  已生成 updater 令牌：{_mask(token)}")
    else:
        print(f"  保留 updater 令牌：{_mask(token)}")
    env["WATCHTOWER_HTTP_API_TOKEN"] = token

    if choice == 1:
        env["WECHAT_UPDATE_ENABLED"] = "false"
        print("  已关闭微信内更新。")
        return

    env["WECHAT_UPDATE_ENABLED"] = "true"
    if existing.get("WATCHTOWER_URL"):
        env["WATCHTOWER_URL"] = existing["WATCHTOWER_URL"]
    print("  已开启微信内更新。")


def _setup_admin_password(env: dict, existing: dict) -> None:
    cur = existing.get("ADMIN_PASSWORD", "")
    print("  管理台用于添加/管理叮咚助手账号。")
    print("  管理密码用于保护管理台访问。\n")

    if cur:
        print(f"  当前密码：{_mask(cur)}")
        regen = _ask("重新生成密码？(y/N)", "N")
        if regen.lower() not in ("y", "yes"):
            env["ADMIN_PASSWORD"] = cur
            print("  保留现有密码。")
            return

    password = secrets.token_urlsafe(16)
    env["ADMIN_PASSWORD"] = password
    port = existing.get("ADMIN_API_PORT", "8081")
    print(f"\n  ┌─────────────────────────────────────┐")
    print(f"  │  管理台密码（请妥善保存）：             │")
    print(f"  │  {password:<36s}│")
    print(f"  └─────────────────────────────────────┘")

    print()
    options = ["Cloudflare Tunnel（免费自动穿透，无需域名）", "自行配置（Nginx 等）", "仅本地访问"]
    cur_tunnel = _is_enabled(existing.get("CF_TUNNEL", ""))
    default = 0 if cur_tunnel else 2
    choice = _select("公网访问方式", options, default=default)
    if choice == 0:
        env["CF_TUNNEL"] = "true"
        print("  已开启。启动后公网地址将显示在管理台页面和日志中。")
    else:
        env.pop("CF_TUNNEL", None)
        if choice == 1:
            print(f"  管理台监听 0.0.0.0:{port}，请自行配置反向代理。")


def _print_update_hint(env: dict) -> None:
    if not _is_enabled(env.get("WECHAT_UPDATE_ENABLED", "")):
        return
    print("  微信内更新已开启。有更新提醒时回复「确认更新」；也可发「检查更新」手动检查。")
