// Package main verifies the helper against the frozen wire-protocol fixtures.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

const checkpointProtocol = "../../tests/fixtures/github_ci_successor_checkpoint/protocol"

type conformanceFixture struct {
	Request  json.RawMessage `json:"request"`
	Response Response        `json:"response"`
}

func TestConformanceFixtures(t *testing.T) {
	dir := filepath.Join(checkpointProtocol, "conformance")
	paths := protocolFixturePaths(t, dir, ".json")
	for _, path := range paths {
		path := path
		t.Run(filepath.Base(path), func(t *testing.T) {
			fixture := readConformanceFixture(t, path)
			request, err := DecodeRequest(fixture.Request)
			if err != nil {
				t.Fatalf("fixture request %q rejected: %v", path, err)
			}
			got, err := Certify(request)
			if err != nil {
				t.Fatalf("Certify(%q): %v", path, err)
			}
			if got.ProtocolVersion != fixture.Response.ProtocolVersion || got.ProtocolVersion != 1 {
				t.Fatalf("protocol_version = %d, want frozen value %d", got.ProtocolVersion, fixture.Response.ProtocolVersion)
			}
			if got.ParserVersion != fixture.Response.ParserVersion || got.ParserVersion != parserVersion() {
				t.Fatalf("parser_version = %q, want frozen identity %q", got.ParserVersion, fixture.Response.ParserVersion)
			}
			if got.HelperVersion != helperVersion || got.HelperVersion == "" {
				t.Fatalf("helper_version = %q, want active non-empty identity %q", got.HelperVersion, helperVersion)
			}
			assertResultsEqual(t, fixture.Response.Results, got.Results)
		})
	}
}

func TestNegativeProtocolFixtures(t *testing.T) {
	dir := filepath.Join(checkpointProtocol, "negative")
	paths := protocolFixturePaths(t, dir, ".bin", ".json")
	foundEscapedLoneSurrogate := false
	for _, path := range paths {
		path := path
		if filepath.Base(path) == "escaped-lone-surrogate.json" {
			foundEscapedLoneSurrogate = true
		}
		t.Run(filepath.Base(path), func(t *testing.T) {
			data, err := os.ReadFile(path)
			if err != nil {
				t.Fatalf("read negative fixture %q: %v", path, err)
			}
			var stdout bytes.Buffer
			var stderr bytes.Buffer
			if code := run(bytes.NewReader(data), &stdout, &stderr); code != 2 {
				t.Fatalf("run(%q) exit = %d, want 2; stdout=%q stderr=%q", path, code, stdout.Bytes(), stderr.Bytes())
			}
			if stdout.Len() != 0 {
				t.Fatalf("run(%q) wrote %d stdout bytes on rejection: %q", path, stdout.Len(), stdout.Bytes())
			}
		})
	}
	if !foundEscapedLoneSurrogate {
		t.Fatal("negative fixture escaped-lone-surrogate.json was not exercised")
	}
}

func TestBoundaryProtocolFixtures(t *testing.T) {
	dir := filepath.Join(checkpointProtocol, "boundary")
	paths := protocolFixturePaths(t, dir, ".json")
	required := []string{
		"max-length-four-byte-source.json",
		"source-count-at-limit.json",
	}
	foundRequired := make(map[string]bool, len(required))
	for _, path := range paths {
		path := path
		name := filepath.Base(path)
		for _, requiredName := range required {
			if name == requiredName {
				foundRequired[name] = true
			}
		}
		t.Run(name, func(t *testing.T) {
			data, err := os.ReadFile(path)
			if err != nil {
				t.Fatalf("read boundary fixture %q: %v", path, err)
			}
			request, err := DecodeRequest(data)
			if err != nil {
				t.Fatalf("boundary fixture %q rejected by DecodeRequest: %v", path, err)
			}

			var stdout bytes.Buffer
			var stderr bytes.Buffer
			if code := run(bytes.NewReader(data), &stdout, &stderr); code != 0 {
				t.Fatalf("run(%q) exit = %d, want 0; stdout=%q stderr=%q", path, code, stdout.Bytes(), stderr.Bytes())
			}
			if !json.Valid(stdout.Bytes()) {
				t.Fatalf("run(%q) emitted invalid JSON: %q", path, stdout.Bytes())
			}
			var response Response
			if err := json.Unmarshal(stdout.Bytes(), &response); err != nil {
				t.Fatalf("decode response for %q: %v", path, err)
			}
			assertResponseIdentityBasics(t, path, request, &response)
		})
	}
	for _, name := range required {
		if !foundRequired[name] {
			t.Errorf("required boundary fixture %s was not exercised", name)
		}
	}
}

