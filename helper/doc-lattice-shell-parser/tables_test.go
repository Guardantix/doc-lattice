// Package main tests generated bounds and checkpoint tables.
package main

import (
	"bytes"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"testing"
)

const checkpointRootPath = "../../tests/fixtures/github_ci_successor_checkpoint/"

const checkpointTablesPath = "../../tests/fixtures/github_ci_successor_checkpoint/tables/"

func TestGeneratedBoundsMatchCheckpoint(t *testing.T) {
	if aggregateRequestCapBytes != 8_388_608 || helperSourceCapBytes != 4_194_304 {
		t.Fatalf("byte caps = (%d, %d), want (8388608, 4194304)", aggregateRequestCapBytes, helperSourceCapBytes)
	}
	if maxSourcesPerBatch != 4_096 || jsonMaxDepth != 64 {
		t.Fatalf("wire caps = (%d, %d), want (4096, 64)", maxSourcesPerBatch, jsonMaxDepth)
	}
	if statementCap != 65_536 || visitorNodeCap != 100_000 || visitorDepthCap != 200 || eventCap != 10_000 {
		t.Fatalf("certifier caps = (%d, %d, %d, %d), want (65536, 100000, 200, 10000)", statementCap, visitorNodeCap, visitorDepthCap, eventCap)
	}
	if maxArgvWordsPerSite != 4_096 || maxAssignmentsPerSite != 256 {
		t.Fatalf("per-site caps = (%d, %d), want (4096, 256)", maxArgvWordsPerSite, maxAssignmentsPerSite)
	}

	var checkpoint struct {
		AggregateRequestCapBytes int `json:"aggregate_request_cap_bytes"`
		HelperSourceCapBytes     int `json:"helper_source_cap_bytes"`
		MaxSourcesPerBatch       int `json:"max_sources_per_batch"`
		JSONMaxDepth             int `json:"json_max_depth"`
		StatementCap             int `json:"statement_cap"`
		VisitorNodeCap           int `json:"visitor_node_cap"`
		VisitorDepthCap          int `json:"visitor_depth_cap"`
		EventCap                 int `json:"event_cap"`
		MaxArgvWordsPerSite      int `json:"max_argv_words_per_site"`
		MaxAssignmentsPerSite    int `json:"max_assignments_per_site"`
	}
	loadCheckpointJSON(t, checkpointRootPath+"limits.json", &checkpoint)
	got := []int{
		aggregateRequestCapBytes,
		helperSourceCapBytes,
		maxSourcesPerBatch,
		jsonMaxDepth,
		statementCap,
		visitorNodeCap,
		visitorDepthCap,
		eventCap,
		maxArgvWordsPerSite,
		maxAssignmentsPerSite,
	}
	want := []int{
		checkpoint.AggregateRequestCapBytes,
		checkpoint.HelperSourceCapBytes,
		checkpoint.MaxSourcesPerBatch,
		checkpoint.JSONMaxDepth,
		checkpoint.StatementCap,
		checkpoint.VisitorNodeCap,
		checkpoint.VisitorDepthCap,
		checkpoint.EventCap,
		checkpoint.MaxArgvWordsPerSite,
		checkpoint.MaxAssignmentsPerSite,
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("generated bounds = %v, want checkpoint bounds %v", got, want)
	}
}

func TestLimitGeneratorRejectsDuplicateRequiredKey(t *testing.T) {
	workspace := newGeneratorWorkspace(t)
	copyTestFile(t, "gen_limits.go", filepath.Join(workspace.helperDir, "gen_limits.go"))

	input := readTestFile(t, checkpointRootPath+"limits.json")
	closingBrace := bytes.LastIndexByte(input, '}')
	if closingBrace < 0 {
		t.Fatal("limits checkpoint has no closing object brace")
	}
	input = append(append([]byte{}, input[:closingBrace]...), []byte(",\n  \"aggregate_request_cap_bytes\": 0\n}\n")...)
	writeTestFile(t, filepath.Join(workspace.checkpointDir, "limits.json"), input)

	output, err := runTestGenerator(t, workspace.helperDir, "gen_limits.go")
	if err == nil {
		t.Fatalf("generator accepted a duplicate required key; output: %s", output)
	}
	if !bytes.Contains(output, []byte("duplicate")) {
		t.Fatalf("generator duplicate-key failure was unclear: %s", output)
	}
}

