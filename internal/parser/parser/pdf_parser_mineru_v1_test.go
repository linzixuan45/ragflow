package parser

import (
	"archive/zip"
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	models "ragflow/internal/entity/models"
)

// minerUV1MiddleJSON is a representative MinerU 4.x `middle_json` document: a
// heading, body text, a captioned figure, an equation, a table and a run of
// reference blocks that the projection must coalesce into one list item.
const minerUV1MiddleJSON = `{
  "pages": [
    {
      "page_idx": 0,
      "blocks": [
        {"type": "doc_title", "level": 1, "bbox": [10, 20, 300, 60], "content": [{"type": "text", "content": "Contract Title"}]},
        {"type": "text", "bbox": [10, 80, 500, 120], "content": [{"type": "text", "content": "Body paragraph."}]},
        {"type": "image", "bbox": [10, 130, 300, 330], "content": [
          {"type": "image_body", "image_path": "images/a.png"},
          {"type": "image_caption", "index": 0, "content": [{"type": "text", "content": "Figure one"}]}
        ]},
        {"type": "equation", "bbox": [10, 340, 200, 380], "content": [{"type": "text", "content": "E=mc^2"}]}
      ]
    },
    {
      "page_idx": 1,
      "blocks": [
        {"type": "table", "bbox": [10, 20, 400, 200], "content": [
          {"type": "table_body", "content": "<table><tr><td>a</td></tr></table>"},
          {"type": "table_caption", "index": 0, "content": [{"type": "text", "content": "Table one"}]}
        ]},
        {"type": "ref_text", "bbox": [10, 210, 300, 240], "content": [{"type": "text", "content": "Ref A"}]},
        {"type": "ref_text", "bbox": [10, 250, 300, 280], "content": [{"type": "text", "content": "Ref B"}]}
      ]
    }
  ]
}`

