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
	"archive/zip"
	"bytes"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// minerUV1TestMiddleJSON covers every block family the V1 projection handles.
const minerUV1TestMiddleJSON = `{"pages":[
 {"page_idx":0,"blocks":[
   {"type":"doc_title","level":1,"bbox":[10,20,300,60],"content":[{"type":"text","content":"Contract Title"}]},
   {"type":"text","bbox":[10,80,500,120],"content":[{"type":"text","content":"Body paragraph."}]},
   {"type":"image","bbox":[10,130,300,330],"content":[
     {"type":"image_body","image_path":"images/a.png"},
     {"type":"image_caption","index":0,"content":[{"type":"text","content":"Figure one"}]}]},
   {"type":"equation","bbox":[10,340,200,380],"content":[{"type":"text","content":"E=mc^2"}]}]},
 {"page_idx":1,"blocks":[
   {"type":"table","bbox":[10,20,400,200],"content":[
     {"type":"table_body","content":"<table><tr><td>a</td></tr></table>"},
     {"type":"table_caption","index":0,"content":[{"type":"text","content":"Table one"}]}]},
   {"type":"ref_text","bbox":[10,210,300,240],"content":[{"type":"text","content":"Ref A"}]},
   {"type":"ref_text","bbox":[10,250,300,280],"content":[{"type":"text","content":"Ref B"}]}]}]}`

