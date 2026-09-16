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

### 一件没关掉的事（刻意留着）

`continue-on-error: true` 挂在 browser job 层，导致**红的浏览器 job 在 run 列表上
显示为绿**（实测 run `35116972105`）。已提出第三条路（让探针升格成闸门：
环境不可用则 skip、代码坏了则真红），但它改变的是 CI 姿态、不是技术方案，
**需要明确拍板才动**。在那之前，这个已知偏差就留在这份文档里，而不是留在"我以为已经解决了"里。
