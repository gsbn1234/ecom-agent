# Phase 2 探路结论（真浏览器，零 token，零联网）

七个 spike 全部 `OK`。脚本在 `devtools/spike_*.py`，共用夹具在 `devtools/spike_lib.py`。

```bash
uv run python devtools/spike_s1_callback_rewrite.py     # S1
uv run python devtools/spike_s2_element_text.py         # S2
uv run python devtools/spike_s3_domain_block.py         # S3
uv run python devtools/spike_s4_fake_llm.py             # S4
uv run python devtools/spike_s5_screenshot_paths.py     # S5
uv run python devtools/spike_s6_structured_output.py    # S6
uv run python devtools/spike_s7_localhost_allowlist.py  # S7
```

---

## 这份文档的规矩

1. **判据写成 `assert`，不写成注释。** 注释会和实现漂移，断言不会。
   每个 spike 的成功条件都在代码里，失败时打 `SPIKE_XXX_FAIL: <原因>`。
2. **每条结论必须附「看到的输出」**，不是「应该可以」。
3. **每条结论必须写「它在什么情况下就不成立」**——否则它半年后会变成一条没人敢动的传说。
4. **尽量带对照组。** 只证明「A 能用」是没有信息量的：护栏没生效、白名单没生效、
   网络根本不通，都会让「A 能用」为真。所以本地站点是**同一个服务器、同一个端口、
   两个主机名**（`127.0.0.1` 当允许、`localhost` 当禁止），把唯一的变量隔离出来。

> 本文档里有 **4 条计划外的发现**（S2 一条、S2 附带一条、S5 一条、S6 一条）。
> 它们都不是「探到了就能写代码」那种结论，而是**改变了设计**的结论。
> spike 的价值主要在这里——已验证的东西本来就在计划里，意外才是跑它的理由。

★ 本文档是**怎么知道的**；[`guardrail_design.md`](guardrail_design.md) 是**据此定了什么**。
  两边共用同一组事实：S3 的「拦下导航会留空白页」在那边是「Layer 0 只能当最后一道网」，
  S2-4 的「快照被原地清空」在那边是「拦截器必须当场取值」。
  判据的失效条件也汇总在那份文档的末尾，两边一起改。

---

## 汇总

| # | 探什么 | 结论 | 对设计的影响 |
|---|---|---|---|
| S1 | `register_new_step_callback` 被调用？改写动作生效？ | ✅ 全部成立 | **R1 退役**，护栏首选机制确定 |
| S2 | `get_meaningful_text_for_llm()` 与 LLM 所见一致？ | ✅ 一致，但**快照会被原地清空** | **R5 退役**；拦截器必须当场取文本（**计划外**） |
| S3 | `allowed_domains` 拦不拦得住 | ✅ 拦得住，但**被拦后页面变空白** | Layer 0 只能当最后一道网 |
| S4 | 零继承的假 LLM 能被接受？ | ✅ 成立；且**一次 run ≠ N 次调用** | 成本模型改写（**计划外**） |
| S5 | 截图两条路都能拿到真 PNG？ | ✅ 都能，且两路一致 | 审计必须自己另存 |
| S6 | 结构化输出存盘往返 | ✅ getter 可用，property 静默变 None | 落库/报告统一走 getter |
| S7 | `["127.0.0.1"]` 能匹配 `http://127.0.0.1:PORT`？ | ✅ 能 | mock 站点 `e2e` 方案成立 |

---

## S1 —— 在回调里改写 LLM 的动作（最高风险项 R1）

**探什么**：护栏的「人工拒绝后让 LLM 改道」依赖三件事同时成立——
回调真的会被调用、回调里改写 `agent_output.action` 真的生效、`await` 期间浏览器还活着。

**判据**：不能是「回调被调用了」（被调用但改写无效，护栏就是个安慰剂）。
判据是**最终 URL 落在被改写到的那个页面上**：LLM 脚本说去 `/`，回调把它换成 `/page2`，
如果只有回调没生效，URL 会是 `/`。两条路径唯一可分。

```
回调调用次数: 2
   step=1 actions=[{'navigate': {'url': 'http://127.0.0.1:24982/'}}]
   step=2 actions=[{'done': {'text': '结束', 'success': True}}]
被改写的步数: 1
history.urls(): ['about:blank', 'http://127.0.0.1:24982/page2']
心跳在这期间跳了: 73 次
SPIKE_S1_OK
```

**结论**：

1. 回调被调用，且在 **LLM 输出之后、action 执行之前**（与源码事实 5 一致）。
2. 改写生效：`/page2` 不是脚本要求的地址，只可能来自改写。
3. 事件循环在回调执行期间照常转（73 次心跳）→ 回调里 `await` 人工审批**不会**掐断 CDP，
   也**不受** `Tools.act` 的 180s 超时约束（事实 20）。R3 就此绕开。

**⚠️ 附带踩坑（已变成 S1 的第二条判据）**：第一版回调是**无条件**改写的，
于是 `done` 也被换成 `navigate`，run 永远结束不了，靠脚本耗尽才收场——
而「断言 1」（URL 里有 `/page2`）**照样通过**。

> 一条会放过「把任务搞死」的断言，等于没断言。
> 拦截器必须**只改策略判定要改的动作**；「顺手全改一遍」看着更安全，
> 实际是让 agent 再也完不成任务。所以 S1 现在断言 `被改写的步数 == 1`
> 且 `done` 原样穿过（`final_result() == "结束"`）。

**什么情况下不成立**：库改掉 `register_new_step_callback` 的调用点，或改成传值拷贝。
`compat.py` 的哨兵 + 本 spike 会同时红。

---

## S2 —— 元素文本：护栏 `match_element_text` 的来源

**探什么**：`match_element_text` 规则（`删除|批量删除|下架|…`）是按**文本**判定的。
拿不到点击目标的可读文本，护栏就两种坏法，且**都不会自己发出声音**：

- 规则永不命中 → 护栏静默失效，测试还是绿的（没规则命中不是错误）
- 命中的是 LLM 根本看不见的东西 → 误拦，LLM 反复改道、任务卡死

**判据**：刻意**不用**计划里原本写的「打表肉眼一致」——肉眼一致没有证伪力，
表里二十行，人眼只会去挑自己期待的那几行看。换成四条可执行断言。

### 判据 1：取值优先级

探针页上每个元素只填**一种**文本来源，所以「谁赢了」是可证伪的。

```
── index → 可读文本 ──
  [ 2] '仅占位符'        <- <input placeholder>
  [ 3] '仅aria标签'      <- <input aria-label placeholder="不该赢">   aria-label 赢了
  [ 4] '仅title属性'     <- <input title>
  [ 5] '仅value值'       <- <input value placeholder="该输给value">   value 赢了
  [25] '搜索'            <- <button>
  [28] '批量删除'
  [31] '立即支付'
  [34] '编辑'
  [37] '嵌套在span里的下架'  <- <div role=button><span>…</span></div> 兜底路径通了
  [41] '第二页'          <- <a>
```

优先级实测与 `dom/views.py:616` 的列表一致：`value → aria-label → title → placeholder → alt → 子文本`。

`img` 的 `alt` **没有**进 `selector_map`（`False`）——不影响护栏（护栏只判点击目标），
所以只报告不断言。

### 判据 2：与 LLM 所见一致

docstring 的原话是 *"matches exactly what goes into the DOMTreeSerializer output"*。实测成立。

> **⚠️ 附带踩坑（我自己的解析错了，不是库错了）**：序列化结果是**每个元素占多行**的——
>
> ```
> [25]<button />
> 	搜索
> ```
>
> 子元素文本放在**下一行的缩进**里，而不是标签中间。第一版我按「一个索引 = 一行」匹配，
> 于是每个按钮都判成「文本对不上」。
> **假警报和漏报一样费时间，而且更打击人：它会让你去改一个本来正确的东西。**

反向探针也做了：**没有**「可读文本为空、但 LLM 看得到文本」的元素（那是最危险的一类）。

### 判据 3：接到真策略上（这条才是最终价值）

用 `tasks/pdd_search_products.yaml` 编译出的**真 policy** 去判探针页的**真实元素**：

```
── 真策略对这一页的判定 ──
  批量删除: block    rule=block-destructive
  立即支付: confirm  rule=confirm-money
  编辑:     confirm  rule=confirm-edit
  搜索:     confirm  rule=<default>
```

「索引 → 文本 → 决策」整条链是通的。

**顺带一条对 Phase 4 有用的发现**：只读的「搜索」在 mock 主机上落到了 `default_decision=confirm`，
因为 `allow-readonly` 规则带 `match_url: "*mms.pinduoduo.com/*"`，本站不匹配。
这不是 bug——是「规则集是按真实站点写的」的必然结果，也正是计划里要单独有
`platform/mock.yaml` 的原因。**提前看到，Phase 4 就不会卡在「e2e 里每点一次搜索都要人工审批」。**

### 判据 4：⚠️ 计划外发现 —— 快照不是不可变的

**这是 S2 自己踩出来的，也是这次探路里最危险的一条。**

第一版把 `browser_state` 对象存起来、跑完再读 `selector_map` —— 读出来是**空的**。
我一度以为库有 bug，查下去发现机制很干净：

```
── 判据 4：kill 前 selector_map=10，kill 后=0 ──
   dom_state 是同一个对象: True；dict 是同一个对象: True
   它与会话内部缓存 _cached_selector_map 是同一个 dict: True
── 判据 4b：keep_alive=False，run() 返回时 selector_map=0 ──
   两轮抓到的文本条数：10 vs 10（应相同）
```

- `BrowserSession.update_cached_selector_map()` **直接赋值、没有拷贝**（`session.py:2494`），
  于是快照里的 `selector_map` 和会话内部缓存**是同一个 dict 对象**。
- `BrowserSession.reset()` 对它调 `.clear()`（`session.py:664`）——**原地清空**。
- 而 `run()` 在 `keep_alive` 为假时**自己就会 reset**（事实 15）。
  即**默认配置下，`run()` 一返回，所有快照都已经是空的**。

**为什么这条比前三条更危险**：失败形态是**静默的**。
一个存了 `browser_state`、想在最后统一处理的地方（`RunRecorder` 很容易这么写），
会拿到空 `selector_map` → **没有任何规则命中** → 而「没有任何规则命中」是一个**合法结果**，
不报错、不告警、测试全绿。护栏看起来在跑，实际一步都没判。

**结论（已定死写法）**：拦截器/记录器必须在拿到 `browser_state` 的那**一刻**
把要用的东西取走，**不能存下对象晚点再读**。

> **⚠️ 这条判据的第一版是空转的。** 我直接读 `before`，打印出来是 `0/0`，断言恒不触发——
> 因为 `run()` 已经 reset 过了。**一条恒不触发的断言比没有断言更糟：
> 它会让读者以为这件事已经验过了。** 所以这一轮 capture 改用 `keep_alive=True`
> 才看到 `10 → 0` 这个过程，并补了判据 4b 对照 `keep_alive=False` 的情形。

**什么情况下不成立**：库改成赋值前 `dict(...)` 拷贝，或 `reset()` 改成重新赋值。
那时判据 4 的 `aliased` 断言会红，本文档和 `guardrail_design.md` 一起改。

---

## S3 —— Layer 0 的拦截效果，以及拦完之后页面在哪

**探什么**：计划里给的判据是「站外 navigate → `ActionResult` 含 `Navigation failed`，run 不崩」。
跑 S7 时发现那不够——被拦的导航会把标签留在一个**空白页**上。

