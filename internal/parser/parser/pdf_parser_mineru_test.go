package parser

import (
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

func TestPDFParser_ParseWithResult_MinerUMarkdownIntegration(t *testing.T) {
	var submitCalled atomic.Bool
	var resultCalled atomic.Bool

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/tasks":
			submitCalled.Store(true)
			if got, want := r.Header.Get("Authorization"), "Bearer secret"; got != want {
				t.Errorf("Authorization = %q, want %q", got, want)
				return
			}
			reader, err := r.MultipartReader()
			if err != nil {
				t.Errorf("MultipartReader: %v", err)
				return
			}
			fields := map[string]string{}
			var fileSeen bool
			for {
				part, err := reader.NextPart()
				if err == io.EOF {
					break
				}
				if err != nil {
					t.Errorf("NextPart: %v", err)
					return
				}
				if part.FormName() != "files" {
					body, _ := io.ReadAll(part)
					fields[part.FormName()] = string(body)
				}
				if part.FormName() == "files" {
					fileSeen = true
					body, _ := io.ReadAll(part)
					if !strings.HasPrefix(string(body), "%PDF") {
						t.Errorf("uploaded file = %q, want PDF bytes", string(body))
						return
					}
				}
			}
			if fields["backend"] != "pipeline" {
				t.Errorf("backend = %q, want pipeline", fields["backend"])
				return
			}
			for key, want := range map[string]string{
				"effort":              "high",
				"image_analysis":      "true",
				"return_content_list": "true",
				"return_images":       "true",
			} {
				if got := fields[key]; got != want {
					t.Errorf("%s = %q, want %q", key, got, want)
					return
				}
			}
			if !fileSeen {
				t.Error("multipart upload missing files part")
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"data":{"task_id":"task-1"}}`))
		case r.Method == http.MethodGet && r.URL.Path == "/tasks/task-1/result":
			resultCalled.Store(true)
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"results":{"doc":{"md_content":"# Title\n\nBody paragraph.\n"}}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	pdf := NewPDFParser()
	pdf.ConfigureFromSetup(map[string]any{
		"parse_method":     "MinerU",
		"output_format":    "markdown",
		"mineru_apiserver": server.URL,
		"mineru_api_key":   "secret",
		"mineru_backend":   "pipeline",
	})

	res := pdf.ParseWithResult("sample.pdf", []byte("%PDF-1.4\nmock"))
	if res.Err != nil {
		t.Fatalf("ParseWithResult: %v", res.Err)
	}
	if !submitCalled.Load() || !resultCalled.Load() {
		t.Fatalf("submit/result called = %v/%v, want true/true", submitCalled.Load(), resultCalled.Load())
	}
	if got, want := res.OutputFormat, "markdown"; got != want {
		t.Fatalf("OutputFormat = %q, want %q", got, want)
	}
	if got, want := res.Markdown, "# Title\n\nBody paragraph.\n"; got != want {
		t.Fatalf("Markdown = %q, want %q", got, want)
	}
	if got, want := res.File["name"], "sample.pdf"; got != want {
		t.Fatalf("File.name = %v, want %v", got, want)
	}
}

func TestPDFParser_ParseWithResult_MinerUJSONIntegration(t *testing.T) {
	t.Setenv("MINERU_BACKEND", "")
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/tasks":
			if err := r.ParseMultipartForm(1 << 20); err != nil {
				t.Errorf("ParseMultipartForm: %v", err)
				return
			}
			if got, want := r.FormValue("backend"), "hybrid-engine"; got != want {
				t.Errorf("backend = %q, want %q", got, want)
				return
			}
			if got, want := r.FormValue("effort"), "high"; got != want {
				t.Errorf("effort = %q, want %q", got, want)
				return
			}
			if got, want := r.FormValue("image_analysis"), "true"; got != want {
				t.Errorf("image_analysis = %q, want %q", got, want)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"data":{"task_id":"task-2"}}`))
		case r.Method == http.MethodGet && r.URL.Path == "/tasks/task-2/result":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"results":{"doc":{"md_content":"# Title\n\n![](images/chart.jpg)\n","content_list":"[{\"type\":\"text\",\"text\":\"Title\",\"page_idx\":0,\"bbox\":[1,2,30,40]},{\"type\":\"chart\",\"img_path\":\"images/chart.jpg\",\"content\":\"| Category | Value |\",\"chart_caption\":[\"Performance chart\"],\"page_idx\":1,\"bbox\":[10,20,110,220]}]","images":{"chart.jpg":"data:image/jpeg;base64,Y2hhcnQ="}}}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	pdf := NewPDFParser()
	pdf.ConfigureFromSetup(map[string]any{
		"parse_method":     "MinerU",
		"output_format":    "json",
		"mineru_apiserver": server.URL,
	})

	res := pdf.ParseWithResult("sample.pdf", []byte("%PDF-1.4\nmock"))
	if res.Err != nil {
		t.Fatalf("ParseWithResult: %v", res.Err)
	}
	if got, want := res.OutputFormat, "json"; got != want {
		t.Fatalf("OutputFormat = %q, want %q", got, want)
	}
	if len(res.JSON) == 0 {
		t.Fatal("JSON is empty; want markdown-normalized items")
	}
	if got := res.JSON[0]["text"]; got == nil {
		t.Fatal("JSON[0].text missing")
	}
	if got, want := len(res.JSON), 2; got != want {
		t.Fatalf("len(JSON) = %d, want %d: %#v", got, want, res.JSON)
	}
	chart := res.JSON[1]
	if got, want := chart["doc_type_kwd"], "image"; got != want {
		t.Fatalf("chart.doc_type_kwd = %#v, want %#v", got, want)
	}
	if got, want := chart["image"], "data:image/jpeg;base64,Y2hhcnQ="; got != want {
		t.Fatalf("chart.image = %#v, want %#v", got, want)
	}
	if got, want := chart["layout"], "figure"; got != want {
		t.Fatalf("chart.layout = %#v, want %#v", got, want)
	}
	positions, ok := chart["positions"].([][]float64)
	if !ok || len(positions) != 1 || positions[0][0] != 2 {
		t.Fatalf("chart.positions = %#v, want one-based page 2", chart["positions"])
	}
}