func protocolFixturePaths(t *testing.T, dir string, extensions ...string) []string {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("read protocol fixture directory %q: %v", dir, err)
	}
	allowed := make(map[string]struct{}, len(extensions))
	for _, extension := range extensions {
		allowed[extension] = struct{}{}
	}
	paths := make([]string, 0, len(entries))
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		if _, ok := allowed[filepath.Ext(entry.Name())]; !ok {
			continue
		}
		paths = append(paths, filepath.Join(dir, entry.Name()))
	}
	sort.Strings(paths)
	if len(paths) == 0 {
		t.Fatalf("protocol fixture directory %q has no %s files", dir, strings.Join(extensions, " or "))
	}
	return paths
}

func readConformanceFixture(t *testing.T, path string) conformanceFixture {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read conformance fixture %q: %v", path, err)
	}
	var fixture conformanceFixture
	if err := json.Unmarshal(data, &fixture); err != nil {
		t.Fatalf("decode conformance fixture %q: %v", path, err)
	}
	if len(fixture.Request) == 0 {
		t.Fatalf("conformance fixture %q has no request", path)
	}
	return fixture
}

func assertResultsEqual(t *testing.T, want, got []Result) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("result count = %d, want %d", len(got), len(want))
	}
	for resultIndex := range want {
		wantResult := want[resultIndex]
		gotResult := got[resultIndex]
		path := fmt.Sprintf("results[%d]", resultIndex)
		if gotResult.ID != wantResult.ID {
			t.Fatalf("%s.id = %d, want %d", path, gotResult.ID, wantResult.ID)
		}
		if gotResult.WorkUnits <= 0 {
			t.Fatalf("%s.work_units = %d, want a positive value", path, gotResult.WorkUnits)
		}
		if (gotResult.Events == nil) != (wantResult.Events == nil) {
			t.Fatalf("%s.events presence = %t, want %t", path, gotResult.Events != nil, wantResult.Events != nil)
		}
		if len(gotResult.Events) != len(wantResult.Events) {
			t.Fatalf("%s event count = %d, want %d", path, len(gotResult.Events), len(wantResult.Events))
		}
		for eventIndex := range wantResult.Events {
			assertEventEqual(t, fmt.Sprintf("%s.events[%d]", path, eventIndex), wantResult.Events[eventIndex], gotResult.Events[eventIndex])
		}
	}
}

func assertEventEqual(t *testing.T, path string, want, got Event) {
	t.Helper()
	if got.Kind != want.Kind {
		t.Fatalf("%s.kind = %q, want %q", path, got.Kind, want.Kind)
	}
	if got.StartByte != want.StartByte || got.EndByte != want.EndByte {
		t.Fatalf("%s span = [%d,%d), want [%d,%d)", path, got.StartByte, got.EndByte, want.StartByte, want.EndByte)
	}
	switch want.Kind {
	case "command_site":
		if got.Ordinal != want.Ordinal {
			t.Fatalf("%s.ordinal = %d, want %d", path, got.Ordinal, want.Ordinal)
		}
		assertAssignmentsEqual(t, path+".assignments", want.Assignments, got.Assignments)
		assertWordsEqual(t, path+".argv", want.Argv, got.Argv)
	case "refusal":
		if got.Code != want.Code {
			t.Fatalf("%s.code = %q, want %q", path, got.Code, want.Code)
		}
	default:
		t.Fatalf("%s has unsupported frozen event kind %q", path, want.Kind)
	}
}

