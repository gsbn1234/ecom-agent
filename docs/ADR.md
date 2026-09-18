# 架构决策记录（ADR）

这份文档里的每个标题，都是一个**当时真的需要做决定**的问题 —— 不是事后给代码配的说明书。
每条四段：**决定 / 为什么 / 代价与被否掉的选项 / 证据在哪**。

三条规矩，先说在这里：

1. **"证据在哪"必须指向能重跑或能翻到的东西** —— 测试名、`文件:行`、或者
   `docs/spikes.md` 的某一节。指不到的东西不写进来：这个项目栽过太多次
   "文档里写着、代码里从来没有"。
2. **每条都要写代价**。只写优点的决策记录读起来像广告，而面试官恰恰会追问
   "这个选择的代价是什么"。
3. **不知道的写不知道**。原因不明的事不许被写成"应该是偶发" —— 见 README 的
   「这个项目**没做到**的事」。

---

## ADR 1 · 为什么基于 browser-use 二次开发，而不是自研 CDP 驱动 / Playwright + 手写选择器

**决定**：把 `browser-use==0.13.10` 当**执行引擎**用，一行库源码都不改 ——
DSL、护栏、落库、可观测这四块全部在上层实现。（"库源码只读不改"是硬约束，
要改行为就 monkeypatch 或包装。）

**为什么**：卖家后台这类活有个共同特征 —— **逻辑简单到不值得为它写死选择器，
但页面一改版选择器就全废**。LLM 驱动的好处不在"聪明"，而在它读的是 DOM 语义，
页面换个 class 名它照样认得。真自研的话只有两条路：回到选择器（把已知的坑重踩一遍），
或者自己造一个"看页面 → 决定动作 → 执行 → 再观察"的循环 —— 那基本等于重写
browser-use，而且是一个人写。

**代价与被否掉的选项**：
- 依赖压在若干个**未文档化行为**上（见 ADR 13）。升级会漂移，所以精确锁版本 + 版本哨兵。
- 库的失败模式我们**只能观测，不能改**。比如 Phase 6 查出的
  `BrowserProfile._copy_profile()` 会把 `user_data_dir` 偷偷拷到 `%TEMP%` ——
  我们只能在上面写包装（`pin_user_data_dir`），不能去改库。
- 被否掉的"自研"并不是没有优点的选项：它换来的是**完全可控**。否掉它的理由是
  成本确定性地更高，而面试里"我重写了一个浏览器驱动"不加分，"我知道我为什么用这个库、
  以及它的边界在哪"才加分。

**证据在哪**：`ecom_agent/compat.py`（唯一访问点）+ `tests/test_compat.py`（版本哨兵）；
`docs/spikes.md` 的「Phase 6 探路结论」（连库内部行为都实测过的记录）。

---

## ADR 2 · 为什么零 VLM，以及这条在什么情况下会失效

**决定**：`use_vision=False`，**并且用 DSL 把它锁死** —— YAML 里写
`use_vision: true` 会在**加载期**被拒绝，跑不起来。

**为什么**：本仓库建立**之前**的本机冒烟已经证过：DeepSeek 文本模型 + DOM 树驱动，
2 步完成页面抽取、0 错误（那次实验没法从仓库里重跑 —— 它发生在仓库存在之前，
所以这里只当背景，不作为证据）。另外一条容易被忽略的事实：**库在
`use_vision=False` 时照样采集截图**（`agent/service.py` 里那句注释原文是 always capture），
所以我们省掉的是**喂给模型的图**，不是审计证据 —— 截图照旧落盘、照旧可回放。

**代价与失效边界**（这条必须主动划，别等被问）：
- canvas 渲染的图表、纯图片按钮、需要"看图才知道是什么"的元素，**做不了**。
- 验证码页只可能被识别成"需要人工" —— 我们本来也不绕它（见 ADR 14）。
- 这条边界写进了 README 的安全边界一节，不是藏在这里。

