# 部署指南

本项目的几种运行方式，按推荐程度排序。配置项详解见 [configuration.md](./configuration.md)。

> 📌 前置依赖(仅方式 D 需要):Python ≥ 3.11 · Node ≥ 20 · [`uv`](https://docs.astral.sh/uv/) · `pnpm`（`npm i -g pnpm`）

---

## 方式 A:GHCR 现成镜像(免本地构建,多数用户推荐)

GitHub Actions 每次推送都会自动构建多架构镜像(linux/amd64 · arm64)并发布到 GHCR,直接拉取运行,本地无需 Python / Node,也不用现场 build:

```bash
docker run -d --name tsp -p 3018:3018 -v ${PWD}/data:/app/data ghcr.io/shy3130/tick-stock-panel:latest
# 打开 http://localhost:3018
```

- 需要配置时:从 `.env.example` 复制出 `.env`,命令里加 `--env-file .env`。
- 镜像默认**不含** stock-sdk 插件(合规考虑),也不含 `legacy-cpu` / `backtest` extras —— 老 CPU(无 AVX2)或需要 vectorbt 回测时,请用方式 B 通过 `BACKEND_EXTRAS` 自构建。
- 跑自己改过的代码:fork 后到仓库 Actions 页启用 workflow(fork 默认禁用),构建出的 `ghcr.io/<你的用户名>/tick-stock-panel` 用法相同。
- 想要 compose 全套挂载(`.env` / `tiers.yaml` / 数据卷):参考根目录 `docker-compose.yml`,把 `build:` 段换成 `image: ghcr.io/shy3130/tick-stock-panel:latest`。

更新到新版本:

```bash
docker pull ghcr.io/shy3130/tick-stock-panel:latest
docker rm -f tsp
# 重新执行上面的 docker run
```

---


## 方式 B:Docker Compose(本地构建,全套挂载)

```bash
cp .env.example .env
docker compose up --build
# 打开 http://localhost:3018
```

Docker 采用两阶段构建,前端 dist 拷进后端镜像,**单容器**运行,数据完全在自己手里。

> ⚠️ **stock-sdk 插件默认不打包(合规考虑)**
>
> stock-sdk 数据源本质是抓取第三方财经网站(如东方财富)的行情接口,未经对方授权,可能违反其服务条款并涉及交易所行情版权问题。**出于合规考虑,Docker 默认构建不再内置 stock-sdk 插件依赖**。
>
> - **默认行为**:`docker compose up --build` 构建出的镜像**不含** stock-sdk,插件不可用。
> - **如确需启用**(自行承担合规责任):
>   ```bash
>   docker compose build --build-arg INCLUDE_STOCKSDK=1
>   docker compose up -d
>   ```
> - 启用后镜像会额外内置 Node.js 运行时并预装 stock-sdk 依赖,插件开箱即用。
> - **建议优先使用 TickFlow 等正规授权数据源。**

更新到新版本:

```bash
git pull
docker compose up --build -d
```

### 容器不以 root 运行

`docker-compose.yml` 里的 app 和 dsa 都使用 `user: ${APP_UID:-1000}:${APP_GID:-1000}`。`.env` 里的 `APP_UID` / `APP_GID` 默认是 `1000`，和 Ubuntu 上的 `ubuntu` 用户一致。生产机上 `id -g ubuntu` 经常是 `1001`，`.env` 里就写成 `APP_GID=1001`。app 镜像把家目录放在 `/home/app`，uv 缓存放在 `/home/app/.cache/uv`，Codex 登录态挂到 `/codex-home`（`CODEX_HOME`）。dsa 镜像把家目录放在 `/home/dsa`。这些路径不在 `/root` 下面，因为基础镜像里 `/root` 只有 root 能进入。

已经用 root 跑过的机器，`data/` 里会有 root 拥有的文件。切换前先停容器，执行一次：

```bash
sudo chown -R 1000:1000 data
```

`1000:1000` 要和 `.env` 里的 `APP_UID` / `APP_GID` 相同，并且等于宿主机采集器用户的 uid/gid。用 `id -u ubuntu` 和 `id -g ubuntu` 确认；不是 1000 就把 `.env` 和这条 chown 改成那一对数字。

生产环境是 `APP_UID=1000`、`APP_GID=1001`。dsa 以前没有 `user:`，停容器之后只改 `data/` 也不够：sidecar 仍会把 `data/dsa` 写成 root，而且决策页会写回 `.env`。先停 app 和 dsa，再执行：

```bash
sudo chown -R 1000:1001 data
sudo chown 1000:1001 .env
chmod 600 .env
```

`.env` 通常是 `600`。app 只读挂载它；dsa 把定时设置写回去，所以容器用户必须是它的属主。Codex 目录通常是 `700`，容器用户也必须是属主，否则读不到。

然后：

```bash
python3 scripts/deploy_preflight.py --data-dir ./data
docker compose up -d
# 启用了 dsa profile 时还要重建 sidecar：
docker compose --profile dsa up -d --build --force-recreate
```

预检要求 `data/` 和 `data/news` 对这对 uid/gid 可写。`data/dsa` 存在时同样要可写，不存在时只提示。`.env` 的属主必须是这对 uid/gid，并且对它可写。采集器虚拟环境的 Python 不低于 3.10，并且 `data/` 下面（含 `data/dsa`）没有别人拥有的文件。失败时退出码不是 0。采集器虚拟环境的重建步骤在 [news-sources.md](./news-sources.md)。dsa 的写路径和迁移说明见 [dsa-integration.md](./dsa-integration.md)。

回滚：把 `.env` 里的 `APP_UID` 和 `APP_GID` 改成下面这样，再重建容器。app 和 dsa 都会重新以 root 运行。root 可以写已经属于 1000 的文件，所以不一定要把属主改回去。

```bash
APP_UID=0
APP_GID=0
docker compose up -d --force-recreate
```

如果确实要恢复成 root 拥有整个 `data/`，执行 `sudo chown -R root:root data` 之后，还要把收件箱交回采集器用户，例如 `sudo chown -R ubuntu:ubuntu data/news`。否则宿主机定时器写不进 `data/news/inbox`。

---

## 方式 C:本机 AI 代部署(小白推荐)

装一个本机 AI 编程助手(Trae / Codex / OpenCode / ZCode / WorkBuddy 等,任选其一),把 [README · 快速开始](../README.md#-快速开始) 里方式 C 的提示词原样发给它,AI 会自动完成克隆、装依赖、启动服务。适合完全不想碰命令行的用户;AI 最终执行的仍是方式 A / B / D 之一。

---


## 方式 D:Dev 模式(二次开发推荐)

由于刚开源近期更新频繁,推荐开发模式运行,可随时 `git pull` 同步最新代码。

```bash
git clone https://github.com/shy3130/tick-stock-panel.git
cd tick-stock-panel
cp .env.example .env       # 按需填 TICKFLOW_API_KEY(留空 = None 模式)
./dev.sh                   # Windows: .\dev.ps1
```

`dev.sh` 自动检查 / 下载依赖、释放端口、同时起前后端,Ctrl-C 一并关闭。默认:

- 后端 → <http://localhost:3018> · 前端 → <http://localhost:3011>
- 自定义端口:`BACKEND_PORT=8000 FRONTEND_PORT=5173 ./dev.sh`

### 手动分别启动(不想用 dev.sh)

```bash
# 后端
cd backend && uv sync --extra backtest   # 含回测依赖
# 老 CPU: uv sync --extra legacy-cpu
# 老 CPU + 回测: uv sync --extra legacy-cpu --extra backtest
uv run uvicorn app.main:app --reload --port 3018

# 前端
cd frontend && pnpm install && pnpm dev   # http://localhost:3011
```

---

## 老 CPU 兼容(avx2/fma 缺失)

如果运行时报 `avx2`/`fma` 缺失,或进程 `exit 132`,说明 CPU 不支持 AVX2 指令集(常见于老 VPS)。解决:

- **Dev 源码启动**:在根目录 `.env` 设置后运行 `./dev.sh` 或 Windows 的 `.\dev.ps1`;即使已有 `.venv`,启动器也会同步兼容内核
- **Docker**:在根目录 `.env` 设置后执行 `docker compose up --build`

```ini
BACKEND_EXTRAS=legacy-cpu          # 兼容老 CPU
BACKEND_EXTRAS=legacy-cpu backtest # 兼容老 CPU + 回测依赖
```

手动启动源码时，也可以在 `backend/` 目录直接执行 `uv sync --extra legacy-cpu`。不要设置 `POLARS_SKIP_CPU_CHECK`，它只会隐藏警告，实际执行不支持的指令时仍可能崩溃。

### 回测依赖说明

vectorbt → numba 体积较大,作为可选 extras(`uv sync --extra backtest`)。macOS / Intel 无预构建 wheel 时需 `brew install cmake` 现场编译。

---

## 更新代码(已部署用户必读)

拉取新版本只需一条命令(Dev / Compose 本地构建用户):

```bash
git pull
```

> 用方式 A 镜像直跑(无本地仓库)的用户:`docker pull ghcr.io/shy3130/tick-stock-panel:latest` 后删除旧容器重跑;compose 换 `image:` 的用户执行 `docker compose pull && docker compose up -d`。

**整个 `data/` 目录都不纳入 git** —— 行情 K线、财务、自选、回测、监控记录,乃至概念/行业扩展数据,全部是程序运行时生成/拉取的用户数据,`git pull` 物理上无法影响它们。新用户首次启动时,概念/行业两份扩展数据会自动从远程接口拉取,无需任何手动操作。

> ⚠️ **切勿使用以下命令"解决冲突"或"清理",它们会一次性删光 `data/` 下所有未被 git 跟踪的数据:**
> - `git clean -fdx`(最危险,会删掉所有 `.gitignore` 忽略的文件)
> - `git reset --hard`
> - 直接删除整个项目文件夹重新 `git clone`
>
> 若 `git pull` 报冲突,通常是本地误改了被跟踪的文件,请先 `git stash` 暂存再 pull,或单独联系作者,不要直接执行上面的命令。

---

## 访问密码设置(公网部署必读)

面板部署在公网服务器时,首次设置访问密码有限制 —— **必须从本机或内网访问**,以防公网上陌生人抢先设置密码锁死你的面板。

如果你在公网浏览器直接打开页面,会看到提示:

> 首次设置密码仅允许本机或内网访问,请通过 SSH/本地浏览器操作

有两种方式解决,任选其一。

### 方式一:环境变量预置密码(最简单,推荐)

在 `.env` 文件(或 Docker / 系统环境变量)里设置 `AUTH_PASSWORD`:

```bash
AUTH_PASSWORD='你的密码'
```

然后重启服务。启动时会自动:

1. 读取 `AUTH_PASSWORD`
2. 用 PBKDF2 哈希后写入 `auth.json`(`chmod 600`,只存哈希不存明文)
3. **之后这个环境变量就不再被读取** —— 是一次性的初始化

设完后即可用公网地址登录。登录页需要用户名:这份旧版共享密码使用用户名 `admin`,也可以把用户名留空。后续改密码请用页面 UI(`设置 → 修改密码`),不受环境变量影响。

**注意事项:**

- **密码至少 6 位**,否则会被跳过并记一条 warning 日志
- **仅在未设过密码时生效**。已设过密码后,改这里不会覆盖(避免重启时重置你在 UI 改的密码)
- 密码建议使用单引号包裹，避免 Docker Compose 插值 `$VAR`；启动时也会从只读挂载的原始 `.env` 初始化，兼容已有的未加引号配置
- `.env` 文件权限保持 `600`,**不要提交到 Git**
- 明文密码只存在于 `.env` / 环境变量中,落盘的是哈希,安全性等同 `auth.json`

**重置密码(忘密码时):** 删除或清空 `data/user_data/auth.json`,重启服务,会回到"未设密码"状态,此时 `AUTH_PASSWORD` 会重新生效。

```bash
rm data/user_data/auth.json   # 停服后执行,清空后重启
```

### 多用户账号

独立账号写在 `data/users.json`(可用 `AUTH_USERS` 指向其他文件,或内联只含 Argon2id/bcrypt 哈希的 JSON)。明文密码不会写入文件。

```bash
cd backend && uv run python ../scripts/manage_users.py add alice
cd backend && uv run python ../scripts/manage_users.py reset alice
cd backend && uv run python ../scripts/manage_users.py remove alice
```

`add` 和 `reset` 会生成强密码并只打印一次。登录失败按来源 IP 和用户名限流(5 次后锁定 5 分钟)。各账号会话分开,退出只注销自己,权限目前相同。`AUTH_PASSWORD` 仍是用户名 `admin`(或留空用户名)的旧版登录,不会覆盖账号文件。说明见 [deploy-password.md](./deploy-password.md)。

### 方式二:SSH 端口转发

不用改配置,在你**自己电脑**的终端执行(不是服务器上):

```bash
ssh -L 3018:127.0.0.1:3018 用户名@服务器IP
```

例如服务器是 `123.45.67.89`、用户名 `root`、面板端口 `3018`:

```bash
ssh -L 3018:127.0.0.1:3018 root@123.45.67.89
```

保持这个 SSH 连接**不要关**,然后在**自己电脑的浏览器**打开 `http://127.0.0.1:3018`。此时后端看到的客户端 IP 是 `127.0.0.1`(本机),能通过校验,正常显示设置密码界面。

**设完密码后**,SSH 连接可以断开 —— 密码已存进服务器,之后直接用公网地址 + 刚设的密码访问即可。

> 如果用 `PORT` 改过端口(比如 `PORT=8080`),两处都要替换:`ssh -L 8080:127.0.0.1:8080 root@IP`。

### 两种方式怎么选

| | 环境变量 | SSH 转发 |
|---|---|---|
| 操作 | 改一行配置 + 重启 | 一条 ssh 命令 |
| 需要改配置 | 是 | 否 |
| 适合 | Docker / 自动化部署 / 不熟 SSH | 临时设密码 / 能 SSH 到服务器 |
| 后续改密码 | UI(`设置 → 修改密码`) | 同左 |

推荐**方式一(环境变量)**,一次配置即可,Docker 部署尤其方便。

### 原理说明

- **为什么限制本机/内网?** 面板部署到公网后,任何人都能访问 URL。如果不限制,攻击者可以在你之前打开页面、设置一个密码,把你的面板锁死。
- **本机/内网如何判断?** 后端检查客户端 IP 是否属于 `127.0.0.1 / ::1 / 10.x / 192.168.x / 172.16-31.x`。
- **SSH 转发为什么有效?** `-L` 把本机端口通过 SSH 隧道转发到服务器的 `127.0.0.1`,等同于在服务器本地访问,客户端 IP 变成 `127.0.0.1`,通过校验。
- **反向代理注意:** 若面板在 Nginx 等反代之后,需正确配置 `X-Forwarded-For` 头,后端据此取真实客户端 IP。
