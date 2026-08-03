# zibllCrawler

一个面向 Zibll 主题 WordPress 站点的自动化爬虫与任务调度框架原型。
项目采用模块化设计，将登录适配、业务逻辑与基础设施解耦，支持通过数据库配置多账号、多站点与多任务调度。

## 功能特性

- **多账号 / 多站点管理**：账号与网站独立维护，通过关联表灵活组合。
- **数据库驱动的登录适配器选择**：不同站点可配置不同的登录适配器，实现通用逻辑与特例逻辑分离。
- **Session 复用与 Cookie 持久化**：运行前自动探测数据库中保存的 Cookie 是否有效，有效则跳过登录；支持仅导入 Cookie、不导入密码的账号。
- **环境检测与初次运行初始化**：启动时自动检查 Python 版本、依赖、项目文件完整性与数据库状态，辅助完成首次部署。
- **任务级执行日志**：每次调度执行结果写入 `execution_logs` 表，便于追踪与去重。
- **全局线程池**：基于 `concurrent.futures.ThreadPoolExecutor` 并发执行多个调度任务。
- **循环调度器**：支持 `now` / `fixed` / `window` / `interval` 四种调度类型，每 60 秒扫描一次到期任务。
- **全局代理支持**：启动时可通过参数设置全局代理，并选择代理失败时退出或禁用代理继续运行。
- **批量站点适配性检测**：通过 `--detect` 参数批量检测 URL 列表是否适配当前登录适配器。
- **网站别名**：`websites` 表支持 `aliases` 字段，同一站点的多个域名可配置为别名；调度执行时优先使用主域名，仅当主域名无法通信时才尝试别名。
- **手动数据导入**：通过 `--import` 进入交互式菜单，或通过 `--import-site` / `--import-task` / `--import-account` 非交互式导入网站、任务、账号；导入时进行 URL/账号校验、适配性检测与登录测试。
- **业务模块纯洁性**：`business/` 下仅存放业务函数，由 `core/` 统一注入 `session` 与执行上下文。
- **密码加密存储**：`accounts` 表密码通过 `core/password_crypto.py` 加密后落库。

## 项目结构

```text
zibllCrawler/
├── main.py                 # 主程序入口
├── logger_setup.py         # 全局日志配置
├── requirements.txt        # Python 依赖清单
├── config/                 # 配置文件
├── adapters/               # 登录与会话适配器
├── business/               # 业务函数
├── core/                   # 基础设施（连接池、线程池、业务加载器、执行日志、密码加解密、代理配置、调度计划、环境检测）
├── sqldb/                  # 数据库初始化脚本
└── logs/                   # 日志输出目录（运行时生成，已被 .gitignore 排除）
```

各模块详细职责见代码内文档字符串。

## 环境要求

- Python 3.10+
- 依赖见 `requirements.txt`（仅包含项目源码中直接导入使用的包：`requests`、`PyYAML`、`cryptography`）

## 安装与运行

### 1. 创建虚拟环境并安装依赖

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 初始化数据库

```bash
python sqldb/init_db.py
```

该脚本会在 `sqldb/` 下创建 SQLite 数据库，包含项目所需的全部表结构、索引与触发器。

> 首次运行 `main.py` 时，若 `config/settings.yaml` 中 `initialization.status` 为 `"pending"` 或不存在，会自动调用 `core/env_check.py` 进行环境检测，并在检测通过后自动初始化数据库、标记初始化状态。

### 3. 写入站点与账号数据

项目使用数据库管理目标站点、账号、任务与调度信息。表结构说明见 `sqldb/init_db.py`，也可使用 `--import` 交互式导入或在本地编写初始化脚本写入真实数据：

```bash
python main.py --import
# 或手动使用数据库管理工具写入 websites、accounts、site_accounts、tasks、schedules 等表
```

生产环境请勿在代码中明文保存密码，建议使用加密或环境变量注入方案。所有账号密码均由 `core/password_crypto.py` 加密后写入数据库。