**证据在哪**：`tests/test_dsl.py::test_use_vision_true_is_rejected`（**加载期**拒绝，
不是靠自觉遵守）；`docs/spikes.md` 的「Phase 3 live 验收」—— 那是一次**真 DeepSeek、
`use_vision=False`、真站点**跑通的记录，仓库里的人能翻到。

---

## ADR 3 · 为什么任务用 YAML DSL 而不是每次写一段自然语言提示词

**决定**：一个 YAML = 一个任务定义，编译成固定的几个产物：`task_text`（给 LLM 的文本）、
`output_model`、`browser_kwargs`、`agent_kwargs`、`policy`（给拦截器）、`fingerprint`（落库做可复现追溯）。

**为什么**：**DSL 的本质是把"希望 LLM 做的事"和"不管 LLM 做什么都必须成立的事"分开**。
每个字段有**独立**的强制点：`params` 由 pydantic 强制、`guardrails` 由 interceptor 强制、
`output_model` 由 pydantic 强制、`max_steps`／`max_failures` 由 browser-use 自己强制。

⚠️ **这一段的第四项原先是 `pagination.max_pages`，2026-09-18 删掉了** ——
因为它不是强制点，是**反例**。`max_pages` 唯一的去处是编译成 `task_text` 里的一句
"最多翻 N 页"（`compiler.py:200`），运行期没有任何计数器读它；`CompiledTask`
（`compiler.py:39-57`）里**根本没有 `pagination` 这个字段**。
实测：`mock_shop_readonly.yaml` 写 `max_pages: 1`，run
`20260918T034157+0000-c9413c` 照样翻到了第 2 页。

留着它比删掉更糟：**一个假例证会把整段论点的可信度一起拉下水** ——
面试官只要问一句"给我看看那个计数器"，听到的就不是"这一个字段没做到"，
而是"这四个里到底还有几个是真的"。剩下三项都经得起查。
而一段自然语言提示词里，所有约束只有**一个**强制点：LLM 愿不愿意听。

**代价与被否掉的选项**：
- 写 YAML 比写一句话慢，而且参数化能力有上限 —— 复杂分支就得加新字段（而加字段
  意味着编译器、校验、测试都要动）。
- ⚠️ 说清楚一件事：**它不能替代写提示词**。`task_text` 最终仍是一段自然语言，
  只不过它由可 review 的结构**生成**。如果有人把它读成"用 YAML 就不用写 prompt 了"，
  那是读错了。
- 被否掉的选项是"把 prompt 挪进 YAML 就算完"。那只是可维护性，不是强制力 ——
  差别在面试里值得专门讲一遍。
- ⚠️ **明确不做 `max_pages` 硬闸**（这是决定，不是遗漏）。它防的是**成本**不是**安全**；
  而"翻了一页"在三种 `mode` 下没有统一定义（`click_next` 点按钮 / `scroll` 滚动触发 /
  `none` 不翻），真站点上访问详情页同样会让 URL 变化 —— 按 URL 计数会**误停**。
  **一个数不准的硬闸比一句诚实的话更危险**：它会让人以为这里有保护。
  兜底是 `agent.max_steps`（browser-use 强制）和 `stop_when` 的"翻到空页就停"。

**证据在哪**：`ecom_agent/dsl/compiler.py`（产物表与 `compile_task`）；
`devtools/show_compiled.py`（把编译产物直接打出来看，不用跑浏览器）；
`tests/test_dsl.py::test_unknown_field_error_names_the_field` —— 报错必须**点名那个字段**，
这条钉的是"拼错字段名被 `extra="forbid"` 抓住"，而不是别的原因造成的失败。

---

## ADR 4 · 为什么护栏必须自己造：三层防御各自能挡什么、**挡不住什么**

**决定**：三层 + 一条写进文档的规矩。