// minerUV1Zip builds the archive MinerU 4.x returns for a finished job.
func minerUV1Zip(t *testing.T) []byte {
	t.Helper()
	buffer := &bytes.Buffer{}
	writer := zip.NewWriter(buffer)
	for name, body := range map[string]string{
		"sample/middle_json.json": minerUV1MiddleJSON,
		"sample/images/a.png":     "PNGDATA",
	} {
		entry, err := writer.Create(name)
		if err != nil {
			t.Fatalf("zip create %s: %v", name, err)
		}
		if _, err := entry.Write([]byte(body)); err != nil {
			t.Fatalf("zip write %s: %v", name, err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatalf("zip close: %v", err)
	}
	return buffer.Bytes()
}

// minerUV1Server is a fake MinerU 4.x endpoint covering the whole V1 flow.
type minerUV1Server struct {
	t         *testing.T
	server    *httptest.Server
	zipBytes  []byte
	tiers     []string
	uploads   atomic.Int32
	completes atomic.Int32
	downloads atomic.Int32
	// jobFailsUntil makes the first N job submissions report a failed job, to
	// exercise the stale-reused-upload retry.
	jobFailsUntil atomic.Int32
	jobsSeen      atomic.Int32
	// legacyHits counts submits to the 3.x routes, which a 4.x server must
	// never receive.
	legacyHits atomic.Int32
	mu         sync.Mutex
}

func newMinerUV1Server(t *testing.T) *minerUV1Server {
	t.Helper()
	fake := &minerUV1Server{t: t, zipBytes: minerUV1Zip(t)}
	fake.server = httptest.NewServer(http.HandlerFunc(fake.handle))
	t.Cleanup(fake.server.Close)
	return fake
}

func (f *minerUV1Server) handle(w http.ResponseWriter, r *http.Request) {
	switch {
	case r.Method == http.MethodGet && r.URL.Path == "/v1/health":
		writeJSON(w, `{"status":"ok","version":"4.0.8"}`)
	case r.Method == http.MethodPost && r.URL.Path == "/v1/uploads":
		body, _ := io.ReadAll(r.Body)
		if !strings.Contains(string(body), `"purpose":"parse"`) {
			f.t.Errorf("upload creation body = %s, want purpose=parse", body)
		}
		f.uploads.Add(1)
		writeJSON(w, `{"id":"upload-1","upload_url":"`+f.server.URL+`/upload-body/upload-1"}`)
	case r.Method == http.MethodPut && r.URL.Path == "/upload-body/upload-1":
		io.Copy(io.Discard, r.Body)
	case r.Method == http.MethodPost && r.URL.Path == "/v1/uploads/upload-1/complete":
		f.completes.Add(1)
		id := "file-" + strconv.Itoa(int(f.completes.Load()))
		writeJSON(w, `{"file":{"id":"`+id+`"}}`)
	case r.Method == http.MethodPost && r.URL.Path == "/v1/parse/jobs":
		body, _ := io.ReadAll(r.Body)
		var payload struct {
			Tier  string `json:"tier"`
			Files []struct {
				Source struct {
					Type   string `json:"type"`
					FileID string `json:"file_id"`
				} `json:"source"`
			} `json:"files"`
			OutputFormats []string `json:"output_formats"`
		}
		if err := json.Unmarshal(body, &payload); err != nil {
			f.t.Errorf("job payload = %s: %v", body, err)
			return
		}
		f.mu.Lock()
		f.tiers = append(f.tiers, payload.Tier)
		f.mu.Unlock()
		f.jobsSeen.Add(1)
		if len(payload.OutputFormats) != 1 || payload.OutputFormats[0] != "zip" {
			f.t.Errorf("output_formats = %v, want [zip]", payload.OutputFormats)
		}
		writeJSON(w, `{"job_id":"job-1"}`)
	case r.Method == http.MethodGet && r.URL.Path == "/v1/parse/jobs/job-1":
		if int(f.jobsSeen.Load()) <= int(f.jobFailsUntil.Load()) {
			writeJSON(w, `{"status":"failed","files":[{"status":"failed","error":"file not found"}]}`)
			return
		}
		writeJSON(w, `{"status":"completed","files":[{"status":"completed","output_files":{"zip":{"file_id":"zip-1"}}}]}`)
	case r.Method == http.MethodGet && r.URL.Path == "/v1/files/zip-1/content":
		f.downloads.Add(1)
		w.Header().Set("Content-Type", "application/zip")
		_, _ = w.Write(f.zipBytes)
	default:
		if r.URL.Path == "/tasks" || r.URL.Path == "/file_parse" {
			f.legacyHits.Add(1)
		}
		f.t.Logf("unexpected MinerU v1 route %s %s", r.Method, r.URL.Path)
		http.NotFound(w, r)
	}
}

func (f *minerUV1Server) tierList() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.tiers...)
}

func writeJSON(w http.ResponseWriter, body string) {
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write([]byte(body))
}