### 4. 运行主程序

```bash
python main.py
```

主程序进入循环调度模式，每 60 秒扫描 `schedules` 表中已启用的调度：
- `now`：启动时立即执行一次，执行后自动禁用。
- `fixed`：每天固定时间执行（如 `08:00`）。
- `window`：每天时间窗口内随机执行一次（如 `08:00-10:00`）。
- `interval`：按固定间隔执行（如 `30` 表示 30 分钟）。

测试循环调度时可限制循环次数，避免程序一直运行：

```bash
python main.py --max-loops 3
```

### 使用代理

```bash
python main.py --proxy host:port
# 代理测试失败时禁用代理继续运行
python main.py --proxy host:port --proxy-fail-action continue
# 自定义代理测试 URL
python main.py --proxy host:port --proxy-test-url https://example.com/generate_204
```

### 批量检测站点适配性

准备一个每行一个站点根 URL 的文本文件（如 `sites.txt`）：

```bash
python main.py --detect sites.txt --detect-output compatible_sites.txt
```

### 手动导入数据

项目提供交互式与非交互式两种导入方式，导入完成后程序自动退出，不进入调度循环。

```bash
# 交互式导入菜单（支持导入网站、任务、账号）
python main.py --import

# 非交互式导入网站，同时进行适配性检测与可选的测试账号登录
python main.py --import-site \
  --site-url https://example.com \
  --site-name example \
  --site-test-username your_username \
  --site-test-password your_password

# 非交互式导入任务，会校验 business 模块/函数是否存在
python main.py --import-task \
  --task-name daily_checkin \
  --task-module checkin_business \
  --task-func perform_checkin \
  --task-desc "每日签到任务"

# 非交互式导入账号并绑定已有网站与任务，登录测试通过后落库
python main.py --import-account \
  --account-username your_username \
  --account-password your_password \
  --account-site https://example.com \
  --account-task daily_checkin \
  --schedule-type now

# 非交互式仅导入 Cookie（不导入密码）
python main.py --import-account \
  --account-username your_username \
  --account-cookie "wordpress_logged_in_xxx=...; PHPSESSID=..." \
  --account-site https://example.com \
  --account-task daily_checkin \
  --schedule-type now
```

## 配置说明

### 日志配置

修改 `config/settings.yaml`：

```yaml
logging:
  level: "INFO"        # DEBUG, INFO, WARNING, ERROR
  console: true        # 是否输出到终端
  file: "logs/app.log"  # null 表示不写入文件

initialization:
  status: "initialized"  # "pending" 首次运行检测并初始化；"initialized" 跳过检测
```

日志文件 `logs/app.log` 采用**新记录前置**写入方式，打开文件即可直接看到最新日志，无需翻到底部。该方式适合日志量不大的本地调试场景，若日志量很大请改用追加式日志策略。

### 数据库表说明

核心表包括：

- `websites`：目标站点信息；`url` 为第一域名，`aliases` 为 JSON 数组，存放同一站点的其他可访问域名
- `accounts`：账号凭证（已加密存储；仅使用 Cookie 的账号密码字段可为空）
- `site_accounts`：站点与账号的关联配置，可指定登录适配器
- `tasks`：业务任务定义（模块名、函数名）
- `schedule_type`：`now` / `fixed` / `window` / `interval`
- `schedule_value`：根据类型填写时间或间隔，如 `08:00`、`08:00-10:00`、`30`
- `next_run_at`：下次执行时间，由程序自动维护
- `is_enabled`：是否启用
- `execution_logs`：每次任务执行的日志与耗时

表结构说明与 SQLite 建表脚本见 `sqldb/init_db.py`。

## 食用指南（新手友好版）

> 本项目是一个仓促完成的小玩具/原型，笔者在开发过程中借助了 Kimi AI 的辅助，所以代码里可能藏着一些只有我和 AI 才看得懂的默契（bug）。如果你在食用过程中遇到任何问题，欢迎在 Issues 里提，虽然我不一定修，但一定会看。

