# 运行环境与测试口径

> 覆盖：knowledge-dev 容器启动与 overlay 挂载、环境变量生效口径、测试与 lint 命令口径、耗时测量的环境前提。
> 双栈归属见 [ARCHITECTURE.md](../ARCHITECTURE.md)；解析链路的具体配置项见 [parse-pipeline.md](parse-pipeline.md)。

## 1. 容器启动与 overlay 挂载

**当前实例**：Compose 项目 `ragflow-knowledge-dev`，主服务容器 `ragflow-knowledge-dev-ragflow-gpu-1`，镜像 `ragflow:onnx-gpu-runtime`。Web `http://127.0.0.1/`，API `http://127.0.0.1:9380/api/v1`。GPU 为 RTX 3060，容器经 NVIDIA runtime 透传。

**重建只动 `ragflow-gpu`**，避免牵连 MySQL / MinIO / Elasticsearch / Redis：

```bash
cd docker
docker compose --env-file .env.knowledge-dev \
  -f docker-compose.yml -f docker-compose.knowledge-dev.yml \
  --profile elasticsearch --profile gpu \
  up -d --no-deps --force-recreate ragflow-gpu
```

启动入口是仓库根 [re_start.sh](../re_start.sh)，它 `cd docker/` 后执行同样的组合。

**变量分层（最容易踩）**

- `--env-file` 指向的 `docker/.env.knowledge-dev` **只做 Compose 文件内的变量替换**。
- 容器的实际 `env_file` 是 base 的 `docker/.env` 与 overlay 的 `docker/.env.local`。
- 因此**新增变量只写进 `.env.knowledge-dev` 不会进入容器**；要进入运行进程必须写 `docker/.env.local`（overlay）或 `docker/.env`。

**路由表不随挂载更新（会导致业务级 405）**

Quart 的路由表在进程启动时构建，**bind mount 的文件后续变化不会刷新它**。2026-08-18 就因此踩到：容器在新增 `POST /datasets/corpus-search` 之前启动，请求落到 `/datasets/<dataset_id>` 的方法集，`OPTIONS` 只允许 `GET/PUT/HEAD`，DeerFlow 侧表现为 502 + 业务错误 405（RAGFlow 传输层仍是 HTTP 200）。**新增或改动路由后必须 `--force-recreate`，重启不够。**

**逐文件 bind mount（改动后必须重建）**

- overlay 以**逐文件 bind mount** 为主（当前 56 个 `.py` 单文件；`rag/svr/task_executor_refactor/` 是唯一整目录挂载，`web/dist` 为目录只读）。
- **新增或重命名的源文件必须同步加进 [docker/docker-compose.knowledge-dev.yml](../docker/docker-compose.knowledge-dev.yml)**（目录挂载的子树除外，如 `rag/svr/task_executor_refactor/`），否则容器用的是镜像内旧副本，工作区实现不生效（历史上多次因挂载缺失导致新旧模块混搭、源码与容器行为不一致）。
- 改动**已挂载的单个文件**后必须 `--force-recreate`：原子写会换 inode，容器仍绑在旧 inode 上，看到的是旧副本（实测踩到过）。
- 进程**导入时读取**的配置（例：`CORPUS_POOL_BUDGET_CANDIDATES`，进程导入时读取）改动后必须重建容器，只改环境变量不生效。

**耗时测量的环境前提**

- 容器内 `rag/svr/task_executor.py` 会因 MinerU 解析 1800s 超时失败退出后被反复拉起（存活 15~20s、占 100~130% CPU，容器 CPU 长期约 101%），**污染一切耗时测量**（容器级读数最高可达进程自身的 3 倍以上）。
- 取静默实例：设 `CORPUS_SEARCH_DIAGNOSTICS=1`，并把 overlay 的 `command: []` 临时改为 `command: ["--disable-taskexecutor"]`；**测完必须改回 `command: []`**——关掉入库是测量手段，不是产品决定。
- 归因时容器级与进程级数据必须分开看，用于区分「在算」与「在等」。

**挂载缺失的真实代价（两次同类事故）**

容器把「工作区的新调用方」和「镜像里的旧被调用方」组合在一起，症状往往不在 Web 层：

- 2026-08-13：为加载 Corpus FS 替换路由重建后，Web 仍 200，但 `9380` API 连接被重置，server 与 task executor 都报 `ModuleNotFoundError: No module named 'common.mineru_formats'`——新增的 `common/mineru_formats.py` 与 `rag/app/parser_router.py` 没进 overlay，而多个已挂载模块依赖它们。补齐后真实 XLSX 上传又以 `get_parser() takes 1 positional argument but 2 were given` 失败：overlay 挂了新的 `chunk_service.py`（按两参调用），却仍在用镜像里只接受一个参数的旧 `chunk_builder.py`。
- 2026-08-18：新路由依赖的 `api/apps/services/corpus_tool_service.py` 未挂载，直接重启会让 API 进程在导入阶段失败。
- 另一轮 overlay 曾只挂了 embedding 实现，漏掉负责传递模型长度的 `tenant_llm_service.py` 和新的 `rerank_model.py` → rerank 的 `max_model_len` 不会进入实例，运行镜像里新旧 Python 模块组合导致源码与容器行为不一致。

判断方法：报 `ModuleNotFoundError` 或「签名不匹配」时，先核对 overlay 的挂载清单与容器内实际符号签名（`docker exec ... python -c "import x; print(x.f.__doc__ or ...)"`），不要先怀疑业务代码。