func TestLimitGeneratorRejectsNonpositiveCap(t *testing.T) {
	workspace := newGeneratorWorkspace(t)
	copyTestFile(t, "gen_limits.go", filepath.Join(workspace.helperDir, "gen_limits.go"))

	input := readTestFile(t, checkpointRootPath+"limits.json")
	input = bytes.Replace(input, []byte(`"helper_source_cap_bytes": 4194304`), []byte(`"helper_source_cap_bytes": 0`), 1)
	writeTestFile(t, filepath.Join(workspace.checkpointDir, "limits.json"), input)

	output, err := runTestGenerator(t, workspace.helperDir, "gen_limits.go")
	if err == nil {
		t.Fatalf("generator accepted a nonpositive cap; output: %s", output)
	}
	if !bytes.Contains(output, []byte("positive")) {
		t.Fatalf("generator nonpositive-cap failure was unclear: %s", output)
	}
}

func TestTableGeneratorRejectsDuplicateJSONField(t *testing.T) {
	workspace := newGeneratorWorkspace(t)
	copyTestFile(t, "gen_tables.go", filepath.Join(workspace.helperDir, "gen_tables.go"))

	constructs := readTestFile(t, checkpointTablesPath+"certified_constructs.json")
	constructs = bytes.Replace(constructs, []byte(`"node": "File",`), []byte(`"node": "File", "node": "File",`), 1)
	writeTestFile(t, filepath.Join(workspace.checkpointDir, "tables", "certified_constructs.json"), constructs)
	copyTestFile(t, checkpointTablesPath+"reason_codes.json", filepath.Join(workspace.checkpointDir, "tables", "reason_codes.json"))

	output, err := runTestGenerator(t, workspace.helperDir, "gen_tables.go")
	if err == nil {
		t.Fatalf("generator accepted a duplicate JSON field; output: %s", output)
	}
	if !bytes.Contains(output, []byte("duplicate")) {
		t.Fatalf("generator duplicate-field failure was unclear: %s", output)
	}
}

func TestTableGeneratorEmitsOwnedConstructCodes(t *testing.T) {
	workspace := newGeneratorWorkspace(t)
	copyTestFile(t, "gen_tables.go", filepath.Join(workspace.helperDir, "gen_tables.go"))
	copyTestFile(t, checkpointTablesPath+"certified_constructs.json", filepath.Join(workspace.checkpointDir, "tables", "certified_constructs.json"))
	copyTestFile(t, checkpointTablesPath+"reason_codes.json", filepath.Join(workspace.checkpointDir, "tables", "reason_codes.json"))

	output, err := runTestGenerator(t, workspace.helperDir, "gen_tables.go")
	if err != nil {
		t.Fatalf("generator failed: %v\n%s", err, output)
	}
	generated := readTestFile(t, filepath.Join(workspace.helperDir, "tables_gen.go"))
	for _, want := range [][]byte{
		[]byte("type constructRule struct"),
		[]byte(`{node: "ExtGlob", role: "*"}`),
		[]byte(`{node: "ProcSubst", role: "*"}`),
		[]byte(`{node: "RedirOperator", role: "*"}`),
		[]byte(`code: "expansion-unsupported"`),
		[]byte(`code: "unsupported-construct"`),
		[]byte(`code: "redirect-unsupported"`),
	} {
		if !bytes.Contains(generated, want) {
			t.Errorf("generated table does not contain %q", want)
		}
	}
}

func TestTableGeneratorRejectsMissingReferencedCode(t *testing.T) {
	workspace := newGeneratorWorkspace(t)
	copyTestFile(t, "gen_tables.go", filepath.Join(workspace.helperDir, "gen_tables.go"))
	copyTestFile(t, checkpointTablesPath+"certified_constructs.json", filepath.Join(workspace.checkpointDir, "tables", "certified_constructs.json"))
	reasons := readTestFile(t, checkpointTablesPath+"reason_codes.json")
	reasons = bytes.Replace(reasons, []byte(`"code": "expansion-unsupported"`), []byte(`"code": "missing-expansion-code"`), 1)
	writeTestFile(t, filepath.Join(workspace.checkpointDir, "tables", "reason_codes.json"), reasons)

	output, err := runTestGenerator(t, workspace.helperDir, "gen_tables.go")
	if err == nil {
		t.Fatalf("generator accepted a missing referenced code; output: %s", output)
	}
	if !bytes.Contains(output, []byte("expansion-unsupported")) {
		t.Fatalf("missing-code failure did not name the referenced code: %s", output)
	}
}

