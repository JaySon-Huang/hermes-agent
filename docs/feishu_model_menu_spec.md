# 飞书多级交互模型选择菜单 — 设计规格（实现依据）

## 目标
把 `/menu` 主菜单的「🔧 切换模型」按钮从"直接发 /model 命令"升级为**三级交互卡片流程**：

```
主菜单卡 ──点「切换模型」──▶ provider 选择卡（新发）
provider 选择卡 ──点某 provider──▶ 模型列表卡（原地更新，含「← 选择其他 provider」）
模型列表卡 ──点某模型──▶ adapter 直接调 switch_model 切换 + 原地更新为结果卡
             ──点「← 选择其他 provider」──▶ 原地更新回 provider 选择卡
```

## 已验证的技术事实（不可违背）
1. **更新卡片必须走 `P2CardActionTriggerResponse.card` 回传**，不是 `im/v1/message/{id}` PUT。
   - 实测：`message.update` 带 `msg_type=interactive` 报 `230001 invalid msg_type`（2026-08-24 验证）。
   - 官方正确机制：卡片动作的 handler 返回 `P2CardActionTriggerResponse`，其 `.card` 字段
     （`CallBackCard(type="raw", data=新卡片JSON字符串)`）让飞书**原地替换**当前卡片。
   - 参照：`_handle_approval_card_action`（约 2958-2963 行）、`_handle_update_prompt_card_action`。
   - ⚠️ `card.data` 必须是 **JSON 字符串**（`json.dumps`），不能是 dict——上游 #42465 的 bug 教训。
2. **provider 列表来源**：`hermes_cli/model_switch.py` 的 `list_authenticated_providers()`（2571 行），
   与 `/model` 命令一致（`_handle_model_command` 里 import 过）。
3. **模型列表来源**：点 provider 后**同步查**该 provider 的模型列表。复用 `/model` 的
   `list_picker_providers` / catalog 逻辑（`hermes_cli/model_switch.py` 3926 行）。
   等待期间卡片按钮转圈（可接受，通常几秒）。
4. **切换模型**：`switch_model(raw_input, current_provider, current_model, current_base_url,
   current_api_key, is_global, explicit_provider, user_providers, custom_providers)` 是**纯函数**
   （1439 行），不依赖 agent 会话上下文。adapter 直接调用即可。
   - 切换目标：`switch_model(model, current_provider, current_model, ..., is_global=True,
     explicit_provider=<所选provider>)`。用 `is_global=True`（点击即全局持久化，最符合直觉；
     卡片菜单没有"会话"概念）。
   - `user_providers`/`custom_providers` 从 `self.config`（GatewayConfig）读。
5. **事件路由**：所有新 action 都走 `_handle_card_action_event`（普通按钮分支，**不经过**
   `_handle_approval_card_action` / `_handle_update_prompt_card_action`）。该 handler 目前
   (a) 把 action 翻译成合成命令、(b) 返回空 `P2CardActionTriggerResponse()`。本次扩展它：
   - 识别模型选择相关 action 时**不合成命令**，而是构造卡片并返回带 card 的响应。

## 新增 action 协议（按钮 value 约定）
| action | value 额外字段 | 行为 |
|---|---|---|
| `model_choose` | 无 | 新发 provider 选择卡（不更新，无原卡片可更新） |
| `model_provider` | `provider` | 同步查该 provider 模型列表 → 原地更新为模型列表卡 |
| `model_select` | `provider`, `model` | 调 switch_model 切换 → 原地更新为结果卡 |
| `model_provider_back` | 无 | 原地更新回 provider 选择卡 |

主菜单模板（`templates/feishu_menu_card.json` 或 adapter 的 DEFAULT 菜单）里
「切换模型」按钮的 value 改为 `{"action": "model_choose"}`。

## 实现位置（全部在 plugins/platforms/feishu/adapter.py）
1. `_handle_card_action_event` 内：在 `_ACTION_MAP` 查不到/命中模型类 action 时，新增分支：
   - `model_choose` → `await self._send_provider_card(chat_id)`；返回空响应
   - `model_provider` → 查模型 → 返回 `self._card_response(模型列表卡)`
   - `model_select` → 调 switch_model → 返回 `self._card_response(结果卡)`
   - `model_provider_back` → 返回 `self._card_response(provider 选择卡)`
2. 新增构造方法：
   - `_build_provider_card(providers)` → provider 选择卡 JSON（按钮 value 带 provider）
   - `_build_model_list_card(provider, models)` → 模型列表卡 JSON（含「← 选择其他 provider」按钮）
   - `_build_model_result_card(result)` → 切换结果卡 JSON
   - `_card_response(card_dict)` → `P2CardActionTriggerResponse(card=CallBackCard(type="raw", data=json.dumps(card_dict, ensure_ascii=False)))`
3. 卡片构造须保持：`config.update_multi: true`、按钮 `value={"action": ..., ...}`、
   两按钮一行 `layout: bisect`、header 模板。

## 边界与注意
- **返回响应与合成命令互斥**：模型类 action 返回带 card 的响应后，**不要再**合成 `/card ...` 命令，
  也不要发独立回复消息（避免重复）。非模型类 action 保持现有行为不变。
- **provider 卡是新发**：`model_choose` 时没有可更新的原卡片（主菜单点击），用 `_feishu_send_with_retry`
  新发。`model_provider_back` 是**更新**回 provider 卡（有原卡片）。
- **chat_id 获取**：`_handle_card_action_event` 已解析出 `chat_id`（现有代码），直接复用。
- **查询失败容错**：模型列表查询异常时，返回一张带错误提示的卡（「查询失败，请稍后重试」+
  「← 选择其他 provider」），不要空响应。
- **英文注释**（开源代码规范）。
- 不改 `hermes_cli/models.py`、`package-lock.json`；不动 `_handle_approval_card_action` /
  `_handle_update_prompt_card_action` / `_ACTION_MAP` 现有条目（除新增外）。
