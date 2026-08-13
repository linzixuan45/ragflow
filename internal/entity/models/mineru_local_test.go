package models

import (
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestMinerULocalModelParseFilePreservesFilenameAndContentType(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/tasks" {
			http.NotFound(w, r)
			return
		}
		reader, err := r.MultipartReader()
		if err != nil {
			t.Errorf("MultipartReader: %v", err)
			http.Error(w, "invalid multipart", http.StatusBadRequest)
			return
		}
		for {
			part, err := reader.NextPart()
			if err == io.EOF {
				break
			}
			if err != nil {
				t.Errorf("NextPart: %v", err)
				http.Error(w, "invalid part", http.StatusBadRequest)
				return
			}
			if part.FormName() != "files" {
				continue
			}
			if got, want := part.FileName(), "report.docx"; got != want {
				t.Errorf("filename = %q, want %q", got, want)
			}
			if got, want := part.Header.Get("Content-Type"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"; got != want {
				t.Errorf("Content-Type = %q, want %q", got, want)
			}
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":{"task_id":"task-1"}}`))
	}))
	defer server.Close()

	driver := NewMinerLocalUModel(map[string]string{"default": server.URL}, URLSuffix{DocumentParse: "tasks"})
	baseURL := server.URL
	response, err := driver.ParseFile(nil, []byte("docx"), nil, &APIConfig{BaseURL: &baseURL}, &ParseFileConfig{Filename: "report.docx"})
	if err != nil {
		t.Fatalf("ParseFile: %v", err)
	}
	if got, want := response.TaskID, "task-1"; got != want {
		t.Fatalf("TaskID = %q, want %q", got, want)
	}
}
