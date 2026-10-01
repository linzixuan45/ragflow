package parser

import (
	"fmt"
	"os"
	"strings"
	"time"

	models "ragflow/internal/entity/models"
)

const minerUPollTimeout = 30 * time.Minute
const minerUPollInterval = 2 * time.Second

// minerUProtocolCandidates lists the legacy (3.x) submit endpoints to try in
// order.
//
// MinerU 4.x is detected through `/v1/health` and served by the V1 flow in
// ParseFileV1, so this list only covers 3.x servers. Within 3.x the submit
// route is not discoverable up front, so — as the Docling backend does for its
// `/v1` vs `/v1alpha` routes — we try the later route first and fall through
// when the submit is rejected. Only a failed *submit* advances the candidate;
// once a task id is issued the protocol is settled. The result route
// (`tasks/<id>/result`) is shared by both legacy routes.
//
// Any submit error advances the candidate, so a transient 5xx on the preferred
// route costs one extra rejected POST before the error is surfaced. That is
// deliberate: it cannot start a duplicate task (the server rejected the
// submit), and distinguishing "wrong route" from "server unhealthy" would
// otherwise need a separate probe.
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

	driver := models.NewMinerLocalUModel(
		map[string]string{"default": apiServer},
		models.URLSuffix{DocumentParse: minerUProtocolCandidates[0], Task: "tasks"},
	)

	// MinerU 4.x answers `/v1/health` and is served by the V1 upload/job flow;
	// 3.x has no such endpoint and keeps the legacy multipart submit. The
	// probe cannot fail a parse: an unreachable or foreign server simply
	// reports "legacy" and the 3.x path surfaces the real error.
	if driver.MinerUHealth(apiConfig) {
		result, err := driver.ParseFileV1(&backend, data, apiConfig, submitConfig)
		if err != nil {
			return ParseResult{Err: fmt.Errorf("parser: MinerU v1: %w", err)}
		}
		return parseMinerUTaskResult(filename, result, parser.OutputFormat)
	}

	taskID, err := submitLegacyMinerUTask(apiServer, &backend, data, apiConfig, submitConfig)
	if err != nil {
		return ParseResult{Err: err}
	}

	result, err := pollMinerUTask(driver, taskID, apiConfig, timeout)
	if err != nil {
		return ParseResult{Err: fmt.Errorf("parser: MinerU result: %w", err)}
	}
	return parseMinerUTaskResult(filename, result, parser.OutputFormat)
}

// submitLegacyMinerUTask submits to a MinerU 3.x server, trying each legacy
// submit route in order, and returns the issued task id.
func submitLegacyMinerUTask(apiServer string, backend *string, data []byte, apiConfig *models.APIConfig, submitConfig *models.ParseFileConfig) (string, error) {
	attempts := make([]string, 0, len(minerUProtocolCandidates))
	for _, submitRoute := range minerUProtocolCandidates {
		candidate := models.NewMinerLocalUModel(
			map[string]string{"default": apiServer},
			models.URLSuffix{DocumentParse: submitRoute, Task: "tasks"},
		)
		task, err := candidate.ParseFile(backend, data, nil, apiConfig, submitConfig)
		if err != nil {
			attempts = append(attempts, fmt.Sprintf("%s: %v", submitRoute, err))
			continue
		}
		return task.TaskID, nil
	}
	return "", fmt.Errorf("parser: MinerU submit failed for every protocol (%s)", strings.Join(attempts, "; "))
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
		layout := segment.Layout
		if segment.LayoutType != "" {
			layout = segment.LayoutType
		}
		item := map[string]any{
			"text":         segment.Content,
			"doc_type_kwd": segment.Type,
			"layout":       layout,
			"layout_type":  layout,
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
