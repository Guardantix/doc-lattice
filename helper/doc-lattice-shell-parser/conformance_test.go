// Package main verifies the helper against the frozen wire-protocol fixtures.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
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

func decodeConformanceFixtureStrict(data []byte) (conformanceFixture, error) {
	if err := rejectDuplicateJSONFields(data); err != nil {
		return conformanceFixture{}, err
	}
	root, err := strictJSONObject(data, "fixture", "request", "response")
	if err != nil {
		return conformanceFixture{}, err
	}
	if _, err := DecodeRequest(root["request"]); err != nil {
		return conformanceFixture{}, fmt.Errorf("fixture request: %w", err)
	}
	if err := validateFixtureResponse(root["response"]); err != nil {
		return conformanceFixture{}, err
	}
	var fixture conformanceFixture
	if err := json.Unmarshal(data, &fixture); err != nil {
		return conformanceFixture{}, fmt.Errorf("decode validated fixture: %w", err)
	}
	return fixture, nil
}

func rejectDuplicateJSONFields(data []byte) error {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	if err := scanUniqueJSONValue(decoder, "fixture"); err != nil {
		return err
	}
	if _, err := decoder.Token(); err != io.EOF {
		if err == nil {
			return fmt.Errorf("fixture contains a trailing JSON value")
		}
		return fmt.Errorf("fixture trailing JSON: %w", err)
	}
	return nil
}

func scanUniqueJSONValue(decoder *json.Decoder, path string) error {
	token, err := decoder.Token()
	if err != nil {
		return fmt.Errorf("%s: invalid JSON: %w", path, err)
	}
	delimiter, isDelimiter := token.(json.Delim)
	if !isDelimiter {
		return nil
	}
	switch delimiter {
	case '{':
		seen := make(map[string]struct{})
		for decoder.More() {
			keyToken, err := decoder.Token()
			if err != nil {
				return fmt.Errorf("%s: invalid object key: %w", path, err)
			}
			key, ok := keyToken.(string)
			if !ok {
				return fmt.Errorf("%s: object key has type %T", path, keyToken)
			}
			if _, duplicate := seen[key]; duplicate {
				return fmt.Errorf("%s: duplicate field %q", path, key)
			}
			seen[key] = struct{}{}
			if err := scanUniqueJSONValue(decoder, path+"."+key); err != nil {
				return err
			}
		}
		closing, err := decoder.Token()
		if err != nil || closing != json.Delim('}') {
			return fmt.Errorf("%s: invalid object close", path)
		}
	case '[':
		for index := 0; decoder.More(); index++ {
			if err := scanUniqueJSONValue(decoder, fmt.Sprintf("%s[%d]", path, index)); err != nil {
				return err
			}
		}
		closing, err := decoder.Token()
		if err != nil || closing != json.Delim(']') {
			return fmt.Errorf("%s: invalid array close", path)
		}
	default:
		return fmt.Errorf("%s: unexpected closing delimiter %q", path, delimiter)
	}
	return nil
}

func validateFixtureResponse(data json.RawMessage) error {
	response, err := strictJSONObject(data, "response", "protocol_version", "helper_version", "parser_version", "results")
	if err != nil {
		return err
	}
	version, err := strictNonnegativeInteger(response["protocol_version"], "response.protocol_version")
	if err != nil {
		return err
	}
	if version != 1 {
		return fmt.Errorf("response.protocol_version = %d, want 1", version)
	}
	if _, err := strictString(response["helper_version"], "response.helper_version"); err != nil {
		return err
	}
	if _, err := strictString(response["parser_version"], "response.parser_version"); err != nil {
		return err
	}
	results, err := strictJSONArray(response["results"], "response.results")
	if err != nil {
		return err
	}
	for index, result := range results {
		if err := validateFixtureResult(result, fmt.Sprintf("response.results[%d]", index)); err != nil {
			return err
		}
	}
	return nil
}

func validateFixtureResult(data json.RawMessage, path string) error {
	result, err := strictJSONObject(data, path, "id", "events", "work_units")
	if err != nil {
		return err
	}
	if _, err := strictNonnegativeInteger(result["id"], path+".id"); err != nil {
		return err
	}
	events, err := strictJSONArray(result["events"], path+".events")
	if err != nil {
		return err
	}
	for index, event := range events {
		if err := validateFixtureEvent(event, fmt.Sprintf("%s.events[%d]", path, index)); err != nil {
			return err
		}
	}
	_, err = strictNonnegativeInteger(result["work_units"], path+".work_units")
	return err
}