## 2. GPU / CUDA 动态库路径（DeepDOC 回退 CPU）

**现象**：启动日志出现 `Failed to load library libonnxruntime_providers_cuda.so` / `libcublasLt.so.12: cannot open shared object file`，DeepDOC 的 `det.onnx`、`rec.onnx` 回退 CPU。

**根因不是缺库**：容器内 `nvidia-smi` 正常、`onnxruntime-gpu` 包含 `CUDAExecutionProvider`、CUDA 12 与 cuDNN 9 动态库都在 `/usr/local/cuda/lib64`。真实原因是 `docker/entrypoint.sh` 与 `docker/launch_backend_service.sh` 把 `LD_LIBRARY_PATH` **覆盖**成 `/usr/lib/x86_64-linux-gnu/`，实际 API 与 task executor 进程看不到 CUDA 库。

**修复**：两个启动脚本都保留 CUDA、系统与调用方已有的动态库路径：

```bash
export LD_LIBRARY_PATH="/usr/local/cuda/lib64:/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
```

**验证方法（重要）**：**不要只看 `onnxruntime.get_available_providers()`**——它可能列出 CUDA provider，但真正创建 session 时仍会因动态库路径错误回退 CPU。要用真实模型验证：

```bash
docker exec ragflow-knowledge-dev-ragflow-gpu-1 sh -lc 'python3 - <<"PY"
from deepdoc.vision.ocr import load_model
for name in ("det", "rec"):
    session, _ = load_model("/ragflow/rag/res/deepdoc", name)
    print(name, session.get_providers())
PY'
```

正确结果两行都是 `['CUDAExecutionProvider', 'CPUExecutionProvider']`。

**判读**：`Some nodes were not assigned to the preferred execution providers` 是 ONNX Runtime 把形状计算等节点放在 CPU 上的**正常提示**，不等于整个模型回退 CPU。

**验证证据**：两个启动脚本通过 `bash -n`，Compose 配置检查通过，Web 与 API 版本接口均返回 200；API 与 task executor 进程的 `LD_LIBRARY_PATH` 已含 `/usr/local/cuda/lib64`；`det.onnx` 与 `rec.onnx` 均实际启用 `CUDAExecutionProvider`，并在 GPU 上观察到 ONNX 计算进程与显存占用。

## 3. 测试命令口径

- `uv run pytest` 每次都会尝试 sync 依赖（含 `en-core-web-sm` 的 GitHub wheel），**无外网时失败** → 用 `uv run --no-sync pytest ...`（venv 已存在时）。
- 根 `conftest.py` 会尝试下载 NLTK `wordnet`，会让单文件 pytest 卡住 → 用隔离 confcutdir：

  ```bash
  source env.sh
  .venv/bin/python -m pytest test/unit_test/deepdoc/parser/test_mineru_parser.py -q \
    --confcutdir=test/unit_test/deepdoc
  ```

- refactor 全套测试在**收集** `test_dataflow_service.py` 时需要本机 OCR 模型缓存，缺缓存且网络不可达会中止（是环境问题，不是断言失败）。`uv` 也可能因无法下载 spaCy 模型失败。
- **本机 `uv` 缓存不可写**（`~/.cache/uv/archive-v0` 属于 root，历史 `sudo` 留下的）：`uv lock` / `uv run --group ...` 会以 `failed to rename file ... Permission denied` 失败。绕过：`UV_CACHE_DIR=/tmp/uv-cache uv ...`；根治：`sudo chown -R "$(id -un)" ~/.cache/uv`。
- Git 钩子：`.git/hooks/*` 由 lefthook 生成，它按「PATH → 仓库根 `node_modules/` → …」的顺序找二进制，**最后才回退 `uv run lefthook`**；而 lefthook 不在依赖里，`uv run` 会联网解析并挂住整次提交。因此根目录提供 [package.json](../package.json)（只含 lefthook），先 `npm install` 再提交；CI 不依赖钩子，直接跑同样的检查命令。
- Go 侧：**禁止裸 `go test` / `go build`**，必须走 [build.sh](../build.sh)（CGO + 原生静态库 `office_oxide` / `pdfium-static` / `pdf_oxide` + clang/lld）。无 CGO 依赖的包可 `CGO_ENABLED=0 go test ./internal/entity/models/...` 直跑。

## 4. lint 口径

- ruff 版本以 [pyproject.toml](../pyproject.toml) 的 `lint` 依赖组（`ruff==0.16.1`）+ `uv.lock` 为准，用 `uv run --group lint ruff`；裸 `ruff` 会取 PATH 上任意版本（本机当前是 conda 里那份），与 CI 可能不一致。
- `ruff I001`（Import block is un-sorted or un-formatted）会被**在 import 块与首个 `def` 之间插入模块级常量/注释**触发：HEAD 版本通过、插入后不通过。`ruff check --fix` 可自动修复，diff 无副作用。
- 目标大文件上存在既存的日志、宽异常捕获与旧式注解债务，全规则扫描必红 → 用基础错误规则单独验证，**不要声称「ruff 全绿」**。改了这类文件时，lefthook 会只对**暂存的文件**跑 ruff/ruff-format（并自动修好再暂存）；CI 同样只查变更文件，见 [.github/workflows/lint.yml](../.github/workflows/lint.yml)。