### 第一步：把环境跑起来

```bash
python -m venv .venv
source .venv/bin/activate  # Windows 请用 .venv\Scripts\activate
pip install -r requirements.txt
```

### 第二步：让项目自检并启动

首次运行时，`main.py` 会自动检查 Python 版本、依赖、项目文件完整性以及数据库状态。如果 `config/settings.yaml` 里的 `initialization.status` 是 `"pending"` 或者压根没有这行，它会帮你初始化数据库并标记为已初始化。所以你只需要：

```bash
python main.py
```

如果它报错，请认真看终端里的修复建议，百分之八十是没装依赖或者 Python 版本太低。

### 第三步：往数据库里塞数据

项目本身不带任何站点或账号数据，一片空白。你可以：

- 使用交互式导入（最推荐新手）：
  ```bash
  python main.py --import
  ```
  按照提示输入网站 URL、别名、账号、密码、选择任务即可。

- 使用非交互式导入（适合脚本化）：
  ```bash
  python main.py --import-site --site-url https://example.com --site-name example
  python main.py --import-task --task-name daily_checkin --task-module checkin_business --task-func perform_checkin
  python main.py --import-account --account-username your_username --account-password your_password --account-site https://example.com --account-task daily_checkin --schedule-type now
  ```

- 如果你熟悉 SQLite，也可以直接连 `sqldb/zibllcrawler.db` 写入数据。

### 第四步：观察日志

日志默认写在 `logs/app.log`，并且是最新的日志在最上面，方便你直接看到刚才发生了什么。如果日志里出现敏感信息，那说明你自己填的，项目代码里可没有。

### 一些注意事项

- 本项目的滑块验证码逻辑基于对前端 JS 的逆向分析，**只应在自有站点或已获得授权的环境下使用**。
- 账号密码会加密后存入数据库，但生产环境请务必设置 `ZIBLLCRAWLER_ENCRYPTION_KEY` 环境变量，不要使用内置默认密钥。
- 循环调度默认每 60 秒扫描一次，测试时记得加 `--max-loops N`，否则它会一直跑下去。
- 如果你发现某站点登录适配不上，可能是该站点关闭了验证码或修改了接口，目前只适配了通用的 Zibll 滑块验证码登录流程。

## 扩展指南

### 新增业务任务

1. 在 `business/` 下创建新的业务模块，实现 `session` 为第一参数的函数。
2. 在 `business/__init__.py` 中导出。
3. 在 `tasks` 表中注册 `module` 与 `func`。
4. 在 `schedules` 表中关联站点账号与任务。

### 新增登录适配器

1. 在 `adapters/` 下创建新的适配器模块，继承 `BaseLoginAdapter`。
2. 实现 `login(...)` 方法，返回有效的 `requests.Session`。
3. 可选实现 `execution_url(base_url)` 以处理跨域等特例。
4. 在 `adapters/factory.py` 中注册适配器名称。
5. 在 `site_accounts.login_adapter` 字段中使用该名称。

## 注意事项

- 本项目的滑块验证码逻辑基于对前端 JavaScript 的逆向分析，仅应在自有站点或获得授权的环境下使用。
- 当前调度器已实现 `now` / `fixed` / `window` / `interval` 类型，`now` 类型启动时执行一次，`fixed` / `window` / `interval` 在循环调度中自动执行。
- 账号密码在数据库中通过 `core/password_crypto.py` 加密存储；生产环境请设置环境变量 `ZIBLLCRAWLER_ENCRYPTION_KEY` 并使用强随机主密钥。

## 许可证

本项目为学习与研究用途的原型代码，**禁止用于商业用途**。代码按“现状”提供，作者不对使用本项目产生的任何后果负责。请在遵守相关法律法规及目标站点服务条款的前提下使用。

若需引用或二次开发，请注明本项目来源，并同样保持非商用、学习研究的性质。