func validateFixtureEvent(data json.RawMessage, path string) error {
	event, err := rawJSONObject(data, path)
	if err != nil {
		return err
	}
	kindData, exists := event["kind"]
	if !exists {
		return fmt.Errorf("%s: missing required field %q", path, "kind")
	}
	kind, err := strictString(kindData, path+".kind")
	if err != nil {
		return err
	}
	switch kind {
	case "command_site":
		if err := requireExactFields(event, path, "kind", "ordinal", "start_byte", "end_byte", "assignments", "argv"); err != nil {
			return err
		}
		if _, err := strictNonnegativeInteger(event["ordinal"], path+".ordinal"); err != nil {
			return err
		}
		if err := validateFixtureSpan(event, path); err != nil {
			return err
		}
		assignments, err := strictJSONArray(event["assignments"], path+".assignments")
		if err != nil {
			return err
		}
		for index, assignment := range assignments {
			if err := validateFixtureAssignment(assignment, fmt.Sprintf("%s.assignments[%d]", path, index)); err != nil {
				return err
			}
		}
		argv, err := strictJSONArray(event["argv"], path+".argv")
		if err != nil {
			return err
		}
		for index, word := range argv {
			if err := validateFixtureWord(word, fmt.Sprintf("%s.argv[%d]", path, index)); err != nil {
				return err
			}
		}
	case "refusal":
		if err := requireExactFields(event, path, "kind", "code", "start_byte", "end_byte"); err != nil {
			return err
		}
		if _, err := strictString(event["code"], path+".code"); err != nil {
			return err
		}
		return validateFixtureSpan(event, path)
	default:
		return fmt.Errorf("%s.kind = %q, want command_site or refusal", path, kind)
	}
	return nil
}

func validateFixtureAssignment(data json.RawMessage, path string) error {
	assignment, err := strictJSONObject(data, path, "name", "value_known", "start_byte", "end_byte")
	if err != nil {
		return err
	}
	if _, err := strictString(assignment["name"], path+".name"); err != nil {
		return err
	}
	if err := strictBool(assignment["value_known"], path+".value_known"); err != nil {
		return err
	}
	return validateFixtureSpan(assignment, path)
}

func validateFixtureWord(data json.RawMessage, path string) error {
	word, err := strictJSONObject(data, path, "text", "single", "start_byte", "end_byte")
	if err != nil {
		return err
	}
	if !isJSONNull(word["text"]) {
		if _, err := strictString(word["text"], path+".text"); err != nil {
			return err
		}
	}
	if err := strictBool(word["single"], path+".single"); err != nil {
		return err
	}
	return validateFixtureSpan(word, path)
}

func validateFixtureSpan(object map[string]json.RawMessage, path string) error {
	start, err := strictNonnegativeInteger(object["start_byte"], path+".start_byte")
	if err != nil {
		return err
	}
	end, err := strictNonnegativeInteger(object["end_byte"], path+".end_byte")
	if err != nil {
		return err
	}
	if start > end {
		return fmt.Errorf("%s span [%d,%d) is reversed", path, start, end)
	}
	return nil
}

func strictJSONObject(data json.RawMessage, path string, fields ...string) (map[string]json.RawMessage, error) {
	object, err := rawJSONObject(data, path)
	if err != nil {
		return nil, err
	}
	if err := requireExactFields(object, path, fields...); err != nil {
		return nil, err
	}
	return object, nil
}

func rawJSONObject(data json.RawMessage, path string) (map[string]json.RawMessage, error) {
	if isJSONNull(data) {
		return nil, fmt.Errorf("%s must be an object, not null", path)
	}
	var object map[string]json.RawMessage
	if err := json.Unmarshal(data, &object); err != nil || object == nil {
		return nil, fmt.Errorf("%s must be an object", path)
	}
	return object, nil
}