func assertAssignmentsEqual(t *testing.T, path string, want, got []Assignment) {
	t.Helper()
	if (got == nil) != (want == nil) {
		t.Fatalf("%s presence = %t, want %t", path, got != nil, want != nil)
	}
	if len(got) != len(want) {
		t.Fatalf("%s count = %d, want %d", path, len(got), len(want))
	}
	for index := range want {
		wantAssignment := want[index]
		gotAssignment := got[index]
		itemPath := fmt.Sprintf("%s[%d]", path, index)
		if gotAssignment.Name != wantAssignment.Name {
			t.Fatalf("%s.name = %q, want %q", itemPath, gotAssignment.Name, wantAssignment.Name)
		}
		if gotAssignment.ValueKnown != wantAssignment.ValueKnown {
			t.Fatalf("%s.value_known = %t, want %t", itemPath, gotAssignment.ValueKnown, wantAssignment.ValueKnown)
		}
		if gotAssignment.StartByte != wantAssignment.StartByte || gotAssignment.EndByte != wantAssignment.EndByte {
			t.Fatalf("%s span = [%d,%d), want [%d,%d)", itemPath, gotAssignment.StartByte, gotAssignment.EndByte, wantAssignment.StartByte, wantAssignment.EndByte)
		}
	}
}

func assertWordsEqual(t *testing.T, path string, want, got []Word) {
	t.Helper()
	if (got == nil) != (want == nil) {
		t.Fatalf("%s presence = %t, want %t", path, got != nil, want != nil)
	}
	if len(got) != len(want) {
		t.Fatalf("%s count = %d, want %d", path, len(got), len(want))
	}
	for index := range want {
		wantWord := want[index]
		gotWord := got[index]
		itemPath := fmt.Sprintf("%s[%d]", path, index)
		if (gotWord.Text == nil) != (wantWord.Text == nil) {
			t.Fatalf("%s.text presence = %t, want %t", itemPath, gotWord.Text != nil, wantWord.Text != nil)
		}
		if wantWord.Text != nil && *gotWord.Text != *wantWord.Text {
			t.Fatalf("%s.text = %q, want %q", itemPath, *gotWord.Text, *wantWord.Text)
		}
		if gotWord.Single != wantWord.Single {
			t.Fatalf("%s.single = %t, want %t", itemPath, gotWord.Single, wantWord.Single)
		}
		if gotWord.StartByte != wantWord.StartByte || gotWord.EndByte != wantWord.EndByte {
			t.Fatalf("%s span = [%d,%d), want [%d,%d)", itemPath, gotWord.StartByte, gotWord.EndByte, wantWord.StartByte, wantWord.EndByte)
		}
	}
}

func assertResponseIdentityBasics(t *testing.T, fixturePath string, request *Request, response *Response) {
	t.Helper()
	if response.ProtocolVersion != request.ProtocolVersion || response.ProtocolVersion != 1 {
		t.Fatalf("response for %q has protocol_version %d, want %d", fixturePath, response.ProtocolVersion, request.ProtocolVersion)
	}
	if response.HelperVersion != helperVersion || response.HelperVersion == "" {
		t.Fatalf("response for %q has helper_version %q, want active non-empty identity %q", fixturePath, response.HelperVersion, helperVersion)
	}
	if response.ParserVersion != parserVersion() {
		t.Fatalf("response for %q has parser_version %q, want %q", fixturePath, response.ParserVersion, parserVersion())
	}
	if len(response.Results) != len(request.Sources) {
		t.Fatalf("response for %q has %d results, want %d", fixturePath, len(response.Results), len(request.Sources))
	}
	for index, result := range response.Results {
		if result.ID != request.Sources[index].ID {
			t.Fatalf("response for %q result[%d].id = %d, want %d", fixturePath, index, result.ID, request.Sources[index].ID)
		}
		if result.Events == nil {
			t.Fatalf("response for %q result[%d].events is null", fixturePath, index)
		}
		if result.WorkUnits <= 0 {
			t.Fatalf("response for %q result[%d].work_units = %d, want a positive value", fixturePath, index, result.WorkUnits)
		}
	}
}
