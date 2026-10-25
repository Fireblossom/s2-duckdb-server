# s2-duckdb-server

[English](README.md)

把 [Semantic Scholar 学术图谱（S2AG）](https://www.semanticscholar.org/product/api#datasets)
下载到本地并持续更新，建成 DuckDB 数据库，再像 `vllm serve` 一样通过 HTTP 端口提供 SQL 查询。
数据规模约 2.4 亿篇论文、11 亿条带引用上下文的引用。

```
S2 Datasets API ──s2db download──▶ raw/*.gz ──s2db build──▶ cache/*.duckdb ──s2db serve──▶ HTTP :8300
```

## 组成

- **`s2db download`**：下载 papers、abstracts、paper-ids、citations 四个数据集。第一次下载完整版本，之后每周只下载增量（更新文件和删除文件）。支持断点续传，文件先写成 `.part` 再改名；一个版本的所有文件都下完才算同步完成。可以配置完成后执行的钩子命令和 webhook 通知（兼容 Discord/Slack）。
- **`s2db build`**：把 jsonl.gz 转成 parquet，再建两个 DuckDB 库。同一条记录保留最新版本，应用 S2 的删除记录，没有引用上下文的引用不入库。
- **`s2db serve`**：HTTP 查询服务，返回 JSON / CSV / Arrow，支持 API key 鉴权、单条查询超时、并发上限、返回行数上限；只读，并禁止查询读取文件和 URL。
- **`ddq.py`**：单文件客户端，只依赖 Python 标准库；另有一个[给 agent 用的 skill](skills/s2-duckdb/SKILL.md)，教 agent 表结构和怎么写出快的查询。
- **`deploy/`**：Slurm 部署脚本。服务长期在线；有新数据时自动重建，换库只中断几秒；每次启动后自动预热；另有可选的 SSH 隧道脚本，用来从外面访问计算节点。

## 安装和使用

```bash
git clone https://github.com/Fireblossom/s2-duckdb-server.git
cd s2-duckdb-server
python3 -m venv .venv && .venv/bin/pip install -e .

export S2_API_KEY=...                                    # S2 API key
s2db download --raw-dir raw                              # 第一次会下载完整版本，几百 GB
s2db build --all --convert --raw-dir raw --data-dir data --cache-dir cache
s2db serve --database cache/citations.duckdb --attach papers=cache/papers.duckdb \
           --read-only --no-external-access --api-key-file ~/.s2db_key
s2db query "SELECT count(*) FROM papers.papers_optimized"
```

需要 Python 3.10 以上。DuckDB 1.4、1.5、2.0 都能用（测试覆盖了 1.5 和 2.0）。注意：DuckDB 2.0 写出的库文件 1.x 打不开，构建和服务要用同一个大版本。

资源需求：完整构建 citations 约需 1.2 TB 内存（papers 约 1 TB），64 核约 1 小时；两个库合计约 1.2 TB 磁盘。

## 在 Slurm 集群上长期运行

```bash
cp deploy/s2db.env.example deploy/s2db.env    # 填路径、账号、分区、内存、中转机
deploy/slurm/start.sh                         # 启动 s2db-serve 和 s2db-watch
```

- **s2db-serve**：服务进程挂了会自动重启。往 `$S2DB_STATE_DIR/serve.ctrl` 写 `reload` 会在当前库文件上重启，写 `hold` 会停掉服务但保留资源。每次启动后自动预热；启动时先排好下一个作业，到时限后自动接上。
- **s2db-watch**：每 15 分钟检查一次 `raw/` 里有没有还没进库的新文件。确认同步已经完成（收到版本完成标记，或者 `QUIET_MIN` 分钟内没有新文件）后，把两个库构建到临时目录，用 `s2db verify` 检查行数，通过后换上线，旧版本留在 `cache/prev_auto/`，再让服务重新加载。失败的话旧库照常服务，24 小时后重试。进度写在 `$S2DB_STATE_DIR/auto/status.log`。

计算节点通常从外面连不上。在 `s2db.env` 里设置 `RELAY` 后，服务作业会一直保持一条到中转机的反向 SSH 隧道；使用方用 [`deploy/tunnel/forward.sh`](deploy/tunnel/forward.sh) 把本机端口转发过去，再用 cron 跑 [`watchdog.sh`](deploy/tunnel/watchdog.sh) 保证它不断。

如果能上网的机器和集群不是同一台（例如小虚拟机负责下载、集群负责构建），在下载机上加
`--on-complete "deploy/sync/rclone-push.sh --delete cluster:/data/s2/raw"`。它会把新文件推到集群并核对，最后才推版本完成标记，然后删掉本地副本。集群一看到标记就开始构建。

构建代码来自 [citation-context-dataset-discovery](https://github.com/Fireblossom/citation-context-dataset-discovery)（JCDL 2025）。

## 许可

MIT。Semantic Scholar 数据本身受 [S2 API 许可协议](https://www.semanticscholar.org/product/api/license) 约束。
