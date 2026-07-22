// Package main tests fail-closed refusal emission.
package main

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	"mvdan.cc/sh/v3/syntax"
)

func refusalCodes(events []Event) []string {
	codes := make([]string, 0)
	for _, event := range events {
		if event.Kind == "refusal" {
			codes = append(codes, event.Code)
		}
	}
	return codes
}

func commandHeads(t *testing.T, events []Event) []string {
	t.Helper()
	heads := make([]string, 0)
	for _, event := range events {
		if event.Kind != "command_site" {
			continue
		}
		if len(event.Argv) == 0 || event.Argv[0].Text == nil {
			t.Fatalf("command site has no static head: %#v", event)
		}
		heads = append(heads, *event.Argv[0].Text)
	}
	return heads
}

func refusalEvents(events []Event) []Event {
	refusals := make([]Event, 0)
	for _, event := range events {
		if event.Kind == "refusal" {
			refusals = append(refusals, event)
		}
	}
	return refusals
}

func TestRefusalCodesAreHelperScoped(t *testing.T) {
	sources := []string{
		`cat <(doc-lattice check)`,
		`A=*($(doc-lattice hidden)|b) echo outer; doc-lattice later`,
		`((1)); doc-lattice later`,
	}
	resp, err := Certify(mustRequest(t, sources...))
	if err != nil {
		t.Fatal(err)
	}
	for _, result := range resp.Results {
		got := refusalCodes(result.Events)
		if len(got) == 0 {
			t.Fatalf("result %d has no refusal event: %#v", result.ID, result.Events)
		}
		for _, code := range got {
			scope := reasonScopes[code]
			if scope != "terminal" && scope != "subtree-local" {
				t.Fatalf("helper emitted code %q with scope %q", code, scope)
			}
		}
	}
}

func TestProcSubstIsTerminalUnsupportedConstruct(t *testing.T) {
	const src = `cat <(doc-lattice hidden); doc-lattice later`
	resp, err := Certify(mustRequest(t, src))
	if err != nil {
		t.Fatal(err)
	}
	events := resp.Results[0].Events
	if got := commandHeads(t, events); !reflect.DeepEqual(got, []string{"cat"}) {
		t.Fatalf("command heads = %#v, want only the completed outer command", got)
	}
	if got := refusalCodes(events); !reflect.DeepEqual(got, []string{"unsupported-construct"}) {
		t.Fatalf("refusal codes = %#v, want terminal unsupported-construct", got)
	}
	last := events[len(events)-1]
	if last.Kind != "refusal" || reasonScopes[last.Code] != "terminal" {
		t.Fatalf("terminal refusal must be last, got %#v", last)
	}
}

func TestArithmCmdTerminalSuppressesLaterSibling(t *testing.T) {
	const src = `((1)); doc-lattice later`
	resp, err := Certify(mustRequest(t, src))
	if err != nil {
		t.Fatal(err)
	}
	events := resp.Results[0].Events
	if got := commandHeads(t, events); len(got) != 0 {
		t.Fatalf("command heads = %#v, want terminal refusal to suppress later sibling", got)
	}
	if got := refusalCodes(events); !reflect.DeepEqual(got, []string{"unsupported-construct"}) {
		t.Fatalf("refusal codes = %#v, want terminal unsupported-construct", got)
	}
}

func TestUnsupportedExpansionRefusesLocallyWithoutLeakingNestedSite(t *testing.T) {
	const src = `A=*($(doc-lattice hidden)|b) echo outer; doc-lattice later`
	resp, err := Certify(mustRequest(t, src))
	if err != nil {
		t.Fatal(err)
	}
	events := resp.Results[0].Events
	if got := commandHeads(t, events); !reflect.DeepEqual(got, []string{"echo", "doc-lattice"}) {
		t.Fatalf("command heads = %#v, want outer and later sites without nested hidden site", got)
	}
	if got := refusalCodes(events); !reflect.DeepEqual(got, []string{"expansion-unsupported"}) {
		t.Fatalf("refusal codes = %#v, want one expansion-unsupported", got)
	}
	refusalIndex := indexOfEventKind(events, "refusal", 0)
	laterIndex := indexOfEventKind(events, "command_site", 1)
	if refusalIndex < 0 || laterIndex < 0 || refusalIndex >= laterIndex {
		t.Fatalf("events are not in local-refusal then later-site order: %#v", events)
	}
	if events[refusalIndex].EndByte > events[laterIndex].StartByte {
		t.Fatalf("local refusal overlaps later site: refusal %#v, site %#v", events[refusalIndex], events[laterIndex])
	}
}

