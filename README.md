<p align="center">
  <img src="imgs/logo.png" width="140" alt="code-graph logo">
</p>

<h1 align="center">code-graph</h1>

<p align="center">
  Claude Code 技能：为项目生成「模块 — 文件 — 函数」三级调用图，按关键词检索小子图，用图代替读源码。
  <br>
  <b>全部在本机执行</b>：扫描、解析、查询、可视化全程无网络请求，代码不出本地，无第三方依赖。
</p>

<p align="center">
  <a href="README.en.md">English</a> · 中文
</p>

---

## 这是什么

让 Claude 理解一个陌生项目，常规做法是把源码塞进上下文——项目一大就超 token。code-graph 反过来：

1. 先把项目结构解析成一张图（模块依赖、文件关系、函数调用）；
2. 存到项目根目录的 `.code-graph/`；
3. Claude 理解项目时先读图、按关键词检索小子图，只在图信息不够时局部读源码。

检索图代替读源码，省 token 的同时，跨文件依赖也说得清。

解析和查询全部跑在你自己机器上（只需要系统自带的 Python 3.8+），**代码内容不会上传到任何地方**。

## 功能

- **三级结构**：模块（import 级依赖）→ 文件 → 函数/类调用关系
- **哈希增量更新**：按内容哈希（sha256）检测变更，只重新解析变化的文件；`git pull` 拉来的别人改动也能发现
- **关键词检索**：返回小子图——命中符号的签名、文件、行号、调用者/被调用者，以及所属模块分片
- **自包含可视化**：`code-graph.html` 的数据、样式、脚本全部内联，双击即开（`file://`），不需要服务器
- **四种视图**：模块图 / 文件图 / 类图 / 函数图；节点按模块着色、按引用关系区域化布局
- **多语言**：Kotlin / Java / Python / TypeScript（含 JS）/ Go 解析类与函数；其他语言退化为文件级结构

## 截图

函数图视图（点击节点查看签名、文档注释、文件:行号、调用者/被调用者）：

![函数图](imgs/FuncGraphPage.png)

模块图视图（import 级依赖，节点按模块着色、按引用关系分堆布局）：

![模块图](imgs/ModuleGraphPage2.png)

生成物目录 `.code-graph/`（哈希库 + 图数据 + 模块分片 + 可视化页面）：

![目录结构](imgs/FolderContents.png)

## 安装

需要 [Claude Code](https://docs.anthropic.com/zh-CN/docs/claude-code/setup)（建议 1.0.0+）。把本仓库的 `SKILL.md` 和 `scripts/` 放到 Claude 的全局技能目录：

```bash
# macOS / Linux
git clone https://github.com/<your-name>/code-graph-skill.git
mkdir -p ~/.claude/skills
cp -r code-graph-skill ~/.claude/skills/code-graph
```

```powershell
# Windows (PowerShell)
git clone https://github.com/<your-name>/code-graph-skill.git
Copy-Item -Recurse code-graph-skill "$env:USERPROFILE\.claude\skills\code-graph"
```

装好后是**全局技能**，对所有项目自动生效，每个项目不用重复复制。唯一要求：Python 3.8+，无任何第三方包。

## 使用方法

日常不需要手动操作：Claude Code 在会话开始、接到代码修改/理解任务时会自动调用本技能（先更新图，再按关键词检索，图信息不足时才读源码）。

也可以直接跑脚本：

```bash
# 更新图（增量。Windows 用 python，其他用 python3）
python ~/.claude/skills/code-graph/scripts/update.py --root <项目根>
# Windows: python %USERPROFILE%\.claude\skills\code-graph\scripts\update.py --root <项目根>

# 按关键词检索
python ~/.claude/skills/code-graph/scripts/query.py --root <项目根> <关键词>

# 按文件检索 / 机器可读输出
python ~/.claude/skills/code-graph/scripts/query.py --root <项目根> --file src/main.kt
python ~/.claude/skills/code-graph/scripts/query.py --root <项目根> <关键词> --json
```

`update.py` 每次运行后会自动重新生成 `.code-graph/code-graph.html`，双击即可浏览：

- 顶部工具栏切换模块图 / 文件图 / 类图 / 函数图
- 点击节点，右侧面板显示详情（函数签名、文档注释、文件:行号、调用者/被调用者，可点击跳转）
- 拖拽节点重排、滚轮缩放、双击节点聚焦、搜索框跨视图检索
- 数千节点的项目也能流畅交互（Canvas 渲染 + 力导向模拟）

> `update.py` 也支持只重新生成 HTML（不重新扫描源码）：
> `python ...\scripts\render_html.py --root <项目根>`

## 工作原理

```
源码扫描 ──► 词法静态解析（类 / 函数 / 调用关系）──► sha256 对比 manifest（增量）
                                                          │
                                                          ▼
                                       写入 .code-graph/
                                       ├── manifest.json   内容哈希库
                                       ├── graph.json      图数据（modules/symbols/edges）
                                       ├── index.md        全局索引（定位模块）
                                       ├── modules/<id>.md 模块分片（签名 + 调用链 + AI 摘要）
                                       └── code-graph.html  自包含可视化
```

Claude 理解项目时：先读 `index.md` 了解结构 → 按任务关键词跑 `query.py` 拿到小子图 → 只在图信息不足时按 `file:` 定位读目标文件。语义摘要（模块职责）由 AI 写入 `modules/<id>.md`，可手动维护。

## 限制

- 调用关系是**词法级静态匹配**：多态分发、反射、动态 import、跨进程调用检测不到；同名函数（重载）会尽力消歧，仍无法解析时退化为「同文件优先，否则全连」，图标注为"近似"
- 接口 / 抽象方法（无函数体）不产生调用边（如 Room DAO、Android 系统回调的调用方在图外，属正常）
- 模块级 import 依赖只对基于 package 的语言（Kotlin/Java）解析；其他语言模块图只有节点
- 图是快照：AI 补的语义摘要可能与代码漂移，结构部分由 `update.py` 重建时自动刷新

详见 `SKILL.md`。

## 常见问题

**`.code-graph/` 目录要提交到版本库吗？**
不需要，它是生成物，建议加入 `.gitignore`。

**改了代码，图不更新？**
下次技能调用时自动哈希检测、增量更新；也可以在改动完成后手动跑一次 `update.py`。

**能用非 Python 环境吗？**
不能，解析脚本是 Python 的（3.8+，标准库，无第三方包）。

## License

[Apache-2.0](LICENSE)
