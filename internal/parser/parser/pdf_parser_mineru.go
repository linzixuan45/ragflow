package parser

import (
	"fmt"
	"os"
	"strings"
	"time"

	models "ragflow/internal/entity/models"
)

const minerUPollTimeout = 30 * time.Minute
const minerUPollInterval = 200 * time.Millisecond

// minerUProtocolCandidates lists the submit endpoints to try in order.
//
// MinerU 4.x replaced the 3.x `file_parse` submit route with `tasks`;
// the result route (`tasks/<id>/result`) is unchanged across both. The
// server version is not discoverable up front, so — as the Docling
// backend does for its `/v1` vs `/v1alpha` routes — we try the newer
// protocol first and fall through to the legacy one when the submit is
// rejected. Only a failed *submit* advances the candidate; once a task
// id is issued the protocol is settled.
//
// Any submit error advances the candidate, so a transient 5xx on the
// preferred route costs one extra rejected POST before the error is
// surfaced. That is deliberate: it cannot start a duplicate task (the
// server rejected the submit), and distinguishing "wrong version" from
// "server unhealthy" would otherwise need a separate probe.
var minerUProtocolCandidates = []string{"tasks", "file_parse"}

func parseWithMinerU(filename string, data []byte, parser *PDFParser) ParseResult {
	if len(data) == 0 {
		return emptyPDFResult(filename)
	}
	apiServer := strings.TrimSpace(parser.MinerUAPIServer)
	if apiServer == "" {
		apiServer = strings.TrimSpace(os.Getenv("MINERU_APISERVER"))
	}
	if apiServer == "" {
		return ParseResult{Err: fmt.Errorf("parser: MinerU requires mineru_apiserver or MINERU_APISERVER")}
	}
	apiKey := parser.MinerUAPIKey
	if strings.TrimSpace(apiKey) == "" {
		apiKey = strings.TrimSpace(os.Getenv("MINERU_API_KEY"))
	}
	backend := strings.TrimSpace(parser.MinerUBackend)
	if backend == "" {
		backend = strings.TrimSpace(os.Getenv("MINERU_BACKEND"))
	}
	if backend == "" {
		backend = "pipeline"
	}
	timeout := parser.MinerUPollTimeout
	if timeout <= 0 {
		timeout = minerUPollTimeout
	}

	apiConfig := &models.APIConfig{
		BaseURL: &apiServer,
	}
	if apiKey != "" {
		apiConfig.ApiKey = &apiKey
	}

	submitConfig := &models.ParseFileConfig{
		Filename:      filename,
		Effort:        parser.MinerUEffort,
		ImageAnalysis: &parser.MinerUImageAnalysis,
	}

	var (
		taskID   string
		driver   *models.MinerULocalModel
		attempts []string
	)
	for _, submitRoute := range minerUProtocolCandidates {
		candidate := models.NewMinerLocalUModel(
			map[string]string{"default": apiServer},
			models.URLSuffix{DocumentParse: submitRoute, Task: "tasks"},
		)
		task, err := candidate.ParseFile(&backend, data, nil, apiConfig, submitConfig)
		if err != nil {
			attempts = append(attempts, fmt.Sprintf("%s: %v", submitRoute, err))
			continue
		}
		driver, taskID = candidate, task.TaskID
		break
	}
	if driver == nil {
		return ParseResult{Err: fmt.Errorf("parser: MinerU submit failed for every protocol (%s)", strings.Join(attempts, "; "))}
	}

	result, err := pollMinerUTask(driver, taskID, apiConfig, timeout)
	if err != nil {
		return ParseResult{Err: fmt.Errorf("parser: MinerU result: %w", err)}
	}
	return parseMinerUTaskResult(filename, result, parser.OutputFormat)
}

func pollMinerUTask(driver *models.MinerULocalModel, taskID string, apiConfig *models.APIConfig, timeout time.Duration) (*models.TaskResponse, error) {
	if timeout <= 0 {
		timeout = minerUPollTimeout
	}
	deadline := time.Now().Add(timeout)
	var lastErr error
	for {
		task, err := driver.ShowTask(taskID, apiConfig)
		if err == nil {
			if strings.TrimSpace(task.Content) != "" || len(task.Segments) > 0 {
				return task, nil
			}
			lastErr = fmt.Errorf("empty MinerU task content")
		} else {
			lastErr = err
		}
		if time.Now().After(deadline) {
			if lastErr == nil {
				lastErr = fmt.Errorf("timed out waiting for MinerU task %s", taskID)
			}
			return nil, lastErr
		}
		time.Sleep(minerUPollInterval)
	}
}

func parseMinerUTaskResult(filename string, task *models.TaskResponse, outputFormat string) ParseResult {
	if task == nil {
		return ParseResult{Err: fmt.Errorf("parser: empty MinerU task result")}
	}
	pageCount := 0
	items := make([]map[string]any, 0, len(task.Segments))
	for _, segment := range task.Segments {
		item := map[string]any{
			"text":         segment.Content,
			"doc_type_kwd": segment.Type,
			"layout":       segment.Layout,
		}
		if segment.Image != "" {
			item["image"] = segment.Image
		}
		if len(segment.Positions) == 5 {
			position := append([]float64(nil), segment.Positions...)
			item["positions"] = [][]float64{position}
			if page := int(position[0]); page > pageCount {
				pageCount = page
			}
		}
		items = append(items, item)
	}
	if pageCount == 0 && strings.TrimSpace(task.Content) != "" {
		pageCount = 1
	}

	switch strings.ToLower(strings.TrimSpace(outputFormat)) {
	case "", "json":
		if len(items) > 0 {
			return ParseResult{OutputFormat: "json", File: pdfFileMeta(filename, pageCount), JSON: items}
		}
		return parseMinerUMarkdownResult(filename, task.Content, "json", pageCount)
	case "markdown":
		return ParseResult{OutputFormat: "markdown", File: pdfFileMeta(filename, pageCount), Markdown: task.Content}
	default:
		return ParseResult{Err: fmt.Errorf("parser: unsupported MinerU output_format %q", outputFormat)}
	}
}

func parseMinerUMarkdownResult(filename, markdown, outputFormat string, pageCount int) ParseResult {
	fileMeta := pdfFileMeta(filename, pageCount)
	switch strings.ToLower(strings.TrimSpace(outputFormat)) {
	case "", "json":
		mp, err := NewMarkdownParser(GoMarkdown)
		if err != nil {
			return ParseResult{Err: err}
		}
		res := mp.ParseWithResult(filename, []byte(markdown))
		if res.Err != nil {
			return res
		}
		res.File = fileMeta
		return res
	case "markdown":
		return ParseResult{
			OutputFormat: "markdown",
			File:         fileMeta,
			Markdown:     markdown,
		}
	default:
		return ParseResult{Err: fmt.Errorf("parser: unsupported MinerU output_format %q", outputFormat)}
	}
}