func requireExactFields(object map[string]json.RawMessage, path string, fields ...string) error {
	allowed := make(map[string]struct{}, len(fields))
	for _, field := range fields {
		allowed[field] = struct{}{}
		if _, exists := object[field]; !exists {
			return fmt.Errorf("%s: missing required field %q", path, field)
		}
	}
	unknown := make([]string, 0)
	for field := range object {
		if _, exists := allowed[field]; !exists {
			unknown = append(unknown, field)
		}
	}
	if len(unknown) > 0 {
		sort.Strings(unknown)
		return fmt.Errorf("%s: unknown fields %q", path, unknown)
	}
	return nil
}

func strictJSONArray(data json.RawMessage, path string) ([]json.RawMessage, error) {
	if isJSONNull(data) {
		return nil, fmt.Errorf("%s must be an array, not null", path)
	}
	var array []json.RawMessage
	if err := json.Unmarshal(data, &array); err != nil || array == nil {
		return nil, fmt.Errorf("%s must be an array", path)
	}
	return array, nil
}

func strictString(data json.RawMessage, path string) (string, error) {
	if isJSONNull(data) {
		return "", fmt.Errorf("%s must be a string, not null", path)
	}
	var value string
	if err := json.Unmarshal(data, &value); err != nil {
		return "", fmt.Errorf("%s must be a string", path)
	}
	return value, nil
}

func strictBool(data json.RawMessage, path string) error {
	if isJSONNull(data) {
		return fmt.Errorf("%s must be a boolean, not null", path)
	}
	var value bool
	if err := json.Unmarshal(data, &value); err != nil {
		return fmt.Errorf("%s must be a boolean", path)
	}
	return nil
}

func strictNonnegativeInteger(data json.RawMessage, path string) (int64, error) {
	if isJSONNull(data) {
		return 0, fmt.Errorf("%s must be a nonnegative integer, not null", path)
	}
	value, ok := parseExactJSONInteger(bytes.TrimSpace(data))
	if !ok || value < 0 {
		return 0, fmt.Errorf("%s must be a nonnegative integer", path)
	}
	return value, nil
}