```
每一步的 URL:
  step 0: about:blank
  step 1: http://127.0.0.1:18838/
  step 2: about:blank
  step 3: about:blank
错误:
   Navigation failed: Navigation to http://localhost:18838/page2 blocked by security policy

【关键发现】被拦之后的页面: 'about:blank'
SPIKE_S3_OK
```

**结论**：

1. 拦得住，且错误信息里能读出是**安全策略**拦的（不是网络失败）。run 不崩，走到了 `done`。
2. **被拦的导航把标签留在了空白页**（step 1 还是站点页，step 2 就变 `about:blank` 了）。

第 2 条决定了 Layer 0 的定位：**它是「最后一道网」，不是「拦下来之后继续干活」的机制。**
拦下一次导航 = 顺手弄丢当前页面 = 后面的提取全采到空。
**Layer 1 必须在动作执行【前】拦**——那样根本不产生导航，页面状态不会被破坏。

**什么情况下不成立**：库改成拦下来时不改变当前标签的 URL。

---

## S4 —— 零继承的假 LLM，以及「一次 run = 几次 LLM 调用」

```
FakeLLM.__mro__[1:] = ['object']
history.urls(): ['about:blank', 'http://127.0.0.1:15202/', 'http://127.0.0.1:15202/']
步进调用（脚本游标）次数: 3；耗尽: False

── 非步进 LLM 调用明细 ──
  <抽取类: output_format=None>: 1 次
  JudgementResult: 1 次
  合计: 步进 3 次 + 非步进 2 次 = 5 次
SPIKE_S4_OK
```

**结论 1**：纯鸭子类型成立（`__mro__` 里除 `object` 没有别的基类，MRO 写成断言是因为
「我没继承」这件事会被人无意改掉）。脚本真的驱动了浏览器（URL 历史里有站点地址）。

**结论 2（⚠️ 计划外）——成本模型**：**这个 run 只有 3 步，却发了 5 次 LLM 请求。**

- **judge**：每次 run 末尾固定 +1（`agent/service.py:1620`，`use_judge` 默认 `True`）
- **extract**：每调用一次 +1（`tools/service.py:1197`）——**extract 不是「读页面」，是问模型**

> 这直接改变了「采集为什么走结构化 `done` 而不是 `extract`」的论证：
> 不只是「更结构化」，也是**更省**。原先我以为「只读采集」是最便宜的任务，
> 实际上如果靠 `extract` 采数据，每次都要多付一次调用。

**⚠️ 附带踩坑**：第一版统计用 `type(output_format).__name__` 分组，judge 被归成了
`"ModelMetaclass"`——因为 `output_format` 是个**类**，而 `type(类)` 是它的元类。
**统计代码自己出错时不会报错，只会安静地把东西归到一栏你从没见过的名字下面。**

**什么情况下不成立**：`use_judge` 默认值改变，或 `extract` 改成不发 LLM 调用。

---

## S5 —— 截图两条路，都必须能拿到真 PNG

**为什么判「是不是真 PNG」而不是「有没有截图」**：「有没有」极易满足——
一段报错文本、一个转坏的 base64、一个 0 字节文件，都能让 `screenshot is not None` 成立。
而这些图是亮点 4 的最终交付物，**图坏了必须在这一层发现**。

```
── 路 A：new_step_callback 的 screenshot（base64）──
  step 1: None
  step 2: len(b64)=49164  解出 36871 字节  PNG=True  sha256=0db98ff5da9b
  step 3: len(b64)=49164  解出 36871 字节  PNG=True  sha256=0db98ff5da9b

── 路 B：on_step_end 的 history[-1].state.screenshot_path ──
  第 1 次钩子调用: 该步无截图路径
  第 2 次钩子调用: 36871 字节  PNG=True  sha256=0db98ff5da9b
      C:\Users\21702\AppData\Local\Temp\browser_use_agent_06aaaace-…\screenshots\step_2.png
  第 3 次钩子调用: 36871 字节  PNG=True  sha256=0db98ff5da9b
      C:\Users\21702\AppData\Local\Temp\browser_use_agent_06aaaace-…\screenshots\step_3.png

── 判据 3：两路的 sha256 集合对照 ──
  路 A: ['0db98ff5da9b', '0db98ff5da9b']
  路 B: ['0db98ff5da9b', '0db98ff5da9b']

── 判据 4：use_vision=False 下，图采集了但一张都没发给模型 ──
  采集到的截图数: 2；步进调用里的图片分片总数: 0

── 判据 5：库自己把图落在哪 ──
  …\Temp\browser_use_agent_…\screenshots\step_2.png
    在项目目录下？False   在系统临时目录下？True
SPIKE_S5_OK
```

**结论**：

1. 两条路都拿得到真 PNG（36871 字节，魔数正确）。两路的 sha256 集合**完全相同**。
   > 这条对照不做，两条路会各修各的 bug：回调那条一直好好的，钩子那条可能差一步。
   > **两边单测都绿，而报告里的图是错帧的。**
2. **`use_vision=False` 下截图照样采集，但一张都没发给模型**（图片分片总数 `0`）。
   这条把两件事分开了：**「采集了截图」不能推出「把截图发给了模型」**（ADR-2 的可执行证据）。
   合成一条的话，将来有人把 `use_vision` 打开，断言照样绿，而成本已经翻倍。
3. 库落的图**全在系统临时目录**（`agent_directory`，`service.py:448-449`），
   **不在项目目录下，关机即失**——审计必须自己另存。这条从「文档里写的一句要求」
   变成了一条会红的断言。
4. `history.screenshot_paths()` 给的是同一批路径。

**⚠️ 计划外发现：连续两步 sha 相同这个信号，单独用是错的**

计划里写「连续两步 sha 相同 → 报告里标注『页面未变化（可能是点击无效）』」。实测：

```
── 判据 7：画面变化 vs 动作名 ──
  step 1: 动作=['navigate'] 截图sha=<无>      错误=[]
  step 2: 动作=['scroll']   截图sha=0db98ff5da9b 错误=[]
  step 3: 动作=['done']     截图sha=0db98ff5da9b 错误=[]
```

step 2 与 step 3 的 sha 完全相同，而 step 2 的动作是 `scroll`——日志明写
`🔍 Scrolled down 1080px`，它**成功执行了**，只是这个 mock 页面比视口还短，滚不动，
画面自然没变。**所以「画面没变」在这里是完全正常的，而同一句话用在 `click` 上就是可疑的。**

**结论**：sha 相同只是「画面没变」这个**原始事实**，必须**和动作名并排展示**才有意义。
报告里直接标注成「点击无效」会让人把正常行为当故障查。

**什么情况下不成立**：库改成不在 `use_vision=False` 时采集截图；或截图存储位置移出临时目录。

---

## S6 —— 结构化输出存盘往返

**为什么这条是「亮点 2（结构化落库）」的地基**：我们的产物是 `result.json` + sqlite 的行，
所以「跑完 → 序列化落盘 → 之后读回来用」**一定会发生**。如果那条路上
`history.structured_output` 变成 `None`：报告里商品是空的、入库 0 行，
**而 run 本身是成功的，没有任何报错**。
一个「成功但什么都没采到」的 run，比一个失败的 run 难查得多。

```
── 判据 1（对照）：刚落盘之前 ──
  type=ProductRowList  行数=2
  清洗后的价格: ['59.90', '39.00']
  序列化产物 12143 字符
── 判据 5：'_output_model_schema' 不在序列化产物里（不是丢了，是没写过）──
── 判据 2：读回之后 ──
  property  = None
  getter    = ProductRowList  行数=2
── 判据 4：final_result() 挺过了往返，228 字符，可直接 model_validate_json ──
  判据 4b：这份 raw 是【清洗后】的形态 —— LLM 给的 '¥39.00' 在这里是 '39.00'
SPIKE_S6_OK
```

**结论**：

1. **判据 1 是对照组，不能省。** 不做它，「落盘后是 None」就可能只是
   「结构化输出压根没工作」——两边都是 `None`，断言成了空转。
2. `_output_model_schema` 是 pydantic 的**私有属性**，**从来不进序列化产物**——
   它不是「丢了」，是「**从来没被写出去**」。所以读回对象上它是默认值 `None`，
   property 于是**安静地返回 `None`**。
3. `get_structured_output(Model)` 在同一个读回对象上仍能解析出**等价**的模型。✅
4. 兜底：原始 JSON 字符串本身挺过了往返（`final_result()`），可直接 `model_validate_json`。
5. `¥39.00` 被 `clean_price` 清洗成 `Decimal("39.00")`，走的**真路径**（不是单测里手造的）。

> **为什么 property 的危险在于「静默」**：`history.structured_output` 返回 `None` 时，
> 它**长得像「这次没采到数据」，而不像「你调错方法了」**。
> 所以落库/报告一律走 `get_structured_output(Model)`（ADR-9）。

**⚠️ 计划外发现（判据 4b）：`final_result_raw` 是「清洗后的原文」，不是「LLM 的原话」**

第一版断言写错了才看清：LLM 给的是 `"¥39.00"`，而 `final_result()` 里是 `"39.00"`——
因为它是 `params.data.model_dump(mode='json')` 的产物（`tools/service.py:2017`），
即**过了 pydantic 校验和 `field_validator` 之后**的形态。

计划里写「隔离时把 `final_result_raw` 留着，以后能看 LLM 当时到底返回了什么」——
**留的不是 LLM 的原话**。想留原话只能自己在步进记录里存，而那正是 `steps.jsonl` 要做的事。
把这两者混为一谈，排查时会对着一份「看起来正常」的数据想不通为什么会被隔离。

**什么情况下不成立**：库把 `_output_model_schema` 改成非私有字段，或改用 `PrivateAttr` 之外
的持久化方式。那时判据 2 会红，我们就不需要再绕私有字段了。

---

## S7 —— 白名单能不能匹配带端口的 IP 主机名

**为什么这条必须先验**：Phase 4 的本地 mock 卖家后台跑在 `127.0.0.1` 上。
如果白名单匹配不了带端口的 IP 主机名，整个 e2e 会在「连站点都进不去」上卡住，
而报错会指向「导航失败」，看起来像浏览器问题。

**判据必须带对照组**：光断言「能打开 `127.0.0.1` 的页面」是不够的——
白名单要是压根没生效，这条断言照样通过。所以同一次运行里还要断言
**同一台服务器、同一端口、换个主机名 `localhost` 就打不开**。

```
访问过的 URL: ['about:blank', 'http://127.0.0.1:15177/', 'about:blank']
  ActionResult.error: Navigation failed: Navigation to http://localhost:15177/ blocked by security policy
SPIKE_S7_OK
```

**结论**：`allowed_domains=["127.0.0.1"]` 能匹配 `http://127.0.0.1:PORT`（端口被忽略），
而同一端口的 `localhost` 被拦。两个断言同时成立 → **白名单真的在按域名判定**，
S3 的结论因此可信。mock 站点方案成立。

**什么情况下不成立**：库改用 `browser_use/utils.py:563` 那套匹配器（默认 scheme 是 https，
与 SecurityWatchdog 不兼容，见事实 7）。

---

## 这四条计划外发现，改变了哪几处设计

| 发现 | 定型了 |
|---|---|
| 快照会被会话 reset 原地清空（S2-4） | `GuardrailInterceptor` / `RunRecorder` **当场取值**，不持有 `browser_state` |
| 序列化结果每元素占多行（S2-2） | 任何对着 DOM 文本做的断言都必须按「段」而不是「行」切 |
| 抽取/裁判都是额外 LLM 调用（S4） | 采集走结构化 `done`；`steps.jsonl` 记**总调用次数**而不只是步数 |
| sha 相同必须配动作名（S5-7） | 报告里该信号与动作名**并排**，不单独下「点击无效」的结论 |
| `final_result_raw` 是清洗后形态（S6-4b） | LLM 原话由 `steps.jsonl` 自己记，不能指望 `final_result()` |

