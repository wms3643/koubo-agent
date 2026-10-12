---
name: qc-reference
title: 成片质检参考
applies_to: 成片质检
summary: 16 项数字断言的含义与阈值、失败时先看什么、怎么取证复核
---

# 成片质检参考

`run_qc` 跑完会产出 `edit/qc_report.json`。它的 `checks` 是**固定 16 项**，
每项形如 `{id, ok, cause, detail}` —— 判读看 `id` 认出是哪一项，看 `detail` 拿数字。

## 断言清单（按链路顺序）

| id | 在查什么 |
|---|---|
| `streams_present` | 视频轨、音频轨都在（少了哪条后面全无意义） |
| `av_duration_equal` | 音视频轨时长一致 |
| `audio_no_clipping` | 没有削波（看 `max_volume`，单位 dB） |
| `no_black_frame_head` / `no_black_frame_tail` | 首/尾 2 秒内无黑帧 |
| `srt_no_bom` | 字幕档不带 BOM（带 BOM 的 SRT 有些播放器整条不显示） |
| `srt_has_entries` | 字幕条目数 > 0 |
| `srt_no_trailing_symbol` | 句末不带标点符号（排版要求） |
| `srt_monotonic` | 字幕时间**无重叠** |
| `srt_timeline_matches_video` | 末条字幕结束时间与视频时长对齐 |
| `edited_duration_vs_plan` | 实测时长（量自 `edited_final.mp4`）vs 计划时长 |
| `bgm_present` | BGM 真的混进去了（比对人声轨与混音后的 LUFS） |
| `bgm_not_overpowering` | BGM 没盖住人声 |
| `bgm_true_peak_ok` | 没有削顶（真峰值） |
| `bgm_covers_duration` | BGM 覆盖整片（短了会循环补齐，正常） |
| `bgm_has_fade_out` | 有淡出 |

## 数字阈值（实测合法值作参考）

| 项 | 上限/要求 | 一份合格成片的实测 |
|---|---|---|
| BGM 相对人声的响度增量 | **≤ 4.0 LU** | +1.50 LU |
| 真峰值 | **≤ −1.0 dBFS** | −5.7 dBFS |
| 开头/结尾黑帧 | 2 秒内不得有 | 无 |

## 失败时先看什么

1. **`cause` 字段** —— 它是**机器可读的**，编排层就是靠它决定回退到哪一步。
   不要去猜"可能是字幕错了"，直接看 cause。
2. **`failed_checks`** —— 只列出没过的项，比在 16 项里翻快。
3. **`frames` / `junction_frames`** —— 抓的帧。**审内容要真去看图**，别只看数字：
   数字全绿但画面里有半截字，这种事数字看不出来。
4. `route_hint` —— 内核给的"该退回哪一步"的提示。

## 两条容易误判的

- **`bgm_covers_duration` 显示"已循环补齐（正常）"** —— 这不是缺陷。
  BGM 比片长短时会开 `bgm_loop` 循环，属预期行为。
- **`srt_has_entries` 报出的条数是"字幕条"，不等于"逐字稿段数"** ——
  字幕会把连续短段合并，条数少于段数是正常的。

## 你不改产物

你的产物是**结论**。查出问题就把"哪一项、什么数字、该退到哪一步"写清楚交回，
需要动手修的是别的角色（字幕 / 剪辑 / 特效）。