func TestParameterExpansionOwnsNestedCommandSubstitutionRefusal(t *testing.T) {
	const src = `unset x; echo "${x:-$(doc-lattice hidden)}"; doc-lattice later`
	resp, err := Certify(mustRequest(t, src))
	if err != nil {
		t.Fatal(err)
	}
	events := resp.Results[0].Events
	if got := commandHeads(t, events); !reflect.DeepEqual(got, []string{"unset", "echo", "doc-lattice"}) {
		t.Fatalf("command heads = %#v, want prior, outer, and later sites without buried hidden site", got)
	}
	refusals := refusalEvents(events)
	if len(refusals) != 1 || refusals[0].Code != "expansion-unsupported" {
		t.Fatalf("refusals = %#v, want one expansion-unsupported", refusals)
	}
	wantStart := strings.Index(src, "${x:")
	wantEnd := strings.Index(src, `}";`) + 1
	if refusals[0].StartByte != wantStart || refusals[0].EndByte != wantEnd {
		t.Fatalf("refusal span = [%d, %d), want complete parameter expansion [%d, %d)", refusals[0].StartByte, refusals[0].EndByte, wantStart, wantEnd)
	}
	later := events[len(events)-1]
	if later.Kind != "command_site" || refusals[0].EndByte > later.StartByte {
		t.Fatalf("local refusal did not resynchronize before later site: %#v", events)
	}
}

func TestArithmeticExpansionOwnsNestedCommandSubstitutionRefusal(t *testing.T) {
	const src = `echo $(( $(doc-lattice hidden) + 1 )); doc-lattice later`
	resp, err := Certify(mustRequest(t, src))
	if err != nil {
		t.Fatal(err)
	}
	events := resp.Results[0].Events
	if got := commandHeads(t, events); !reflect.DeepEqual(got, []string{"echo", "doc-lattice"}) {
		t.Fatalf("command heads = %#v, want outer and later sites without buried hidden site", got)
	}
	refusals := refusalEvents(events)
	if len(refusals) != 1 || refusals[0].Code != "expansion-unsupported" {
		t.Fatalf("refusals = %#v, want one expansion-unsupported", refusals)
	}
	wantStart := strings.Index(src, "$((")
	wantEnd := strings.Index(src, ")); ") + 2
	if refusals[0].StartByte != wantStart || refusals[0].EndByte != wantEnd {
		t.Fatalf("refusal span = [%d, %d), want complete arithmetic expansion [%d, %d)", refusals[0].StartByte, refusals[0].EndByte, wantStart, wantEnd)
	}
	later := events[len(events)-1]
	if later.Kind != "command_site" || refusals[0].EndByte > later.StartByte {
		t.Fatalf("local refusal did not resynchronize before later site: %#v", events)
	}
}

func TestOutermostUnsupportedExpansionOwnsNestedRefusal(t *testing.T) {
	const src = `echo "${outer:-${inner:-$(doc-lattice hidden)}}"; doc-lattice later`
	resp, err := Certify(mustRequest(t, src))
	if err != nil {
		t.Fatal(err)
	}
	events := resp.Results[0].Events
	if got := commandHeads(t, events); !reflect.DeepEqual(got, []string{"echo", "doc-lattice"}) {
		t.Fatalf("command heads = %#v, want outer and later sites without buried hidden site", got)
	}
	refusals := refusalEvents(events)
	if len(refusals) != 1 || refusals[0].Code != "expansion-unsupported" {
		t.Fatalf("refusals = %#v, want one enclosing expansion refusal", refusals)
	}
	wantStart := strings.Index(src, "${outer:")
	wantEnd := strings.Index(src, `}";`) + 1
	if refusals[0].StartByte != wantStart || refusals[0].EndByte != wantEnd {
		t.Fatalf("refusal span = [%d, %d), want outermost expansion [%d, %d)", refusals[0].StartByte, refusals[0].EndByte, wantStart, wantEnd)
	}
}