| 层 | 在哪 | 挡什么 |
|---|---|---|
| Layer 0 网络层 | `BrowserSession(allowed_domains=...)` → 库的 SecurityWatchdog | 导航到站外、重定向到站外、新标签页到站外 |
| Layer 1 动作层 | 本项目的 `GuardrailInterceptor` | 站内的危险动作（删除/下架/支付/发布/编辑） |
| Layer 2 结果层 | 输出校验 + 入库前过滤 | schema 漂移、幻觉字段、把"删除成功"提示当商品数据 |

**为什么必须自己造**：0.13.10 **全库零命中** —— 它**没有**内置的人工确认（HITL）机制。
不是"没配好"，是库里就没有这个能力。而"确认"对卖家后台是刚需：资金、发布、删除，
这三类操作点错一次的代价是不可逆的。

**代价与边界（每层都必须写清"挡不住什么"）**：
- Layer 0 挡不住**页面内** `fetch()` 到站外 —— XHR 不走导航事件。
- Layer 1 挡不住 LLM 用两个"无害"动作**组合**出危险效果（先"全选"再"批量操作"）。
  这是设计上的已知缺口，不是没测到。
- Layer 2 挡不住"**真实但错误**"的数据（把"近 30 天销量"填进"库存"列）——
  处置是 sanity 标记 + 人工抽检，不是假装能自动判定。
- 自造护栏的额外代价：**得自证它有效**。所以每一条都配了对照实验（见下）。

**证据在哪**：`docs/guardrail_design.md`（那份文档的规矩就是"挡不住什么"比"挡什么"重要）；
`tests/test_mock_pdd_e2e.py` 的三条，其中第一条是**对照**：

- `test_unguarded_click_on_batch_delete_really_writes` —— **不挂护栏时它真的写进去了**。
  没有这条，"护栏挡住了"这句话是没有分量的：它只证明"没发生"，不证明"是被挡住的"。
- `test_guarded_run_blocks_the_click_and_still_collects_rows` —— 挡住的同时，该采的行照样采到
  （护栏不该把整个 run 废掉）。
- `test_denied_approval_makes_the_llm_reroute` —— 拒绝之后 LLM **改道**，而不是卡死或重试同一个动作。

---

## ADR 5 · 拦截点为什么选未文档化的 `register_new_step_callback`

**决定**：动作层的拦截挂在 `register_new_step_callback` 上，在"LLM 输出之后、动作执行之前"
**就地改写** `agent_output.action`。

**为什么**：三个理由，每条都对应一条实测出来的引擎事实：

1. **能真的 await 人工，而且不终止 run** —— 回退方案（停机审批再续跑）做不到这一点。
2. **不受 `Tools.act` 的 180s 超时约束** —— 审批发生在这个回调里，**不在 action 函数体内**。
   否则会出现：人工还没点"批准"，action 先被库掐掉了，表现为一个莫名其妙的
   `ActionResult(error=...)`。这条是本项目最想避开的一个坑（R3），而选这个拦截点
   等于从架构上绕开它。
3. **能就地改写动作列表**，而**库自己就这么干**（`agent/service.py` 里既直接给
   `model_output.action` 赋值，也用 `setattr` 造非 LLM 产出的动作）——
   我们走的是库的既有范式，不是野路子。这一点很重要：借用未文档化行为时，
   "库自己也依赖它"是最强的稳定性论据。

**代价**：
- 它是**未文档化**的。所以做了三件事兜底：三条机制共享同一个 `policy.evaluate()`
  （换机制不改策略，约 150 行）；S1 spike 第一个就验它；`compat.py` 哨兵盯着相关签名。
- 它**只在有 LLM 参与时**才被触发 —— 绕过 Agent 直接调 action（比如测试里走实现本体）
  不走这条路。所以核心逻辑不能只活在这个回调里。

**证据在哪**：`devtools/spike_s1_callback_rewrite.py` + `docs/spikes.md` 的 S1 一节
（当时的具体输出，不是"应该可以"）；`ecom_agent/guardrails/interceptor.py`；
`tests/test_compat.py`（签名哨兵）。

