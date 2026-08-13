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

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"mime"
	"mime/multipart"
	"net/http"
	"net/textproto"
	"path"
	"path/filepath"
	"strconv"
	"strings"
)

var minerUUploadContentTypes = map[string]string{
	".pdf":  "application/pdf",
	".png":  "image/png",
	".jpeg": "image/jpeg",
	".jpg":  "image/jpeg",
	".jp2":  "image/jp2",
	".webp": "image/webp",
	".gif":  "image/gif",
	".bmp":  "image/bmp",
	".tiff": "image/tiff",
	".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
	".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
	".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

type MinerULocalModel struct {
	baseModel BaseModel
}

func NewMinerLocalUModel(baseURL map[string]string, urlSuffix URLSuffix) *MinerULocalModel {
	return &MinerULocalModel{
		baseModel: BaseModel{
			BaseURL:          baseURL,
			URLSuffix:        urlSuffix,
			AllowEmptyAPIKey: true,
			httpClient:       NewDriverHTTPClient(),
		},
	}
}

func (m *MinerULocalModel) NewInstance(baseURL map[string]string) ModelDriver {
	return NewMinerLocalUModel(baseURL, m.baseModel.URLSuffix)
}

func (m *MinerULocalModel) Name() string {
	return "mineru"
}

func (m *MinerULocalModel) ChatWithMessages(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig) (*ChatResponse, error) {
	return nil, fmt.Errorf("%s no such method", m.Name())
}

func (m *MinerULocalModel) ChatStreamlyWithSender(modelName string, messages []Message, apiConfig *APIConfig, modelConfig *ChatConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s no such method", m.Name())
}

func (m *MinerULocalModel) Embed(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([]EmbeddingData, error) {
	return nil, fmt.Errorf("%s no such method", m.Name())
}

func (m *MinerULocalModel) Rerank(modelName *string, query string, documents []string, apiConfig *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
	return nil, fmt.Errorf("%s no such method", m.Name())
}

func (m *MinerULocalModel) TranscribeAudio(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig) (*ASRResponse, error) {
	return nil, fmt.Errorf("%s no such method", m.Name())
}

func (m *MinerULocalModel) TranscribeAudioWithSender(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s no such method", m.Name())
}

func (m *MinerULocalModel) AudioSpeech(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig) (*TTSResponse, error) {
	return nil, fmt.Errorf("%s no such method", m.Name())
}

func (m *MinerULocalModel) AudioSpeechWithSender(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s no such method", m.Name())
}

func (m *MinerULocalModel) OCRFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, ocrConfig *OCRConfig) (*OCRFileResponse, error) {
	return nil, fmt.Errorf("%s no such method", m.Name())
}

func (m *MinerULocalModel) ListModels(apiConfig *APIConfig) ([]ListModelResponse, error) {
	return nil, fmt.Errorf("%s no such method", m.Name())
}

func (m *MinerULocalModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	return nil, fmt.Errorf("%s no such method", m.Name())
}

func (m *MinerULocalModel) CheckConnection(apiConfig *APIConfig) error {
	return fmt.Errorf("%s no such method", m.Name())
}

func (m *MinerULocalModel) ParseFile(modelName *string, content []byte, documentURL *string, apiConfig *APIConfig, parseFileConfig *ParseFileConfig) (*ParseFileResponse, error) {
	if err := m.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if len(content) == 0 {
		return nil, fmt.Errorf("local MinerU API requires file content byte array, but content is empty")
	}

	resolvedBaseURL, err := m.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	apiURL := fmt.Sprintf("%s/%s", resolvedBaseURL, m.baseModel.URLSuffix.DocumentParse)

	var body bytes.Buffer
	writer := multipart.NewWriter(&body)

	filename := "upload_document.pdf"
	if parseFileConfig != nil && strings.TrimSpace(parseFileConfig.Filename) != "" {
		filename = filepath.Base(strings.TrimSpace(parseFileConfig.Filename))
	}
	contentType := minerUUploadContentTypes[strings.ToLower(filepath.Ext(filename))]
	if contentType == "" {
		contentType = "application/octet-stream"
	}
	header := make(textproto.MIMEHeader)
	disposition := mime.FormatMediaType("form-data", map[string]string{"name": "files", "filename": filename})
	if disposition == "" {
		return nil, fmt.Errorf("failed to encode multipart filename %q", filename)
	}
	header.Set("Content-Disposition", disposition)
	header.Set("Content-Type", contentType)
	part, err := writer.CreatePart(header)
	if err != nil {
		return nil, fmt.Errorf("failed to create multipart file field: %w", err)
	}
	if _, err = part.Write(content); err != nil {
		return nil, fmt.Errorf("failed to write file content: %w", err)
	}

	if modelName != nil && *modelName != "" {
		_ = writer.WriteField("backend", *modelName)
	} else {
		_ = writer.WriteField("backend", "pipeline")
	}
	effort := "high"
	imageAnalysis := true
	if parseFileConfig != nil {
		if strings.TrimSpace(parseFileConfig.Effort) != "" {
			effort = strings.TrimSpace(parseFileConfig.Effort)
		}
		if parseFileConfig.ImageAnalysis != nil {
			imageAnalysis = *parseFileConfig.ImageAnalysis
		}
	}
	_ = writer.WriteField("effort", effort)
	_ = writer.WriteField("image_analysis", strconv.FormatBool(imageAnalysis))
	_ = writer.WriteField("lang_list", "ch")
	_ = writer.WriteField("parse_method", "auto")
	_ = writer.WriteField("formula_enable", "true")
	_ = writer.WriteField("table_enable", "true")
	_ = writer.WriteField("return_md", "true")
	_ = writer.WriteField("return_content_list", "true")
	_ = writer.WriteField("return_images", "true")

	if err = writer.Close(); err != nil {
		return nil, fmt.Errorf("failed to close multipart writer: %w", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), longOpCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "POST", apiURL, &body)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", writer.FormDataContentType())

	if auth := BearerAuth(apiConfig); auth != "" {
		req.Header.Set("Authorization", auth)
	}

	resp, err := m.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK && resp.StatusCode != 202 {
		return nil, fmt.Errorf("local MinerU API failed with status %d: %s (URL: %s)", resp.StatusCode, string(respBody), apiURL)
	}

	var result map[string]interface{}
	if err := json.Unmarshal(respBody, &result); err != nil {
		return nil, fmt.Errorf("failed to parse response JSON: %w, body: %s", err, string(respBody))
	}
	// Get task ID
	var taskID string
	if dataMap, ok := result["data"].(map[string]interface{}); ok {
		if tid, ok := dataMap["task_id"].(string); ok {
			taskID = tid
		}
	} else if tid, ok := result["task_id"].(string); ok {
		taskID = tid
	}

	if taskID == "" {
		return nil, fmt.Errorf("failed to extract task_id from local MinerU response: %s", string(respBody))
	}

	return &ParseFileResponse{
		TaskID: taskID,
	}, nil
}

func (m *MinerULocalModel) ListTasks(apiConfig *APIConfig) ([]ListTaskStatus, error) {
	return nil, fmt.Errorf("%s no such method", m.Name())
}

func (m *MinerULocalModel) ShowTask(taskID string, apiConfig *APIConfig) (*TaskResponse, error) {
	if err := m.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if taskID == "" {
		return nil, fmt.Errorf("taskID is empty")
	}

	resolvedBaseURL, err := m.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s/%s/result", resolvedBaseURL, m.baseModel.URLSuffix.Task, taskID)
	ctx, cancel := context.WithTimeout(context.Background(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create status request: %w", err)
	}

	if auth := BearerAuth(apiConfig); auth != "" {
		req.Header.Set("Authorization", auth)
	}

	resp, err := m.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send status request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read status response: %w", err)
	}

	if resp.StatusCode != http.StatusOK && resp.StatusCode != 202 {
		return nil, fmt.Errorf("MinerU local status API failed with status %d: %s", resp.StatusCode, string(body))
	}

	var result struct {
		Results map[string]struct {
			MDContent   string            `json:"md_content"`
			ContentList string            `json:"content_list"`
			Images      map[string]string `json:"images"`
		} `json:"results"`
	}
	if err := json.Unmarshal(body, &result); err != nil {
		return nil, fmt.Errorf("failed to parse JSON: %w", err)
	}
	if len(result.Results) == 0 {
		return nil, fmt.Errorf("missing results field")
	}

	for _, fileResult := range result.Results {
		content := inlineMinerUImages(fileResult.MDContent, fileResult.Images)
		segments, err := minerUContentSegments(fileResult.ContentList, fileResult.Images)
		if err != nil {
			return nil, err
		}
		if strings.TrimSpace(content) == "" && len(segments) == 0 {
			continue
		}
		return &TaskResponse{Content: content, Segments: segments}, nil
	}
	return nil, fmt.Errorf("MinerU result contains no markdown or structured content")
}

type minerUContentBlock struct {
	Type           string    `json:"type"`
	Text           string    `json:"text"`
	Content        string    `json:"content"`
	TableBody      string    `json:"table_body"`
	ImagePath      string    `json:"img_path"`
	TableImagePath string    `json:"table_img_path"`
	ImageCaption   []string  `json:"image_caption"`
	ImageFootnote  []string  `json:"image_footnote"`
	ChartCaption   []string  `json:"chart_caption"`
	ChartFootnote  []string  `json:"chart_footnote"`
	TableCaption   []string  `json:"table_caption"`
	TableFootnote  []string  `json:"table_footnote"`
	BBox           []float64 `json:"bbox"`
	PageIndex      int       `json:"page_idx"`
}

func minerUContentSegments(raw string, images map[string]string) ([]TaskSegment, error) {
	if strings.TrimSpace(raw) == "" {
		return nil, nil
	}
	var blocks []minerUContentBlock
	if err := json.Unmarshal([]byte(raw), &blocks); err != nil {
		return nil, fmt.Errorf("failed to parse MinerU content_list: %w", err)
	}
	segments := make([]TaskSegment, 0, len(blocks))
	for _, block := range blocks {
		segment := TaskSegment{Index: len(segments), Type: "text", Layout: "text"}
		switch block.Type {
		case "header", "footer", "page_number", "discarded":
			continue
		case "table":
			segment.Type = "table"
			segment.Layout = "table"
			segment.Content = joinMinerUText(append([]string{block.TableBody}, append(block.TableCaption, block.TableFootnote...)...)...)
			segment.Image = minerUImage(images, block.TableImagePath)
		case "image":
			segment.Type = "image"
			segment.Layout = "figure"
			segment.Content = joinMinerUText(append(block.ImageCaption, block.ImageFootnote...)...)
			segment.Image = minerUImage(images, block.ImagePath)
		case "chart":
			segment.Type = "image"
			segment.Layout = "figure"
			segment.Content = joinMinerUText(append(append(block.ChartCaption, block.ChartFootnote...), block.Content)...)
			segment.Image = minerUImage(images, block.ImagePath)
		default:
			segment.Content = joinMinerUText(block.Text, block.Content)
		}
		if segment.Content == "" && segment.Image != "" {
			segment.Content = "[Image]"
		}
		if segment.Content == "" {
			continue
		}
		if len(block.BBox) == 4 {
			segment.Positions = []float64{float64(block.PageIndex + 1), block.BBox[0], block.BBox[2], block.BBox[1], block.BBox[3]}
		}
		segments = append(segments, segment)
	}
	return segments, nil
}

func minerUImage(images map[string]string, imagePath string) string {
	if imagePath == "" {
		return ""
	}
	return images[path.Base(imagePath)]
}

func inlineMinerUImages(markdown string, images map[string]string) string {
	for name, dataURL := range images {
		markdown = strings.ReplaceAll(markdown, "images/"+name, dataURL)
	}
	return markdown
}

func joinMinerUText(values ...string) string {
	parts := make([]string, 0, len(values))
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value != "" {
			parts = append(parts, value)
		}
	}
	return strings.Join(parts, "\n")
}