func TestMultipleOwnedExpansionRefusalsRemainOrdered(t *testing.T) {
	const src = `echo "${x:-$(doc-lattice one)}" "$(( $(doc-lattice two) + 1 ))"; doc-lattice later`
	resp, err := Certify(mustRequest(t, src))
	if err != nil {
		t.Fatal(err)
	}
	events := resp.Results[0].Events
	if got := commandHeads(t, events); !reflect.DeepEqual(got, []string{"echo", "doc-lattice"}) {
		t.Fatalf("command heads = %#v, want outer and later sites without buried sites", got)
	}
	refusals := refusalEvents(events)
	if len(refusals) != 2 {
		t.Fatalf("refusals = %#v, want two local expansion refusals", refusals)
	}
	for index, refusal := range refusals {
		if refusal.Code != "expansion-unsupported" || reasonScopes[refusal.Code] != "subtree-local" {
			t.Fatalf("refusal %d = %#v, want subtree-local expansion-unsupported", index, refusal)
		}
		if index > 0 && refusals[index-1].EndByte > refusal.StartByte {
			t.Fatalf("refusals overlap or are out of order: %#v", refusals)
		}
	}
	if refusals[len(refusals)-1].EndByte > events[len(events)-1].StartByte {
		t.Fatalf("last local refusal overlaps later site: %#v", events)
	}
}

func TestDirectCommandSubstitutionStillTraverses(t *testing.T) {
	const src = `echo "$(doc-lattice direct)"; doc-lattice later`
	resp, err := Certify(mustRequest(t, src))
	if err != nil {
		t.Fatal(err)
	}
	events := resp.Results[0].Events
	if got := commandHeads(t, events); !reflect.DeepEqual(got, []string{"echo", "doc-lattice", "doc-lattice"}) {
		t.Fatalf("command heads = %#v, want outer, direct nested, and later sites", got)
	}
	if got := refusalCodes(events); len(got) != 0 {
		t.Fatalf("direct command substitution refusals = %#v, want none", got)
	}
}

func TestInertParameterAndArithmeticExpansionsRemainAccepted(t *testing.T) {
	const src = `echo "${x:-fallback}" "$((1+2))"; doc-lattice later`
	resp, err := Certify(mustRequest(t, src))
	if err != nil {
		t.Fatal(err)
	}
	events := resp.Results[0].Events
	if got := commandHeads(t, events); !reflect.DeepEqual(got, []string{"echo", "doc-lattice"}) {
		t.Fatalf("command heads = %#v, want outer and later sites", got)
	}
	if got := refusalCodes(events); len(got) != 0 {
		t.Fatalf("inert expansion refusals = %#v, want none", got)
	}
}

func TestMultipleLocalRefusalsAndLaterSiteStayInSourceOrder(t *testing.T) {
	const src = `A=*($(doc-lattice hidden)|b) echo first; A=*($(doc-lattice buried)|b) echo second; doc-lattice later`
	resp, err := Certify(mustRequest(t, src))
	if err != nil {
		t.Fatal(err)
	}
	events := resp.Results[0].Events
	if got := commandHeads(t, events); !reflect.DeepEqual(got, []string{"echo", "echo", "doc-lattice"}) {
		t.Fatalf("command heads = %#v, want both outer commands and later site", got)
	}
	if got := refusalCodes(events); !reflect.DeepEqual(got, []string{"expansion-unsupported", "expansion-unsupported"}) {
		t.Fatalf("refusal codes = %#v, want two expansion-local refusals", got)
	}
	previousStart := -1
	for _, event := range events {
		if event.StartByte < previousStart {
			t.Fatalf("events are out of source order: %#v", events)
		}
		previousStart = event.StartByte
	}
	for index, event := range events {
		if event.Kind != "refusal" {
			continue
		}
		for _, later := range events[index+1:] {
			if later.Kind == "command_site" && event.EndByte > later.StartByte {
				t.Fatalf("local refusal %#v overlaps later site %#v", event, later)
			}
		}
	}
}

