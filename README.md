# ecom-agent

**基于 [browser-use](https://github.com/browser-use/browser-use) 二次开发的电商卖家后台自动化助手。**

不改动 browser-use 源码 —— 把它当执行引擎，上层包自己的业务逻辑：任务 DSL、三层安全护栏、
结构化落库、逐步可观测与回放。首个适配平台是拼多多商家后台（`mms.pinduoduo.com`）。

> 🚧 施工中。当前进度：**Phase 0（骨架 + CI）**。
> 各阶段验收标准见 `docs/`（Phase 1 起陆续补齐）。

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
uv run pytest             # 全离线，零 token，应当全绿
```

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

另外，**公开演示用的全部数据来自本地 mock 站点**（`devtools/mock_pdd/`），
仓库里不含任何真实店铺数据。

---

## 架构决策记录（ADR）

面试主战场。每条都基于**源码核实过的事实**，不是文档推测 ——
网上关于 browser-use 0.13.x 的教程大半是过期的（`Controller`→`Tools`、
`Browser`→`BrowserSession`、`max_steps` 从构造函数搬到了 `run()`）。

*Phase 7 定稿，此处先列目录：*

1. 为什么基于 browser-use 二次开发，而不是自研 CDP 驱动
2. 为什么零 VLM，以及这条在什么情况下会失效
3. 为什么任务用 YAML DSL 而不是自然语言提示词
4. 为什么护栏必须自己造：三层防御各自能挡什么、**挡不住什么**
5. 拦截点为什么选未文档化的 `register_new_step_callback`
6. 为什么规则聚合用「最严优先」而不是「先匹配先赢」
7. 为什么 `default_decision` 默认是 `confirm` 而不是 `allow`
8. 为什么用持久 profile 而不是 `sensitive_data` 占位符替换
9. 为什么截图必须自己另存；为什么用 `get_structured_output(Model)`
10. 为什么不做 LLM 修复解析失败的结果
11. 为什么 quarantine 整体拒绝；为什么 suspicious 只标记不删除
12. 为什么前端是原生单页而不是 Streamlit
13. `compat.py` + 版本哨兵：把未文档化依赖隔离成可测试的东西
14. 为什么不做反检测
15. 为什么 mock 站点不复制真实站点的 class 名

---

## 测试分层

```bash
uv run pytest                      # 离线：DSL / 护栏 / 脱敏 / 落库 / 报告 / API。零 token 零网络
uv run pytest -m needs_browser     # 需要真实浏览器：对着本地 mock 站点跑
```

**本地 `needs_browser` 是硬门禁，CI 上是尽力跑**（`continue-on-error`）。
理由：CI 里启动 Chromium 依赖 runner 沙箱、Chrome 版本、一组 apt 包，
任一环变化都会红 —— 但那是环境问题不是代码问题。混在一起会让两者看起来一样，
久而久之没人看 CI。分开之后，"代码坏了"和"CI 机器没配好"一眼可辨。