---

## ADR 6 · 为什么规则聚合用「最严优先」而不是「先匹配先赢」

**决定**：多条规则同时命中时，取最严的那条（`block` > `confirm` > `allow`），
**与 YAML 里的书写顺序无关**。同severity 并列时用 `rule_id` 排序做 tiebreaker —— 也是顺序无关的。

**为什么**：YAML 是**有顺序**的。如果按"先匹配先赢"，那么"把一条 `allow` 写在 `block` 前面"
就是一个**静默的安全漏洞**：规则集看上去没问题，review 的人也不会觉得哪里不对，
但那条 block 永远不会生效。最严优先让规则集变成**集合语义** —— 评审时只需看集合本身，
不需要在脑子里模拟匹配顺序。

**代价与被否掉的选项**：
- **不能**用"把例外规则写在前面"来表达例外。要表达例外，必须改规则条件本身
  （比如把 `match_url` 写得更具体），这更难写 —— 但有代价的难写好过静默失效。
- 被否掉的"先匹配先赢"不是没有好处：它更容易表达"一般规则 + 特例覆盖"这种直觉。
  否掉它的理由只有一个，但足够：**它的失效模式是静默的，而且失效的恰好是安全的那一侧**。

**证据在哪**：`tests/test_guardrail_policy.py` 三条 ——
`test_most_severe_wins_when_multiple_rules_match`、
`test_rule_order_does_not_affect_decision`（**两个顺序相反的规则集，断言决策完全一致**）、
`test_ties_broken_by_rule_id_not_yaml_order`。

---

## ADR 7 · 为什么 `default_decision` 默认是 `confirm` 而不是 `allow`

**决定**：没有命中任何规则时 → **人工确认**（fail-closed），不是放行。

**为什么**：LLM 的失败模式恰恰是**做了你没预料到的那个动作**。默认放行等于承认
"护栏只在写了规则的地方生效"—— 而危险恰恰来自没写规则的地方（规则是人写的，
人想不到的正是漏掉的那些）。默认 confirm 把"没想到"从**静默放行**变成
**一次必须有人回答的提问**。

**代价**：
- **误报会明显变多**：每个没预料到的动作都要人点一下。这是真实的、每天都会感觉到的成本。
- 所以必须配够用的审批通道，否则这个默认值会把人逼回去改成 allow：
  `WebApprover`（默认）/ `CliApprover`（本机调试，用 `asyncio.to_thread` 避免阻塞事件循环
  掐断 CDP 心跳）/ `FileApprover`（无头）/ `AutoDenyApprover`（测试专用，永远拒）。
- **超时一律按拒绝**（fail-closed），`CliApprover` 默认选项是 `N`，回车即拒绝。
- 另一条代价说直白些：默认 confirm 意味着**这个系统需要有人在场**。它不是无人值守的
  批量工具 —— 那是另一个产品，不是这个。

**证据在哪**：`tests/test_guardrail_policy.py::test_default_decision_is_confirm_not_allow`；
`tests/test_approver.py::test_timeout_is_denied_not_approved` 与
`test_channel_exception_is_denied_not_approved`（连"审批通道自己抛异常"都判拒绝，
不是放行）—— 这两条钉的是"fail-closed 不是口号"。

---

## ADR 8 · 为什么用持久 profile，而不是 `sensitive_data` 占位符替换

**决定**：人工跑一次 `devtools/login_pdd.py` 扫码登录 → 之后复用那个 profile 目录，
**agent 永远不碰登录表单**。`sensitive_data` 机制仍按**按域形态**搭好（面试会问到），
但**绝不用扁平形态**。

**为什么持久 profile 更硬**：密码**根本不进入 Agent** ——
不出现在提示词里、不出现在截图里、**也不需要脱敏代码来兜底**。
占位符替换的失败模式是"替换没生效"或"替换错了字段"，而这两者都是**静默**的：
run 照常跑完，只是一个密码被填进了不该填的地方。

