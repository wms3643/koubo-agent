---
name: hyperframes-craft
title: 动效工程排查参考
applies_to: 特效设计
summary: 静态检查、常见渲染问题及排查顺序
---

# 动效工程排查参考

## 检查顺序

动效工程先由 `design_overlay` 生成，再运行 HyperFrames lint；静态检查通过后才执行 `render_overlay`。渲染通常比静态检查耗时，并会生成较大的透明视频文件。

在项目的 `overlays/` 目录运行：

```powershell
./node_modules/.bin/hyperframes.cmd lint
./node_modules/.bin/hyperframes.cmd lint --verbose
```

常规模式只输出错误与告警数量；需要检查信息级规则时使用 `--verbose`。

## 常见问题

### `gsap_exit_missing_hard_kill`

逐帧 seek 可能落在淡出动画结束之后，导致元素保持可见。应在 clip 的结束边界显式设置透明度：

```js
tl.set("#tip1", { opacity: 0 }, 26.98);
```

边界秒数应按对应 clip 的实际结束时间调整。

### `font_family_without_font_face`

若 `font-family` 使用的族名没有对应的 `@font-face` 声明，lint 会报错。检查每个族名与工程中的字体声明是否一致。

## 渲染与覆盖

`render_overlay` 输出带 alpha 通道的 ProRes 4444 透明层，后续由 `apply_overlay` 叠加到视频。渲染前应确认工程与时点，避免重复生成大文件。内核期望项目目录下存在 `overlays/node_modules/.bin/hyperframes.cmd`；缺失时会返回 `missing_hyperframes`。GSAP 由本地 `vendor/gsap.min.js` 提供，渲染不应依赖外部 CDN。

`design_overlay` 会重写 `overlays/index.html`。手工修正该文件后再次执行设计阶段会覆盖改动；应修正上游生成约束，或保留当前文件继续完成本次渲染。

工程结构和时间线注册规则由运行时提示词规定；本文件用于排查执行问题。