// A 4.x server is detected via /v1/health and served end to end through the V1
// upload -> job -> zip flow, with the middle_json projected onto the same item
// shape the 3.x content_list produces.
func TestPDFParser_ParseWithResult_MinerUV1Integration(t *testing.T) {
	fake := newMinerUV1Server(t)

	pdf := NewPDFParser()
	pdf.ConfigureFromSetup(map[string]any{
		"parse_method":     "MinerU",
		"output_format":    "json",
		"mineru_apiserver": fake.server.URL,
		"mineru_backend":   "hybrid-engine",
	})

	res := pdf.ParseWithResult("sample.pdf", []byte("%PDF-1.4\nmock"))
	if res.Err != nil {
		t.Fatalf("ParseWithResult: %v", res.Err)
	}
	if got, want := res.OutputFormat, "json"; got != want {
		t.Fatalf("OutputFormat = %q, want %q", got, want)
	}
	if fake.uploads.Load() != 1 || fake.completes.Load() != 1 || fake.downloads.Load() != 1 {
		t.Fatalf("uploads/completes/downloads = %d/%d/%d, want 1/1/1", fake.uploads.Load(), fake.completes.Load(), fake.downloads.Load())
	}
	if got, want := fake.tierList(), []string{"standard"}; len(got) != 1 || got[0] != want[0] {
		t.Fatalf("tiers = %v, want %v", got, want)
	}
	if got, want := res.File["page_count"], 2; got != want {
		t.Fatalf("File.page_count = %v, want %v", got, want)
	}

	byText := map[string]map[string]any{}
	for _, item := range res.JSON {
		text, _ := item["text"].(string)
		byText[strings.TrimSpace(text)] = item
	}
	for _, want := range []string{"Contract Title", "Body paragraph.", "E=mc^2", "Ref A\nRef B"} {
		if _, ok := byText[want]; !ok {
			t.Fatalf("missing item %q in %#v", want, res.JSON)
		}
	}
	title := byText["Contract Title"]
	if got, want := title["layout"], "title"; got != want {
		t.Fatalf("title.layout = %#v, want %#v", got, want)
	}
	// The figure carries its zip image as an inline data URL and its caption.
	figure := byText["Figure one"]
	if figure == nil {
		t.Fatalf("missing figure item in %#v", res.JSON)
	}
	if got, want := figure["doc_type_kwd"], "image"; got != want {
		t.Fatalf("figure.doc_type_kwd = %#v, want %#v", got, want)
	}
	if got, want := figure["layout"], "figure"; got != want {
		t.Fatalf("figure.layout = %#v, want %#v", got, want)
	}
	if image, _ := figure["image"].(string); !strings.HasPrefix(image, "data:image/png;base64,") {
		t.Fatalf("figure.image = %#v, want inline PNG data URL", figure["image"])
	}
	table := byText["<table><tr><td>a</td></tr></table>\nTable one"]
	if table == nil {
		t.Fatalf("missing table item in %#v", res.JSON)
	}
	if got, want := table["doc_type_kwd"], "table"; got != want {
		t.Fatalf("table.doc_type_kwd = %#v, want %#v", got, want)
	}
	positions, ok := byText["Body paragraph."]["positions"].([][]float64)
	if !ok || len(positions) != 1 || positions[0][0] != 1 {
		t.Fatalf("body.positions = %#v, want one-based page 1", byText["Body paragraph."]["positions"])
	}
}

// output_format=markdown must still produce markdown on the V1 path, where the
// server returns middle_json rather than a rendered document.
func TestPDFParser_ParseWithResult_MinerUV1Markdown(t *testing.T) {
	fake := newMinerUV1Server(t)

	pdf := NewPDFParser()
	pdf.ConfigureFromSetup(map[string]any{
		"parse_method":     "MinerU",
		"output_format":    "markdown",
		"mineru_apiserver": fake.server.URL,
	})

	res := pdf.ParseWithResult("sample.pdf", []byte("%PDF-1.4\nmock"))
	if res.Err != nil {
		t.Fatalf("ParseWithResult: %v", res.Err)
	}
	if got, want := res.OutputFormat, "markdown"; got != want {
		t.Fatalf("OutputFormat = %q, want %q", got, want)
	}
	for _, want := range []string{"## Contract Title", "Body paragraph.", "E=mc^2", "Ref A\nRef B"} {
		if !strings.Contains(res.Markdown, want) {
			t.Fatalf("Markdown = %q, want it to contain %q", res.Markdown, want)
		}
	}
	if !strings.Contains(res.Markdown, "![Image](data:image/png;base64,") {
		t.Fatalf("Markdown = %q, want the figure inlined as a data URL", res.Markdown)
	}
}

// Page-range tasks of one document carry identical bytes, so the second parse
// must reuse the first upload instead of re-uploading the file.
func TestPDFParser_ParseWithResult_MinerUV1ReusesUpload(t *testing.T) {
	fake := newMinerUV1Server(t)

	pdf := NewPDFParser()
	pdf.ConfigureFromSetup(map[string]any{
		"parse_method":     "MinerU",
		"mineru_apiserver": fake.server.URL,
	})

	content := []byte("%PDF-1.4\nsame bytes across page ranges")
	for i := 0; i < 2; i++ {
		if res := pdf.ParseWithResult("sample.pdf", content); res.Err != nil {
			t.Fatalf("ParseWithResult #%d: %v", i, res.Err)
		}
	}
	if got, want := fake.uploads.Load(), int32(1); got != want {
		t.Fatalf("uploads = %d, want %d (second parse must reuse the first upload)", got, want)
	}
	if got, want := fake.downloads.Load(), int32(2); got != want {
		t.Fatalf("downloads = %d, want %d", got, want)
	}
}

