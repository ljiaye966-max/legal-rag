# AI 法律案例推理系统：本地数据库 + 通义千问 RAG 完整版

本项目是课程设计可运行完整版，包含前端页面、后端接口、本地数据库持久化、知识库导入、账号管理、通义千问 RAG 问答。

## 已完成能力

- 登录、退出、角色权限控制
- 账号密码存入数据库，密码使用哈希保存
- 管理员可新增账号、停用账号、重置密码
- 支持 SQLite 默认本地数据库
- 支持切换到本机 MySQL 数据库
- 支持知识库文本录入和文件上传
- 使用 Chroma 持久化保存知识库向量，默认目录为 `storage/chroma`
- 使用 LangChain 编排 RAG：Prompt、通义千问模型、输出解析组成调用链
- 支持 TXT、PDF、CSV、JSON 知识库文件
- 知识库可编辑、发布/下线、删除
- 知识库内容入库并切分为检索文本块
- 支持关键词检索 + 向量相似度融合排序
- 支持接入通义千问，让模型基于检索到的知识库证据回答
- 支持案情录入、要素抽取、RAG 推理、引用溯源
- 支持历史报告、Markdown 导出、反馈和审计日志

## 启动方法

第一次运行先双击：

```text
install.bat
```

安装完成后双击：

```text
start.bat
```

浏览器打开：

```text
http://127.0.0.1:8001
```

如果 8001 被占用，把 `start.bat` 里的端口改成 8002。

## 默认账号

| 角色 | 用户名 | 密码 |
| --- | --- | --- |
| 普通用户 | user01 | 123456 |
| 知识库管理员 | kb_admin | 123456 |
| 系统管理员 | admin | 123456 |

## 使用 SQLite

默认就是 SQLite，不需要安装 MySQL。数据库文件会自动生成：

```text
storage/legal_rag.db
```

Chroma 向量库会自动生成：

```text
storage/chroma
```

## 连接本机 MySQL

打开 `.env`，把配置改成：

```text
DB_TYPE=mysql
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=请改成你的MySQL密码
MYSQL_DATABASE=legal_rag
```

项目启动时会自动创建 `legal_rag` 数据库和所需表。

## 接入通义千问

打开 `.env`，填写你的 DashScope API Key：

```text
USE_LLM=true
CHAT_MODEL_NAME=qwen-plus
DASHSCOPE_API_KEY=你的API_KEY
```

如果也想用通义千问的向量模型，把下面打开：

```text
USE_DASHSCOPE_EMBEDDING=true
EMBEDDING_MODEL_NAME=text-embedding-v4
```

不打开向量模型时，系统会使用本地向量算法，仍然可以完成 RAG 演示。

## 接入阿里云短信认证

默认 `SMS_PROVIDER=local` 是本地演示模式，验证码会返回到页面。要让注册验证码真实发送到手机，请先在阿里云开通“号码认证服务”的“短信认证服务”，并为 RAM 用户授予 `AliyunDypnsFullAccess`。

然后打开 `.env`，填写后端专用的 AccessKey：

```text
SMS_PROVIDER=aliyun_sms_verify
ALIYUN_ACCESS_KEY_ID=你的RAM用户AccessKey_ID
ALIYUN_ACCESS_KEY_SECRET=你的RAM用户AccessKey_Secret
ALIYUN_SMS_VERIFY_SIGN_NAME=恒创联众
ALIYUN_SMS_VERIFY_TEMPLATE_CODE=100001
ALIYUN_SMS_VERIFY_ENDPOINT=dypnsapi.aliyuncs.com
```

不要把 `AccessKey Secret` 放到微信小程序前端、网页前端或代码仓库中。真实短信模式下，验证码只会发送到手机，系统会调用阿里云接口完成校验，不会向页面返回验证码。

## 知识库导入

用 `kb_admin / 123456` 或 `admin / 123456` 登录，进入“知识库管理”：

- 可以直接粘贴文本导入
- 可以上传 TXT、PDF、CSV、JSON 文件
- 导入后内容会保存到数据库
- 系统会自动切分文本块，并把向量写入 Chroma 持久化向量库
- 后续法律检索和案情推理都会使用这些知识库内容

## 检查数据库是否连接

启动后访问：

```text
http://127.0.0.1:8001/api/health
```

看到 `database: connected` 和 `chroma: connected` 就说明数据库和 Chroma 都连接成功。

## 注意

本系统用于课程设计和教学演示。系统输出仅供辅助分析，不构成正式法律意见。

## Docker 部署与更新

仓库不保存 `.env`、SQLite 数据库、Chroma 向量库、上传文件和导出报告。服务器上的这些运行数据统一保存在：

```text
/opt/legal-rag/runtime/
```

首次部署时，将 `.env.server.example` 复制为 `.env`，填写服务器专用配置后执行：

```bash
cd /opt/legal-rag
sh deploy.sh
```

通过 Git 更新代码时，保留服务器现有的 `.env` 与 `runtime/` 目录：

```bash
cd /opt/legal-rag
git pull --ff-only
docker compose up -d --build --remove-orphans
docker compose ps
curl http://127.0.0.1/api/health
```

## Android 客户端

安卓端位于 `android-client/`，是基于 WebView 的原生客户端，不修改后端接口和数据。使用 Android Studio 打开该目录，配置 JDK 17 和 Android SDK Platform 35 后即可运行。

客户端支持在“Server Settings”中修改后端地址：模拟器使用 `http://10.0.2.2:8001`，真机使用电脑局域网地址。正式发布时应改用已配置合法域名的 HTTPS 地址。
