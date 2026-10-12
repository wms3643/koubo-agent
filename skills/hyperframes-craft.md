---
name: hyperframes-craft
title: 动效工程排查参考
applies_to: 特效设计
summary: lint 怎么跑、两条已踩的坑及修法、渲染的代价与排查顺序
---

# 动效工程排查参考

## 排查顺序（先便宜后昂贵）

```
写工程 (design_overlay，秒级、只写文件)
  → 跑 lint（秒级，纯静态检查）
    → 通过才渲染 (render_overlay，分钟级、产物上百 MB)
```

⛔ **不要跳过 lint 直接渲染** —— 渲染失败要等几分钟才知道，而 lint 一秒就能告诉你。

### 怎么跑 lint

在**项目自己的** `overlays/` 目录下：

```bash
./node_modules/.bin/hyperframes.cmd lint            # 只看问题
./node_modules/.bin/hyperframes.cmd lint --verbose  # 连 info 级一起看
```

通过时它只输出 `0 errors, 0 warnings`（**不列规则**）。所以想确认"确实一条都没漏"，
用 `--verbose`。

## 两条真踩过的坑（都属"lint 能拦住、不看就会白渲一遍"）

### 1. `gsap_exit_missing_hard_kill`

**症状**：退场动画淡到"下一个 clip 的开始秒"就结束了，但渲染是**逐帧 seek**，
会落进淡出**之后**，留下残余的可见状态（画面里残留一行字）。

**修法**：在 clip 的**结束边界**再补一条硬杀：

```js
tl.set("#tip1", { opacity: 0 }, 26.98);   // ← 边界秒数，硬杀
```

### 2. `font_family_without_font_face`

**症状**：`font-family` 里写了 `system-ui` / `"Microsoft YaHei"`，但工程里**没有**对应的
`@font-face` 声明 —— lint 会拒收"没有声明的字体族"。

**修法**：`font-family` 里出现的**每一个族名**，都要能在工程里找到同名 `@font-face`。
自查一句话：**把 font-family 里所有族名抄出来，逐个去 @font-face 里找。**

## 渲染的代价（别反复渲）

- 输出是**透明叠加层**（ProRes4444 **带 alpha**）—— 不是直接改视频，
  最后靠 `apply_overlay` 叠到成片上。
- 1080p 实测**约 132 秒**、产物**上百 MB**。
- 任务只要"看看效果"就渲一层，**别反复渲**。要不要渲这种判断交回主管。

## 两个环境事实

- **HyperFrames 是"每个项目各一份"的**。内核要求
  `<项目>/overlays/node_modules/.bin/hyperframes.cmd` 存在；缺了就报 `missing_hyperframes`。
  常规做法是**唯一实体 + 目录联接**（不要每个项目复制一份 343 MB）。
- **GSAP 用本地文件**（`./vendor/gsap.min.js`），不要用 CDN —— 渲染时不该依赖外网。

## ⛔ 一个会让排查白费的坑

`design_overlay` 会**重写** `overlays/index.html`。
所以 **"手工改好工程再重跑"是保不住改动的** —— 重跑会用同一份提示词生成一份新的。
真出了 lint 问题：**要么改上游（提示词的硬契约清单），要么这一版就手工渲完往下走**，
不要在"手改工程"上耗时间。

## 硬契约在别处

HyperFrames 的**结构硬契约**（根元素属性、`data-start`/`data-duration`、时间线注册方式等）
写在你的系统提示词里 —— 那些是每次都必须遵守的。这份文件装的是**排查用的参考**。