其中第一条和计划里原本的写法是**冲突**的（原本打算在 `Recorder` 里收集状态最后统一处理），
已经按实测结果改掉。

---

## 这些结论现在有守卫了：`tests/test_browser_contract.py`

spike 是**一次性探路**——它回答「这条路能不能走」，跑过一次、结论写进本文档之后
就再也不会跑第二次。而本项目有四个关键设计压在**未文档化的运行时行为**上
（回调时序、改写生效、快照生命周期、私有字段存盘）。这些一次升级就可能变，而变了之后：

- 代码不报错
- 类型检查通过
- `tests/test_compat.py` 可能照样绿（它只验签名在不在）
- 只是**护栏静默失效**

所以光有「当年跑通过」不够。七个 spike 的判据已经改写成 8 条 `needs_browser` 测试，
每次 CI 重跑一遍：`uv run pytest -m needs_browser`（本地 ~114s，8 passed）。

**和 `test_compat.py` 的分工**（两个正交的哨兵，不是重复）：

| | 验什么 | 覆盖不了什么 |
|---|---|---|
| `test_compat.py` | 静态：签名、版本号、保留字名单**还在不在** | 签名**背后的行为** |
| `test_browser_contract.py` | 运行时：那些行为**还成不成立** | — |

「`Agent.__init__` 还接受 `register_new_step_callback`」和「回调真的会被调用，且改写真的生效」
是两件事。前者是后者的必要条件，不是充分条件——只做前者的哨兵，
在「回调被调用但返回值被忽略」这种改动面前完全无能。

### 几处测试比 spike 更严（不是照搬）

spike 里有些断言是**有条件**的——那时候还在发现阶段，不知道会看到什么。
测试里一律改成无条件断言它们当年看到的那件事：

| 位置 | spike 的写法 | 测试的写法 | 为什么改 |
|---|---|---|---|
| S3 被拦之后的页面 | `if after == "about:blank"` 才打印结论 | 无条件 `assert urls[-1] == "about:blank"` | 有条件的断言在条件不成立时**静默通过**，读者却以为验过了 |
| S2 只读操作的判定 | 只 `print` 出 `confirm` | 断言 `is Decision.CONFIRM` | 它是 ADR-7（fail-closed）在真实页面上的证据，也是 `platform/mock.yaml` 必须有自己的 URL 作用域的依据 |

### 断言有牙：一次变异检验

S1 那两条断言（`rewritten == 1` + `done` 能穿过回调）针对的正是 spike 第一版踩的坑。
为了确认它们不是摆设，把改写**故意改回无条件**（原样复现当年的 bug）跑了一遍：

```
E       AssertionError: 预期只改写 1 步，实际 4 步 —— 回调改多了。
E           真实拦截器犯这个错时，agent 会再也完不成任务，而'URL 里有 /page2'照样成立。
E       assert 4 == 1
1 failed
```

关键细节：失败发生在第 163 行，也就是**它先通过了**前面那条「URL 里有 /page2」的断言。
这正好印证了那句判断——一条会放过「把任务搞死」的断言等于没断言。

---

## 追加：CI 上浏览器全红的根因（2026-09-16 排查记录）

Phase 2 的 8 条契约测试在本地全绿，在 CI 上全红。这份记录留在这里，因为
**根因是一条未文档化的库行为，而且它的失败形态指向完全错误的方向**。

### 现象与三轮误判

| 轮次 | 我看到什么 | 我的判断 | 实际 |
|---|---|---|---|
| 1 | `needs_browser` 全红，公开注解只有 `Process completed with exit code 1` | 日志读不到，先修"注解"通道 | 对了（`b27a86b`） |
| 2 | 注解给到 `local_browser_watchdog.py:428 raise RuntimeError` | 猜"Chromium 在 runner 上装不上"（R9） | **错**，探针一次就报出 `/usr/bin/google-chrome` 在 |
| 3 | 探针报"环境没问题"、测试照红；我以为是 pytest 把消息截了 | 改 `--tb=long` | **错**，`...<N lines>...` 是 Python 3.11+ `traceback` 模块自己干的，与 pytest 无关 |

第 3 轮那次错得很典型：**我在找一个不存在的开关**。花了一轮 CI 才验证
`--tb=long` 改完输出逐字未变。教训：改之前先用一行脚本在本地确认那个截断是谁产生的。

### 真正卡住的不是 traceback，是一个没人读的管道

```
local_browser_watchdog.py:146-151
    subprocess = await asyncio.create_subprocess_exec(
        browser_path, *launch_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,     # ← 全库没有一行读它
    )
```

第 428 行的 `RuntimeError` 消息是**硬编码**的通用提示（"可能要 `--no-sandbox`"），
所以补 traceback 永远补不出死因。死因一直在 Chrome 的 stderr 里，被接了管道却没人听。

**办法：既然库不读那个管道，那就别用管道。** 在 `create_subprocess_exec` 这一层
把 `PIPE` 换成文件句柄 —— 不去改库，也不用去数库里有几个启动点。
见 `devtools/probe_browser.py` 第二段和 `tests/conftest.py::_capture_child_process_stdio`。

### 拿到的逐字证据

```
FATAL:content/browser/zygote_host/zygote_host_impl_linux.cc:129] No usable sandbox! If you
are running on Ubuntu 23.10+ or another Linux distro that has disabled unprivileged user
namespaces with AppArmor, see https://chromium.googlesource.com/chromium/src/+/main/docs/
security/apparmor-userns-restrictions.md. ... If you want to live dangerously and need an
immediate workaround, you can try using --no-sandbox.
Received signal 6
#12 content::ZygoteHostImpl::Init()
退出码 = -6
```

### 根因：库里有**两份**清单回答"哪个 Chrome"

```
两者不是同一个二进制！探针=/usr/bin/google-chrome / 库=/usr/bin/chromium
```

| 用在哪 | 函数 | 策略 | runner 上的结果 |
|---|---|---|---|
| 探针（我按 `config.CHROME_PATH or` 它） | `browser/chrome.py:find_chrome_executable()` | `which google-chrome\|google-chrome-stable\|chromium\|chromium-browser`，**google-chrome 优先** | `/usr/bin/google-chrome` ✅ |
| **库里真正启动用的那条** | `local_browser_watchdog.py:264-279` `_find_installed_browser_path()` | 硬编码路径表，**chromium 组优先**（`prioritized + rest`，第 321-323 行） | `/usr/bin/chromium` ❌ |

两者的差别在正常机器上看不出来（挑中的是同一个二进制），**只在两个二进制都存在
且其中一个沙箱不可用时才暴露** —— 而这恰好就是 GitHub runner 的情况：

- `/usr/bin/chromium` 没有可用的沙箱助手 → `No usable sandbox!` → `LOG(FATAL)` → SIGABRT
- `/usr/bin/google-chrome` 的 deb 带 setuid 的 `chrome-sandbox` → 沙箱可用

所以同一份 `get_args()`、同一个 runner、同一个 headless，**只换二进制就从
"CDP 就绪"变成进程秒退**。这也是为什么第一轮探针会得出"环境没问题"——
它测的是另一个二进制。

### 修法与取舍

**钉死二进制**（`ci.yml` 的 `ECOM_AGENT_CHROME_PATH: /usr/bin/google-chrome`），
而不是 `BrowserSession(chromium_sandbox=False)` 关沙箱。

关沙箱那一条探针已经在本 runner 上验证可行（"对照结论：同一条库路径，
`chromium_sandbox=False` 就起得来"），但那是**用降低安全姿态换绿色**。
这个项目通篇在讲护栏，为了 CI 变绿去关掉浏览器自己的沙箱是本末倒置。
钉一个带可用沙箱的二进制，效果一样而不用让步。

两个附带结论：

1. **探针必须和被测对象共享同一份环境**，否则它会给出自信的错答案。
   之前 env 挂在测试 step 上、探针 step 没有，两者测的不是同一个浏览器 ——
   这就是"环境没问题"这句错结论的来源。现在 env 提到 job 层。
2. `ECOM_AGENT_CHROME_PATH` 从"CI 上设成空串、让库自己探测"改成"CI 上也显式钉死"。
   理由见上：让库自己挑，挑的是哪一份清单是不确定的。

### 取证方式的一条经验

注解原来取 `tail -40`，拿到的末尾几十行全是栈帧和寄存器；死因（那行 `FATAL`）
在栈**前面**，正好被挤出去。改成 grep `FATAL|Check failed|Received signal|zygote|sandbox`。

**`tail` 对"人写的日志"好用，对"崩溃转储"恰恰相反：越靠后越没有信息量。**
差一点就因为"末尾没看到 FATAL"而得出"没有 FATAL 行"的结论。

### 修好之后的验证（run 35117778593）

上面那段是"我认为修法可行"，这一段是"修法在 CI 上真的生效了"。
区分的意义：`chromium_sandbox=False` 也验证过可行，但 `ECOM_AGENT_CHROME_PATH`
这条是**另一条路径**，探针验证过前者不等于后者成立。

```
库真正挑中的二进制: /usr/bin/google-chrome     ← 修前是 /usr/bin/chromium
第二段：库自己的启动路径也成功了 → 失败与启动方式无关
矩阵结论：沙箱开着也能起 → 崩溃与沙箱无关
```

三句连起来读，才是完整的：库和探针现在挑同一个二进制；
**库自己那条启动路径**也成功了（不是探针另起的一次）；沙箱保持开启。

### 修的过程中又暴露两个问题（都已修）

**一、"绿"这个颜色在这里是二义的。** 这个 job 是 `continue-on-error`，
于是"8 条全过"和"一条都没收集到"（pytest 退出码 5）**在 step 层面都是 success**。
我第一次看到绿的时候无法回答"那 8 条到底跑了没有"——而这恰恰是这个 job
一开始要避免的病：颜色不携带信息。修法是在测试步骤里无条件把 pytest 末行的
summary 抬成注解。本地先验证了提取逻辑：

```
$ pytest -v -m needs_browser --tb=long -rf | grep -aoE '[0-9]+ (passed|failed|error|skipped)[^=]*' | tail -1
8 passed, 138 deselected, 39 warnings in 92.69s (0:01:32)
```

**二、修好之后注解里仍有红字。** Chrome 起来了，但"死因行"标题下挂着三条
`Failed to connect to the bus: Could not parse server address` —— 容器里没有
D-Bus，连不上是常态，与崩溃无关。加了噪声白名单（`cpufreq`、`dbus/bus.cc`）。

**排掉它不是因为不重要，而是因为「注解区出现红字却没有信息量」会训练人跳过注解。**
一个只喊狼来了的通道，等于没有通道 —— 这和上面第一条是同一个病。

### 一个还没解决的观察

`continue-on-error: true` 挂在 job 层，所以浏览器 job 红的时候**整个 run 的结论仍是 success**
（实测：run 35116972105 的浏览器 job 是 failure，run 结论是 success）。
也就是说 run 列表和徽章上完全看不出它红过，只有点进 job 才看得到。
这等于把"让人忽略 CI"从日志层挪到了列表层，没有根治。

保留这个设置的理由仍然成立（环境问题不该冒充代码问题），
但**"浏览器 job 红了"这件事需要一个 run 级别的可见信号** —— 待办。

### 第三层：同一个 bug 的第二次出现，暴露的是设计方向错了

