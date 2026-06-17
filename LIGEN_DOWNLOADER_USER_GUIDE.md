# 李根文献下载器使用说明

## 当前定位

这是一个本地文献下载管理器雏形，用来把 DOI 列表变成可追踪的下载任务。

它负责：

- 粘贴或导入 DOI / DOI URL
- 自动识别出版社
- 检测各出版社 Chrome 调试端口状态
- 启动登录预热
- 批量下载
- 输出结果表、日志和 PDF 文件夹

它不负责：

- 绕过验证码
- 绕过机构授权或出版社权限
- 自动使用非授权下载源

## 打开方式

双击桌面：

```text
C:\Users\logan\Desktop\李根文献下载器.bat
```

或在 PowerShell 中运行：

```powershell
& "C:\Users\logan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" `
  "C:\Users\logan\doi_harvest\scripts\launch_ligen_gui.py"
```

## 推荐输入

每行一个 DOI，例如：

```text
10.1016/j.ijadhadh.2021.102871
10.3390/polym9050184
10.1039/c6ra19883j
```

也可以输入 DOI URL：

```text
https://doi.org/10.1016/j.ijadhadh.2021.102871
```

## 推荐流程

1. 打开软件。
2. 把 DOI-only txt 内容粘贴进去，或选择输入文件。
3. 点击端口检测，确认目标出版社端口状态。
4. 先运行登录预热。
5. 在打开的浏览器窗口中完成机构登录和必要验证。
6. 回到软件运行下载。
7. 在输出目录查看：
   - `combined_download_results.csv`
   - `combined_downloaded_doi_filename_map.csv`
   - 分出版社 PDF 文件夹
   - 运行日志

## 当前聚氨酯 DOI 输入文件

全量可用 DOI-only：

```text
C:\Users\logan\doi_harvest\outputs\polyurethane_waterproof_latent_curing_20260526_v1_final_full_usable_doi_only.txt
```

核心优先读 DOI-only：

```text
C:\Users\logan\doi_harvest\outputs\polyurethane_waterproof_latent_curing_20260526_v1_core_only.txt
```

## 当前限制

- 目前是 Python 桌面版，还不是安装包。
- 主要支持 DOI/出版社下载链路；CNKI 需要作为单独模块接入。
- 登录、验证码、机构权限仍需要用户在浏览器中完成。
- 如果出版社页面改版，单个 provider 可能需要修。

## 下一步产品化

P0：

- 加入任务历史和断点续跑界面。
- 加入重复 DOI / 已下载 PDF 自动跳过。
- 把 CNKI / CAJ / 专利题录作为独立模块接入。

P1：

- 打包成 Windows exe。
- 做设置页：下载目录、Chrome 路径、端口、浏览器用户目录。
- 做失败原因中文解释和一键重试。

P2：

- 加自动更新。
- 加项目级语料库目录管理。
- 加 NotebookLM / graphify 输入导出。
