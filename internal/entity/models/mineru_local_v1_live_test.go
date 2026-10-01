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

//go:build mineru_integration

package models

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// TestMinerU4LiveParse exercises the V1 client against a real MinerU 4.x
// server. It is excluded from normal runs because it needs network access and
// a GPU-backed parser:
//
//	MINERU_APISERVER=http://10.100.15.70:8886 \
//	  CGO_ENABLED=0 go test -tags mineru_integration -v -run TestMinerU4LiveParse \
//	  -timeout 20m ./internal/entity/models/
//
// Set MINERU_INTEGRATION_PDF to parse a specific document instead of the small
// repository sample.
func TestMinerU4LiveParse(t *testing.T) {
	apiServer := strings.TrimSpace(os.Getenv("MINERU_APISERVER"))
	if apiServer == "" {
		apiServer = "http://10.100.15.70:8886"
	}
	apiConfig := &APIConfig{BaseURL: &apiServer}
	driver := NewMinerLocalUModel(map[string]string{"default": apiServer}, URLSuffix{})

	if !driver.MinerUHealth(apiConfig) {
		t.Fatalf("no MinerU 4.x /v1/health at %s; this test needs a running 4.x server", apiServer)
	}
	t.Logf("MinerU 4.x detected at %s", apiServer)

	pdfPath := strings.TrimSpace(os.Getenv("MINERU_INTEGRATION_PDF"))
	if pdfPath == "" {
		pdfPath = filepath.Join(repoRoot(t), "test", "benchmark", "test_docs", "Doc1.pdf")
	}
	content, err := os.ReadFile(pdfPath)
	if err != nil {
		t.Fatalf("read %s: %v", pdfPath, err)
	}

	if raw := strings.TrimSpace(os.Getenv("MINERU_INTEGRATION_JOB_TIMEOUT")); raw != "" {
		if d, err := time.ParseDuration(raw); err == nil && d > 0 {
			previous := minerUV1JobTimeout
			minerUV1JobTimeout = d
			defer func() { minerUV1JobTimeout = previous }()
		}
	}

	started := time.Now()
	task, err := driver.ParseFileV1(strPtr("hybrid-engine"), content, apiConfig, &ParseFileConfig{
		Filename: filepath.Base(pdfPath),
	})
	if err != nil {
		t.Fatalf("ParseFileV1 against %s: %v", apiServer, err)
	}
	elapsed := time.Since(started)

	if len(task.Segments) == 0 {
		t.Fatal("live parse returned no segments")
	}
	if strings.TrimSpace(task.Content) == "" {
		t.Error("live parse returned empty markdown")
	}
	// The V1 projection must carry layout and geometry through to segments; an
	// empty LayoutType for every segment would mean the middle_json blocks were
	// flattened to plain text.
	layouts := map[string]int{}
	withPositions := 0
	for _, segment := range task.Segments {
		layouts[segment.LayoutType]++
		if len(segment.Positions) == 5 {
			withPositions++
		}
	}
	t.Logf("live parse: %d segments, %d with page positions, markdown %d bytes, %s",
		len(task.Segments), withPositions, len(task.Content), elapsed.Round(time.Millisecond))
	for layout, count := range layouts {
		t.Logf("  layout_type=%q x%d", layout, count)
	}
	for i, segment := range task.Segments {
		if i >= 5 {
			break
		}
		preview := []rune(strings.TrimSpace(segment.Content))
		if len(preview) > 60 {
			preview = preview[:60]
		}
		t.Logf("  [%d] type=%q layout=%q image=%v pos=%v text=%q",
			i, segment.Type, segment.LayoutType, segment.Image != "", segment.Positions, string(preview))
	}
	if withPositions == 0 {
		t.Error("no segment carried page positions; V1 geometry was lost")
	}
	for _, segment := range task.Segments {
		if segment.LayoutType == "" {
			t.Fatal("segments without layout_type: the middle_json projection dropped block kinds")
		}
	}
}

func repoRoot(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("Getwd: %v", err)
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatal("could not locate repository root (go.mod)")
		}
		dir = parent
	}
}

func strPtr(value string) *string {
	return &value
}