修好之后仍有一条红字挂在注解里。第一反应是"白名单漏了一项"，但漏的那项是
`dbus/object_proxy.cc`，而我上一轮刚把 `dbus/bus.cc` 加进去 —— **同一个子系统，
只是抛错的文件不同**。于是我修的不是漏项，是方向。

当时的实现是**黑名单**：「除已知噪声外，所有 `ERROR:` 行都算死因」。

```
NOISE = ("cpufreq", "dbus/bus.cc")          # 第一版：按某次看到的字符串
if "ERROR:" in s and not any(n in s for n in NOISE)
```

它有个不对称，而且这个不对称是**结构性的、不是运气问题**：

> **黑名单保证会误报。** 只要 runner 上还会冒出任何一种我没想到的良性 ERROR，
> 注解里就会出现一条没有信息量的红字，标题还写着「死因行」。
> 而白名单只会漏报，漏报时会安静 —— 安静比乱喊更容易被发现和修。

同一个仓库里那份 CI grep 用的恰好是白名单，所以它从来没出过这个 bug：

```bash
grep -aE 'FATAL|Check failed|Received signal|zygote|sandbox'
```

而黑名单想换来的那个好处（"不放过未知故障"）用白名单加一档兜底就能拿到：

| 档 | 条件 | 报什么 | 对应状态 |
|---|---|---|---|
| 一 | 命中死亡关键词 | 只报这些 | 有真凶 —— 只报真凶 |
| 二 | 一条都没命中 | 报非噪声的 ERROR | 没有真凶 —— 报嫌疑人，因为它是仅有的线索 |

**有真凶时只报真凶，没有真凶时才报嫌疑人。** 已固化为
`tests/test_probe_forensics.py`，其中一条专门断言"健康运行 → 一条都不报"。

### 顺带修掉的两处结构问题

**一、同一件事有两份实现，而且思路相反。** `fatal_lines` 是黑名单，
workflow 里的 grep 是白名单（见上表）。这意味着改对一份、另一份还是错的，
**而且没人会发现 —— 因为两边都跑得动**。

改成 CI 只决定【什么时候】去看，不决定【怎么看】：

```yaml
- name: 把 Chrome 自己的输出抬成注解（库接了管道，却从不读它）
  if: always() && steps.browser_tests.outcome == 'failure'
  run: python devtools/probe_browser.py --fatal-lines /tmp/chrome_capture.log
```

**二、注解转义在替换时丢了。** 原来那段 shell 里有 `sed -e 's/%/%25/g'`，
我换成 Python 调用时把它弄丢了 —— 而丢掉的后果是**静默的**：注解照样打印，
只是在 GitHub 页面上解析错位，本地跑脚本完全看不出来。
现在转义在 `_ann()` 里，也就是唯一生成注解行的地方，而不是靠每个调用点自己记得。

★ 顺序不能反：**必须先把 `%` 转成 `%25`，再转换行**。反过来的话 `%0A`
里那个 `%` 会被第二次转义成 `%250A`，消息就坏了。已用 capsys 断言生成的那一行本身。

### 这一段的三条，其实是同一个病

1. 白名单按"上次看到的那条字符串"写 → 下次换个文件就失效
2. 同一件事两份实现 → 修好一份另一份还是错的，且没人发现
3. 转义纪律写在调用点 → 换个调用方式就丢了

**都是在"某一次具体的调用"里解决问题，而不是在"这件事本身"里解决。**
共同点是：三次失误的失败形态都是**安静**的 —— 没有报错，没有崩溃，
只是注解里多一条红字、少一次转义、留一份过期的实现。

可观测性这一章最难的地方不是"怎么把信息记下来"，
是"怎么保证记下来的东西在被读的那一刻还是对的"。

### 第三层的验证（run 35118896356，commit 7cae07a）

和前两层一样，"我认为改对了"和"改法在生产里确实如此"要分开记：

```
needs_browser 实际结果：8 passed, 145 deselected, 39 warnings in 14.84s ；退出码 0
矩阵结论：沙箱开着也能起 → 崩溃与沙箱无关，是库那条路径特有的差别
库真正挑中的二进制: /usr/bin/google-chrome
死因行 —— 命中 0 条
```

最后一行是本次修复的正面证据：**同一份探针、同一个 runner，上一轮注解里挂着
三条 dbus 噪声（标题写着「死因行」），这一轮一条都没有。**
"不报了"本身很容易被当成"没有检查"，所以它必须和上面三行一起读 ——
探针确实跑了（三行结论都在），只是这回无可报。

`145 deselected` 顺带证明 CI 收全了新增的 7 条测试（本地 138 → 145）。
一份只在本地跑过的测试和没有测试，在 CI 眼里是同一件事。

---

## Phase 2 关账（2026-09-17）

七个 spike 重跑一遍，取实打实的判决行。**重跑不是为了拿到新结论 ——
是为了确认这些结论今天还成立**：spike 的价值全在"它验证过"这四个字上，
而一份没人重跑过的验证记录，半年后和一段注释没有区别。

```
########## spike_s1_callback_rewrite ##########   退出码=0  SPIKE_S1_OK
########## spike_s2_element_text ##########      退出码=0  SPIKE_S2_OK
########## spike_s3_domain_block ##########       退出码=0  SPIKE_S3_OK
########## spike_s4_fake_llm ##########           退出码=0  SPIKE_S4_OK
########## spike_s5_screenshot_paths ##########   退出码=0  SPIKE_S5_OK
########## spike_s6_structured_output ##########  退出码=0  SPIKE_S6_OK
########## spike_s7_localhost_allowlist ########## 退出码=0  SPIKE_S7_OK
```

★ 七项全 OK 意味着**一次回退方案都没切换**：S1 的 `new_step_rewrite` 是首选机制
（R1 退役）、S2 的元素文本可用（R5 退役）、S7 的 mock 站点方案成立 ——
计划里为这三项准备的回退路径（`stop_and_restart` / `action_gate` /
退路 1·2）都**没有动用**，但都还留在设计里作为纵深防御。

★ **判决行不是全部证据。** `run_spike` 只在"断言全过"时打 OK，
而这些断言的**内容**才是结论本身。想知道"OK 到底保证了什么"，要回去读
各 spike 正文里的判据 —— 本文档上半部分记的就是那些判据和它们的实测输出。

### 与 CI 的关系（两处互相补位）

| | 在哪跑 | 保证什么 |
|---|---|---|
| 本文档的 spike | 本机（Windows） | 结论在**开发环境**成立，且**判据可读**（打印中间值） |
| `tests/test_browser_contract.py` | CI（Linux） | 同样的结论在**另一个平台**成立，且**每次提交都重验** |

两者不是重复：spike 输出人能读的中间值（"快照 10 → 0"那种），适合排查；
契约测试只给退出码，适合守门。**S2-4 那条"快照会被原地清空"两处都有** ——
因为它改变的是写法，一旦库升级就必须两处一起知道。

### 交接给 Phase 3 的东西

四条**计划外发现**已定型了写法（见上文《这四条计划外发现，改变了哪几处设计》），
其中两条直接约束 Phase 3 的 `observability/`：

1. **`RunRecorder` 必须当场取值**，不能存 `browser_state` 晚点再读（S2-4）——
   否则记录器拿到的 `selector_map` 是空的，而"空"是合法结果，**静默失效**。
2. **`steps.jsonl` 要记 LLM 总调用次数**，不只是步数（S4）——
   抽取/裁判各自都是额外调用，只记步数会把成本算错。

### CI 的 `continue-on-error` 姿态：已拍板，挂 Phase 4（2026-09-17）

`continue-on-error: true` 挂在 browser job 层，导致**红的浏览器 job 在 run 列表上
显示为绿**（实测 run `35116972105`）。用户已拍板：**改，但挂到 Phase 4，现在不动。**

#### 纠一处我（Claude）说轻了的表述

我最初提议的是"让探针升格成闸门"，听起来像是拿一个现成信号来用一下。**不是。**
探针文件头第 43 行明写它**刻意**永远 `exit 0`：

> ★ 为什么失败也 exit 0：它是探针，职责是【说明】，不是【判决】。红了由下游的 pytest 去红。

这个立场本身是对的，但它意味着「升格成闸门」= **要给探针加一个新出口**，不是零成本。

#### 为什么延后不是拖延，是排序

判别器要回答的是「这次红，是环境的还是代码的」。这要求判别器**知道这个环境有哪些
环境依赖** —— 而 Phase 4 会引入 **mock 站点**：uvicorn 起不起得来、端口分配、
`data-mock-version` 那一套，全是**新的环境依赖面**。而"环境依赖"恰恰是门禁误红的
**唯一**来源。现在设计判别器 = 在一个还不知道 Phase 4 有哪些环境面的时刻把它猜出来。

另一头是收益：现在 `needs_browser` 只有 8 条，**gate 不住任何东西**；到 Phase 4 它会长成
e2e，其中包括护栏那条 `/goods/delete/` 的 HTTP 层断言 —— 那是全项目最重要的一条测试，
它在 CI 上无声腐烂是不可接受的。

#### 落地形状（保持探针「只说明不判决」的立场不变）

**判决归 gate，不归探针。**

1. 探针加 `--verdict-json <path>`：把已观测到的结论写成机器可读的一份
   （`{"chrome_found":…, "launch":…, "sandbox_matrix":…}`）。**`exit 0` 的契约不动。**
2. `browser_tests` 的 `continue-on-error` 从 **job 级降到步骤级**，后面加一步 gate：
   - 探针判「环境不可用」→ `::warning::` + 绿
   - 探针判「环境可用」但测试红 → **`exit 1`，job 真红 → run 真红 → 会发通知**
3. **必须做对照实验**（本项目通用手法，这次不能省）：故意破坏一条 `needs_browser`
   测试 → 确认 gate 真红；再人为让探针报环境坏 → 确认它只是 warning 不红。
   **没有前一半，后一半可能是空转。**

#### 被否掉的第三条路

「只加注解、不改红绿」—— **不推荐**：GitHub 的注解**不触发通知**，
"看得见"和"会被看见"是两回事，那等于加了机件却没换到东西。

#### 为什么这条仍然重要（不是洁癖）

今天**两种红外观完全一样，且两种都不通知**。而 README 里写着「CI 上已跑通」——
这句话今天是**真的**（run `35117778593` / `35118896356` / `35120305012` 三轮实测），
但只要 browser job 开始无声腐烂（例如库升级破掉一条未文档化契约），
CI 会一直绿，而那句 README **会在没人知道的情况下变成假的**。

#### 已落地（2026-09-17，Phase 4）+ 两条对照实验的实测结果

形状与上面那条计划一致，只有一处**改良**（见下）。落点：

| 位置 | 改了什么 |
|---|---|
| `devtools/probe_browser.py` | 加 `--verdict-json <path>`，落一份机器可读结论。**`exit 0` 契约未动。** |
| `devtools/ci_gate.py` | **新增**：判决逻辑（纯函数 `gate()` + `main()`） |
| `tests/test_ci_gate.py` | **新增**：真值表 + 两条对照实验（13 条，毫秒级） |
| `.github/workflows/ci.yml` | job 级 `continue-on-error` 删除；降到 `apt`/`probe`/`browser_tests` 三步；末尾加 gate 步骤（**它自己没有 continue-on-error**，于是 job 的颜色由它决定） |

**改良的那一处**：计划里 gate 是"后面加一步"（暗示写在 YAML 的 shell 里）。
实际抽成了 `devtools/ci_gate.py`。理由是它是**一条有真假的判断**，不是一次 IO：

