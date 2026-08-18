---
title: DeepDOC 解析结果与 Markdown 预览接口
---

# DeepDOC 解析结果与 Markdown 预览接口

本文基于当前 RAGFlow 服务端路由与实现，说明文档解析、解析状态查询、源文件预览和已解析文本读取的接口边界。

## 结论

当前服务区分上传原件、解析投影和可编辑 Markdown 派生稿；三者不能混用。

- 已上传的 `.md` 或 `.mdx` 文件可以通过源文件预览接口直接读取，前端已有 Markdown 渲染组件。
- PDF 等文档的 `GET /fs/content` 返回已完成解析后的可读文本投影：优先返回 PDF 的 source projection，其次按排序拼接检索索引中的 Chunk 文本。该文本不是 DeepDOC 原始 Markdown 文件，也不保证保留 Markdown 语法、表格格式或图片链接。
- `GET /fs/blob?path=...` 按 Corpus FS 路径返回原始上传文件。例如 PDF 返回 PDF 字节，不会在服务端转换成 Markdown。
- `GET/PUT /fs/derived-content` 提供独立、不可变版本化的 Markdown 派生稿；它不是解析器的原始中间产物，也不会覆盖上传原件。

因此，若需求是“直接预览 DeepDOC 输出的 Markdown”，仍需要解析器显式生成并保存该派生稿；不能把 Chunk 拼接文本标记成 DeepDOC 原始 Markdown。

## 认证与通用约定

所有下列接口都在 `/api/v1` 前缀下，并要求拥有目标知识库或文档的访问权限。调用时携带：

```http
Authorization: Bearer <RAGFLOW_API_KEY>
```

服务端的 JSON 响应使用 RAGFlow 标准外层结构，业务载荷位于 `data`。示例仅展示 `data` 的关键字段。

## 现有接口

| 目标 | 接口 | 当前返回 | 是否等同 DeepDOC 原始 Markdown |
| --- | --- | --- | --- |
| 发起解析 | `POST /datasets/{dataset_id}/documents/parse` | 已受理的文档数 | 否 |
| 查询解析状态 | `GET /datasets/{dataset_id}/documents` | `docs[].run`、`chunk_count`、`token_count` 等 | 否 |
| 预览原始文件 | `GET /datasets/{dataset_id}/fs/blob?path=...` | 原始文件二进制流与对应 Content-Type | 仅上传文件本身是 `.md`/`.mdx` 时是 |
| 读取/保存 Markdown 派生稿 | `GET/PUT /datasets/{dataset_id}/fs/derived-content` | 独立的版本化 Markdown | 仅当内容由解析器原样写入时是 |
| 读取解析后文本 | `GET /datasets/{dataset_id}/fs/content?path=...` | 文本片段、块、图片引用、游标 | 否 |
| 读取解析图片 | `GET /datasets/{dataset_id}/fs/image?path=...&image_ref=...` | 图片二进制流 | 否 |
| 查询文档路径和状态 | `GET /datasets/{dataset_id}/fs/tree?path=/` | 文件路径、`parse_status`、Chunk/Token 数 | 否 |

### 1. 发起 DeepDOC 解析

```bash
curl --request POST \
  --url 'http://{address}/api/v1/datasets/{dataset_id}/documents/parse' \
  --header 'Authorization: Bearer <RAGFLOW_API_KEY>' \
  --header 'Content-Type: application/json' \
  --data '{"document_ids":["{document_id}"]}'
```

解析任务异步运行。成功提交不表示内容已可读取；应轮询文档列表或 Corpus FS 树，直到状态为 `DONE`。

### 2. 查询解析状态

```bash
curl --request GET \
  --url 'http://{address}/api/v1/datasets/{dataset_id}/documents?id={document_id}' \
  --header 'Authorization: Bearer <RAGFLOW_API_KEY>'
```

关键字段：

```json
{
  "docs": [
    {
      "id": "{document_id}",
      "name": "report.pdf",
      "run": "3",
      "chunk_count": 42,
      "token_count": 18234
    }
  ]
}
```

当前实现将 `run == "3"` 映射为 Corpus FS 的 `parse_status: "DONE"`。不要使用固定等待时间判断解析完成。

### 3. 原始文件预览

```bash
curl --request GET \
  --url 'http://{address}/api/v1/datasets/{dataset_id}/fs/blob?path=/reports/report.pdf' \
  --header 'Authorization: Bearer <RAGFLOW_API_KEY>' \
  --output source-file
```

此接口从对象存储读取原始上传文件，依据文件扩展名设置预览响应头。前端 `DocumentPreview` 对 `.md`、`.mdx` 使用 `react-markdown` 渲染；对 PDF 使用 PDF 预览器。

适用场景：用户上传的就是 Markdown 文件，或客户端已将解析结果另存为 Markdown 文档并作为独立文档上传。