**代价与三个实测坑**（这条 ADR 里最值钱的其实是坑，不是结论）：
1. ★★★ 库的 `BrowserProfile._copy_profile()` 会把 `user_data_dir` **偷偷拷到 `%TEMP%`
   再把字段换成那个目录**（因为我们的 `executable_path` 永远指向 chrome.exe →
   `is_chrome` 恒真 → 必走此路）。后果：**扫码看着成功 → 之后每次 run 都在登录页 →
   退出码 0、报告齐全、sqlite 零行**。修法是构造后把目录钉回去（`pin_user_data_dir`）。
2. `BrowserSession(browser_profile=obj)` **不使用** `obj` —— 必须构造完再钉在
   `session.browser_profile` 上。这条和上一条合起来，构成"看着配了、其实没配"的典型。
3. **cookie 落盘取决于怎么关**：`session.stop()` 实测丢 cookie，必须走 CDP `Browser.close`
   （`close_gracefully_and_flush`）。
- ⚠️ **扁平形态是红线，理由是源码事实**：扁平形态**无条件**放行到所有域，
  于是一份拼多多密码在访问任何站点时都可能被填入。

**证据在哪**：`ecom_agent/runtime/browser.py` 的三个包装；
`tests/test_profile_persistence.py`（含**对照**：不钉 → cookie 落到库的临时目录 → 登录态丢）；
`docs/spikes.md` 的「Phase 6 探路结论」；
`tests/test_compat.py::test_sensitive_data_is_not_a_reserved_param_name`。

---

## ADR 9 · 为什么截图必须自己另存；为什么用 `get_structured_output(Model)`

**决定**：截图自己另存到 `runs/{run_id}/screenshots/` 并记 `sha256`；
结构化输出只走 `history.get_structured_output(Model)`，不用 `.structured_output`。

**为什么截图要另存**：库自己落的截图在**系统临时目录**（`agent_directory`）——
**关机即失**。审计证据不能放在会被系统清掉的地方。顺带收获一个便宜且好用的诊断信号：
连续两步的 `sha256` 相同 = "页面没变化（可能点击无效）"。

**为什么必须用 getter**：`history.structured_output` 这个 property 依赖私有字段
`_output_model_schema`，而它**序列化后就丢失** → 存过盘再读回**永远返回 `None`，
且不报错**。这是"数据没了但没有任何信号"的典型，也正是本项目最怕的那一类。

**代价**：自己另存要处理"裸 base64 vs data URI 前缀"。库给的是**裸 base64**
（`data:image/png;base64,` 前缀只在发给 LLM 时才加），所以代码里显式判断并剥前缀 ——
成本一行，收益是"哪天库改了，不会静默写出一个损坏的 PNG 直到打开报告才发现"。

**证据在哪**：`tests/test_browser_contract.py`（S5/S6 的守卫）；
`docs/spikes.md` 的 S5、S6 两节（含当时的实际输出）；
`tests/test_compat.py::test_history_accessors_we_depend_on`。

---

## ADR 10 · 为什么不做 LLM 修复解析失败的结果

**决定**：四档容错 —— ① LLM 侧自动重试（`done` 的参数是 pydantic 模型，不合 schema 时
库自己的校验就会让模型重试，白捡）；② 字段级**确定性**清洗（`"¥12.00"` → `Decimal("12.00")`，
可枚举可单测）；③ run 级重试（**必须新建 BrowserSession**）；④ 拒绝入库 + quarantine。
**明确不做第五档：让 LLM 去修解析失败的结果。**

**为什么不做**：修复需要第二次 LLM 调用。真正的问题是 —— **修复后的值由谁负责？**
如果修复"成功"，库里存的是一个**经过 LLM 二次加工**的值，而它和页面上真实的值
是什么关系，**没有任何人知道**。对一个电商卖家后台的数据，这比"没采到"严重得多：
卖家可能拿着这个数去做补货决策。所以：解析失败 = 这个 run 失败，
raw 原文留着、报告里标红、让人去看。