func TestManyLocalRefusalsPreserveOrderingAndLaterSites(t *testing.T) {
	const localCount = 128
	var source strings.Builder
	for range localCount {
		source.WriteString(`A=*($(doc-lattice hidden)|b) echo outer; `)
	}
	source.WriteString(`doc-lattice later`)

	resp, err := Certify(mustRequest(t, source.String()))
	if err != nil {
		t.Fatal(err)
	}
	events := resp.Results[0].Events
	if len(events) != localCount*2+1 {
		t.Fatalf("event count = %d, want %d", len(events), localCount*2+1)
	}
	if got := len(commandHeads(t, events)); got != localCount+1 {
		t.Fatalf("command-site count = %d, want %d outer and later sites", got, localCount+1)
	}
	if got := refusalCodes(events); len(got) != localCount {
		t.Fatalf("refusal count = %d, want %d", len(got), localCount)
	}
	lastStart := -1
	for index, event := range events {
		if event.StartByte < lastStart {
			t.Fatalf("event %d is out of source order: %#v", index, events)
		}
		lastStart = event.StartByte
		if event.Kind == "refusal" && (event.Code != "expansion-unsupported" || reasonScopes[event.Code] != "subtree-local") {
			t.Fatalf("event %d = %#v, want subtree-local expansion refusal", index, event)
		}
	}
	if last := events[len(events)-1]; last.Kind != "command_site" || last.StartByte < events[len(events)-2].EndByte {
		t.Fatalf("final later site does not follow all local refusals: %#v", events[len(events)-2:])
	}
}

func TestValidateEventOrderingChecksLaterSiteSuffix(t *testing.T) {
	tests := []struct {
		name    string
		events  []Event
		wantErr bool
	}{
		{
			name: "same-start earlier site and boundary later site are valid",
			events: []Event{
				{Kind: "command_site", StartByte: 2, EndByte: 10},
				{Kind: "refusal", Code: "expansion-unsupported", StartByte: 2, EndByte: 5},
				{Kind: "command_site", StartByte: 5, EndByte: 8},
			},
		},
		{
			name: "non-adjacent later site overlaps local refusal",
			events: []Event{
				{Kind: "refusal", Code: "expansion-unsupported", StartByte: 2, EndByte: 9},
				{Kind: "refusal", Code: "expansion-unsupported", StartByte: 4, EndByte: 5},
				{Kind: "command_site", StartByte: 8, EndByte: 10},
			},
			wantErr: true,
		},
		{
			name: "terminal refusal must be last",
			events: []Event{
				{Kind: "refusal", Code: "unsupported-construct", StartByte: 2, EndByte: 3},
				{Kind: "command_site", StartByte: 4, EndByte: 5},
			},
			wantErr: true,
		},
		{
			name: "terminal refusal last is valid",
			events: []Event{
				{Kind: "command_site", StartByte: 2, EndByte: 3},
				{Kind: "refusal", Code: "unsupported-construct", StartByte: 4, EndByte: 5},
			},
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			err := validateEventOrdering(test.events)
			if (err != nil) != test.wantErr {
				t.Fatalf("validateEventOrdering() error = %v, wantErr %t", err, test.wantErr)
			}
		})
	}
}

func TestUnsupportedRedirectOperatorRefusesLocallyAtCompleteSpan(t *testing.T) {
	const src = `>x; doc-lattice later`
	stmts, parseRefusal := parseStatements(src)
	if parseRefusal != nil {
		t.Fatalf("parseStatements refusal = %#v, want none", parseRefusal)
	}
	stmts[0].Redirs[0].Op = syntax.RedirOperator(255)
	sites, refusals, _ := walk(stmts, src)
	if len(sites) != 1 || len(sites[0].argv) == 0 || sites[0].argv[0].Lit() != "doc-lattice" {
		t.Fatalf("sites = %#v, want only the certified next sibling", sites)
	}
	if len(refusals) != 1 || refusals[0].code != "redirect-unsupported" {
		t.Fatalf("refusals = %#v, want one redirect-unsupported", refusals)
	}
	if refusals[0].startByte != 0 || refusals[0].endByte != 2 {
		t.Fatalf("redirect refusal span = [%d, %d), want complete redirect span [0, 2)", refusals[0].startByte, refusals[0].endByte)
	}
}

