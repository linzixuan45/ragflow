---
title: DeepDOC 解析结果与 Markdown 预览接口
---

# DeepDOC 解析结果与 Markdown 预览接口

本文基于当前 RAGFlow 服务端路由与实现，说明文档解析、解析状态查询、源文件预览和已解析文本读取的接口边界。

## 结论

当前服务提供了组成预览流程所需的大部分接口，但**没有提供一个保证返回 DeepDOC 原始 Markdown 产物的接口**。

- 已上传的 `.md` 或 `.mdx` 文件可以通过源文件预览接口直接读取，前端已有 Markdown 渲染组件。
- PDF 等文档的 `GET /fs/content` 返回已完成解析后的可读文本投影：优先返回 PDF 的 source projection，其次按排序拼接检索索引中的 Chunk 文本。该文本不是 DeepDOC 原始 Markdown 文件，也不保证保留 Markdown 语法、表格格式或图片链接。
- `GET /documents/{document_id}/preview` 始终返回原始上传文件。例如 PDF 返回 PDF 字节，不会在服务端转换成 Markdown。

因此，若需求是“直接预览 DeepDOC 输出的 Markdown”，还需要新增“持久化解析 Markdown 派生物并按文档读取”的后端契约；不能仅复用现有 Chunk 或原文件预览接口。

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
| 预览原始文件 | `GET /documents/{document_id}/preview` | 原始文件二进制流与对应 Content-Type | 仅上传文件本身是 `.md`/`.mdx` 时是 |
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
  --url 'http://{address}/api/v1/documents/{document_id}/preview' \
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
      "kind": "paragraph",
      "page": 1,
      "positions": [[1, 0, 0, 0, 0]],
      "content": "..."
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
  -> 左侧：GET /documents/{document_id}/preview 显示源 PDF
  -> 右侧：GET /fs/content 分页读取解析文本；按需 GET /fs/image 获取图片
```

对于原生 Markdown 文档，右侧可直接使用 `/documents/{document_id}/preview` 并交给 Markdown 渲染器。对于 PDF，右侧应明确标为“解析文本投影”或“Chunk 视图”，不能标称为“DeepDOC Markdown”。

## 缺失的接口契约

要支持稳定的“DeepDOC Markdown 直接预览”，建议增加一个单一路径的只读接口：

```http
GET /api/v1/datasets/{dataset_id}/documents/{document_id}/parsed-markdown
```

建议行为：

- 仅在解析状态为 `DONE` 且存在当前派生 Markdown 版本时返回 `200 text/markdown; charset=utf-8`；未完成返回 `409` 或 `425`，不存在派生物返回 `404`。
- 返回的是解析器实际产生并持久化的 Markdown 文件，不从 `content_with_weight` 反向拼接。
- 源 PDF 保持只读；Markdown 是独立的、可版本化的派生物。
- 图片使用稳定的受控 URL 或同一文档作用域的图片引用，避免把临时本地文件路径泄露给客户端。
- 响应提供派生物版本或内容哈希，客户端据此处理缓存与刷新。

在该接口和派生物持久化实现落地前，`fs/content` 是读取已解析内容的服务接口，`/documents/{document_id}/preview` 是读取源文件的服务接口；二者均不能替代原始 Markdown 派生物接口。

## 实现依据

- 路由前缀由 `api/apps/__init__.py` 为 `restful_apis` 自动注册为 `/api/v1`。
- 文档解析、状态与源文件预览在 `api/apps/restful_apis/document_api.py`。
- Corpus FS 文本、图片接口在 `api/apps/restful_apis/corpus_fs_api.py`；其内容选择逻辑在 `api/apps/services/corpus_fs_service.py`。
- 前端 Markdown 预览在 `web/src/components/document-preview/md/index.tsx`，文件类型分发在 `web/src/components/document-preview/index.tsx`。
