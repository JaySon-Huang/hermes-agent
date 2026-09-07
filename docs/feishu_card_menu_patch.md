# 飞书卡片菜单补丁方案（方案 B 轻量实现）

## 背景
飞书卡片按钮点击 → SDK → adapter `_on_card_action_trigger` → `_handle_card_action_event`
→ 合成命令 `/card button {"action":"status"}` → 交给 agent 管道。

但当前版本 `commands.py` 未注册 `card` 命令，gateway 的 safety net 把 `/card ...`
当 unknown command 丢弃，回 "unknown-command notice"。飞书客户端因此卡在 loading。

## 修复（两处改动）

### 改动 1：注册 /card 命令（消除 unknown-command 报错）
文件：`hermes_cli/commands.py`，在 COMMAND_REGISTRY 任意位置加：

```python
CommandDef("card", "Internal: handle Feishu/interactive card button callbacks", "Gateway",
           gateway_only=True, busy_policy="dispatch"),
```

（只是让 resolve_command("card") 返回非 None，避免被 safety net 丢弃。
真正的动作由改动 2 在 adapter 里翻译成 agent 可处理的文本。）

### 改动 2：adapter 把 action 翻译成 agent 可处理文本
文件：`plugins/platforms/feishu/adapter.py`，`_handle_card_action_event` 方法内，
把 3082-3087 行：

```python
        synthetic_text = f"/card {action_tag}"
        if action_value:
            try:
                synthetic_text += f" {json.dumps(action_value, ensure_ascii=False)}"
            except Exception:
                pass
```

替换为：

```python
        # 把卡片按钮的 action 翻译成 agent 可直接处理的指令文本。
        # 菜单卡用 value={"action":"status"} 等形式；审批/更新卡仍走原路径
        # （已在上层 _on_card_action_trigger 分流，不会到这里）。
        _ACTION_MAP = {
            "status": "/status",
            "model": "列出当前会话可用模型",
            "skills": "/skills",
            "usage": "查询 opencodego/cursor/copilot/deepseek 各家 AI 用量",
            "model_param": "查询某 AI 模型的接入参数（上下文/输出等）",
            "cron": "列出当前所有定时任务",
        }
        if action_value and isinstance(action_value, dict):
            _a = action_value.get("action") or action_value.get("hermes_action")
            if _a and _a in _ACTION_MAP:
                synthetic_text = _ACTION_MAP[_a]
            else:
                # 未知 action：把原始 value 透传，便于排查
                try:
                    synthetic_text = f"/card {action_tag} {json.dumps(action_value, ensure_ascii=False)}"
                except Exception:
                    synthetic_text = f"/card {action_tag}"
        else:
            synthetic_text = f"/card {action_tag}"
```

## 应用后
1. 重启 gateway：`systemctl --user restart hermes-gateway`
2. 重新发菜单卡片：`python3 ~/.hermes/scripts/send_menu_card.py`
3. 点按钮 → adapter 合成对应指令 → agent 执行 → 卡片解析、返回结果

## 风险
- 改动 1 只是注册元数据，零逻辑风险。
- 改动 2 仅在原合成逻辑处加了一层 action→文本映射，默认分支保持原行为，
  不影响审批卡/更新卡（它们在上层已分流，不进此分支）。
- 仍属于"改 Hermes 应用源码"。按用户安全偏好，此改动需用户自行 apply + 重启，
  不可由 agent 自动执行。

## 备选（不改源码）
若不想改 Hermes 源码，可把菜单卡片按钮的 value 直接写成已注册命令，例如
`{"action":"/status"}`，并在 adapter 合成时特判：若 action 以 "/" 开头则原样作为
指令文本。但这仍需改 adapter 合成逻辑（同样要改源码）。
纯不改源码的唯一办法是：卡片按钮 value 里直接带完整自然语言，且 adapter 已有逻辑
把整个 value dump 成 `/card button {...}` —— 这条路因 /card 未注册而断。
故方案 B 必须改源码。