func isJSONNull(data json.RawMessage) bool {
	return len(data) == 0 || bytes.Equal(bytes.TrimSpace(data), []byte("null"))
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

func TestStrictConformanceFixtureDecodeRejectsMissingAndNullRequiredFields(t *testing.T) {
	tests := []struct {
		name      string
		fixture   string
		wantError string
		mutation  func(t *testing.T, root map[string]any)
	}{
		{
			name:      "missing zero ordinal",
			fixture:   "single-certified.json",
			wantError: `missing required field "ordinal"`,
			mutation: func(t *testing.T, root map[string]any) {
				delete(fixtureEventObject(t, root, 0, 0), "ordinal")
			},
		},
		{
			name:      "null ordinal",
			fixture:   "single-certified.json",
			wantError: "must be a nonnegative integer, not null",
			mutation: func(t *testing.T, root map[string]any) {
				fixtureEventObject(t, root, 0, 0)["ordinal"] = nil
			},
		},
		{
			name:      "missing null text",
			fixture:   "dynamic-word.json",
			wantError: `missing required field "text"`,
			mutation: func(t *testing.T, root map[string]any) {
				event := fixtureEventObject(t, root, 0, 0)
				argv := fixtureArrayValue(t, event["argv"], "event argv")
				delete(fixtureObjectValue(t, argv[1], "event argv[1]"), "text")
			},
		},
		{
			name:      "missing false value_known",
			fixture:   "multi-prefix.json",
			wantError: `missing required field "value_known"`,
			mutation: func(t *testing.T, root map[string]any) {
				event := fixtureEventObject(t, root, 0, 0)
				assignments := fixtureArrayValue(t, event["assignments"], "event assignments")
				delete(fixtureObjectValue(t, assignments[1], "event assignments[1]"), "value_known")
			},
		},
		{
			name:      "null value_known",
			fixture:   "multi-prefix.json",
			wantError: "must be a boolean, not null",
			mutation: func(t *testing.T, root map[string]any) {
				event := fixtureEventObject(t, root, 0, 0)
				assignments := fixtureArrayValue(t, event["assignments"], "event assignments")
				fixtureObjectValue(t, assignments[1], "event assignments[1]")["value_known"] = nil
			},
		},
	}
	for _, test := range tests {
		test := test
		t.Run(test.name, func(t *testing.T) {
			data := mutateConformanceFixture(t, test.fixture, test.mutation)
			_, err := decodeConformanceFixtureStrict(data)
			if err == nil {
				t.Fatal("strict fixture decoder accepted a missing or null required field")
			}
			if !strings.Contains(err.Error(), test.wantError) {
				t.Fatalf("strict fixture decoder error = %q, want it to contain %q", err, test.wantError)
			}
		})
	}
}

func TestStrictConformanceFixtureDecodeRejectsDuplicateKeys(t *testing.T) {
	tests := []struct {
		name     string
		fixture  string
		old, new string
	}{
		{
			name:    "response object",
			fixture: "single-certified.json",
			old:     `"helper_version":`,
			new:     `"protocol_version": 1, "helper_version":`,
		},
		{
			name:    "nested word object",
			fixture: "single-certified.json",
			old:     `"text": "doc-lattice",`,
			new:     `"text": "duplicate", "text": "doc-lattice",`,
		},
	}
	for _, test := range tests {
		test := test
		t.Run(test.name, func(t *testing.T) {
			path := filepath.Join(checkpointProtocol, "conformance", test.fixture)
			data, err := os.ReadFile(path)
			if err != nil {
				t.Fatalf("read conformance fixture %q: %v", path, err)
			}
			if count := bytes.Count(data, []byte(test.old)); count != 1 {
				t.Fatalf("fixture mutation target %q occurs %d times, want 1", test.old, count)
			}
			mutated := bytes.Replace(data, []byte(test.old), []byte(test.new), 1)
			_, err = decodeConformanceFixtureStrict(mutated)
			if err == nil {
				t.Fatal("strict fixture decoder accepted a duplicate object field")
			}
			if !strings.Contains(err.Error(), "duplicate field") {
				t.Fatalf("strict fixture decoder error = %q, want a duplicate-field error", err)
			}
		})
	}
}

func TestStrictConformanceFixtureDecodeRejectsUnknownAndCrossKindFields(t *testing.T) {
	tests := []struct {
		name     string
		fixture  string
		mutation func(t *testing.T, root map[string]any)
	}{
		{
			name:    "fixture root",
			fixture: "single-certified.json",
			mutation: func(_ *testing.T, root map[string]any) {
				root["unexpected"] = true
			},
		},
		{
			name:    "response",
			fixture: "single-certified.json",
			mutation: func(t *testing.T, root map[string]any) {
				fixtureObjectValue(t, root["response"], "response")["unexpected"] = true
			},
		},
		{
			name:    "result",
			fixture: "single-certified.json",
			mutation: func(t *testing.T, root map[string]any) {
				fixtureResultObject(t, root, 0)["unexpected"] = true
			},
		},
		{
			name:    "command event refusal field",
			fixture: "single-certified.json",
			mutation: func(t *testing.T, root map[string]any) {
				fixtureEventObject(t, root, 0, 0)["code"] = "syntax-error"
			},
		},
		{
			name:    "refusal event command field",
			fixture: "single-refusal.json",
			mutation: func(t *testing.T, root map[string]any) {
				fixtureEventObject(t, root, 0, 1)["ordinal"] = float64(0)
			},
		},
		{
			name:    "assignment",
			fixture: "assignment-prefix.json",
			mutation: func(t *testing.T, root map[string]any) {
				event := fixtureEventObject(t, root, 0, 0)
				assignments := fixtureArrayValue(t, event["assignments"], "event assignments")
				fixtureObjectValue(t, assignments[0], "event assignments[0]")["unexpected"] = true
			},
		},
		{
			name:    "word",
			fixture: "single-certified.json",
			mutation: func(t *testing.T, root map[string]any) {
				event := fixtureEventObject(t, root, 0, 0)
				argv := fixtureArrayValue(t, event["argv"], "event argv")
				fixtureObjectValue(t, argv[0], "event argv[0]")["unexpected"] = true
			},
		},
	}
	for _, test := range tests {
		test := test
		t.Run(test.name, func(t *testing.T) {
			data := mutateConformanceFixture(t, test.fixture, test.mutation)
			_, err := decodeConformanceFixtureStrict(data)
			if err == nil {
				t.Fatal("strict fixture decoder accepted an unknown or cross-kind field")
			}
			if !strings.Contains(err.Error(), "unknown fields") {
				t.Fatalf("strict fixture decoder error = %q, want an unknown-field error", err)
			}
		})
	}
}

func mutateConformanceFixture(t *testing.T, name string, mutate func(t *testing.T, root map[string]any)) []byte {
	t.Helper()
	path := filepath.Join(checkpointProtocol, "conformance", name)
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read conformance fixture %q: %v", path, err)
	}
	var root map[string]any
	if err := json.Unmarshal(data, &root); err != nil {
		t.Fatalf("decode conformance fixture %q for mutation: %v", path, err)
	}
	mutate(t, root)
	mutated, err := json.Marshal(root)
	if err != nil {
		t.Fatalf("encode mutated conformance fixture %q: %v", path, err)
	}
	return mutated
}

