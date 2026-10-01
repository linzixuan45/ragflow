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
	"net/http"
	"path/filepath"
	"strings"
	"time"
)

// MinerU 4.x replaced the 3.x `file_parse` submit protocol with a three-step V1
// flow: create an upload, PUT the bytes, complete the upload to obtain a
// `file_id`, then submit a parse job referencing that id. The legacy multipart
// submit below cannot reach a 4.x server at all (its routes 404), so both
// protocols are implemented side by side and the caller picks one based on
// /v1/health.
const (
	minerUV1HealthPath   = "/v1/health"
	minerUV1UploadsPath  = "/v1/uploads"
	minerUV1CompletePath = "/v1/uploads/%s/complete"
	minerUV1JobsPath     = "/v1/parse/jobs"
	minerUV1JobPath      = "/v1/parse/jobs/%s"
	minerUV1FilePath     = "/v1/files/%s/content"

	minerUV1PollInterval = 2 * time.Second
	minerUV1MaxBytes     = 1 << 30
)

// minerUV1JobTimeout caps how long a parse job is polled before giving up.
// It is a variable so bounded integration probes can shorten it when the
// remote parse queue is backed up.
var minerUV1JobTimeout = 30 * time.Minute

// minerUV1TerminalStatuses are the job states that stop polling.
var minerUV1TerminalStatuses = map[string]bool{
	"completed": true,
	"partial":   true,
	"failed":    true,
	"canceled":  true,
}

// minerUBackendTiers maps a 3.x backend onto the MinerU 4.x tier carrying the
// same quality/speed trade-off (the official 4.0 changelog mapping), so
// `MINERU_BACKEND=hybrid-engine` keeps its meaning after the upgrade. Python
// applies the identical table in deepdoc/parser/mineru_parser.py.
var minerUBackendTiers = map[string]string{
	"pipeline":           "basic",
	"vlm-engine":         "advanced",
	"vlm-http-client":    "advanced",
	"hybrid-engine":      "standard",
	"hybrid-http-client": "standard",
}

// MinerUV1TierForBackend resolves the 4.x tier for a 3.x backend name; an
// unknown or empty backend keeps the historical hybrid default of `standard`.
func MinerUV1TierForBackend(backend string) string {
	if tier, ok := minerUBackendTiers[strings.ToLower(strings.TrimSpace(backend))]; ok {
		return tier
	}
	return "standard"
}

// MinerUHealth reports whether the endpoint speaks the 4.x V1 protocol.
// A 3.x server has no /v1/health, so an unreachable or non-conforming answer
// means legacy rather than an error: the caller then uses the 3.x submit path.
func (m *MinerULocalModel) MinerUHealth(apiConfig *APIConfig) bool {
	baseURL, err := m.baseModel.GetBaseURL(apiConfig)
	if err != nil || baseURL == "" {
		return false
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimSuffix(baseURL, "/")+minerUV1HealthPath, nil)
	if err != nil {
		return false
	}
	if auth := BearerAuth(apiConfig); auth != "" {
		req.Header.Set("Authorization", auth)
	}
	resp, err := m.baseModel.httpClient.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusServiceUnavailable {
		return false
	}
	var payload map[string]any
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&payload); err != nil {
		return false
	}
	// A 4.x /v1/health carries at least one of these keys; unrelated JSON is
	// not a MinerU V1 endpoint.
	_, hasVersion := payload["version"]
	_, hasStatus := payload["status"]
	return hasVersion || hasStatus
}