func TestTableGeneratorRejectsCommandLocalConstructCode(t *testing.T) {
	workspace := newGeneratorWorkspace(t)
	copyTestFile(t, "gen_tables.go", filepath.Join(workspace.helperDir, "gen_tables.go"))
	copyTestFile(t, checkpointTablesPath+"certified_constructs.json", filepath.Join(workspace.checkpointDir, "tables", "certified_constructs.json"))
	reasons := readTestFile(t, checkpointTablesPath+"reason_codes.json")
	reasons = bytes.Replace(reasons,
		[]byte("\"code\": \"expansion-unsupported\",\n      \"scope\": \"subtree-local\""),
		[]byte("\"code\": \"expansion-unsupported\",\n      \"scope\": \"command-local\""), 1)
	writeTestFile(t, filepath.Join(workspace.checkpointDir, "tables", "reason_codes.json"), reasons)

	output, err := runTestGenerator(t, workspace.helperDir, "gen_tables.go")
	if err == nil {
		t.Fatalf("generator accepted a command-local construct code; output: %s", output)
	}
	if !bytes.Contains(output, []byte("command-local")) {
		t.Fatalf("unowned-code failure did not name its scope: %s", output)
	}
}

func TestTableGeneratorRejectsIncorrectHelperOwnedScope(t *testing.T) {
	tests := []struct {
		code      string
		fromScope string
		toScope   string
	}{
		{code: "unsupported-construct", fromScope: "terminal", toScope: "subtree-local"},
		{code: "expansion-unsupported", fromScope: "subtree-local", toScope: "terminal"},
		{code: "redirect-unsupported", fromScope: "subtree-local", toScope: "terminal"},
	}
	for _, test := range tests {
		t.Run(test.code, func(t *testing.T) {
			workspace := newGeneratorWorkspace(t)
			copyTestFile(t, "gen_tables.go", filepath.Join(workspace.helperDir, "gen_tables.go"))
			copyTestFile(t, checkpointTablesPath+"certified_constructs.json", filepath.Join(workspace.checkpointDir, "tables", "certified_constructs.json"))
			reasons := readTestFile(t, checkpointTablesPath+"reason_codes.json")
			from := []byte("\"code\": \"" + test.code + "\",\n      \"scope\": \"" + test.fromScope + "\"")
			to := []byte("\"code\": \"" + test.code + "\",\n      \"scope\": \"" + test.toScope + "\"")
			modified := bytes.Replace(reasons, from, to, 1)
			if bytes.Equal(modified, reasons) {
				t.Fatalf("reason fixture did not contain %q with scope %q", test.code, test.fromScope)
			}
			writeTestFile(t, filepath.Join(workspace.checkpointDir, "tables", "reason_codes.json"), modified)

			output, err := runTestGenerator(t, workspace.helperDir, "gen_tables.go")
			if err == nil {
				t.Fatalf("generator accepted %q with scope %q; output: %s", test.code, test.toScope, output)
			}
			if !bytes.Contains(output, []byte(test.code)) || !bytes.Contains(output, []byte(test.toScope)) {
				t.Fatalf("wrong-scope failure did not identify code and scope: %s", output)
			}
		})
	}
}

func TestReasonScopesAreHelperEmittable(t *testing.T) {
	for code, scope := range reasonScopes {
		if scope != "terminal" && scope != "subtree-local" && scope != "command-local" {
			t.Errorf("reasonScopes[%q] = %q, want an allowed scope", code, scope)
		}
	}
	if reasonScopes["syntax-error"] != "terminal" {
		t.Fatalf("reasonScopes[syntax-error] = %q, want terminal", reasonScopes["syntax-error"])
	}
}

func TestCertifiedConstructsMatchCheckpoint(t *testing.T) {
	var checkpoint struct {
		ExportedNodeTypes []string `json:"exported_node_types"`
		Rows              []struct {
			Node        string `json:"node"`
			Role        string `json:"role"`
			Disposition string `json:"disposition"`
		} `json:"rows"`
	}
	loadCheckpointJSON(t, checkpointTablesPath+"certified_constructs.json", &checkpoint)

	expansionRefuseNodes := map[string]struct{}{
		"ArithmExp": {}, "ArithmExpr": {}, "BinAritOperator": {}, "BinaryArithm": {},
		"BraceExp": {}, "DblQuoted": {}, "Expansion": {}, "ExtGlob": {},
		"FlagsArithm": {}, "GlobOperator": {}, "ParamExp": {}, "ParenArithm": {},
		"ParExpOperator": {}, "ParNamesOperator": {}, "Replace": {}, "Slice": {},
		"UnAritOperator": {}, "UnaryArithm": {}, "WordPart": {},
	}
	expectedCode := func(node, disposition string) string {
		if disposition != "refuse" {
			return ""
		}
		if node == "RedirOperator" {
			return "redirect-unsupported"
		}
		if _, ok := expansionRefuseNodes[node]; ok {
			return "expansion-unsupported"
		}
		return "unsupported-construct"
	}

	want := make(map[constructKey]string, len(checkpoint.Rows))
	for _, row := range checkpoint.Rows {
		key := constructKey{node: row.Node, role: row.Role}
		if _, exists := want[key]; exists {
			t.Fatalf("checkpoint contains duplicate construct key: %+v", key)
		}
		want[key] = row.Disposition
	}
	if len(certifiedConstructs) != len(want) {
		t.Fatalf("certifiedConstructs has %d rows, want %d checkpoint rows", len(certifiedConstructs), len(want))
	}
	for key, disposition := range want {
		rule, ok := certifiedConstructs[key]
		if !ok || rule.disposition != disposition {
			t.Errorf("certifiedConstructs[%+v] = %+v, want disposition %q", key, rule, disposition)
			continue
		}
		if wantCode := expectedCode(key.node, disposition); rule.code != wantCode {
			t.Errorf("certifiedConstructs[%+v] code = %q, want exact code %q", key, rule.code, wantCode)
		}
	}
	if certifiedNodeTypeCount != len(checkpoint.ExportedNodeTypes) {
		t.Fatalf("certifiedNodeTypeCount = %d, want %d", certifiedNodeTypeCount, len(checkpoint.ExportedNodeTypes))
	}
}

