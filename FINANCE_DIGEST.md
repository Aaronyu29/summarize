# 美股视频研究助手：本地样板与飞书交付

工作仓库：`https://github.com/Aaronyu29/summarize.git`；开发分支：`feat/rhino-finance-digest`。

## 当前完成范围

- 以 2026-10-22 抓取时的频道列表为准，取得 RhinoFinance 最近三条公开、已结束、超过三分钟的视频，发布时间分别为北京时间 10 月 22、21、20 日。
- 保存频道 XML、元数据、720p 无声视频、独立音轨、完整自动转写、118 张时间戳截图。
- 本次助理阅读完整转写、检查关键画面，整理 21 个重点，报告选用 20 张画面；输出 Markdown、HTML 和结构化分析 JSON。
- 校验观点证据引用、时间戳、音轨和图片 SHA256；保留分析版本；SQLite 保存逐视频分析及全文检索索引。
- 飞书上传器默认仅本地预检：Markdown 导入 docx、上传并绑定本地截图、回读核验、最后向 webhook 发送文档链接；已实现离线模拟测试。

云端无人值守流程已于 2026-10-23 部署到独立目录 `/opt/rhino-video-digest`，北京时间每天 10:45、14:30 扫描。`finance_cloud.py` 自动处理新视频；`finance_sample_analysis.py` 仅保留最初人工审阅样板的来源。10 月 17 日历史验收及 10 月 23 日当天新视频均已跑通下载、分批转写、视觉分析、二次审核、归档、飞书图文及 Lark 通知。真实重复扫描已验证没有重复分析或发送。

部署、资源限制、质量门槛和验收链接见 [云端运行记录](deploy/finance/README.md)。原始材料、Markdown 和分析版本已同步到本地 `research-data/cloud-runs/2026-10-23/` 并加入全文检索；凭据和 cookies 不在备份中。

## 样板位置

```text
research-data/
  library.sqlite
  rhinofinance/2026-10-22/
    brief-v1.md                    # 主交付物，图片使用相对路径
    brief-v1.html                  # 本地图文阅读版
    analysis.json                  # 当前编辑稿
    analysis-v1.json               # 不可覆盖的分析版本
    manifest-v1.json               # 原始材料和交付文件校验值
    discovery.json
    channel-feed.xml
    <video_id>/
      <video_id>.info.json
      media.f136.mp4
      media.f139.m4a
      transcript.asr.json
      transcript.asr.txt
      frames.json
      frames/frame-*.jpg
      contact-*.jpg
```

原始视频和凭据不提交 Git；上游核心实现保持独立。SQLite 可由原始档案重建，不能只备份索引而丢弃字幕和图片。

## 本地生成与检索

Python 3.12；视频抓取/截图/转写分别使用 yt-dlp、imageio-ffmpeg、Pillow、faster-whisper。整理、证据验证和索引只依赖 Python 标准库；上传器还需要 requests。上游 CLI 的开发环境按原项目的锁文件安装。

```powershell
python scripts/finance_collect.py --output research-data/rhinofinance/新的快照目录
# 抓取、转写和人工核对后，提供带证据引用的 analysis.json：
python scripts/finance_archive.py research-data/rhinofinance/2026-10-22/analysis.json
python scripts/finance_archive.py --query ISRG
python -m unittest discover -s scripts -p 'test_finance*.py' -v
```

本机验证发现最新版 CTranslate2 与 ONNX Runtime 有原生访问冲突，PyAV 19 与本次转写器接口不兼容。样板实际使用独立工具目录中的 `faster-whisper 1.2.1 + CTranslate2 4.6.0 + PyAV 15.1.0 + setuptools 80.9.0`，CPU INT8、4 线程、beam size 5、关闭可选 VAD，成功处理三条完整音轨。模型通过大小和 SHA256 校验。云端 Ubuntu 独立 Python 环境已实测同一兼容组合；采用单核、每次最多 5 分钟音频的分批处理，完成后释放内存并保存断点。

