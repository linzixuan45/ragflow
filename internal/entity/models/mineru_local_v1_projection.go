//
//  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
//
//  Licensed under the Apache License, Version 2.0 (the "License");
//  you may not use this file except in compliance with the License.
//  You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
//

package models

// Projection of the MinerU 4.x `middle_json.json` document onto the 3.x
// Content List V1 shape, so both protocols feed one downstream mapping.

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"mime"
	"path"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
)

// minerUV1TaskResponse projects a V1 zip onto the legacy TaskResponse so the
// parser layer maps one shape regardless of the server version.
func minerUV1TaskResponse(zipBytes []byte) (*TaskResponse, error) {
	middle, images, err := minerUV1ReadZip(zipBytes)
	if err != nil {
		return nil, err
	}
	items := minerUMiddleJSONItems(middle, images)
	if len(items) == 0 {
		return nil, fmt.Errorf("MinerU v1 middle_json contained no renderable blocks")
	}
	raw, err := json.Marshal(items)
	if err != nil {
		return nil, fmt.Errorf("failed to encode MinerU v1 content list: %w", err)
	}
	segments, err := minerUContentSegments(string(raw), images)
	if err != nil {
		return nil, err
	}
	if len(segments) == 0 {
		return nil, fmt.Errorf("MinerU v1 middle_json produced no segments")
	}
	return &TaskResponse{Content: minerUV1Markdown(items), Segments: segments}, nil
}

// minerUV1Markdown renders content-list items as markdown, mirroring how the
// Python parser walks the same list (titles get `##`, figures are inlined as
// data URLs, tables/code keep their body). The 3.x protocol returns markdown
// from the server directly; V1 ships `middle_json`, so it is rendered here.
func minerUV1Markdown(items []minerUV1ContentItem) string {
	var builder strings.Builder
	for _, item := range items {
		layout := item.LayoutType
		if layout == "" {
			layout = minerUV1Layout(item)
		}
		if layout == "title" {
			builder.WriteString("\n## ")
		}
		if layout == "figure" && len(item.ImageData) > 0 {
			builder.WriteString("\n![Image](")
			builder.WriteString(item.ImageData[0])
			builder.WriteString(")")
			continue
		}
		text := minerUV1ItemText(item)
		if strings.TrimSpace(text) == "" {
			continue
		}
		builder.WriteString(text)
		builder.WriteString("\n")
	}
	return builder.String()
}

func minerUV1ItemText(item minerUV1ContentItem) string {
	switch item.Type {
	case "table":
		return joinMinerUText(append([]string{item.TableBody}, append(item.TableCaption, item.TableFootnote...)...)...)
	case "code":
		return joinMinerUText(append([]string{item.CodeBody}, append(item.ImageCaption, item.ImageFootnote...)...)...)
	case "image":
		return joinMinerUText(append(item.ImageCaption, item.ImageFootnote...)...)
	case "chart":
		return joinMinerUText(append(append(item.ChartCaption, item.ChartFootnote...), item.Content)...)
	case "list", "index":
		return strings.Join(item.ListItems, "\n")
	case "equation":
		return item.Text
	default:
		return joinMinerUText(item.Text, item.Content)
	}
}

// minerUV1Layout maps a content-list item onto the layout vocabulary the shared
// segment mapping uses (see minerUContentSegments).
func minerUV1Layout(item minerUV1ContentItem) string {
	switch item.Type {
	case "table":
		return "table"
	case "image", "chart":
		return "figure"
	case "doc_title", "paragraph_title":
		return "title"
	default:
		if item.TextLevel > 0 {
			return "title"
		}
		return "text"
	}
}