func TestTerminalParseRefusalIsLastAfterCompletedStatement(t *testing.T) {
	const src = `doc-lattice check; echo "$(`
	resp, err := Certify(mustRequest(t, src))
	if err != nil {
		t.Fatal(err)
	}
	events := resp.Results[0].Events
	if got := commandHeads(t, events); !reflect.DeepEqual(got, []string{"doc-lattice"}) {
		t.Fatalf("command heads = %#v, want retained completed statement", got)
	}
	last := events[len(events)-1]
	if last.Kind != "refusal" || last.Code != "syntax-error" {
		t.Fatalf("terminal refusal must be last, got %#v", last)
	}
}

func TestTerminalParseRefusalFollowsLocalRefusal(t *testing.T) {
	const src = `A=*($(doc-lattice hidden)|b) echo outer; echo "$(`
	resp, err := Certify(mustRequest(t, src))
	if err != nil {
		t.Fatal(err)
	}
	events := resp.Results[0].Events
	if got := commandHeads(t, events); !reflect.DeepEqual(got, []string{"echo"}) {
		t.Fatalf("command heads = %#v, want retained completed outer statement", got)
	}
	if got := refusalCodes(events); !reflect.DeepEqual(got, []string{"expansion-unsupported", "syntax-error"}) {
		t.Fatalf("refusal codes = %#v, want local expansion then terminal parse refusal", got)
	}
	last := events[len(events)-1]
	if last.Kind != "refusal" || last.Code != "syntax-error" {
		t.Fatalf("terminal parse refusal must be last, got %#v", last)
	}
}

func TestInvalidGeneratedRuleCodeFailsClosed(t *testing.T) {
	key := constructKey{node: "ExtGlob", role: "*"}
	original := certifiedConstructs[key]
	t.Cleanup(func() { certifiedConstructs[key] = original })

	for _, invalidCode := range []string{"not-owned-by-helper", "assignment-prefix"} {
		t.Run(invalidCode, func(t *testing.T) {
			rule := original
			rule.code = invalidCode
			certifiedConstructs[key] = rule

			const src = `A=*($(doc-lattice hidden)|b) echo outer; doc-lattice later`
			resp, err := Certify(mustRequest(t, src))
			if err != nil {
				t.Fatalf("Certify returned an internal error instead of an owned terminal refusal: %v", err)
			}
			events := resp.Results[0].Events
			for _, event := range events {
				if event.Kind == "refusal" && event.Code == invalidCode {
					t.Fatalf("helper emitted invalid generated code %q: %#v", invalidCode, events)
				}
			}
			last := events[len(events)-1]
			if last.Kind != "refusal" || last.Code != "unsupported-construct" || reasonScopes[last.Code] != "terminal" {
				t.Fatalf("invalid generated code did not fail closed with an owned terminal refusal: %#v", events)
			}
			if got := commandHeads(t, events); !reflect.DeepEqual(got, []string{"echo"}) {
				t.Fatalf("command heads = %#v, want only the completed outer command before terminal fallback", got)
			}
		})
	}
}

func TestRefusalWireFormIsFrozen(t *testing.T) {
	payload, err := json.Marshal(Event{Kind: "refusal", Code: "unsupported-construct", StartByte: 4, EndByte: 9})
	if err != nil {
		t.Fatal(err)
	}
	const want = `{"kind":"refusal","code":"unsupported-construct","start_byte":4,"end_byte":9}`
	if string(payload) != want {
		t.Fatalf("refusal JSON = %s, want %s", payload, want)
	}
}

func indexOfEventKind(events []Event, kind string, occurrence int) int {
	for index, event := range events {
		if event.Kind == kind {
			if occurrence == 0 {
				return index
			}
			occurrence--
		}
	}
	return -1
}