- 写在 YAML 里就只能靠"往 main 推一次坏提交看颜色"来验 —— **不可复现**；
- 抽出来之后，真值表在 `tests/test_ci_gate.py` 里本地毫秒级可验，
  而且进了**离线门禁**（那个 job 是硬门禁），所以它以后不会被无声改坏。
- ⚠️ **这一段原来还有半句：「更要命的是验不了反面：没法在 CI 上故意让环境坏掉」。
  那半句在 2026-09-17 当天被自己推翻了**（见下一节）。
  错法值得留着：我把"让环境坏掉"默认成了"让 runner 上真的没有 Chrome"，
  而 browser job 的 env 是**写在我们的 workflow 文件里**的 ——
  测试看到的那份环境本来就归我们摆布。
  **把可配置的东西当成不可改变的，是最容易得出"验不了"结论的一种错觉。**

#### 四个分支真的都跑过了（2026-09-17，真 runner，非本地纯函数）

抽成脚本让**判据**本地可验，但"CI 上到底会不会走这条分支"是另一件事 ——
只有真跑才算数。做法：从 `34f414b` 开一条**一次性分支**，用两次提交把不该有的
两种状态做出来，跑完删分支。**main 全程没被碰过。**

| 分支 | run | 测试 | 探针结论 | gate 输出 | job / run |
|---|---|---|---|---|---|
| 测试绿（main） | `35192505194` | `10 passed`（22.16s，退出码 0） | — | `notice` 无需看环境 | 绿 / 绿 |
| ① 环境可用 + 测试红 | `35193112761` | 1 failed | `usable=true` | `::error::` 这是真的红 | **红 / 红** |
| ② 环境不可用 + 测试红 | `35193459911` | 11 failed | `usable=false` | `::warning::` 放行 | 绿 / 绿 |
| ③ 结论缺失 + 测试红 | `35205084140` | 10 failed | **探针自己崩了，无结论** | `::error::` 按可用处理 | **红 / 红** |

**这一轮真正买到的三样东西：**

1. ★★ **一条承重假设第一次被真环境证实**：步骤级 `continue-on-error` 之下，
   `steps.<id>.outcome` 是 `failure` 而 `conclusion` 是 `success` —— gate 必须读前者。
   在此之前这只在本地用手喂的字符串验证过，**属于"在真环境里不可证伪"的断言**。
   ① 的注解里逐字出现了 `outcome=failure`。这条现在有 run 号了。
2. **"测试全红而 run 绿"是可到达的，且工作正常**（②：11 failed / run Success）。
   job 绿不再等于测试绿，也不再等于"被无声忽略"—— 注解说明了它是哪一种绿。
3. **fail-closed 的默认值在真 run 里也是对的**（③）。这一条是被计划外的故障
   顺带验掉的，见下。

**③ 那次是我把实验杠杆选错了，而错法本身就是一条结论。**

计划里让"环境不可用"用的杠杆是 `ECOM_AGENT_CHROME_PATH=/nonexistent/chrome`。
**它不产生"环境不可用"，它让探针崩掉** —— `create_subprocess_exec` 抛
`FileNotFoundError`，而 `_try_launch` 当时没兜它，于是探针在写结论**之前**就死了。
结果不是"判成可用"，是"**没有结论可判**"，gate 只能走 fail-closed 默认值。
两个后果分得很开，但**只差一层**：一个是判据选对了，一个是判据没被用上。

这个杠杆是先在本地验过才敢花 CI 的，所以没浪费；真正可用的是
**一个存在但立刻退出的假 chrome**（`printf '#!/bin/sh\nexit 1\n' > /tmp/fake-chrome`）——
两条启动路径都是**优雅失败**，才得到 `usable=false`。

**附带的、更值钱的一条：红是对的，红不可读是另一件事。**（已修，见下节）

#### 修法：让"探针崩了"这种红**可读**，判据一格不放宽

③ 的 run 页面上，陌生人看到的只有探针步骤的 `Process completed with exit code 1`
和 gate 说的"结论文件不存在（…）→ 按环境可用处理"。
**`FileNotFoundError` 本身只在要 admin 权限的日志里** —— 又一次"红的原因不可读"。

改法**只动可读性，不动判决**：`_try_launch` 兜住 `OSError`，
把"起不来"当成一个**结论**（`spawn_failed`）报出来，探针于是能正常写出结论文件；
判决仍然由 `spawn_failed_verdict` 给出 `usable=True`（**真红**）。

★ 路径是**我们自己的代码拼进消息里的**，不是从异常消息里捞的 ——
这是写测试时撞出来的：Linux 的 `FileNotFoundError` 消息带路径，**Windows 的不带**。
靠异常消息 = 在 CI 上可读、在自己机器上不可读，而后者恰恰是打错路径最常发生的地方。
**要报的东西自己带上，别转手。**

#### 两个长得像、判决相反的分支：**谁指定的，谁负责**

这一节是上面那条判据的延伸，也是这次唯一新增的判据方向：

| 情形 | 判 | 为什么 |
|---|---|---|
| **一个** Chrome 都找不到（没人指定 + 自动探测也是空） | `usable=False` → **放行** | 环境里确实没装，是**环境问题** |
| 有人**指定**了路径，但那个进程起不来 | `usable=True` → **真红** | 是**有人写下的那行配置错了**，是**配置问题** |

表象一样（都没有可用的 Chrome），后果刻意相反。区别只有一句话：
**谁指定的，谁负责。** 这与本文件已有的那条先例是同一条判据 ——
"探针能起、库自己的路径起不来"判成**可用**，因为那是我们自己的配置不一致。

理由：**唯一能靠改配置修掉的那一类故障，必须留给我们自己看见。**
把它归进"环境坏了"，等于把最容易修的东西藏进最不需要我们修的那一档。

这两条已经写成**成对**的断言（`test_the_two_similar_looking_failures_are_judged_oppositely`）：
单看任何一条都像是随手定的默认值，**成对摆着才看得出这是一个非对称的设计决定**。
谁哪天把它"顺手改一致"，那条测试会红。

#### 本地对照实验（`tests/test_ci_gate.py`，13 条，毫秒级）

这一层仍然保留，它和上面的真 run **不是替代关系**：

| 实验 | 输入 | 实测结果 |
|---|---|---|
| 一：故意破坏一条 `needs_browser` | 坏用例（退出码 1）+ 真实探针结论（`usable=true`） | `::error::` + **退出码 1（真红）** |
| 二：人为让探针报环境坏 | 同一条坏用例 + `usable=false` 结论 | `::warning::` + **退出码 0（绿）** |
| （补）结论文件缺失 | 不存在的路径 | `::error::` + **退出码 1** |

★ 补做第三条是因为它是这套机制里**最危险的默认值**：
探针崩了 / 没写结论 / 字段没了，都必须按"环境可用"处理（→ 真红）。
反过来（默认不可用）会让探针的**任何**故障把真实测试失败洗成绿 ——
**一个能自动把红洗成绿的机制，比没有这个机制危险得多。**
同 `default_decision=confirm`：拿不准时选更严的那边。

真 run 验的是**"CI 上会不会真的走到这条分支"**，本地真值表验的是
**"走到了之后判得对不对"**，而且后者进了硬门禁、前者没有。
② 之所以必须真跑：`usable=false` 从探针里**自然产生**这件事，
本地纯函数永远测不到 —— 本地那份 `usable=false` 是我手写的。

#### 判据刻意写窄，以及为什么"探针能起、库的路径起不来"判成**可用**

判"环境不可用"的唯一条件是：**两条启动路径都起不来**，或者压根找不到 Chrome。

不把"探针能起、库自己的路径起不来"也判成不可用 —— 明明那时测试也跑不起来：

> 那正是 Phase 3 那次 CI 全红的形态（库挑中 `/usr/bin/chromium`，
> 探针挑 `/usr/bin/google-chrome`，**两者不是同一个二进制**）。
> 它看起来像环境问题，实际是**我们自己的配置不一致** —— 是我们的问题，
> 也该由我们看见。把它归进"环境坏了"，等于把这一类**唯一能靠改配置修掉**的
> 故障藏起来。

★ 这条判据每放宽一格，就多一类真失败可能被洗成绿。
**"红能被解释掉"本身没有价值，除非那个解释是可证的。**

★ 上面「谁指定的，谁负责」那节是同一条判据的第二次应用，而且是**反向**的一次：
那边不是放宽（`usable=True` 更严），而是收紧。两次调整方向相反、理由同一条 ——
**"这个故障能不能被我们自己修掉"才是判据，"测试跑不跑得起来"不是。**
修得掉的归我们（真红），修不掉的才叫环境（放行）。

---

# Phase 3 live 验收：一次真 token 运行带回的两条库事实（2026-09-17）

验收动作：`main.py run tasks/books_demo.yaml --approver deny`
（真网络 `books.toscrape.com` + 真 DeepSeek token + 真浏览器）。

这一节存在的理由：**上面所有结论都是"零 token"环境里得到的，而下面第一条
只有真跑一次才会出现** —— 它不在任何单元测试的覆盖范围里，
因为没有任何测试碰过那个回调。

## 结论 1：`register_should_stop_callback` 是四个回调里**唯一**只收 async 的（已修）

### 现象

第一次 live 运行，0 步、0 行，退出码 1：

```
❌ Result failed 1/6 times: 'bool' object can't be awaited
❌ Result failed 2/6 times: 'bool' object can't be awaited
...
❌ Result failed 6/6 times: 'bool' object can't be awaited
❌ Stopping due to 5 consecutive failures
```

报错里**没有一个字**指向我们的代码。它说的是"有个 bool 没法 await"。

### 逐字证据

```
$ uv run python -c "import inspect; from browser_use import Agent; \
    print(inspect.signature(Agent.__init__).parameters['register_should_stop_callback'].annotation)"
collections.abc.Callable[[], collections.abc.Awaitable[bool]] | None
```

也只有这一条：

| 回调 | 0.13.10 的注解 | 同步能跑吗 |
|---|---|---|
| `register_new_step_callback` | `Callable[..., None] \| Callable[..., Awaitable[None]]` | **能** |
| `register_done_callback` | `Callable[..., Awaitable[None]] \| Callable[..., None]` | **能** |
| `register_should_stop_callback` | `Callable[[], Awaitable[bool]] \| None` | **不能** |
| `register_external_agent_status_raise_error_callback` | `Callable[[], Awaitable[bool]] \| None` | **不能** |

调用点：`if await self.register_should_stop_callback():`（`agent/service.py:1018`）。
`await True` → `TypeError: 'bool' object can't be awaited`。

### 为什么这个坑特别隐蔽（三层，逐层加重）

1. **解析层**：库里另外两个回调**两种都收**。所以"回调写成同步的也能跑"
   这条经验是先被建立起来、再在这里失效的 —— 这是最容易踩的形状。
2. **吞异常层**：`_check_stop_or_pause` 在 `service.py` 的 **1109 / 1203 / 1209 / 2773
   四处**被调，每步至少调两次。它抛的异常走 `step()` 的 except →
   `_handle_step_error` → 变成一条**步进错误**。于是表现是"每步都失败"，
   而不是"停机回调坏了"。
3. **假象层（最坏的一层）**：护栏拦单个动作的路径**不经过这个回调**
   （那条路走 `on_new_step` + `guard_notice`），所以"护栏在工作"的假象成立。
   真正坏掉的是**连续被拦到上限后的硬停** —— 那是最后一道保险，
   而它**静默失效**。

### 为什么离线套件没抓到