**代价**：成功率会低一些（有些本来"救一救还能用"的数据直接判失败），而且需要人来看。
这是**有意接受**的成本，不是没优化。

**证据在哪**：`tests/test_runner_offline.py::test_classify_parse_status`、
`test_should_retry_ok_never_and_schema_invalid_always`（重试策略的白名单：只有特定
`parse_status` 才重试 —— 撞护栏那种 `blocked` **不重试**，因为重试只会再撞一次，纯烧 token）、
`test_extract_structured_catches_validation_error_as_quarantine`。

---

## ADR 11 · 为什么 quarantine 整体拒绝，而不是部分插入；为什么 `suspicious` 只标记不删除

**决定**：一次 run 的产物**要么整批入库、要么整批不入**（`parse_status='schema_invalid'`，
`products` 表零行，raw 原文保留）。已经入库的行里，`price <= 0` / `stock < 0` /
`title` 空 / `goods_id` 非数字 → 打 `suspicious=1`，**只标记，不删除**。

**为什么整体拒绝**：部分插入会让"这个 run 的数据全不全"变成一个**必须先看
`parse_status` 才知道**的问题 —— 而下游查询不会去看。宁可能力小一点，
也不产生一张"看起来完整、实际缺行"的表。

**为什么只标记不删除**：删掉就**再也看不到"LLM 出错的方式"**，而那恰恰是最该看的东西
（它告诉你下一条规则该加在哪）。`suspicious` 是给人和报告看的信号。

**代价**：
- 表里**会存在可疑数据**，所以下游必须知道这一列 —— 不知道就会照单全收。
- 整体拒绝意味着**一条坏行能废掉整个 run 的入库**。这个交换是有意的：
  "整个 run 失败"是响的，"少了几行"是哑的。

**证据在哪**：`ecom_agent/store/schema.sql`（`parse_status` 与 `suspicious` 两列）、
`ecom_agent/store/repository.py:336`（`suspicious` 由 flags 推出，不是人手填的）、
`ecom_agent/sites/pinduoduo/output_models.py:85`（`sanity_flags()`）；
`tests/test_runner_offline.py::test_extract_structured_catches_validation_error_as_quarantine`
（整体拒绝那条）。

⚠️ **这里有一条诚实缺口，写 ADR 时核出来的**：`sanity_flags()` / `_detect_pii()` /
`pii_flags` / `suspicious` **目前一条测试都没有**（全库 grep 只在 tests 里命中过一段
不相干的 SQL fixture）。也就是说：**"可疑数据只标记不删除"这条原则，代码里有、
测试里没有**。按本文档开头的第三条规矩，这里写"不知道"而不是写一个漂亮的名字 ——
它已经进了 README 的「这个项目**没做到**的事」，补测试是明确待办。

---

## ADR 12 · 为什么前端是原生单页，而不是 Streamlit

**决定**：FastAPI + 原生 HTML/CSS/JS 单页，**无构建步骤**。同一份页面加个 `?run_id=`
就变成历史回放视图（不是另做一套渲染）。

**为什么**：Streamlit 的 **rerun 模型**与这个 UI 的核心交互是**冲突**的 ——
核心交互是"实时滚动 + 一个长驻的审批卡片"，而点"批准/拒绝"要唤醒一个**正在 await
的 `asyncio.Event`**。Streamlit 每次交互重跑整个脚本的模型，恰好会把"长驻的等待"
这件事做得很别扭。原生单页零依赖零构建，且没有这层阻抗。

**代价**：
- 前端得手写：样式、SSE 处理、渲染都要自己来。这不是小成本。
- **渲染点必须和后端通道一条条对齐** —— 本项目最贵的那类缺陷就出在这里
  （"前端读一个字段、某个通道从没发过它"，一晚上出现过四次）。
  ADR 12 的代价和 ADR 16 的"8 个通道"是同一件事的两个说法。