// ParseFileV1 runs one MinerU 4.x parse: submit the file, wait for the job to
// reach a terminal state, download its zip, and project the zip's
// `middle_json.json` onto the same content_list shape the 3.x protocol
// returns so both protocols feed one downstream mapping.
//
// Uploads are cached by (server, content hash) so the page-range tasks of one
// document reuse a single upload. A reused id can outlive the server's copy,
// so a failed job on the reuse path re-uploads once before giving up.
func (m *MinerULocalModel) ParseFileV1(modelName *string, content []byte, apiConfig *APIConfig, parseFileConfig *ParseFileConfig) (*TaskResponse, error) {
	if err := m.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}
	if len(content) == 0 {
		return nil, fmt.Errorf("local MinerU API requires file content byte array, but content is empty")
	}
	baseURL, err := m.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	baseURL = strings.TrimSuffix(baseURL, "/")

	filename := "upload_document.pdf"
	if parseFileConfig != nil && strings.TrimSpace(parseFileConfig.Filename) != "" {
		filename = filepath.Base(strings.TrimSpace(parseFileConfig.Filename))
	}
	backend := "pipeline"
	if modelName != nil && strings.TrimSpace(*modelName) != "" {
		backend = strings.TrimSpace(*modelName)
	}
	tier := MinerUV1TierForBackend(backend)

	cacheKey := minerUContentCacheKey(baseURL, content)
	fileID, reused, err := m.uploadFileV1(baseURL, cacheKey, filename, content, apiConfig)
	if err != nil {
		return nil, err
	}
	zipBytes, err := m.runParseJobV1(baseURL, fileID, tier, apiConfig)
	if err != nil && reused {
		// A cached file_id may have expired server-side; re-upload once
		// instead of failing the parse on a stale reference.
		minerUV1Uploads.drop(cacheKey)
		fileID, _, retryErr := m.uploadFileV1(baseURL, cacheKey, filename, content, apiConfig)
		if retryErr != nil {
			return nil, fmt.Errorf("%w (re-upload after stale reused upload failed: %v)", err, retryErr)
		}
		zipBytes, err = m.runParseJobV1(baseURL, fileID, tier, apiConfig)
	}
	if err != nil {
		return nil, err
	}
	return minerUV1TaskResponse(zipBytes)
}

// uploadFileV1 performs the V1 create/PUT/complete handshake and returns
// (file_id, reused).
func (m *MinerULocalModel) uploadFileV1(baseURL, cacheKey, filename string, content []byte, apiConfig *APIConfig) (string, bool, error) {
	if cached, ok := minerUV1Uploads.get(cacheKey); ok {
		return cached, true, nil
	}
	contentType := minerUUploadContentTypes[strings.ToLower(filepath.Ext(filename))]
	if contentType == "" {
		contentType = "application/octet-stream"
	}

	var created struct {
		ID            string            `json:"id"`
		UploadURL     string            `json:"upload_url"`
		UploadHeaders map[string]string `json:"upload_headers"`
	}
	if err := m.minerUV1JSON(http.MethodPost, baseURL+minerUV1UploadsPath, map[string]any{
		"filename":  filename,
		"bytes":     len(content),
		"mime_type": contentType,
		"purpose":   "parse",
	}, apiConfig, &created); err != nil {
		return "", false, fmt.Errorf("MinerU v1 upload creation failed: %w", err)
	}
	if created.ID == "" || created.UploadURL == "" {
		return "", false, fmt.Errorf("MinerU v1 upload creation returned no id/url")
	}
	uploadURL := created.UploadURL
	if strings.HasPrefix(uploadURL, "/") {
		uploadURL = baseURL + uploadURL
	}
	if err := m.minerUV1Put(uploadURL, content, created.UploadHeaders, apiConfig); err != nil {
		return "", false, fmt.Errorf("MinerU v1 upload body failed: %w", err)
	}

	var completed struct {
		File struct {
			ID string `json:"id"`
		} `json:"file"`
	}
	if err := m.minerUV1JSON(http.MethodPost, baseURL+fmt.Sprintf(minerUV1CompletePath, created.ID), nil, apiConfig, &completed); err != nil {
		return "", false, fmt.Errorf("MinerU v1 upload completion failed: %w", err)
	}
	if completed.File.ID == "" {
		return "", false, fmt.Errorf("MinerU v1 upload completion returned no file id")
	}
	minerUV1Uploads.put(cacheKey, completed.File.ID)
	return completed.File.ID, false, nil
}

// minerUV1Job is the subset of the V1 parse-job document we consume.
type minerUV1Job struct {
	Status string `json:"status"`
	Error  any    `json:"error"`
	Files  []struct {
		Status      string `json:"status"`
		Error       any    `json:"error"`
		OutputFiles struct {
			Zip struct {
				FileID string `json:"file_id"`
			} `json:"zip"`
		} `json:"output_files"`
	} `json:"files"`
}