**没有任何测试碰过这个回调。** runner 只是把它当一个函数对象传给 `Agent`，
而"传进去"和"库会怎么调它"是两件不同的事。
`tests/test_runner_offline.py` 覆盖了增长闸门、S2-4、LLM 账、落库、CLI，
唯独这一条路径在 I 节之前是完全空的。

### 修法与守卫

- `GuardrailInterceptor.should_stop` → `async def`。
- 新增 `GuardrailInterceptor.verify_stop_callback()`，由 `check_wiring` 在**开跑前**
  `await` 一次。**为什么是"真的 await"而不是查 `iscoroutinefunction`**：
  查签名只能证明"它现在是 async"，证明不了"库会怎么调它" ——
  而这次的 bug 恰恰是**我们的形状**和**库的调用方式**对不上，真正的契约是"库会 await 它"。
- `check_wiring` 因此变成 `async`（runner 侧加 `await`）。
- 守卫共 5 条（`tests/test_runner_offline.py` I 节）+ 1 条版本哨兵
  （`tests/test_compat.py::test_should_stop_callback_is_async_only`，守"这个不对称还在不在"）。
  其中两条是对照实验：同步版**必须**被启动期抓住；正常版**必须**通过
  （只有前者是"永远失败的检查"，会被人注释掉；只有后者是"永远通过的检查"，
  等于没有检查 —— 两个都要）。

### 通用经验（比这个 bug 本身值钱）

> **回调的失败形态是「契约在调用点，不在签名处」。**
> 我们传的是一个函数对象；库拿它怎么用（await 不 await、传几个参数、
> 返回值怎么解释）**只在库的调用点上**。所以自检必须走**真实的调用形状**
> —— 这一次的教训具体写成一句：`await 一下自己`，比读一百遍签名有用。

## 结论 2：库对 DeepSeek 的 `use_vision` 警告是**无条件**打的（假警报，未改库）

### 现象

YAML 里明写 `agent: use_vision: false`，编译产物也确认带上了
（`compiled.agent_kwargs == {'use_vision': False, 'max_actions_per_step': 2, 'max_failures': 5}`），
但每次 run 都打：

```
⚠️ [Agent] DeepSeek models do not support use_vision=True yet. Setting use_vision=False for now...
```

### 根因

```
474| # TODO: move this logic to the LLMs
475| # Handle users trying to use use_vision=True with DeepSeek models
476| if 'deepseek' in self.llm.model.lower():
477|     self.logger.warning('⚠️ DeepSeek models do not support use_vision=True yet. ...')
478|     self.settings.use_vision = False
```

注释写的是"处理**把 use_vision 设成 True** 的用户"，但 `if` 里**只看模型名**，
根本没读 `use_vision`。所以无论调用方设了什么，这条警告都会打。

### 处置：不修库，记在这里

按本项目自己的标准（"假警报比不报警更坏：它训练人忽略这个检查"），
这属于该记下来的一类。但**库源码只读**，而且实际影响是"每次 run 多一行噪音"，
改它的收益不值得破例。**记在这里是为了下一次读到它的人不要花时间去查我们的配置**
—— 那正是它最容易造成的浪费。

## 这次运行顺带验证了什么（真 LLM 下的端到端）

`runs/20260917T050218+0000-4ec444`，退出码 0：

| 项 | 实测值 |
|---|---|
| 状态 | `completed` / `parse_status=ok` |
| 采集行数 | 5（真实书名 + 真实价格） |
| 步数 / LLM 调用 | 6 / 7（步进 6 + 裁判 1，`other_calls=0`） |
| 产物 | `run.json` `steps.jsonl` `result.json` `report.html` `screenshots/` 全在 |
| `unsafe_auto_approved` | `False` |

三条值得记的：

1. **护栏的拒绝→改道在真 LLM 上成立**。steps 2–5 的每个动作都落到
   `default_decision=confirm` → auto-deny → 被换成 `guard_notice`（`actions=['guard_notice']`）
   → LLM 没有重复尝试，第 6 步直接 `done`。**这正是选"替换而不是删除"的理由的实证**：
   删掉的话 LLM 会以为自己没下指令而重试。
2. **`same_frame_steps=[3,4,5,6]` 是正确的诊断**，不是误报 ——
   页面确实一直没变，因为那几步的动作一个都没执行。
   这条"截图 sha 相同 ⇒ 页面未变化"的信号第一次在真实数据上被验证。
3. **`sanity_flags` 把 5 行全标了 `goods_id_not_numeric`** ——
   这是复用 `pdd.ProductRowList` 的**预期后果**（books 没有 goods_id），
   而"标记而不删除"的设计正好把它显式留了下来。见 `books_demo.yaml` 第 37–41 行的注释。

另有一条**诚实的缺口**：`prompt_tokens` / `completion_tokens` 都是 0，
即 DeepSeek 这条链路上的 token 用量没被 `CountingLLM` 捕到
（`library_calls=0`）。`total_calls` 是可信的（那是我们自己的计数器），
所以 LLM **次数**的账是准的、**用量**的账是空的。
Phase 4 若不涉及成本统计可以不动，但**不要**把 0 当成"没花钱"读。

---

# Phase 4 探路结论：真实浏览器 e2e 抓出来的四条（2026-09-17）

`tests/test_mock_pdd_e2e.py` 是 Phase 4 的 e2e 载体（真 Chrome + 本地 mock 站点）。
它一次跑通之后带回来四条**只有跑真路径才能拿到**的事实。四条都是同一个形状：

> **单测全绿，生产路径一跑就崩。** 而根因都不是"某处写错了"，
> 是"我们依赖的那个约定的**颜色/类型**和我们的假设相反"。

这也是这一节的共同教训：**这类错误无法靠"再多写几条单测"发现**，
因为坏掉的恰恰是"单测和真路径之间的那层差异"。

## 事实 1：`verify_extract_table_round_trip` 在它唯一的调用点上**一次都跑不成**

`extract_table.py` 的启动自检内部用 `asyncio.run(...)` 调一次动作函数，
而它唯一的调用点是 `runner.py` 的 `TaskRunner.run()` —— 一个**协程**。

```
RuntimeError: extract_table 的接线不通：
  RuntimeError: asyncio.run() cannot be called from a running event loop。
<sys>:0: RuntimeWarning: coroutine '...extract_table' was never awaited
```

★ 为什么这个特别值得记：

- **所有离线单测都是同步调它**，于是 13 条全绿；生产路径是异步的，一跑就崩。
- `tests/test_extract_table.py` 里恰好有"自检必须当场抛"的对照实验 ——
  它证明了这个自检**写得对**，但它证明不了"这个自检**跑得起来**"。
  两件事完全独立，而只有后者在生产上重要。
- 失败形态极隐蔽：`asyncio.run()` 抛错时那个协程**从未被 await**，
  Python 只打一条 `RuntimeWarning: coroutine was never awaited` ——
  一条默认不致命、容易淹在几十条 warning 里的提示。

**修法**：`verify_extract_table_round_trip` 改 `async def`，调用点 `await`；
`tests/test_extract_table.py` 的那两条用例跟着改 `async def`。

★ 顺带得到一个哨兵：用例的颜色（sync/async）**和被测路径不一致，本身就是测不到**。
改 async 之后，谁再把门禁改回同步实现，这两条会立刻红。

## 事实 2：`page.evaluate` 返回的是 **JSON 字符串**，不是对象

`extract_table_impl` 里写的是：

```python
payload: dict[str, Any] = await page.evaluate(_TABLE_JS, {...})
if not payload.get("found"):     # ← 'str' object has no attribute 'get'
```

`browser_session.get_current_page()` 返回的是 **`browser_use.actor.page.Page`**，
**不是 playwright 的 Page**（它连 `.url` 属性都没有 —— 一个同名异类的直觉陷阱）。
它的签名是 `evaluate(page_function: str, *args) -> str`，docstring 原文：

> *String representation of the JavaScript execution result.
> Objects and arrays are JSON-stringified.*

**它永远返回字符串**：对象走 `json.dumps`、`None` 变空串 `''`、数字/布尔走 `str()`。

★ 实测拿到的东西（JS 执行得**完美**，一个字都没错）：

```python
type: <class 'str'>
repr: '{"found": true, "tableCount": 1, "headers": ["商品ID", "商品标题", ...], ...}'
```

★★ **这条 bug 是被一个写错了的桩放进来的**，而这一点比 bug 本身重要：

`tests/test_extract_table.py` 的 `_FakePage.evaluate` 原来 `return self.payload` ——
**直接回一个 dict**。于是桩喂 dict、实现要 dict，两边一拍即合，13 条全绿。

> 教训不是"这个桩写错了"，而是：**桩的形状比真实对象宽松时，
> 它就不再是桩，而是一块遮羞布。**

桩必须模仿真实契约里**最容易搞错的那一面**（包括"返回值类型反直觉"）。
所以现在的 `_FakePage` 显式 `json.dumps`，并且留了一个 `raw` 口子供
"返回值不是合法 JSON"的对照实验；另加一条用例断言**桩本身回的是字符串**
（防止有人把它改回 dict，让这个 bug 重新变得不可见）。

**修法**：新增 `_decode_table_payload(raw) -> dict | None`，
三种坏形态（空串 / 非 JSON 裸串 / 非 dict）都返回 None → 转成一条给 LLM 看得懂的 error。
另加一条**反向对照**（`test_a_dict_payload_is_now_rejected_...`）：
把旧桩的行为喂进来必须失败 —— 它让"解析这一步是承重的"变成可证伪的。

## 事实 3：`browser_use.actor.Page.evaluate` 的返回值契约不进 tool schema

见上。补一条：**这个类没有 `.url`**（`browser_state.url` 才有）。
第一版探针里 `print(page.url)` 直接 AttributeError —— 排查时很容易
误以为是"页面没导航过去"，而真相是"拿错了类的属性"。

## 事实 4：护栏的 `guard_notice` **替换**了被拦的动作，所以 steps.jsonl 里查不到那个动作

e2e 判据 3 原来断言 `_click_step(steps)["actions"][0]["element_text"] == "批量删除"`，
实际记录是：

```json
{"name": "guard_notice",
 "params": {"message": "HUMAN_DENIED: 动作 click「批量删除」被拒绝 …"},
 "element_text": null}
```

这是**设计如此**（替换而不是删除，理由见 README 的 ADR 4 / interceptor.py），
但带来一处**可观测性的已知边界**，诚实记下来：

> `guardrail_decisions` 里只有 `rule_id / decision / reason / match_element_ids / approved / approved_by / decided_at`，
> **没有**"被拦的动作名和元素文本"。
> 要回答"到底拦了什么"必须去读那条 `guard_notice` 的 message 文本。
> 要补的话是给 decision 记录加字段（`action_name` / `element_text`）——
> Phase 4 不做，但**不要**以为 `steps.jsonl` 里有那个 `click`。

★ 顺带一条**意外收获**：真实运行里那一步的
`matched_rule_ids == ["allow-readonly", "block-destructive"]` ——
同一动作同时命中 allow 和 block，最终判定是 block。
这是**「最严优先」在真实数据上的证据**（真值表在 `test_guardrail_policy.py`），
已固化成 e2e 的一条断言。

## 事实 5：三份任务模板里躺着 5 条**永远不会命中**的护栏条目（已加启动自检）

这条是清 `extract_table` 死条目时**顺手查出来的**，范围比原以为的大得多。
成因是两个**各自都正确**的决定相乘：

1. 四个匹配维度是 AND，而 `match_element_text` 在拿不到元素文本时判【不命中】
   （`guardrails/rules.py:117-124` —— 刻意的：把 `None` 当空串会让写错的规则拦死一切）；