func fixtureResultObject(t *testing.T, root map[string]any, index int) map[string]any {
	t.Helper()
	response := fixtureObjectValue(t, root["response"], "response")
	results := fixtureArrayValue(t, response["results"], "response results")
	if index >= len(results) {
		t.Fatalf("response has %d results, index %d is unavailable", len(results), index)
	}
	return fixtureObjectValue(t, results[index], fmt.Sprintf("response results[%d]", index))
}

func fixtureEventObject(t *testing.T, root map[string]any, resultIndex, eventIndex int) map[string]any {
	t.Helper()
	result := fixtureResultObject(t, root, resultIndex)
	events := fixtureArrayValue(t, result["events"], "result events")
	if eventIndex >= len(events) {
		t.Fatalf("result has %d events, index %d is unavailable", len(events), eventIndex)
	}
	return fixtureObjectValue(t, events[eventIndex], fmt.Sprintf("result events[%d]", eventIndex))
}

func fixtureObjectValue(t *testing.T, value any, path string) map[string]any {
	t.Helper()
	object, ok := value.(map[string]any)
	if !ok {
		t.Fatalf("%s has type %T, want object", path, value)
	}
	return object
}

func fixtureArrayValue(t *testing.T, value any, path string) []any {
	t.Helper()
	array, ok := value.([]any)
	if !ok {
		t.Fatalf("%s has type %T, want array", path, value)
	}
	return array
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
				t.Fatalf("run(%q) exit = %d, want 2; stdout=%s stderr=%s", path, code, summarizeBytes(stdout.Bytes()), summarizeBytes(stderr.Bytes()))
			}
			if stdout.Len() != 0 {
				t.Fatalf("run(%q) wrote stdout on rejection: %s", path, summarizeBytes(stdout.Bytes()))
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
				t.Fatalf("run(%q) exit = %d, want 0; stdout=%s stderr=%s", path, code, summarizeBytes(stdout.Bytes()), summarizeBytes(stderr.Bytes()))
			}
			if !json.Valid(stdout.Bytes()) {
				t.Fatalf("run(%q) emitted invalid JSON: %s", path, summarizeBytes(stdout.Bytes()))
			}
			if err := rejectDuplicateJSONFields(stdout.Bytes()); err != nil {
				t.Fatalf("response for %q violates strict JSON object semantics: %v", path, err)
			}
			if err := validateFixtureResponse(stdout.Bytes()); err != nil {
				t.Fatalf("response for %q violates the response schema: %v", path, err)
			}
			var response Response
			if err := json.Unmarshal(stdout.Bytes(), &response); err != nil {
				t.Fatalf("decode response for %q: %v", path, err)
			}
			if err := validateBoundaryResponse(request, &response); err != nil {
				t.Fatalf("validate response for %q: %v", path, err)
			}
		})
	}
	for _, name := range required {
		if !foundRequired[name] {
			t.Errorf("required boundary fixture %s was not exercised", name)
		}
	}
}

func TestBoundaryResponseValidationRejectsEmptyEvents(t *testing.T) {
	request := &Request{ProtocolVersion: 1, Sources: []Source{{ID: 0, Source: "true"}}}
	response := &Response{
		ProtocolVersion: 1,
		HelperVersion:   helperVersion,
		ParserVersion:   parserVersion(),
		Results:         []Result{{ID: 0, Events: []Event{}, WorkUnits: 1}},
	}
	if err := validateBoundaryResponse(request, response); err == nil {
		t.Fatal("boundary response validator accepted a result with no syntax events")
	}
}

