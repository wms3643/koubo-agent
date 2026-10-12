---
name: qc-reference
title: 成片质检参考
applies_to: 成片质检
summary: 质检项目、关键阈值与问题定位方式
---

# 成片质检参考

`run_qc` 将检查结果写入 `edit/qc_report.json`。检查项通常包含 `id`、`ok`、`cause` 和 `detail`；具体项目集合取决于目标类型，以实际报告及 `pipeline/stage6_qc.py` 为准。

## 检查范围

| 类别 | 检查项示例 |
| --- | --- |
| 媒体流 | `streams_present`、`av_duration_equal`、`audio_no_clipping` |
| 画面 | `no_black_frame_head`、`no_black_frame_tail` |
| 字幕 | `srt_no_bom`、`srt_has_entries`、`srt_monotonic`、`srt_timeline_matches_video` |
| 剪辑 | `edited_duration_vs_plan` |
| 音乐 | `bgm_present`、`bgm_not_overpowering`、`bgm_true_peak_ok`、`bgm_covers_duration`、`bgm_has_fade_out` |

混音后的 BGM 相对人声响度增量上限为 4.0 LU，真峰值上限为 -1.0 dBFS。不同 `target` 的检查集合并不相同，不能用完整成片的检查要求判断短测试片。

## 问题定位

1. 读取报告中的 `cause` 与 `failed_checks`，确定失败项目及测量值。
2. 查看 `frames` 和 `junction_frames` 指向的图像，核对字幕遮挡、切点和画面异常。数字检查不能覆盖全部视觉问题。
3. 结合 `route_hint` 与[回退矩阵](../docs/contracts.md#三回退矩阵)给出修复目标，并附相关证据。

BGM 循环补齐属于配置允许时的正常行为。字幕条数可能少于逐字稿段数，因为相邻短段可以合并。质检角色负责形成结论与证据，不直接修改其他角色的产物。