**证据在哪**：`webapp/app.py`、`webapp/static/index.html`；`tests/test_api.py`；
`docs/spikes.md` 的「Phase 5 探路结论」（五条，其中事实 1 就是这个家族的四个实例）。

---

## ADR 13 · `compat.py` + 版本哨兵：把未文档化依赖隔离成**可枚举、可测试**的东西

**决定**：所有对 browser-use **未文档化行为**的依赖，只允许经由 `ecom_agent/compat.py`
这一个入口；配 `tests/test_compat.py` 当版本哨兵，升级时**主动失败**并报出"哪几个假设被破坏了"。

**为什么**：这个项目至少压在四处未文档化行为上 —— ① `register_new_step_callback` 的时序
保证；② 结构化输出必须绕私有字段（ADR 9）；③ `total_duration_seconds` 是**方法**不是 property；
④ `Tools.act` 的 action 超时。这些散落在各处的话，一次 minor 升级就是一场**全库考古**：
你得先把它们一个个找出来，才知道自己踩了什么。

**代价与这套做法的上限**：
- 多一层间接 —— 改一处得先想到"这属于 compat 吗"。
- ⚠️ **哨兵只能抓签名变化，抓不住语义变化**：签名一样、行为变了，测试照绿。
  这是这套做法的**真实上限**，写在这里而不是留给人自己发现。
  补上它的办法是行为测试（`tests/test_browser_contract.py` 那类）——

**证据在哪**：`ecom_agent/compat.py`；`tests/test_compat.py`，其中
`test_version_pinned`（精确锁 `0.13.10`）、`test_special_param_names_are_reserved`
（9 个保留参数名一个都不能用）、`test_total_duration_seconds_is_a_method_not_a_property`、
`test_history_accessors_we_depend_on`；行为侧在 `tests/test_browser_contract.py`。

---

## ADR 14 · 为什么不做反检测（这是一个判断，不是能力缺失）

**决定**：不写任何反检测 / 拟人化 / 验证码绕过代码 —— 包括延迟随机化、浏览器指纹伪装、
UA 伪造。**这是明确的选择，不是做不到。**

**为什么**：
1. **合规判断**：把 RPA 伪装得更像真人，从合规角度**反而更危险**。深圳某科技公司案的
   争议焦点正是这类"看起来像真人"的行为。
2. **这条路本来就有正当走法**：阿里云 RPA（码栈）自己在卖千牛工具并由阿里云开票；
   淘宝 2023 年的规则明说 RPA 属于**提效工具**、不属于外挂。既然有明路，没有理由往灰处走。
3. **我们做的是卖家自己的账号、自己的数据**。"需要伪装"这个前提本身就不成立。

**代价**：
- 真站点可能因风控失败 —— 所以交付物是**双轨**的：主交付是 mock 站点的可复现 e2e，
  真站点跑通是加分证明而不是必要条件（R2）。处置写死为"**失败不恋战**"。
- 放弃了"更高成功率"这个诱惑。有些场景确实会因为不伪装而跑不通 —— 那是接受的损失。

**证据在哪**：这条的证据是**搜不到** —— 全库搜
`stealth|undetected|fingerprint|human_like|拟人|绕过验证码`，唯一命中的是
`task_fingerprint`（本项目自己的**可复现指纹**，与浏览器指纹无关）。
另有 README 的安全边界一节、`docs/guardrail_design.md`。

---

## ADR 15 · 为什么 mock 站点不复刻真实站点的 class 名

**决定**：mock 卖家后台的 HTML **语义**模仿真实后台，但**不复制** class 名。
每个页面带 `<html data-mock-version="1">`，e2e **断言它存在**。