func TestReasonScopesMatchCheckpoint(t *testing.T) {
	var checkpoint struct {
		Rows []struct {
			Code  string `json:"code"`
			Scope string `json:"scope"`
		} `json:"rows"`
	}
	loadCheckpointJSON(t, checkpointTablesPath+"reason_codes.json", &checkpoint)

	want := make(map[string]string, len(checkpoint.Rows))
	for _, row := range checkpoint.Rows {
		if _, exists := want[row.Code]; exists {
			t.Fatalf("checkpoint contains duplicate reason code: %q", row.Code)
		}
		want[row.Code] = row.Scope
	}
	if !reflect.DeepEqual(reasonScopes, want) {
		t.Fatalf("reasonScopes does not exactly match %d checkpoint rows", len(checkpoint.Rows))
	}
}

func TestTraversalRuleDocsMatchCheckpoint(t *testing.T) {
	var checkpoint struct {
		TraversalConvention struct {
			ContainerRule string `json:"container_rule"`
			WildcardRule  string `json:"wildcard_rule"`
		} `json:"traversal_convention"`
	}
	loadCheckpointJSON(t, checkpointTablesPath+"certified_constructs.json", &checkpoint)

	if traversalContainerRuleDoc == "" || traversalContainerRuleDoc != checkpoint.TraversalConvention.ContainerRule {
		t.Fatalf("traversalContainerRuleDoc does not exactly match the non-empty checkpoint rule")
	}
	if wildcardRuleDoc == "" || wildcardRuleDoc != checkpoint.TraversalConvention.WildcardRule {
		t.Fatalf("wildcardRuleDoc does not exactly match the non-empty checkpoint rule")
	}
}

func loadCheckpointJSON(t *testing.T, path string, target any) {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read checkpoint %s: %v", path, err)
	}
	if err := json.Unmarshal(data, target); err != nil {
		t.Fatalf("decode checkpoint %s: %v", path, err)
	}
}

type generatorWorkspace struct {
	helperDir     string
	checkpointDir string
}

func newGeneratorWorkspace(t *testing.T) generatorWorkspace {
	t.Helper()
	root := t.TempDir()
	workspace := generatorWorkspace{
		helperDir:     filepath.Join(root, "helper", "doc-lattice-shell-parser"),
		checkpointDir: filepath.Join(root, "tests", "fixtures", "github_ci_successor_checkpoint"),
	}
	if err := os.MkdirAll(workspace.helperDir, 0o755); err != nil {
		t.Fatalf("create temporary helper directory: %v", err)
	}
	if err := os.MkdirAll(filepath.Join(workspace.checkpointDir, "tables"), 0o755); err != nil {
		t.Fatalf("create temporary checkpoint directory: %v", err)
	}
	return workspace
}

func runTestGenerator(t *testing.T, directory, generator string) ([]byte, error) {
	t.Helper()
	command := exec.Command("/usr/local/go/bin/go", "run", generator)
	command.Dir = directory
	command.Env = append(os.Environ(), "GOCACHE="+filepath.Join(t.TempDir(), "go-cache"))
	return command.CombinedOutput()
}

func copyTestFile(t *testing.T, source, target string) {
	t.Helper()
	writeTestFile(t, target, readTestFile(t, source))
}

func readTestFile(t *testing.T, path string) []byte {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read test file %s: %v", path, err)
	}
	return data
}

func writeTestFile(t *testing.T, path string, data []byte) {
	t.Helper()
	if err := os.WriteFile(path, data, 0o644); err != nil {
		t.Fatalf("write test file %s: %v", path, err)
	}
}