### 4. 获取已解析文本投影

```bash
curl --request GET \
  --url 'http://{address}/api/v1/datasets/{dataset_id}/fs/content?path=/reports/report.pdf&max_bytes=32768' \
  --header 'Authorization: Bearer <RAGFLOW_API_KEY>'
```

关键返回字段：

```json
{
  "path": "/reports/report.pdf",
  "content": "...",
  "content_source": "source_projection:7",
  "ranges": [{"start": 0, "end": 12480}],
  "blocks": [
    {
      "block_ref": "...",
      "kind": "heading",
      "level": 2,
      "page": 1,
      "positions": [[1, 0, 0, 0, 0]],
      "content": "2 Related Work"
    }
  ],
  "images": [{"image_ref": "...", "offset": 501}],
  "next_cursor": null
}
```

参数：

- `path`：Corpus FS 中的人类可读路径，必填。
- `max_bytes`：单次读取字节数，范围 `1..262144`，默认 `32768`。
- `cursor`：响应中的 `next_cursor`，用于读取后续内容。
- `page`：可选，PDF 页码，从 `1` 开始。
- `type`：可选，逗号分隔的块类型；允许 `heading`、`paragraph`、`table`、`image`、`image_ocr`、`image_caption`、`header_footer`、`other`。

当解析器提供可靠的标题层级时，`heading` 块包含 `level`（`1..6`）。MinerU 投影直接保留其 `text_level`，不依据标题文本或编号猜测层级。

`content_source` 的语义：

- `source_projection:<revision>`：PDF 有可用的结构化源投影；返回按页/块筛选后的文本和布局信息。
- `raw_text`：原文件是 `.txt`、`.md` 或 `.markdown`，返回文件原文。
- `parsed_chunks`：没有可用 source projection 时，按位置或 Chunk 顺序拼接索引内 `content_with_weight`。

后两种都不应被标注为 DeepDOC 原始 Markdown。`cursor` 与 `citation_ref` 是带作用域签名的令牌，只能在对应文档、路径、过滤条件和当前内容版本下使用。

### 5. 获取解析图片

当 `fs/content` 的 `blocks` 或 `images` 返回 `image_ref` 时，可读取对应图片：

```bash
curl --request GET \
  --url 'http://{address}/api/v1/datasets/{dataset_id}/fs/image?path=/reports/report.pdf&image_ref={image_ref}' \
  --header 'Authorization: Bearer <RAGFLOW_API_KEY>' \
  --output extracted-image
```

这可用于构建“文本投影 + 解析图片”的阅读器，但仍不是 Markdown 文件预览。

## 当前可行的客户端流程

```text
上传文档
  -> POST /datasets/{dataset_id}/documents/parse
  -> 轮询 GET /datasets/{dataset_id}/documents 或 GET /fs/tree，直到 DONE
  -> 左侧：GET /fs/blob?path=/... 显示源 PDF
  -> 右侧：GET /fs/content 分页读取解析文本；按需 GET /fs/image 获取图片
```

对于原生 Markdown 文档，右侧可直接使用 `/fs/blob` 并交给 Markdown 渲染器。对于 PDF，右侧应明确标为“解析文本投影”或“Chunk 视图”；需要编辑时使用独立的 `/fs/derived-content` 派生稿。

## Markdown 派生稿契约

当前版本提供按路径读写的独立派生稿接口：

```http
GET /api/v1/datasets/{dataset_id}/fs/derived-content?path=/reports/report.pdf
PUT /api/v1/datasets/{dataset_id}/fs/derived-content
```

当前行为：

- 派生稿不存在时读取返回 `404`；首次保存使用 `expected_version: 0`。
- 每次保存产生不可变新版本；旧版本恢复也会创建新版本。
- 源 PDF 保持只读；Markdown 是独立的、可版本化的派生物。
- 图片使用稳定的受控 URL 或同一文档作用域的图片引用，避免把临时本地文件路径泄露给客户端。
- 响应提供派生物版本和 SHA-256；写请求同时校验派生版本与 dataset `If-Match` revision。

`fs/content` 仍是读取解析投影的接口，`fs/blob` 是读取原件的接口，`fs/derived-content` 是版本化编辑稿接口。

## 实现依据

- 路由前缀由 `api/apps/__init__.py` 为 `restful_apis` 自动注册为 `/api/v1`。
- 文档解析、状态与源文件预览在 `api/apps/restful_apis/document_api.py`。
- Corpus FS 文本、图片接口在 `api/apps/restful_apis/corpus_fs_api.py`；其内容选择逻辑在 `api/apps/services/corpus_fs_service.py`。
- 前端 Markdown 预览在 `web/src/components/document-preview/md/index.tsx`，文件类型分发在 `web/src/components/document-preview/index.tsx`。