// runParseJobV1 submits a parse job for an uploaded file and returns its zip.
func (m *MinerULocalModel) runParseJobV1(baseURL, fileID, tier string, apiConfig *APIConfig) ([]byte, error) {
	var submitted struct {
		JobID string `json:"job_id"`
	}
	payload := map[string]any{
		"files": []map[string]any{
			{"source": map[string]any{"type": "file_id", "file_id": fileID}},
		},
		"tier":           tier,
		"output_formats": []string{"zip"},
	}
	if err := m.minerUV1JSON(http.MethodPost, baseURL+minerUV1JobsPath, payload, apiConfig, &submitted); err != nil {
		return nil, fmt.Errorf("MinerU v1 parse job submission failed: %w", err)
	}
	if submitted.JobID == "" {
		return nil, fmt.Errorf("MinerU v1 parse job submission returned no job_id")
	}

	deadline := time.Now().Add(minerUV1JobTimeout)
	for {
		var job minerUV1Job
		if err := m.minerUV1JSON(http.MethodGet, baseURL+fmt.Sprintf(minerUV1JobPath, submitted.JobID), nil, apiConfig, &job); err != nil {
			return nil, fmt.Errorf("MinerU v1 job status failed: %w", err)
		}
		if minerUV1TerminalStatuses[strings.ToLower(strings.TrimSpace(job.Status))] {
			zipFileID, err := minerUV1ZipFileID(submitted.JobID, job)
			if err != nil {
				return nil, err
			}
			return m.fetchV1File(baseURL, zipFileID, apiConfig)
		}
		if time.Now().After(deadline) {
			return nil, fmt.Errorf("MinerU v1 parse job timed out after %s: job_id=%s", minerUV1JobTimeout, submitted.JobID)
		}
		time.Sleep(minerUV1PollInterval)
	}
}

func minerUV1ZipFileID(jobID string, job minerUV1Job) (string, error) {
	if len(job.Files) == 0 {
		return "", fmt.Errorf("MinerU v1 job %s (%s) reported no files", jobID, job.Status)
	}
	entry := job.Files[0]
	if !strings.EqualFold(strings.TrimSpace(entry.Status), "completed") || entry.OutputFiles.Zip.FileID == "" {
		return "", fmt.Errorf("MinerU v1 job %s (%s) produced no zip (file status %s, error %v)", jobID, job.Status, entry.Status, entry.Error)
	}
	return entry.OutputFiles.Zip.FileID, nil
}

func (m *MinerULocalModel) fetchV1File(baseURL, fileID string, apiConfig *APIConfig) ([]byte, error) {
	requestURL := baseURL + fmt.Sprintf(minerUV1FilePath, fileID)
	req, err := http.NewRequest(http.MethodGet, requestURL, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create MinerU v1 download request: %w", err)
	}
	req.Header.Set("Accept", "application/octet-stream")
	if auth := BearerAuth(apiConfig); auth != "" {
		req.Header.Set("Authorization", auth)
	}
	resp, err := m.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to download MinerU v1 file %s: %w", fileID, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return nil, fmt.Errorf("MinerU v1 file download failed with status %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, minerUV1MaxBytes))
	if err != nil {
		return nil, fmt.Errorf("failed to read MinerU v1 file %s: %w", fileID, err)
	}
	return body, nil
}

func (m *MinerULocalModel) minerUV1JSON(method, requestURL string, payload any, apiConfig *APIConfig, target any) error {
	var body io.Reader
	if payload != nil {
		encoded, err := json.Marshal(payload)
		if err != nil {
			return fmt.Errorf("failed to encode MinerU v1 request: %w", err)
		}
		body = bytes.NewReader(encoded)
	}
	req, err := http.NewRequest(method, requestURL, body)
	if err != nil {
		return fmt.Errorf("failed to create MinerU v1 request: %w", err)
	}
	if payload != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	req.Header.Set("Accept", "application/json")
	if auth := BearerAuth(apiConfig); auth != "" {
		req.Header.Set("Authorization", auth)
	}
	resp, err := m.baseModel.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("MinerU v1 request to %s failed: %w", requestURL, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusCreated && resp.StatusCode != http.StatusAccepted {
		responseBody, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("MinerU v1 request to %s failed with status %d: %s", requestURL, resp.StatusCode, strings.TrimSpace(string(responseBody)))
	}
	if target == nil {
		return nil
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<22)).Decode(target); err != nil {
		return fmt.Errorf("failed to decode MinerU v1 response from %s: %w", requestURL, err)
	}
	return nil
}

func (m *MinerULocalModel) minerUV1Put(requestURL string, content []byte, headers map[string]string, apiConfig *APIConfig) error {
	req, err := http.NewRequest(http.MethodPut, requestURL, bytes.NewReader(content))
	if err != nil {
		return fmt.Errorf("failed to create MinerU v1 upload request: %w", err)
	}
	for name, value := range headers {
		req.Header.Set(name, value)
	}
	if req.Header.Get("Content-Type") == "" {
		req.Header.Set("Content-Type", "application/octet-stream")
	}
	if auth := BearerAuth(apiConfig); auth != "" && req.Header.Get("Authorization") == "" {
		req.Header.Set("Authorization", auth)
	}
	resp, err := m.baseModel.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("MinerU v1 upload PUT failed with status %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	return nil
}
