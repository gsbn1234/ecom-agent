# ecom-agent

**基于 [browser-use](https://github.com/browser-use/browser-use) 二次开发的电商卖家后台自动化助手。**

不改动 browser-use 源码 —— 把它当执行引擎，上层包自己的业务逻辑：任务 DSL、三层安全护栏、
结构化落库、逐步可观测与回放。首个适配平台是拼多多商家后台（`mms.pinduoduo.com`）。

> 🚧 施工中。当前进度：**Phase 0–6 已完成**（骨架 / DSL + 护栏策略 / 真浏览器探路 /
> 可观测与落库 / mock 站点 e2e + CI / Web 看板 / 真站点首跑）；
> **Phase 7 进行中** —— ADR 已定稿，「护栏实战」改了口径（用**可重跑**的 mock e2e 证据，
> 不对真站点做写操作）。
>
> 文档：[`docs/ADR.md`](docs/ADR.md)（**16 条架构决策，面试主战场**）、
> [`docs/spikes.md`](docs/spikes.md)（探路实测结论 + 逐条判据 + CI 排查全程）、
> [`docs/guardrail_design.md`](docs/guardrail_design.md)（三层护栏各能挡什么、**挡不住什么**）。
>
> 测试现状：**411 条 = 394 条离线（CI 硬门禁）+ 17 条 `needs_browser`（本地硬门禁）**。
> 最近一次推送（`68b1edb` = CI #31）两个 job 全绿，且浏览器 job 带的是**真绿**注解：
> `needs_browser 实际结果：17 passed, 394 deselected ；退出码 0` + `探针结论：环境可用`。
>
> ⚠️ 上面这几个数字是 `uv run pytest --collect-only` 数出来的，**不是** `uv run pytest` ——
> 后者默认**包含** `needs_browser`。这个位置**已经因为同一个原因错过两次**，所以把
> "怎么数的"写在这里，而不是指望下次还记得。

---

## 这个项目解决什么

卖家后台的日常运营里有一大类**重复、只读、但必须人工点**的活：查在售商品的价格库存、
盯竞品调价、核对某个状态下的商品清单。这类操作的特点是——**逻辑简单到不值得写死选择器，
但页面一改版选择器就全废**。

browser-use 这类 LLM 驱动的浏览器 Agent 正好补上这一环：它读的是无障碍树/DOM 语义，
不是 CSS 选择器，页面换个 class 名它照样认得。但它缺三样东西，正是本项目补的：

| 缺什么 | 本项目的做法 |
|---|---|
| 任务定义散在 prompt 里，没法 review、没法回归 | **YAML 任务 DSL**：每个字段有独立的强制点 |
| 没有人工确认机制（0.13.10 全库零命中） | **三层护栏** + 网页审批，默认拒绝而非默认放行 |
| 跑完只返回一段文字，无法追溯 | **逐步落盘**：截图 + 操作日志 + 决策记录，可回放 |

---

## 快速开始

```bash
git clone https://github.com/gsbn1234/ecom-agent.git
cd ecom-agent
uv venv && uv pip install -e ".[dev]"

cp .env.example .env      # 填 DEEPSEEK_API_KEY
uv run pytest -m "not needs_browser"   # 离线 394 条，零 token 零网络，应当全绿
uv run pytest                          # 全部 411 条（含 17 条真浏览器，会真的开 Chrome）
```

> ⚠️ **`uv run pytest` 默认包含 `needs_browser`** —— 想"零 token 零网络"地跑一遍，
> 必须显式加 `-m "not needs_browser"`。这不是排版讲究：不加的话，`pytest` 会去启动
> 真浏览器，而本机没配好 Chrome 时它**报的错和"代码写错了"长得一模一样**。
> 这个项目的每个命令都尽量让它"默认行为 == 你以为的行为"，这一条是例外，所以写在这里。

跑通一个只读任务（需要真实 key）：

```bash
uv run python main.py run tasks/books_demo.yaml    # 用公开练手站点，不碰任何真实后台
```

---

## 安全边界（先读这段）

本项目**只做卖家自己账号、自己数据、只读优先**的自动化。明确不做的事：

- ❌ **不导出明文买家个人信息**（个人信息保护法 / 刑法 253 条之一）
- ❌ **不使用他人账号**（有真实判例）
- ❌ **不写任何反检测、拟人化、验证码绕过代码**——这不是能力问题，是合规判断。
  把 RPA 伪装得更像真人，从合规角度反而更危险

⚠️ 还有一条**能力**边界（不是选择，是限制，所以单独说）：**本项目零 VLM** ——
模型只读 DOM 语义文本，**不看图**。因此 canvas 渲染的图表、纯图片按钮、
"看图才知道是什么"的元素**做不了**；验证码页只可能被判成"需要人工"。
换来的是：不烧视觉 token，且截图照样落盘当审计证据（库里 `use_vision=False`
时截图仍会采集）。取舍过程见 [ADR 2](docs/ADR.md)。

另外，**公开演示用的全部数据来自本地 mock 站点**（`devtools/mock_pdd/`），
仓库里不含任何真实店铺数据。

护栏怎么做到上面这些、以及**它做不到哪些**，见
[`docs/guardrail_design.md`](docs/guardrail_design.md) ——
那份文档的规矩是「挡不住什么」比「挡什么」重要，每层都写了失效边界和实测依据。

---

## 架构决策记录（ADR）

面试主战场。每条都基于**源码核实过的事实**，不是文档推测 ——
网上关于 browser-use 0.13.x 的教程大半是过期的（`Controller`→`Tools`、
`Browser`→`BrowserSession`、`max_steps` 从构造函数搬到了 `run()`）。

**全文在 [`docs/ADR.md`](docs/ADR.md)**（16 条，每条四段：**决定 / 为什么 /
代价与被否掉的选项 / 证据在哪**）。下面只是精编版，方便扫 —— 编号与全文一致。

**为什么这么选**

| # | 决定 | 一句话 |
|---|---|---|
| 1 | 基于 browser-use 二次开发，不改库源码 | 页面一改版，写死的选择器就全废；代价是依赖压在**未文档化**行为上 → ADR 13 |
| 2 | **零 VLM**：模型只读 DOM 语义文本 | 文本就够用，且截图照样落盘当审计证据；代价：canvas / 图片按钮做不了 |
| 3 | 任务用 YAML DSL | 每个字段有**独立**强制点；而自然语言提示词的强制点只有一个：LLM 愿不愿意听 |
| 12 | 前端原生单页，不用 Streamlit | 长驻审批按钮 + SSE 与 Streamlit 的 rerun 模型冲突，而那正是本 UI 的核心交互 |
| 13 | `compat.py` + 版本哨兵 | 把未文档化的依赖关进一个文件、变成可枚举可测试的；⚠️ 它只抓**签名**，抓不住**语义**变化 |
| 15 | mock 不复刻真实站点的 class 名 | 真实 class 是构建 hash，复刻它 = 埋一个必然过期的断言 |

**护栏**

| # | 决定 | 一句话 |
|---|---|---|
| 4 | 三层护栏自己造 | 0.13.10 **全库零命中 HITL**；三层各自写清"挡不住什么"，证据先给**对照组** |
| 5 | 拦截点选未文档化的 `register_new_step_callback` | 能 await 人工、**不受 180s action 超时约束**、且库自己也这么改写 action |
| 6 | 规则聚合「最严优先」（block > confirm > allow） | 顺序无关 → 不能靠调 YAML 顺序绕过；"先匹配先赢"的失效是**静默**的 |
| 7 | `default_decision` 默认 `confirm` 而不是 `allow` | LLM 的失败模式正是"做了你没预料到的事"；代价：**这个系统需要有人在场** |
| 14 | 不做反检测 / 拟人化 / 验证码绕过 | 这是**合规判断**，不是能力缺失 —— 把 RPA 伪装得更像真人，合规上反而更危险 |

**数据与可观测**

| # | 决定 | 一句话 |
|---|---|---|
| 8 | 持久 profile，不用 `sensitive_data` 占位符 | 密码根本不进 Agent，也不用脱敏；代价：三个实测坑（库会偷偷把 profile 拷到 `%TEMP%` 等） |
| 9 | 截图自己另存 + `get_structured_output(Model)` | 库的截图在临时目录、关机即失；`.structured_output` 存盘读回**永远 None 且不报错** |
| 10 | 不做 LLM 修复解析失败的结果 | **修复后的值由谁负责？** 对卖家数据，这比"没采到"严重 |
| 11 | quarantine 整体拒绝；`suspicious` 只标记不删除 | "少几行"是哑的，"整个 run 失败"是响的；删掉标记就再也看不到"LLM 出错的方式" |
| 16 | 交付 `extract_cards` 动作，**推迟**机会商品模板 | `products` 表记的是**这个店铺自己的商品**；硬塞会让"这个店有多少商品"从此答错 |

---

## 测试分层

```bash
uv run pytest -m "not needs_browser"   # 394 条离线：DSL / 护栏 / 脱敏 / 落库 / 报告 / API
uv run pytest -m needs_browser         # 17 条：需要真实浏览器，对着本地 mock 站点跑
uv run pytest                          # 411 条全部 = 394 + 17（默认就包含 needs_browser）
```

> 分层的主轴不是"快慢"，是**依赖什么**：纯逻辑 → 临时文件 → asyncio → 真浏览器。
> 每一层的判据都不一样，混在一起就会把"环境没配好"和"代码写错了"读成同一件事
> （CI 上那一节就是这个道理的第二次应用）。

**本地 `needs_browser` 是硬门禁；CI 上是「有分辨地跑」，不是「尽力跑」**。
理由：CI 里启动真实 Chromium 依赖 runner 沙箱、Chrome 版本、一组 apt 包，
任一环变化都会红 —— 但那是**环境**问题，不是**代码**问题。混在一起，两种失败长得一样，
久而久之没人看 CI。

★ 这里改过一次姿态（2026-09-17），方向值得单独说：原来这个 job 是 **job 级**
`continue-on-error: true`，于是它**永远不会红** —— 而那不等于"容忍偶尔失败"，
等于**这个 job 的结论不再携带任何信息**（浏览器测试挂了两周，和一直绿，在页面上
长得一模一样）。改法不是把开关删掉（那会变成"CI 经常红"，而老是红的 CI 比老是绿的
更糟，因为它更吵、同样会被忽略），而是**分辨**：

| 情况 | 结果 |
|---|---|
| 环境不可用 + 测试红 | warning 注解 + 绿（放行） |
| 环境可用 + 测试红 | `::error::` + **真红** |
| 测试绿 | 绿 |

做法：`continue-on-error` 从 **job 级降到步骤级**（只给"可能因环境而失败"的那两步），
最后加一个 **gate 步骤** —— 它自己**没有**这个开关，所以 job 的颜色由它决定。
判别逻辑在 `devtools/ci_gate.py`，真值表 + 两条对照实验在 `tests/test_ci_gate.py`。
⚠️ 把它写成脚本而不是几行 shell，是因为它是一条**有真假**的判断：shell 版只能靠
"往 main 推一次坏提交看颜色"来验，不可复现，而且**验不了反面**
（没法在 CI 上故意让环境坏掉）。
⚠️ 这个机制唯一能把红洗成绿的地方就是 gate 判"环境不可用"，所以那条判据刻意写得
**很窄**（两条启动路径都起不来才算），且结论缺失一律按"可用"处理 → 真红。

CI 上**显式钉死二进制**：`ECOM_AGENT_CHROME_PATH=/usr/bin/google-chrome`。
不是随手写的路径，而是踩过一轮全红之后定的，原因值得单独说 ——

**browser-use 0.13.10 内部有两份互不一致的清单回答「哪个 Chrome」**：

| 用在哪 | 位置 | 策略 |
|---|---|---|
| 库**真正启动**用的那条 | `local_browser_watchdog.py:264-279` | 硬编码路径表，**chromium 组优先** |
| 同库另一个公开函数 | `browser/chrome.py:find_chrome_executable()` | 走 `which`，**google-chrome 优先** |

正常机器上两者挑中的是同一个二进制，所以这个分歧看不出来。一旦两个都装了、
而其中一个的沙箱助手不可用，差别就是「CDP 就绪」和「SIGABRT，退出码 -6」——
GitHub runner 恰好就是这种情况，而 Chromium 那边给出的死因是：

```
FATAL:content/browser/zygote_host/zygote_host_impl_linux.cc:129] No usable sandbox!
```

**修法是钉一个带可用沙箱的二进制，而不是 `chromium_sandbox=False` 关掉沙箱。**
关沙箱同样能让 CI 变绿（`devtools/probe_browser.py` 的对照段在 runner 上验证过），
但那是以降低安全姿态换绿色 —— 对一个通篇在讲护栏的项目，这是本末倒置。

完整排查记录（含逐字证据、为什么 `tail` 对崩溃转储是反向的、
以及「探针必须和被测对象共享同一份环境」这条教训）见 [`docs/spikes.md`](docs/spikes.md)。
诊断脚本本身是 `devtools/probe_browser.py`，本地也能跑。