func minerUV1TestZip(t *testing.T) []byte {
	t.Helper()
	buffer := &bytes.Buffer{}
	writer := zip.NewWriter(buffer)
	for name, body := range map[string]string{
		"sample/middle_json.json": minerUV1TestMiddleJSON,
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

func TestMinerUV1TaskResponseProjectsMiddleJSON(t *testing.T) {
	task, err := minerUV1TaskResponse(minerUV1TestZip(t))
	if err != nil {
		t.Fatalf("minerUV1TaskResponse: %v", err)
	}
	byContent := map[string]TaskSegment{}
	for _, segment := range task.Segments {
		byContent[strings.TrimSpace(segment.Content)] = segment
	}
	for _, want := range []string{"Contract Title", "Body paragraph.", "E=mc^2", "Figure one", "Ref A\nRef B"} {
		if _, ok := byContent[want]; !ok {
			t.Fatalf("missing segment %q in %#v", want, task.Segments)
		}
	}
	title := byContent["Contract Title"]
	if got, want := title.LayoutType, "title"; got != want {
		t.Fatalf("title.LayoutType = %q, want %q", got, want)
	}
	figure := byContent["Figure one"]
	if got, want := figure.Type, "image"; got != want {
		t.Fatalf("figure.Type = %q, want %q", got, want)
	}
	if got, want := figure.LayoutType, "figure"; got != want {
		t.Fatalf("figure.LayoutType = %q, want %q", got, want)
	}
	if !strings.HasPrefix(figure.Image, "data:image/png;base64,") {
		t.Fatalf("figure.Image = %q, want inline PNG data URL", figure.Image)
	}
	table := byContent["<table><tr><td>a</td></tr></table>\nTable one"]
	if got, want := table.Type, "table"; got != want {
		t.Fatalf("table.Type = %q, want %q", got, want)
	}
	body := byContent["Body paragraph."]
	if len(body.Positions) != 5 || body.Positions[0] != 1 {
		t.Fatalf("body.Positions = %v, want one-based page 1", body.Positions)
	}
	if !strings.Contains(task.Content, "## Contract Title") {
		t.Fatalf("markdown = %q, want a rendered heading", task.Content)
	}
	if !strings.Contains(task.Content, "![Image](data:image/png;base64,") {
		t.Fatalf("markdown = %q, want the figure inlined", task.Content)
	}
}

func TestMinerUV1ReadZipRejectsMissingMiddleJSON(t *testing.T) {
	buffer := &bytes.Buffer{}
	writer := zip.NewWriter(buffer)
	entry, err := writer.Create("sample/images/a.png")
	if err != nil {
		t.Fatal(err)
	}
	_, _ = entry.Write([]byte("PNGDATA"))
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	if _, _, err := minerUV1ReadZip(buffer.Bytes()); err == nil {
		t.Fatal("want an error for a zip without middle_json.json")
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
		if got := MinerUV1TierForBackend(backend); got != want {
			t.Errorf("MinerUV1TierForBackend(%q) = %q, want %q", backend, got, want)
		}
	}
}

// The V1 flow must be reachable end to end and page-range tasks carrying the
// same bytes must share one upload.
func TestMinerULocalModelParseFileV1ReusesUpload(t *testing.T) {
	zipBytes := minerUV1TestZip(t)
	var uploads, completes, downloads int
	var serverURL string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json := func(body string) {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(body))
		}
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/v1/health":
			json(`{"status":"ok","version":"4.0.8"}`)
		case r.Method == http.MethodPost && r.URL.Path == "/v1/uploads":
			uploads++
			json(`{"id":"upload-1","upload_url":"` + serverURL + `/upload-body/upload-1"}`)
		case r.Method == http.MethodPut && r.URL.Path == "/upload-body/upload-1":
			w.WriteHeader(http.StatusOK)
		case r.Method == http.MethodPost && r.URL.Path == "/v1/uploads/upload-1/complete":
			completes++
			json(`{"file":{"id":"file-1"}}`)
		case r.Method == http.MethodPost && r.URL.Path == "/v1/parse/jobs":
			body := &bytes.Buffer{}
			_, _ = body.ReadFrom(r.Body)
			if !strings.Contains(body.String(), `"tier":"standard"`) {
				t.Errorf("job payload = %s, want tier=standard", body.String())
			}
			if !strings.Contains(body.String(), `"file_id":"file-1"`) {
				t.Errorf("job payload = %s, want file_id=file-1", body.String())
			}
			json(`{"job_id":"job-1"}`)
		case r.Method == http.MethodGet && r.URL.Path == "/v1/parse/jobs/job-1":
			json(`{"status":"completed","files":[{"status":"completed","output_files":{"zip":{"file_id":"zip-1"}}}]}`)
		case r.Method == http.MethodGet && r.URL.Path == "/v1/files/zip-1/content":
			downloads++
			_, _ = w.Write(zipBytes)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	serverURL = server.URL

	driver := NewMinerLocalUModel(map[string]string{"default": server.URL}, URLSuffix{})
	base := server.URL
	apiConfig := &APIConfig{BaseURL: &base}
	if !driver.MinerUHealth(apiConfig) {
		t.Fatal("MinerUHealth = false, want a 4.x server to be detected")
	}
	backend := "hybrid-engine"
	content := []byte("%PDF-1.4 page range task bytes")
	for i := 0; i < 2; i++ {
		result, err := driver.ParseFileV1(&backend, content, apiConfig, &ParseFileConfig{Filename: "sample.pdf"})
		if err != nil {
			t.Fatalf("ParseFileV1 #%d: %v", i, err)
		}
		if len(result.Segments) == 0 {
			t.Fatalf("ParseFileV1 #%d returned no segments", i)
		}
	}
	if uploads != 1 {
		t.Errorf("uploads = %d, want 1 (second parse must reuse the first upload)", uploads)
	}
	if completes != 1 || downloads != 2 {
		t.Errorf("completes/downloads = %d/%d, want 1/2", completes, downloads)
	}
}

func TestMinerUHealthRejectsLegacyServer(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.NotFound(w, r)
	}))
	defer server.Close()

	driver := NewMinerLocalUModel(map[string]string{"default": server.URL}, URLSuffix{})
	base := server.URL
	if driver.MinerUHealth(&APIConfig{BaseURL: &base}) {
		t.Fatal("MinerUHealth = true for a 3.x server, want false")
	}
}