// minerUV1ReadZip returns the `middle_json.json` document plus every image in
// the archive keyed by base name, mirroring the 3.x `images` map.
func minerUV1ReadZip(zipBytes []byte) (map[string]any, map[string]string, error) {
	reader, err := zip.NewReader(bytes.NewReader(zipBytes), int64(len(zipBytes)))
	if err != nil {
		return nil, nil, fmt.Errorf("parser: MinerU v1 zip: %w", err)
	}
	var middle map[string]any
	images := map[string]string{}
	for _, file := range reader.File {
		name := strings.ReplaceAll(file.Name, "\\", "/")
		if strings.Contains(name, "..") || strings.HasPrefix(name, "/") {
			return nil, nil, fmt.Errorf("parser: MinerU v1 zip contains unsafe path %q", file.Name)
		}
		if strings.HasSuffix(name, "/") {
			continue
		}
		switch {
		case path.Base(name) == "middle_json.json":
			body, err := minerUV1ReadZipFile(file)
			if err != nil {
				return nil, nil, err
			}
			if err := json.Unmarshal(body, &middle); err != nil {
				return nil, nil, fmt.Errorf("parser: MinerU v1 middle_json: %w", err)
			}
		case isMinerUImageName(name):
			body, err := minerUV1ReadZipFile(file)
			if err != nil {
				return nil, nil, err
			}
			images[path.Base(name)] = imageDataURL(name, body)
		}
	}
	if middle == nil {
		return nil, nil, fmt.Errorf("parser: MinerU v1 zip is missing middle_json.json")
	}
	return middle, images, nil
}

func minerUV1ReadZipFile(file *zip.File) ([]byte, error) {
	rc, err := file.Open()
	if err != nil {
		return nil, fmt.Errorf("parser: MinerU v1 zip entry %s: %w", file.Name, err)
	}
	defer rc.Close()
	body, err := io.ReadAll(io.LimitReader(rc, minerUV1MaxBytes))
	if err != nil {
		return nil, fmt.Errorf("parser: MinerU v1 zip entry %s: %w", file.Name, err)
	}
	return body, nil
}

func isMinerUImageName(name string) bool {
	switch strings.ToLower(filepath.Ext(name)) {
	case ".png", ".jpg", ".jpeg", ".jp2", ".webp", ".gif", ".bmp", ".tiff":
		return true
	default:
		return false
	}
}

func imageDataURL(name string, body []byte) string {
	contentType := mime.TypeByExtension(strings.ToLower(filepath.Ext(name)))
	if contentType == "" {
		contentType = "application/octet-stream"
	}
	return "data:" + contentType + ";base64," + base64.StdEncoding.EncodeToString(body)
}

// minerUV1ContentItem is the 3.x Content List V1 shape the 4.x middle_json is
// projected onto. Field names match
// deepdoc/parser/mineru_parser.py:middle_json_to_content_list so both
// protocols converge on one mapping.
type minerUV1ContentItem struct {
	Type          string    `json:"type"`
	Text          string    `json:"text,omitempty"`
	TextLevel     int       `json:"text_level,omitempty"`
	TextFormat    string    `json:"text_format,omitempty"`
	SubType       string    `json:"sub_type,omitempty"`
	ListItems     []string  `json:"list_items,omitempty"`
	ImgPath       string    `json:"img_path,omitempty"`
	TableBody     string    `json:"table_body,omitempty"`
	CodeBody      string    `json:"code_body,omitempty"`
	Content       string    `json:"content,omitempty"`
	ImageCaption  []string  `json:"image_caption,omitempty"`
	ImageFootnote []string  `json:"image_footnote,omitempty"`
	ChartCaption  []string  `json:"chart_caption,omitempty"`
	ChartFootnote []string  `json:"chart_footnote,omitempty"`
	TableCaption  []string  `json:"table_caption,omitempty"`
	TableFootnote []string  `json:"table_footnote,omitempty"`
	PageIndex     int       `json:"page_idx"`
	BBox          []float64 `json:"bbox,omitempty"`
	// LayoutType is the RAGFlow layout vocabulary for this item (`title`,
	// `figure`, `table`, `text`, ...). The 3.x content_list carries it as
	// `layout_type`, so V1 items serialize it the same way.
	LayoutType string `json:"layout_type,omitempty"`
	// ImageData holds the inline data URL for ImgPath. It is resolved while
	// reading the zip and is not part of the serialized content list.
	ImageData []string `json:"-"`
}