func TestPDFParser_ParseWithResult_MinerURequiresAPIServer(t *testing.T) {
	pdf := NewPDFParser()
	pdf.ConfigureFromSetup(map[string]any{"parse_method": "MinerU"})

	res := pdf.ParseWithResult("sample.pdf", []byte("%PDF-1.4\nmock"))
	if res.Err == nil {
		t.Fatal("ParseWithResult: want error when mineru_apiserver is missing, got nil")
	}
	if !strings.Contains(res.Err.Error(), "mineru_apiserver") {
		t.Fatalf("error = %q, want mineru_apiserver context", res.Err.Error())
	}
}

// A MinerU 3.x server has no /v1/health, so the parser takes the legacy path;
// there the `tasks` submit route is also absent and the submit must fall
// through to the older `file_parse` route. The result route is shared.
func TestPDFParser_ParseWithResult_MinerULegacySubmitFallsBackAcrossRoutes(t *testing.T) {
	var v1SubmitCalled, legacySubmitCalled atomic.Bool

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/tasks":
			v1SubmitCalled.Store(true)
			// 3.x servers reject the 4.x submit route.
			http.Error(w, "not found", http.StatusNotFound)
		case r.Method == http.MethodPost && r.URL.Path == "/file_parse":
			legacySubmitCalled.Store(true)
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"task_id":"legacy-task-1"}`))
		case r.Method == http.MethodGet && r.URL.Path == "/tasks/legacy-task-1/result":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"results":{"sample.pdf":{"md_content":"# Legacy\n\nBody.\n"}}}`))
		default:
			http.Error(w, "unexpected route "+r.URL.Path, http.StatusNotFound)
		}
	}))
	defer server.Close()

	pdf := NewPDFParser()
	pdf.ConfigureFromSetup(map[string]any{
		"parse_method":     "MinerU",
		"output_format":    "markdown",
		"mineru_apiserver": server.URL,
		"mineru_backend":   "pipeline",
	})

	res := pdf.ParseWithResult("sample.pdf", []byte("%PDF-1.4\nmock"))
	if res.Err != nil {
		t.Fatalf("ParseWithResult: %v", res.Err)
	}
	if !v1SubmitCalled.Load() {
		t.Fatal("4.x submit route was never attempted; want it tried first")
	}
	if !legacySubmitCalled.Load() {
		t.Fatal("legacy submit route was never attempted; want fallback after 4.x rejection")
	}
	if got, want := res.OutputFormat, "markdown"; got != want {
		t.Fatalf("OutputFormat = %q, want %q", got, want)
	}
	if got, want := res.Markdown, "# Legacy\n\nBody.\n"; got != want {
		t.Fatalf("Markdown = %q, want %q", got, want)
	}
}

// When no submit route is accepted the error must name every attempted
// protocol so an operator can tell a version mismatch from a dead server.
func TestPDFParser_ParseWithResult_MinerUReportsAllProtocolFailures(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "nope", http.StatusNotFound)
	}))
	defer server.Close()

	pdf := NewPDFParser()
	pdf.ConfigureFromSetup(map[string]any{
		"parse_method":     "MinerU",
		"mineru_apiserver": server.URL,
	})

	res := pdf.ParseWithResult("sample.pdf", []byte("%PDF-1.4\nmock"))
	if res.Err == nil {
		t.Fatal("ParseWithResult: want error when every submit route fails")
	}
	for _, route := range []string{"tasks", "file_parse"} {
		if !strings.Contains(res.Err.Error(), route) {
			t.Fatalf("error = %q, want it to mention route %q", res.Err.Error(), route)
		}
	}
}

func TestMinerUUploadShape(t *testing.T) {
	body := &strings.Builder{}
	writer := multipart.NewWriter(body)
	_ = writer.WriteField("backend", "pipeline")
	if err := writer.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
	if !strings.Contains(body.String(), "pipeline") {
		t.Fatalf("multipart body = %q, want backend field", body.String())
	}
}
