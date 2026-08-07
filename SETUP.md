# 交易复盘台 · 新闻抓取 — GitHub Actions 部署指南

## 架构

```
GitHub Actions (cron 每小时)
    │
    ├─ scraper.py  ← 9频道并行抓取 → 去重 → 写入 news/YYYY-MM-DD.json
    │
    ├─ git commit & push  ← 数据持续沉淀
    │
    └─ GitHub Pages  ← 静态站点自动部署
```

## 5 分钟部署步骤

### 1. 创建 GitHub 仓库

```bash
cd fin-desk-deploy
git init
git add .
git commit -m "初始提交"
```

在 GitHub 上创建新仓库（如 `fin-desk`），然后：

```bash
git remote add origin https://github.com/<你的用户名>/fin-desk.git
git branch -M main
git push -u origin main
```

### 2. 启用 GitHub Pages

仓库 → Settings → Pages →
- **Source**: Deploy from a branch
- **Branch**: `gh-pages` / `/ (root)`
- 点击 Save

等待 1-2 分钟，站点地址：`https://<你的用户名>.github.io/fin-desk/`

### 3. 验证

进 Actions 标签 → 选择 "新闻抓取 · 华尔街见闻" → **Run workflow** 手动触发一次。

### 4. 完成

- **自动抓取**: 北京时间 6:00-23:00 每小时整点自动运行
- **周度归档**: 每周一 6:00 自动合并 8-14 天前的数据
- **数据持久化**: 所有新闻数据随 git 仓库永久保存

## 定时规则

| 时区 | 时间 |
|------|------|
| 北京 (CST) | 每天 6:00, 7:00, 8:00 … 23:00 |
| UTC | 每天 22:00, 23:00, 0:00 … 15:00 |

周一 6:00 (北京) 额外执行周度归档。

## 文件结构

```
fin-desk-deploy/
├── scraper.py          ← 抓取脚本（纯标准库）
├── index.html          ← 交易复盘台前端
├── .github/
│   └── workflows/
│       └── scrape.yml  ← GitHub Actions 配置
├── news/
│   ├── index.json      ← 日期索引（保留30天）
│   └── YYYY-MM-DD.json ← 每日新闻
└── news-archive/
    ├── index.json      ← 归档索引
    └── YYYY-WXX.json   ← 周度归档
```

## 可选：CloudStudio 同步

如果仍想保持 CloudStudio 部署，在 WorkBuddy 中保留现有的 CloudStudio 部署自动化，
两个平台互补：GitHub 做数据沉淀 + Pages 做备份站点，CloudStudio 做主力展示。