var (
	minerUV1VisualBodies = map[string]bool{
		"image_body":     true,
		"table_body":     true,
		"chart_body":     true,
		"code_body":      true,
		"algorithm_body": true,
	}
	minerUV1AuxText = map[string]bool{
		"header":        true,
		"footer":        true,
		"page_number":   true,
		"aside_text":    true,
		"page_footnote": true,
	}
)

// minerUMiddleJSONItems converts a V1 `middle_json` document into content-list
// items. Reference blocks are coalesced into one `list` item per run, matching
// the Python projection.
func minerUMiddleJSONItems(middle map[string]any, images map[string]string) []minerUV1ContentItem {
	pages, _ := middle["pages"].([]any)
	items := make([]minerUV1ContentItem, 0)
	for _, rawPage := range pages {
		page, ok := rawPage.(map[string]any)
		if !ok {
			continue
		}
		pageIndex := int(minerUNumber(page["page_idx"]))
		blocks, _ := page["blocks"].([]any)
		references := make([]map[string]any, 0)
		flushReferences := func() {
			if len(references) == 0 {
				return
			}
			listItems := make([]string, 0, len(references))
			for _, block := range references {
				if text := minerUInlineText(block["content"]); text != "" {
					listItems = append(listItems, text)
				}
			}
			bbox := minerUUnionBBox(references)
			references = references[:0]
			if len(listItems) == 0 {
				return
			}
			items = append(items, minerUV1ContentItem{
				Type:      "list",
				SubType:   "ref_text",
				ListItems: listItems,
				PageIndex: pageIndex,
				BBox:      bbox,
			})
		}
		for _, rawBlock := range blocks {
			block, ok := rawBlock.(map[string]any)
			if !ok {
				continue
			}
			if strings.EqualFold(minerUString(block["type"]), "ref_text") {
				references = append(references, block)
				continue
			}
			flushReferences()
			item, ok := minerUMiddleJSONItem(block, images)
			if !ok {
				continue
			}
			item.PageIndex = pageIndex
			item.BBox = minerUNormalizeBBox(block["bbox"])
			items = append(items, item)
		}
		flushReferences()
	}
	return items
}

func minerUMiddleJSONItem(block map[string]any, images map[string]string) (minerUV1ContentItem, bool) {
	blockType := strings.TrimSpace(minerUString(block["type"]))
	switch blockType {
	case "doc_title", "paragraph_title":
		item := minerUV1ContentItem{Type: "text", Text: minerUInlineText(block["content"]), LayoutType: "title"}
		if level := int(minerUNumber(block["level"])); level > 0 {
			item.TextLevel = level
		}
		return item, true
	case "text":
		return minerUV1ContentItem{Type: "text", Text: minerUInlineText(block["content"])}, true
	case "equation":
		item := minerUV1ContentItem{Type: "equation", LayoutType: "equation"}
		if imagePath := minerUString(block["image_path"]); imagePath != "" {
			item.ImgPath = imagePath
		}
		if latex := strings.TrimSpace(minerUInlineText(block["content"])); latex != "" {
			item.Text = latex
			item.TextFormat = "latex"
		}
		return item, true
	case "list":
		item := minerUV1ContentItem{Type: "list", SubType: minerUString(block["sub_type"])}
		if item.SubType == "" {
			item.SubType = "text"
		}
		for _, leaf := range minerUFlattenNested(block["content"], "list", 0) {
			if text := minerUInlineText(leaf.Leaf["content"]); text != "" {
				item.ListItems = append(item.ListItems, text)
			}
		}
		return item, true
	case "index":
		item := minerUV1ContentItem{Type: "index"}
		for _, leaf := range minerUFlattenNested(block["content"], "index", 0) {
			label := strings.TrimSpace(minerUInlineText(leaf.Leaf["content"]))
			if label != "" {
				item.ListItems = append(item.ListItems, strings.Repeat("    ", leaf.Depth)+"- "+label)
			}
		}
		return item, true
	case "image", "table", "chart":
		return minerUVisualItem(block, blockType, images), true
	case "code":
		return minerUVisualItem(block, "code", images), true
	}
	if minerUV1AuxText[blockType] {
		return minerUV1ContentItem{Type: blockType, Text: minerUInlineText(block["content"])}, true
	}
	if text := minerUInlineText(block["content"]); text != "" {
		return minerUV1ContentItem{Type: "text", Text: text}, true
	}
	return minerUV1ContentItem{}, false
}