2. 元素文本的唯一来源是**参数里的 `index`**
   （`guardrails/interceptor.py:_text_for` 只读这一个键）。

→ **任何"不针对元素"的动作在带 `match_element_text` 的规则里永远不可能命中。**

实测（新增的启动自检 `runtime/runner.py:unreachable_text_rules`）：

```
tasks/pdd_search_products.yaml : 3 -> [('block-destructive', 'send_keys'),
                                      ('allow-readonly', 'extract'),
                                      ('allow-readonly', 'go_back')]
tasks/books_demo.yaml          : 2 -> [('allow-readonly', 'extract'),
                                      ('allow-readonly', 'go_back')]
tasks/mock_shop_readonly.yaml  : 0 -> []
```

**两个方向，严重性完全不同，不要混为一谈：**

| 形态 | 例子 | 实际后果 | 方向 |
|---|---|---|---|
| 以为放行了，其实是 `confirm` | `allow-readonly` 里的 `extract` | 每个只读抽取都要人点一次批准 | fail-closed，但**难用 + YAML 在说谎** |
| **以为拦死了，其实只是 `confirm`** | `block-destructive` 里的 `send_keys` | 想挡的那类键盘操作（按 Enter 确认删除…）退化成"请人确认" | ⚠️ **fail-open** |

第二行是这次真正的收获 —— 它和本项目其他几条"看起来在防、实际没防"是同一类，
但**这次的 `YAML` 读起来毫无破绽**：动作名是真的、正则是对的、缩进是对的。

**判据刻意不写成"危险动作名单"**：那种名单会随库版本静默失配，
而这个检查恰恰是用来防静默失配的 —— 它自己不能变成新的静默点。
所以它反射 `RegisteredAction.param_model` 里有没有 `index` 字段，
并由 `tests/test_compat.py` 的哨兵盯着那个未文档化的属性名。

★ **这个检查自己也有一个"读不到就不表态"的分支**（拿不到注册表 → 返回空）。
不能省：返回空集会让"没有任何动作带 index"成立，于是**每条带文本的规则都报警**，
而假警报会训练人忽略这个检查 —— 防静默失效的东西自己静默失效。

**处置（2026-09-17，经用户拍板，已完成）**。修法**不是删掉几个词**，
而是按"判据能不能用"把放行拆成两条：

| 规则 | 判据 | 管什么 |
|---|---|---|
| `allow-readonly-text` | 动作名 + URL + **元素文本** | 点击/输入类 |
| `allow-readonly-noelement` | 动作名 + URL（**无文本判据**） | `extract` `extract_table` `go_back` `scroll` `find_elements` `find_text` |

键盘动作同理拆了 `block-destructive-keys` / `allow-readonly-keys` 两条，
靠**参数**而不是元素文本区分。**刻意不放行** `evaluate`（任意 JS）与
`navigate`（归 Layer 0 白名单）。

★ 修完之后三份模板的 `unreachable_text_rules` 都是 `[]`，并且这条**由断言盯着**
（`test_runner_offline.py::test_all_real_task_yamls_now_have_zero_unreachable_rule_entries`）——
因为拆错了的症状和修复前**一模一样：完全静默**。

★★ 这一轮改测试时出现了一个**重复出现的形状**，值得单独记：
两条"证据型"测试（`test_mock_pdd.py` 里那条、`test_runner_offline.py` 里那条）
原本都是**绑在真实缺陷上**的 —— 它们断言的是"pdd 模板里 extract_table 不是 allow"、
"真实 books 模板里有死条目"。缺陷一修好，它们就红了。
**直接删掉它们会连同"检测能力"一起删掉**，于是都改成用**合成规则**提供对照：
覆盖一样，代价为零，而且不再要求仓库里长期留一个缺陷来养测试。

> 这个形状的一般式：**"断言缺陷存在"的测试，会在缺陷修好时变成负债。**
> 修法不是删断言，而是把断言从"某个真实对象当前坏着"改成
> "给这个坏形状，检测器必须报出来" —— 前者测世界，后者测机制。
> 而测机制的那条才能活过修复。

> ★ 这条最值得记的形状：**"更安全的默认值"会制造出"写了但从不生效"的规则。**
> 安全默认值和静默失效常常是同一个决定的两面。
> 而代价不是"它拦少了"，是**它让 YAML 在说谎** —— 读策略的人会以为自己看到的是实际策略。

## 附：Phase 4 的护栏 e2e 验收怎么做到"可证伪"

验收句是"`/goods/delete/` 的调用标志为 False"。**一条永远为真的否定断言没有信息量**，
所以这个文件里的全部重点是让"那一击**本来会**写成"这件事可证：

| 用例 | 干什么 | 证明什么 |
|---|---|---|
| A（正对照） | **绕过拦截器**，裸 Agent 真点「批量删除」 | mock 真的会收 POST；`write_calls()` 的标志**确实会翻** |
| B（验收） | 走完整 `run_task`，FakeLLM **主动瞄准**「批量删除」 | `write_calls() == []`，且判定记录指向 `block-destructive` |

两个细节让"瞄准"不是空话：

1. **索引从活 DOM 里现找**，不写死（`_click_target_index` 解析提示词文本，
   用例 A/B 共用同一个函数 —— 两边不一致就说明"记录下的"和"发出去的"不是同一份）。
2. **A 额外从活页面读 `data-mock-version`**（`page.evaluate`）并与 `MOCK_VERSION` 比对 ——
   独立证明这一轮跑的是 mock，不是某个真站点。

★ 一处**刻意不做的断言**，值得说明：用例 B 不去断言 `data-mock-version`。
因为可观测性只落 URL / 标题 / 元素文本 / 截图，**不落 DOM 文本** ——
那个属性在 B 的产物里根本不存在。它该断言的地方是
`tests/test_mock_pdd.py::test_every_page_carries_the_mock_marker`（离线）和用例 A（活 DOM）。
把"断言不到但听起来更强"的那条塞进去，只会让人以为覆盖了。

---

# Phase 5 探路结论：Web 层验收抓出来的五条（2026-09-17）

Phase 5 的验收句是三条演示动作（实时滚动 / 点拒绝后改道 / 回放看完整时间线）。
这一节记的是**真跑起来之后**才发现的东西，全部有现场证据，不是推演。

## 事实 1 ★★★：同一类缺陷在一晚上出现了**四次** —— "前端读一个字段，某个通道从没发过它"

这一类值得单独成节，因为它的形态非常一致，而且**不自曝**：

| # | 字段 | 谁缺 | 界面上的表现 |
|---|---|---|---|
| 1 | `run_completed.finished_at` | **两个通道都缺** | `结束 ` 后面永远空着 |
| 2 | `run_started.allowed_domains` | 回放缺 | 印出 `白名单 []` —— 一句**很确定的假话** |
| 3 | `run_completed.attempts` | 回放缺 | `· ? 次尝试` —— 承认不知道，但仍不一致 |
| 4 | 深链里的 `run_id` | 前端**解错了**（见事实 2） | 永远"连接中…" |

四条的共同形状：**不报错、不留日志、不影响任何测试**，只是某一栏在某一路上空着/歪着。
前端 `|| []`、`|| "?"` 这类兜底把"缺"变成了"看起来有值"——兜底本身没错，
错的是**没人知道自己在兜一个从来没送到的字段**。

处置不是逐条补，而是补一条**结构**判据（`test_mock_pdd_e2e.py` 判据 8）：
拿同一次真 run，把**实时载荷**和**回放载荷**逐字段比 —— 除三个说得清理由的键
（`max_attempts` / `run_dir` / `replay`）之外必须完全相等。比的是**值不是键**：
`{"finished_at": ""}` 和 `{"finished_at": "2026-…"}` 键集一样。
对照实验：把回放里的 `attempts` 撤掉 → 判据 8 立刻红，报出 `{'attempts': (1, None)}`。

> ★ 这条经验比三个字段值钱：**"两个通道形状一致"必须是一条可执行的断言，
> 不能是一句架构口号。** 口号会被四次同样的 bug 绕过，断言不会。

## 事实 2 ★★：`?run_id=` 深链对**每一个真 run** 都是坏的（`+` 被解成空格）

现象：在浏览器里打开 `?run_id=20260917T103341+0000-fb79d9`，看板显示"连接中…"，
**永远不出内容，也不报错**。

根因：`new URLSearchParams(location.search).get("run_id")` 按**表单语义**解码 ——
`+` 就是空格。而 `new_run_id()` 生成的是 `20260917T103341+0000-abcdef`，
那个 `+` 来自 `+00:00` 时区，**每个 id 都有**。于是前端拿到的是
`20260917T103341 0000-fb79d9`，那个目录不存在 → 回放空 → SSE 无事件 → 页面停在"连接中…"。

为什么这条比看起来重要：`run_id` 就印在看板上（`20260917T103341+0000-fb79d9`），
**它就是给人复制去分享的**。一个"复制粘贴就坏、而且坏得不出声"的深链，
演示时会让人以为是服务挂了 —— 而 ADR 里"同一份代码加个 query 参数就变成回放"
这句话正是靠它成立的。

修法：自己取 `run_id=` 之后那段，再 `decodeURIComponent`（只解 `%XX`，不碰 `+`）。
钉住它的是 `tests/test_api.py::test_the_deep_link_does_not_mangle_the_run_id` ——
一条**源码级**断言（项目没有 JS 测试框架），挡的是"用错了哪个解码器"这个具体形态。
⚠️ 它的极限写在 docstring 里：挡不住"换一种方式写错"。

> ★ 反面对照也记下来：服务端一侧**本来就是对的**（`+` 在路径段里是字面量，
> `tests/test_api.py` 里那些 `20260101T000000+0000-aaaaaa` 一直跑得通）。
> 所以这次的教训是**接口两侧要一起看**：路径段对、query string 错，
> 光测服务端永远发现不了。

## 事实 3 ★★：`approval_required` 必须在 `await approver.request()` **之前**发

顺序是功能性的。看板是唯一能让人知道"有人在等审批"的地方，而 run 此刻正挂在
那个 await 上、什么都不做。先 await 再发 → **卡片永远不弹**，
而"没有卡片"和"没有需要审批的动作"在界面上长得一模一样。

同理 `approval_resolved` 的载荷用 `outcome.value` 而不是 `approved` 布尔：
四种结局里只有一种意味着"护栏按预期工作"（人真的点了拒绝），
超时和通道故障在布尔上都是"没批准"，但它们说明**审批通道本身有问题**。

## 事实 4 ★：拒绝这条路原来**一条接口级用例都没有**

彩排时发现：`AutoDenyApprover` 把"拒绝后改道"测透了，但**从 Web 端点进来的那条路**
（POST → 唤醒 `asyncio.Event` → run 拿到 `denied` → 落盘审批流水）没有任何用例。
补上 `test_api.py::test_a_denied_approval_actually_denies`，三个断言各钉一头：
端点回显、被唤醒的 run 拿到的结局、`decided/{id}.json` 的流水
（且 `result.outcome` 必须是裸字符串 `"denied"` —— 写成 Enum 会是
`"ApprovalOutcome.DENIED"`，**可读但不可比**，下游全得跟着改成字符串比较）。

对照实验：把 `WebApprover.resolve` 里的分支反转 → 用例在
"点了拒绝，但 run 拿到的结局不是 denied —— 这个反转会把一次写入放行"上红。
一个把「拒绝」实现成「批准」的 bug，是这个项目里最贵的一种。