`finance_model_download.py` 是本次公开 small 模型的可恢复下载辅助工具，不是视频抓取器。模型下载缓存不属于需要备份的研究原始材料。

## Markdown → 飞书 → webhook

已阅读用户提供的《用 Markdown 直接创建飞书文档：官方 API 能力详解.md》，并对照官方导入流程与 SDK。使用导入任务，而不是逐段手工排版。

Markdown 的相对图片路径仅供本地使用。上传器先把图片换成唯一占位段落，导入文本，再按位置建立图片块，上传图片并以 `replace_image` 绑定，回读确认后替换占位文字。不会把本地路径当作公网图片地址，也不需要公开图床。

1. 可直接使用 `--env-file C:\githubProject\.env-lark`；支持现有字段 `APPID`、`APPSecret`、`LARK_WEBHOOK`。文件只读，密钥不复制到仓库。也可复制 `.env.finance.example` 为 `.env.finance` 使用标准字段。
2. `FEISHU_FOLDER_TOKEN` 可选；未指定时导入应用默认根目录。指定时选择已授权且预期读者可访问的云盘文件夹。文档创建成功不等于群成员自动取得阅读权限，需真实验证。
3. 机器人如开启签名校验，还需填独立的 `LARK_WEBHOOK_SECRET`，不是应用的 App Secret。
4. 先运行默认本地预检；只有显式 `--execute` 才访问飞书并发送消息。

```powershell
python scripts/finance_feishu.py research-data/rhinofinance/2026-10-22/brief-v1.md --title 'RhinoFinance 三期研究简报'
python scripts/finance_feishu.py research-data/rhinofinance/2026-10-22/brief-v1.md --title 'RhinoFinance 三期研究简报' --env-file .env.finance --execute
```

中国版飞书默认 `--platform feishu`；国际版应用使用 `--platform lark`。应用的创建文档、编辑文档、导入、素材上传、读取权限以及目标文件夹授权须在真实联调时验证。发送链接不会自动改变文档权限。

上传回执保存在报告旁的 `.feishu-state-*.json`，保存 ticket、文档链接、图片块状态和 webhook 结果，不保存应用密钥、access token 或完整 webhook。相同内容与目的地再次执行会返回既有投递结果；已知 ticket 可继续查询。创建导入、创建图片块、发送 webhook 遇到不确定网络结果时，保留 pending 状态停止，需核实远端后再恢复，避免重复文档或消息。

## 检查状态

新增流程的 38 项测试已在本地和 Linux 通过；覆盖错误证据、越界路径、归档版本、重复投递、图片绑定、截断、未知发送结果、明确拒绝后恢复、现有配置别名、应用根目录及签名。

真实联调：`larkman` 的四项应用身份文档权限已开通。单期样板已成功导入飞书，9 张关键画面已绑定并读回核验，正文标题与图片占位符检查通过，Lark webhook 已返回成功；本地回执用于避免重复投递。登录用户的浏览器已确认可正常阅读。Mermaid 原生画板权限尚未配置。脱敏记录位于样板目录的 `feishu-connection-status.json`。

上游 `pnpm install --frozen-lockfile` 与 build 成功；格式、本地化、lint 和 typecheck 通过。上游全量测试在当前 Windows 环境为 3726 通过、129 失败、52 跳过，涉及 POSIX 子进程、路径及已有摘要测试等。没有修改这些上游功能去掩盖失败，这些上游 Windows 测试失败仍保留记录，不代表本次独立 Python 云端流程未验收。全量日志在 `.cache/checks.log`。

## 官方参考

- [创建导入任务](https://open.feishu.cn/document/server-docs/docs/drive-v1/import_task/create)
- [查询导入结果](https://open.feishu.cn/document/server-docs/docs/drive-v1/import_task/get)
- [上传素材](https://open.feishu.cn/document/server-docs/docs/drive-v1/media/upload_all)
- [官方 CLI 导入实现](https://github.com/larksuite/cli/blob/main/shortcuts/drive/drive_import_common.go)
- [自定义机器人与签名](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)