func minerUVisualItem(block map[string]any, blockType string, images map[string]string) minerUV1ContentItem {
	item := minerUV1ContentItem{Type: blockType}
	body := minerUVisualBody(block)
	imagePath := minerUString(body["image_path"])
	if imagePath == "" {
		imagePath = minerUString(block["image_path"])
	}
	if imagePath != "" {
		item.ImgPath = imagePath
		if data, ok := images[path.Base(imagePath)]; ok {
			item.ImageData = []string{data}
		}
	}
	prefix := "image"
	if blockType != "image" {
		prefix = blockType
	}
	switch blockType {
	case "table":
		item.LayoutType = "table"
		item.TableCaption = minerUAnnotationTexts(block, prefix+"_caption")
		item.TableFootnote = minerUAnnotationTexts(block, prefix+"_footnote")
		item.TableBody = minerUString(body["content"])
	case "code":
		item.LayoutType = "code"
		item.ImageCaption = minerUAnnotationTexts(block, "code_caption")
		item.ImageFootnote = minerUAnnotationTexts(block, "code_footnote")
		item.CodeBody = minerUString(body["content"])
		if strings.EqualFold(minerUString(body["type"]), "algorithm_body") || strings.EqualFold(minerUString(block["sub_type"]), "algorithm") {
			item.SubType = "algorithm"
		} else {
			item.SubType = "code"
		}
	default:
		item.LayoutType = "figure"
		item.ImageCaption = minerUAnnotationTexts(block, prefix+"_caption")
		item.ImageFootnote = minerUAnnotationTexts(block, prefix+"_footnote")
		if content := minerUString(body["content"]); content != "" {
			item.Content = content
		}
		if subType := minerUString(block["sub_type"]); subType != "" {
			item.SubType = subType
		}
	}
	return item
}

func minerUVisualBody(block map[string]any) map[string]any {
	children, _ := block["content"].([]any)
	for _, rawChild := range children {
		child, ok := rawChild.(map[string]any)
		if !ok {
			continue
		}
		if minerUV1VisualBodies[strings.TrimSpace(minerUString(child["type"]))] {
			return child
		}
	}
	return map[string]any{}
}

// minerUAnnotationTexts collects caption/footnote spans of one kind, ordered by
// their `index` when the server provides one.
func minerUAnnotationTexts(block map[string]any, accepted string) []string {
	children, _ := block["content"].([]any)
	type annotated struct {
		index int
		text  string
	}
	found := make([]annotated, 0)
	for _, rawChild := range children {
		child, ok := rawChild.(map[string]any)
		if !ok || !strings.EqualFold(minerUString(child["type"]), accepted) {
			continue
		}
		if text := minerUInlineText(child["content"]); text != "" {
			found = append(found, annotated{index: int(minerUNumber(child["index"])), text: text})
		}
	}
	if len(found) == 0 {
		return nil
	}
	sort.SliceStable(found, func(i, j int) bool { return found[i].index < found[j].index })
	texts := make([]string, 0, len(found))
	for _, entry := range found {
		texts = append(texts, entry.text)
	}
	return texts
}

// minerUFlattenNested walks nested list/index content and returns leaves in
// document order paired with their nesting depth.
func minerUFlattenNested(raw any, nested string, depth int) []minerUNestedLeaf {
	children, _ := raw.([]any)
	leaves := make([]minerUNestedLeaf, 0)
	for _, rawChild := range children {
		child, ok := rawChild.(map[string]any)
		if !ok {
			continue
		}
		if strings.EqualFold(minerUString(child["type"]), nested) {
			leaves = append(leaves, minerUFlattenNested(child["content"], nested, depth+1)...)
			continue
		}
		leaves = append(leaves, minerUNestedLeaf{Depth: depth, Leaf: child})
	}
	return leaves
}