> ⚠️ 彩排里还出现过**一次无法复现**的怪事：点「拒绝」，卡片却显示 `approved · web`（4 次里 1 次）。
> 两个半边都单独证过是对的（curl 送 `{"approved":false}` → 盘上是 `denied`；
> 页面上的 `fetch` 请求体也是 `false`）。几何量过：按钮 86px/58px、间距 10px，
> 误点需要 ≥39px 的偏差。**原因至今没查明**，所以处置是**把那条分支钉死**
> （上面那条用例），而不是写一句"应该是偶发"。没查清的事就记成没查清。

## 事实 5 ★★：本机的 MCP puppeteer `click` 工具**不可信**（实测，不是猜）

`puppeteer_click("#history .item:first-child")` 有一次**挂住 >120s 且一次点击都没派发**
（页面上装了捕获阶段的点击记录器，`window.__rec` 是空的）。同一时期还有一次
"点了 A，更危险的 B 发生了"（点历史条目，却**创建并批准**了一个我没起的 run）。

处置：UI 验证改用**页内 `evaluate` 直接派发**（没有坐标、没有中间层），
并且"我点了 X"不算证据 —— 要有页面状态或服务端副作用作为独立证据。
这条不影响项目本身，但它决定了**我怎么证明 UI 是对的**。

## 附：CI 姿态在 Phase 5 上又验了一次（run `35212595111`，`f0f98ad`）

Phase 4 建的那道门禁在 Phase 5 的推送上再跑一遍，两个 job 都绿：

| job | 结论 | 判据 |
|---|---|---|
| 离线测试 | success | 318 条离线全绿（`跑离线测试` 这一步真执行，不是被跳过） |
| 浏览器测试 | success | `门禁` 步 success，且**注解说的是哪条分支** |

★ 关键在最后那格。`跑浏览器测试` 这一步挂着 `continue-on-error`，
所以「测试真绿」和「环境不可用被放行」在 API 里**都是 `conclusion=success`**
（Phase 4 已实测：步骤级 `continue-on-error` 下 `outcome=failure` 而
`conclusion=success`）。读结论字段分不出来，得读注解：

```
needs_browser 结果=success → 绿（无需看环境）
needs_browser 实际结果：11 passed, 318 deselected, 30 warnings in 21.26s ；退出码 0
探针结论：环境可用 —— 库自己的启动路径成功 → 这台机器能跑 needs_browser
```

第一行是门自己写的分支判词，第二行是 pytest 的真实输出 —— 两者一起才构成"真绿"。
**只有第一行的话，它可能是在说"环境坏了所以放行"**；只有第二行的话，
它可能来自一个被 `continue-on-error` 吞掉的失败。这正是 Phase 4 把
`continue-on-error` 从 job 级降到步骤级、并加一步 gate 的全部理由。

⚠️ 一条与代码无关的预警（今天不处理）：两个 job 都有 GitHub 的
`Node.js 20 is deprecated` warning —— `actions/checkout@v4` 与
`actions/setup-python@v5` 正被强制跑在 Node 24 上。这类东西到某天会变成真红，
而那时报错出现在 action 内部、跟我们自己的代码毫无关系。记录在此，
下次动 CI 时顺手升版本。


---

# Phase 6 探路结论：登录态怎么才能**真的**留下来（2026-09-17）

Phase 6 的目标是"人工扫码一次，之后每次 run 都是登录状态"。这件事看着像配置问题
（传个 `user_data_dir` 就完了），实际是**一个未文档化的库行为 + 一个关闭时序**的合体 ——
两者任一搞错，失败形态都**不是报错**，而是"报告齐全、退出码 0、零行数据"。
这一节记的就是把它们逐条钉死的过程，全部有实测输出。

## 事实 1 ★★★：`BrowserProfile` 会把 `user_data_dir` **静默改道**到临时目录

`browser_use/browser/profile.py` 的 `model_post_init` 里调 `_copy_profile()`：

```python
if self.user_data_dir and self.is_chrome:      # is_chrome = 'chrome' in str(executable_path).lower()
    tmp = tempfile.mkdtemp(prefix='browser-use-user-data-dir-')
    shutil.copytree(self.user_data_dir, tmp, dirs_exist_ok=True, ...)
    self.user_data_dir = tmp                  # ← 把自己的字段换掉了
```

日志只有一行岁月静好的 INFO：

```
INFO [utils] Created new profile (Default) in temp directory: C:\Users\...\Temp\browser-use-user-data-dir-xxxx
```

**为什么这一条足以让整个 Phase 6 静默失败**：

```
人工扫码成功 → cookie 写进 %TEMP%\browser-use-user-data-dir-xxxx
             → 进程结束，临时目录消失
             → 下一次 run 又是干净 profile → 看到登录页
             → 按任务文本第 1 步"遇登录页立刻停止并汇报需要人工登录"
             → 退出码 0、report.html 齐全、sqlite **零行**
```

链条上没有任何一环报错或崩溃。而人看到"需要人工登录"的第一反应是**去查风控**——
正是计划 R2 里那条最贵的错误方向。这是「前端读一个字段、某个通道从没发过它」那个
缺陷家族的**第五个成员**：静默、无错误、无日志（那行 INFO 看起来完全无害）。

★ 它为什么会被踩到：`executable_path` 我们**永远**传 chrome.exe（playwright 缓存的
Chrome 148），于是 `is_chrome` 恒为真 —— 这条路不是"某些情况下才走"，是**必走**。

## 事实 2 ★★：`BrowserSession(browser_profile=obj)` **不使用** `obj`

第一版探测脚本就是这么写的，结果两条支路**都**失败。查下去发现：

```python
session = BrowserSession(browser_profile=prof)
session.browser_profile is prof      # → False
```

构造时它会自己造一个 profile，于是 `_copy_profile()` **又跑了一遍**。
把 `prof.user_data_dir` 钉回原目录也没用 —— **钉在一个库根本没用到的对象上，等于没钉**。
（这个坑我踩了一次才改对，`pin_user_data_dir` 的注释里留着。）

正确姿势是**构造之后**钉在 `session.browser_profile` 上，已固化为
`runtime/browser.py:pin_user_data_dir(session, dir)`。

**被否掉的另一条路**：源码里 `_copy_profile` 有个豁免 —— 路径里含
`browser-use-user-data-dir-` 就跳过拷贝。所以"把我自己的目录命名成那样"技术上可行。
**不采用**：那是借一条会被收回的豁免。哪天库加一句"清理遗留的 browser-use-user-data-dir-*"
就轮到我们的登录态被删了 —— 而且是静默的。**我的登录态不能建立在库的临时文件命名约定上。**

**代价（如实记录）**：钉住之后，同一个 profile 目录不能被两个会话同时使用（Chrome 的
profile 锁）。以前库的拷贝行为顺带避开了这个问题。`devtools/login_pdd.py` 里为此加了
一个**大声的**重试（而不是静默等待）。

## 事实 3 ★★：cookie 能不能落盘，取决于**最后怎么关浏览器**（三组对照实测）

这是另一个独立的一半 —— 就算目录钉对了，cookie 也可能一个字节都没写下去。
三组对照（同一台机器、同一个脚本，只改关闭方式）：

| 关闭方式 | 盘上 `Default/Network/Cookies` 的行数 | 新会话读得到吗 |
|---|---|---|
| 设 cookie → 等 **35s** → `kill()` | 有 | ✔ |
| 设 cookie → 等 2s → `session.stop()` → `kill()` | **0** | ✘ |
| 设 cookie → 等 2s → `await session.cdp_client.send.Browser.close()` | **有（关闭后立刻）** | ✔ |

机制：Chrome 的 cookie store 是**攒着批量提交**的，不是写一次落一次盘。
所以"会话里 `document.cookie` 读得到"**不代表**盘上有 —— 实测里那个值在内存里、
而 SQLite 表是空的。

★ `session.stop()` **不算优雅退出**（这条最反直觉：名字里带 stop，看着像关干净了）。
能用的是标准 CDP 的 `Browser.close`（公开协议，不是私有 API）。已固化为
`runtime/browser.py:close_gracefully_and_flush()`：CDP 优雅关闭 → 等 2s → 兜底强杀；
**CDP 关闭失败时不假装成功**，而是 WARNING 明说"cookie 可能没落盘，请以随后的新会话复核为准"。

⚠️ 一条**无害但会吓人**的库噪声：`Browser.close` 之后库的
`StorageStateWatchdog.on_SaveStorageStateEvent` 会抛
`ConnectionError: Reconnection failed — CDP still not connected`。
它与登录态无关（正是我们主动断的连接），但第一次看到会以为关闭失败了 ——
写在这里，免得下次在同一个地方查半天。

### 测量工具本身错了两次（比结论更值得记）

- 第一次用 `dom_state.llm_representation()` 判断 cookie 在不在 —— **它不是确定性工具**
  （那段表示里不一定包含目标元素）。换成 `page.evaluate("(arg) => document.cookie", None)`。
- 第二次 `document.cookie` 读到了值，就以为成功了 —— 但**没带 `expires` 的是会话 cookie**，
  退出即丢，测出来的是"会话 cookie 没了"，不是"目录不对"。
  换成带 `expires` 的持久 cookie（真实站点的登录 cookie 也是持久的）。
- 最后真正可信的证据是**绕开浏览器去查 SQLite**：把 `Default/Network/Cookies` 拷出来查行数。

**教训**：一个结论"看起来成立"之前，先问一句"我的测量工具凭什么可信"。
这一节三条事实里，有两条是被换掉的测量工具救回来的。

## 这三条改变了哪几处设计

| 改动 | 因为哪条事实 |
|---|---|
| `runtime/browser.py:pin_user_data_dir()`（构造后钉） | 事实 1 + 2 |
| `runtime/browser.py:close_gracefully_and_flush()`（CDP 关 + 兜底） | 事实 3 |
| `devtools/login_pdd.py` 的**三阶段**流程：人工登录 → **新会话复核** → 才写标记 | 事实 1/3 —— **不复核就不知道登录态到底留没留下** |
| `runtime/profile.py:check_profile()` 把四种情况分成**四句不同的话** | 事实 1 的失败形态是静默的 → 必须让"没登录"和"配置没开"看起来不一样 |
| `config.py:USER_DATA_DIR` 默认为**空**（无状态） | 有状态的东西不能做默认值：CI 的 13 条浏览器用例会共用一个 cookie 目录 |
| `tasks/pdd_shop_overview.yaml`：不输入、不点击、不翻页、不重试 | R2 —— 首跑要的是**最小动作集**，把"只读"做成任务本身没有写动作，护栏只作纵深防御 |

## 守卫：`tests/test_profile_persistence.py`（真浏览器，本地硬门禁）

事实 1 是"有一天会变"的那类前提（库一升级就可能不成立），所以它不能只写在文档里。
两条用例，**同一套动作、只改一个变量**：

- **主判据**：钉住 → cookie 写进我们的目录 → 新会话读得回来；
- **对照**：不钉 → cookie 进库的临时目录 → 新会话**读不到**，且我们的目录里
  连 `Default/` 都没有（第二份独立证据，不只靠行为断言）。

没有第二条，第一条在"cookie 其实写哪都留得下"的世界里也会绿。而且对照组的关闭方式
**和主组完全相同** —— 第一版对照用的是 `kill()`，结果它红的原因是"强杀丢 cookie"
而不是"目录不对"，两条路都红，实验什么也没说明。

★ 对照组在"突然变绿"时用注释写明：那不是好消息也不是坏消息，而是
"库改了 `_copy_profile`"的信号，**该去看 `pin_user_data_dir` 还需不需要，
而不是删掉这条用例**。