func TestByteSummaryBoundsLargeDiagnostics(t *testing.T) {
	data := bytes.Repeat([]byte("x"), 4*1024*1024)
	summary := summarizeBytes(data)
	if !strings.Contains(summary, "len=4194304") {
		t.Fatalf("summary does not report total length: %q", summary)
	}
	if len(summary) > 512 {
		t.Fatalf("4 MiB diagnostic summary has %d bytes, want at most 512", len(summary))
	}
}

func summarizeBytes(data []byte) string {
	const excerptLimit = 96
	excerpt := data
	suffix := ""
	if len(excerpt) > excerptLimit {
		excerpt = excerpt[:excerptLimit]
		suffix = " (truncated)"
	}
	return fmt.Sprintf("len=%d excerpt=%q%s", len(data), excerpt, suffix)
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
	fixture, err := decodeConformanceFixtureStrict(data)
	if err != nil {
		t.Fatalf("decode conformance fixture %q: %v", path, err)
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

func validateBoundaryResponse(request *Request, response *Response) error {
	if request == nil || response == nil {
		return fmt.Errorf("request and response must be non-nil")
	}
	if response.ProtocolVersion != request.ProtocolVersion || response.ProtocolVersion != 1 {
		return fmt.Errorf("protocol_version = %d, want %d", response.ProtocolVersion, request.ProtocolVersion)
	}
	if response.HelperVersion != helperVersion || response.HelperVersion == "" {
		return fmt.Errorf("helper_version = %q, want active non-empty identity %q", response.HelperVersion, helperVersion)
	}
	if response.ParserVersion != parserVersion() {
		return fmt.Errorf("parser_version = %q, want %q", response.ParserVersion, parserVersion())
	}
	if len(response.Results) != len(request.Sources) {
		return fmt.Errorf("result count = %d, want %d", len(response.Results), len(request.Sources))
	}
	for index, result := range response.Results {
		if err := validateLiteralBoundaryResult(request.Sources[index], result); err != nil {
			return fmt.Errorf("result[%d]: %w", index, err)
		}
	}
	return nil
}

func validateLiteralBoundaryResult(source Source, result Result) error {
	if result.ID != source.ID {
		return fmt.Errorf("id = %d, want %d", result.ID, source.ID)
	}
	if result.WorkUnits <= 0 {
		return fmt.Errorf("work_units = %d, want a positive value", result.WorkUnits)
	}
	if result.Events == nil {
		return fmt.Errorf("events is null")
	}
	if len(result.Events) != 1 {
		return fmt.Errorf("event count = %d, want 1 literal command_site", len(result.Events))
	}
	event := result.Events[0]
	wantEnd := len(source.Source)
	if event.Kind != "command_site" || event.Ordinal != 0 || event.StartByte != 0 || event.EndByte != wantEnd {
		return fmt.Errorf("event = kind %q ordinal %d span [%d,%d), want command_site ordinal 0 span [0,%d)", event.Kind, event.Ordinal, event.StartByte, event.EndByte, wantEnd)
	}
	if event.Assignments == nil || len(event.Assignments) != 0 {
		return fmt.Errorf("assignment count = %d with present=%t, want a present empty array", len(event.Assignments), event.Assignments != nil)
	}
	if event.Argv == nil || len(event.Argv) != 1 {
		return fmt.Errorf("argv count = %d with present=%t, want one word", len(event.Argv), event.Argv != nil)
	}
	word := event.Argv[0]
	if word.Text == nil {
		return fmt.Errorf("argv[0].text is null, want the literal source text")
	}
	if *word.Text != source.Source {
		return fmt.Errorf("argv[0].text differs at byte %d (got %d bytes, want %d)", firstDifferentByte(*word.Text, source.Source), len(*word.Text), len(source.Source))
	}
	if !word.Single || word.StartByte != 0 || word.EndByte != wantEnd {
		return fmt.Errorf("argv[0] = single %t span [%d,%d), want single true span [0,%d)", word.Single, word.StartByte, word.EndByte, wantEnd)
	}
	return nil
}

func firstDifferentByte(left, right string) int {
	limit := min(len(left), len(right))
	for index := 0; index < limit; index++ {
		if left[index] != right[index] {
			return index
		}
	}
	return limit
}