type minerUNestedLeaf struct {
	Depth int
	Leaf  map[string]any
}

// minerUInlineText renders inline spans (text / equation / code / hyperlink)
// into plain text, recursing through nested hyperlink children.
func minerUInlineText(raw any) string {
	switch value := raw.(type) {
	case nil:
		return ""
	case string:
		return value
	case []any:
		var builder strings.Builder
		for _, span := range value {
			builder.WriteString(minerUInlineText(span))
		}
		return builder.String()
	case map[string]any:
		if strings.EqualFold(minerUString(value["type"]), "hyperlink") {
			if children, ok := value["children"]; ok {
				return minerUInlineText(children)
			}
		}
		return minerUInlineText(value["content"])
	default:
		return fmt.Sprintf("%v", value)
	}
}

// minerUNormalizeBBox rounds a 0-1000 bbox to its canonical integer form.
func minerUNormalizeBBox(raw any) []float64 {
	values, ok := raw.([]any)
	if !ok || len(values) != 4 {
		return nil
	}
	normalized := make([]float64, 4)
	for i, value := range values {
		normalized[i] = float64(int(minerUNumber(value) + 0.5))
	}
	return normalized
}

func minerUUnionBBox(blocks []map[string]any) []float64 {
	var union []float64
	for _, block := range blocks {
		box := minerUNormalizeBBox(block["bbox"])
		if len(box) != 4 {
			continue
		}
		if union == nil {
			union = append([]float64(nil), box...)
			continue
		}
		union[0] = min(union[0], box[0])
		union[1] = min(union[1], box[1])
		union[2] = max(union[2], box[2])
		union[3] = max(union[3], box[3])
	}
	return union
}

// minerUV1UploadCache deduplicates uploads by server plus content hash so the
// page-range tasks of one document share a single uploaded file, matching the
// Python V1UploadCache.
type minerUV1UploadCache struct {
	mu       sync.Mutex
	entries  map[string]string
	order    []string
	capacity int
}

var minerUV1Uploads = &minerUV1UploadCache{entries: map[string]string{}, capacity: 64}

func (c *minerUV1UploadCache) get(key string) (string, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	value, ok := c.entries[key]
	return value, ok
}

func (c *minerUV1UploadCache) put(key, fileID string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, exists := c.entries[key]; !exists {
		c.order = append(c.order, key)
	}
	c.entries[key] = fileID
	for len(c.order) > c.capacity {
		oldest := c.order[0]
		c.order = c.order[1:]
		delete(c.entries, oldest)
	}
}

func (c *minerUV1UploadCache) drop(key string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.entries, key)
	for i, existing := range c.order {
		if existing == key {
			c.order = append(c.order[:i], c.order[i+1:]...)
			break
		}
	}
}

func minerUContentCacheKey(baseURL string, content []byte) string {
	sum := sha256.Sum256(content)
	return baseURL + "\x00" + hex.EncodeToString(sum[:])
}

// minerUString reads a string field from decoded JSON.
func minerUString(raw any) string {
	switch value := raw.(type) {
	case string:
		return value
	case nil:
		return ""
	default:
		return fmt.Sprintf("%v", value)
	}
}

// minerUNumber reads a numeric field from decoded JSON.
func minerUNumber(raw any) float64 {
	switch value := raw.(type) {
	case float64:
		return value
	case float32:
		return float64(value)
	case int:
		return float64(value)
	case int64:
		return float64(value)
	case json.Number:
		parsed, err := value.Float64()
		if err != nil {
			return 0
		}
		return parsed
	case string:
		parsed, err := strconv.ParseFloat(strings.TrimSpace(value), 64)
		if err != nil {
			return 0
		}
		return parsed
	default:
		return 0
	}
}