**为什么**：
1. 真实站点的 class 是**构建产物 hash**，会随发版变。复刻它等于给自己埋一个
   **必然过期**的断言 —— 而且过期的方式是"测试红了但产品没坏"，最消耗信任的那种红。
2. 更根本的：mock 要测的是**我们的 DSL / 护栏 / 观测链路**，不是"拼多多 DOM 稳不稳定"。
   这两件事必须分开，否则 mock 绿了会被读成"真站点一定行"。
3. `data-mock-version` 的第二个作用更重要：它让 e2e **自证它测的是 mock 而不是真站点** ——
   把对照实验的思想用在了测试环境**自身**上。

**代价**：
- mock **不能**用来回归"真站点 DOM 变了没有"。那件事靠 `devtools/spike_*` 人工探路 +
  DSL 里的**语义化提示**（比如"分页条最右侧的下一页"，不是 CSS 选择器）。
- 真实页面与 mock 的差异会让某些缺陷**只在真站点出现**。首跑就撞到一个：
  "登录态失效"和"店里真的没数据"在产物里**长得一模一样**（后来补了 `login_state` 四值可分）。

**证据在哪**：`devtools/mock_pdd/pages.py`（`data-mock-version` 在整份代码里**只出现一处**，
共用一个外壳）；`tests/test_mock_pdd.py`、`tests/test_mock_pdd_e2e.py`
（后者断言 `data-mock-version` 存在）；`docs/spikes.md` 的「Phase 6 收尾」一节（登录态那条）。

---

## ADR 16 · 为什么 `products` 表只记店铺自己的商品 —— 以及一个动作交付了、却没有配套模板

**决定**：交付 `ecom_agent/actions/extract_cards.py`（卡片列表的确定性读取器），
**但推迟** `tasks/pdd_chance_goods.yaml`（机会商品页的任务模板）。机会商品页的行
**不进** `products` 表。

**为什么**：追着"那 35 张卡去哪儿"这条线，追出来的是**语义问题，不是形状问题**：

| | `products` 表 | 机会商品页的行 |
|---|---|---|
| 是什么 | **这个店铺自己的商品** | **平台推荐给这个店铺的商品** |
| 有 goods_id / 库存 / 状态吗 | 五列都有，且 NOT NULL | **页面本来就没有** |
| 证据 | 五列 + `goods_id_not_numeric`、"库存 10 万件以上多半是把销量填进了库存" 这类 sanity 判定，全是店铺商品语义 | S8 实测：35 张卡，四行文本，无 ID/库存/状态 |

两条都不通：**硬塞**进去会让"这个店有多少商品"从此答错（模型还会被逼着编一个
商品 ID 和库存出来 —— 正是本项目拒绝的那种失败）；给表加
`row_kind + extra_json` 等于把表的语义**偷偷改宽**，更糟。

**代价 / 为什么也没做"让它可见"**：要让"某任务的行不进表"这件事**被看见**，
得穿过 **8 个通道**（`run.json` / 报告 / `runs` 表 / CLI / SSE 实时载荷 / 回放载荷 /
`/api/runs` / 三个前端渲染器）。这是本库**最贵**的那类改动，而换回来的是计划里
写明"**不是必要条件**"的加分项。

→ 所以处置是：**交动作（技术产出），不交模板（模板只是动作的声明方式）**。动作本身
是有价值的（`extract_table` 在那一页读 0 行是它只认表格，不是 bug —— 那个 0 行
现在是一次**测量**），而模板所隐含的"这些行进 products 表"是错的。

**证据在哪**：`docs/spikes.md` 的 S8 一节（35 张卡、整页 `<table>` 数 0、
以及第一版探测器把 **30 个行内 `<script>`** 判成"最大重复块"那次翻车）；
`ecom_agent/actions/extract_cards.py`；`tests/test_extract_cards.py`（离线 27 条）+
`tests/test_extract_cards_dom.py`（真 DOM 4 条，含 SKIP 过滤的对照组）。