// A cached upload can expire server-side; a failed job on the reuse path must
// re-upload once and retry rather than failing the parse.
func TestPDFParser_ParseWithResult_MinerUV1ReuploadsStaleFile(t *testing.T) {
	fake := newMinerUV1Server(t)

	pdf := NewPDFParser()
	pdf.ConfigureFromSetup(map[string]any{
		"parse_method":     "MinerU",
		"mineru_apiserver": fake.server.URL,
	})
	content := []byte("%PDF-1.4\nstale upload")

	if res := pdf.ParseWithResult("sample.pdf", content); res.Err != nil {
		t.Fatalf("first ParseWithResult: %v", res.Err)
	}
	// The next job referencing the cached file id fails, as an expired upload would.
	fake.jobFailsUntil.Store(fake.jobsSeen.Load() + 1)

	res := pdf.ParseWithResult("sample.pdf", content)
	if res.Err != nil {
		t.Fatalf("second ParseWithResult: %v", res.Err)
	}
	if got, want := fake.uploads.Load(), int32(2); got != want {
		t.Fatalf("uploads = %d, want %d (stale reuse must re-upload once)", got, want)
	}
	if got, want := fake.completes.Load(), int32(2); got != want {
		t.Fatalf("upload completions = %d, want %d", got, want)
	}
}

// A 3.x server has no /v1/health, so the legacy submit path must serve it.
func TestPDFParser_ParseWithResult_MinerULegacyServerSkipsV1(t *testing.T) {
	var legacySubmitCalled atomic.Bool

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/v1/health":
			http.NotFound(w, r)
		case r.Method == http.MethodPost && r.URL.Path == "/tasks":
			legacySubmitCalled.Store(true)
			writeJSON(w, `{"data":{"task_id":"task-legacy"}}`)
		case r.Method == http.MethodGet && r.URL.Path == "/tasks/task-legacy/result":
			writeJSON(w, `{"results":{"sample.pdf":{"md_content":"# Legacy\n\nBody.\n"}}}`)
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
	})

	res := pdf.ParseWithResult("sample.pdf", []byte("%PDF-1.4\nmock"))
	if res.Err != nil {
		t.Fatalf("ParseWithResult: %v", res.Err)
	}
	if !legacySubmitCalled.Load() {
		t.Fatal("legacy submit route was never attempted for a 3.x server")
	}
	if got, want := res.Markdown, "# Legacy\n\nBody.\n"; got != want {
		t.Fatalf("Markdown = %q, want %q", got, want)
	}
}

// MinerU 4.x health must be authoritative: a server that answers /v1/health is
// never submitted to over the legacy routes.
func TestPDFParser_ParseWithResult_MinerUV1ServerNeverUsesLegacy(t *testing.T) {
	fake := newMinerUV1Server(t)

	pdf := NewPDFParser()
	pdf.ConfigureFromSetup(map[string]any{
		"parse_method":     "MinerU",
		"mineru_apiserver": fake.server.URL,
	})

	if res := pdf.ParseWithResult("sample.pdf", []byte("%PDF-1.4\nmock")); res.Err != nil {
		t.Fatalf("ParseWithResult: %v", res.Err)
	}
	if fake.legacyHits.Load() != 0 {
		t.Fatal("legacy submit route was used against a MinerU 4.x server")
	}
}

func TestMinerUV1TierForBackend(t *testing.T) {
	cases := map[string]string{
		"pipeline":           "basic",
		"vlm-engine":         "advanced",
		"vlm-http-client":    "advanced",
		"hybrid-engine":      "standard",
		"hybrid-http-client": "standard",
		"HYBRID-ENGINE":      "standard",
		"":                   "standard",
		"unknown":            "standard",
	}
	for backend, want := range cases {
		if got := models.MinerUV1TierForBackend(backend); got != want {
			t.Errorf("tier for %q = %q, want %q", backend, got, want)
		}
	}
}
