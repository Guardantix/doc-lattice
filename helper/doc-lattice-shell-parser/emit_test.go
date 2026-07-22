// Package main tests command-site wire emission.
package main

import (
	"testing"

	"mvdan.cc/sh/v3/syntax"
)

func mustRequest(t *testing.T, sources ...string) *Request {
	t.Helper()
	request := &Request{ProtocolVersion: 1, Sources: make([]Source, len(sources))}
	for index, source := range sources {
		request.Sources[index] = Source{ID: index, Source: source}
	}
	return request
}

func findCommandSite(t *testing.T, result Result, ordinal int) Event {
	t.Helper()
	for _, event := range result.Events {
		if event.Kind == "command_site" && event.Ordinal == ordinal {
			return event
		}
	}
	t.Fatalf("command site ordinal %d not found in %#v", ordinal, result.Events)
	return Event{}
}

func TestEmitMultiPrefix(t *testing.T) {
	response, err := Certify(mustRequest(t, `A=1 B=$X doc-lattice check`))
	if err != nil {
		t.Fatalf("Certify error = %v", err)
	}
	if events := response.Results[0].Events; len(events) != 1 || events[0].Kind != "command_site" {
		t.Fatalf("events = %#v, want exactly the command_site fixture event", events)
	}
	site := findCommandSite(t, response.Results[0], 0)
	if site.StartByte != 0 || site.EndByte != 26 {
		t.Fatalf("site span = [%d, %d), want [0, 26)", site.StartByte, site.EndByte)
	}
	if len(site.Assignments) != 2 {
		t.Fatalf("assignments = %#v, want two", site.Assignments)
	}
	wantAssignments := []Assignment{
		{Name: "A", ValueKnown: true, StartByte: 0, EndByte: 3},
		{Name: "B", ValueKnown: false, StartByte: 4, EndByte: 8},
	}
	for index, want := range wantAssignments {
		if got := site.Assignments[index]; got != want {
			t.Errorf("assignment %d = %#v, want %#v", index, got, want)
		}
	}
	if len(site.Argv) != 2 {
		t.Fatalf("argv = %#v, want two words", site.Argv)
	}
	wantWords := []struct {
		text       string
		start, end int
	}{
		{text: "doc-lattice", start: 9, end: 20},
		{text: "check", start: 21, end: 26},
	}
	for index, want := range wantWords {
		got := site.Argv[index]
		if got.Text == nil || *got.Text != want.text || !got.Single || got.StartByte != want.start || got.EndByte != want.end {
			t.Errorf("argv %d = %#v, want text %q, single, span [%d, %d)", index, got, want.text, want.start, want.end)
		}
	}
}

func TestEmitAssignmentPrefixFixture(t *testing.T) {
	const src = `X=1 doc-lattice lint`
	response, err := Certify(mustRequest(t, src))
	if err != nil {
		t.Fatalf("Certify error = %v", err)
	}
	site := findCommandSite(t, response.Results[0], 0)
	wantAssignment := Assignment{Name: "X", ValueKnown: true, StartByte: 0, EndByte: 3}
	if site.StartByte != 0 || site.EndByte != 20 || len(site.Assignments) != 1 || site.Assignments[0] != wantAssignment {
		t.Fatalf("site = %#v, want fixture span and assignment %#v", site, wantAssignment)
	}
}

func TestEmitStaticConcatenationAndEmptyAssignment(t *testing.T) {
	const src = `EMPTY= do'c-'lattice 'check'`
	response, err := Certify(mustRequest(t, src))
	if err != nil {
		t.Fatalf("Certify error = %v", err)
	}
	site := findCommandSite(t, response.Results[0], 0)
	if site.StartByte != 0 || site.EndByte != len(src) {
		t.Fatalf("site span = [%d, %d), want [0, %d)", site.StartByte, site.EndByte, len(src))
	}
	wantAssignment := Assignment{Name: "EMPTY", ValueKnown: true, StartByte: 0, EndByte: 6}
	if len(site.Assignments) != 1 || site.Assignments[0] != wantAssignment {
		t.Fatalf("assignments = %#v, want %#v", site.Assignments, []Assignment{wantAssignment})
	}
	want := []struct {
		text       string
		start, end int
	}{
		{text: "doc-lattice", start: 7, end: 20},
		{text: "check", start: 21, end: 28},
	}
	if len(site.Argv) != len(want) {
		t.Fatalf("argv = %#v, want %d words", site.Argv, len(want))
	}
	for index, expected := range want {
		got := site.Argv[index]
		if got.Text == nil || *got.Text != expected.text || !got.Single || got.StartByte != expected.start || got.EndByte != expected.end {
			t.Errorf("argv %d = %#v, want text %q, single, span [%d, %d)", index, got, expected.text, expected.start, expected.end)
		}
	}
}

func TestEmitAssignmentColonTildeIsDynamic(t *testing.T) {
	response, err := Certify(mustRequest(t, `PATH=/bin:~user doc-lattice check`))
	if err != nil {
		t.Fatalf("Certify error = %v", err)
	}
	assignment := findCommandSite(t, response.Results[0], 0).Assignments[0]
	if assignment.ValueKnown {
		t.Fatalf("assignment = %#v, want environment-dependent tilde value", assignment)
	}
}

func TestEmitAssignmentGlobLiteralIsKnown(t *testing.T) {
	response, err := Certify(mustRequest(t, `A=*.go`))
	if err != nil {
		t.Fatalf("Certify error = %v", err)
	}
	site := findCommandSite(t, response.Results[0], 0)
	if len(site.Assignments) != 1 || !site.Assignments[0].ValueKnown {
		t.Fatalf("assignments = %#v, want literal glob value known", site.Assignments)
	}
}

func TestEmitArgvTildeAfterEqualsIsDynamic(t *testing.T) {
	response, err := Certify(mustRequest(t, `echo x=~`))
	if err != nil {
		t.Fatalf("Certify error = %v", err)
	}
	word := findCommandSite(t, response.Results[0], 0).Argv[1]
	if word.Text != nil || !word.Single {
		t.Fatalf("assignment-shaped argv word = %#v, want unknown text and one field", word)
	}
}

func TestEmitDynamicDoubleQuotedWordIsSingle(t *testing.T) {
	const src = `doc-lattice "$CMD"`
	response, err := Certify(mustRequest(t, src))
	if err != nil {
		t.Fatalf("Certify error = %v", err)
	}
	if events := response.Results[0].Events; len(events) != 1 || events[0].Kind != "command_site" {
		t.Fatalf("events = %#v, want exactly the dynamic-word fixture event", events)
	}
	word := findCommandSite(t, response.Results[0], 0).Argv[1]
	if word.Text != nil || !word.Single || word.StartByte != 12 || word.EndByte != 18 {
		t.Fatalf("dynamic quoted word = %#v, want nil text, single, span [12, 18)", word)
	}
}

func TestEmitArgvTildeContexts(t *testing.T) {
	tests := []struct {
		word   string
		text   *string
		single bool
	}{
		{word: `x=~`, text: nil, single: true},
		{word: `x=a:~user`, text: nil, single: true},
		{word: `x+=~`, text: nil, single: true},
		{word: `_x2+=~user`, text: nil, single: true},
		{word: `x+=a:~user`, text: nil, single: true},
		{word: "x+\\\n=~", text: nil, single: true},
		{word: "x\\\n+=~", text: nil, single: true},
		{word: "x+=\\\n~", text: nil, single: true},
		{word: `_x2=~`, text: nil, single: true},
		{word: `name9=~user`, text: nil, single: true},
		{word: "x=\\\n~", text: nil, single: true},
		{word: "x\\\n=~", text: nil, single: true},
		{word: `bad-name=~`, text: stringPointer(`bad-name=~`), single: true},
		{word: `2x=~`, text: stringPointer(`2x=~`), single: true},
		{word: `bad-name+=~`, text: stringPointer(`bad-name+=~`), single: true},
		{word: `2x+=~`, text: stringPointer(`2x+=~`), single: true},
		{word: `x++=~`, text: stringPointer(`x++=~`), single: true},
		{word: `x\+=~`, text: stringPointer(`x+=~`), single: true},
		{word: `x+\=~`, text: stringPointer(`x+=~`), single: true},
		{word: `x"+"=~`, text: stringPointer(`x+=~`), single: true},
		{word: `x+"="~`, text: stringPointer(`x+=~`), single: true},
		{word: `x\=~`, text: stringPointer(`x=~`), single: true},
		{word: `x"="~`, text: stringPointer(`x=~`), single: true},
		{word: `x=\~`, text: stringPointer(`x=~`), single: true},
		{word: `x=a:\~`, text: stringPointer(`x=a:~`), single: true},
		{word: `x="~"`, text: stringPointer(`x=~`), single: true},
		{word: `plain:~`, text: stringPointer(`plain:~`), single: true},
	}
	for _, test := range tests {
		t.Run(test.word, func(t *testing.T) {
			response, err := Certify(mustRequest(t, `echo `+test.word))
			if err != nil {
				t.Fatalf("Certify error = %v", err)
			}
			word := findCommandSite(t, response.Results[0], 0).Argv[1]
			if word.Single != test.single || (word.Text == nil) != (test.text == nil) || word.Text != nil && *word.Text != *test.text {
				t.Fatalf("word facts = %#v, want text %v and single=%t", word, test.text, test.single)
			}
		})
	}
}

func stringPointer(value string) *string { return &value }

func TestEmitAssignmentValueExpansionContexts(t *testing.T) {
	tests := []struct {
		source string
		known  bool
	}{
		{source: `A=*.go`, known: true},
		{source: `A=\*.go`, known: true},
		{source: `A={a,b}`, known: true},
		{source: `A=~`, known: false},
		{source: `A=/bin:~user`, known: false},
		{source: `A=$X`, known: false},
		{source: `A=$(printf x)`, known: false},
		{source: `A=$((1+1))`, known: false},
	}
	for _, test := range tests {
		t.Run(test.source, func(t *testing.T) {
			response, err := Certify(mustRequest(t, test.source))
			if err != nil {
				t.Fatalf("Certify error = %v", err)
			}
			assignment := findCommandSite(t, response.Results[0], 0).Assignments[0]
			if assignment.ValueKnown != test.known {
				t.Fatalf("assignment = %#v, want value_known=%t", assignment, test.known)
			}
		})
	}
}

func TestEmitAssignmentBraceValueIsExactLiteral(t *testing.T) {
	const src = `A={a,b}`
	statements, refusal := parseStatements(src)
	if refusal != nil {
		t.Fatalf("parse refusal = %#v", refusal)
	}
	sites, refusals, _ := walk(statements, src)
	if len(refusals) != 0 || len(sites) != 1 || len(sites[0].assignments) != 1 {
		t.Fatalf("walk = sites %#v, refusals %#v", sites, refusals)
	}
	value, known := literalWordInContext(sites[0].assignments[0].Value, src, assignmentExpansion)
	if !known || value != `{a,b}` {
		t.Fatalf("assignment value = %q, known=%t; want exact literal {a,b}", value, known)
	}
}

func TestEmitAssignmentExtGlobContexts(t *testing.T) {
	tests := []struct {
		source string
		text   string
		known  bool
	}{
		{source: `A=*(a|b)`, text: `*(a|b)`, known: true},
		{source: `A=@(a|b)`, text: `@(a|b)`, known: true},
		{source: `A=?(a|b)`, text: `?(a|b)`, known: true},
		{source: `A=+(a|b)`, text: `+(a|b)`, known: true},
		{source: `A=!(a|b)`, text: `!(a|b)`, known: true},
		{source: `A=*(a|@(b|c))`, text: `*(a|@(b|c))`, known: true},
		{source: `A=*(a|"b")`, text: `*(a|b)`, known: true},
		{source: `A=*(a|\|)`, text: `*(a||)`, known: true},
		{source: `A=*($X|b)`, known: false},
		{source: `A=*($(printf a)|b)`, known: false},
		{source: `A=*($((1+2))|b)`, known: false},
		{source: `A=*(<(printf a)|b)`, known: false},
		{source: `A=~/*(a|b)`, known: false},
	}
	for _, test := range tests {
		t.Run(test.source, func(t *testing.T) {
			statements, parseRefusal := parseStatements(test.source)
			if parseRefusal != nil {
				t.Fatalf("parse refusal = %#v", parseRefusal)
			}
			sites, refusals, _ := walk(statements, test.source)
			if len(refusals) != 0 || len(sites) != 1 || len(sites[0].assignments) != 1 {
				t.Fatalf("walk = sites %#v, refusals %#v", sites, refusals)
			}
			value, known := literalWordInContext(sites[0].assignments[0].Value, test.source, assignmentExpansion)
			if known != test.known || known && value != test.text {
				t.Fatalf("assignment value = %q, known=%t; want %q, known=%t", value, known, test.text, test.known)
			}
		})
	}
}

func TestEmitArgvExtGlobRemainsDynamicAndNonSingle(t *testing.T) {
	response, err := Certify(mustRequest(t, `echo @(a|b)`))
	if err != nil {
		t.Fatalf("Certify error = %v", err)
	}
	word := findCommandSite(t, response.Results[0], 0).Argv[1]
	if word.Text != nil || word.Single {
		t.Fatalf("argv extglob facts = %#v, want unknown text and cardinality", word)
	}
}

func TestEmitUnquotedExpansionFacts(t *testing.T) {
	tests := []struct {
		name   string
		word   string
		single bool
	}{
		{name: "parameter", word: `$X`, single: false},
		{name: "command substitution", word: `$(printf x)`, single: false},
		{name: "glob", word: `*.go`, single: false},
		{name: "brace", word: `{a,b}`, single: false},
		{name: "tilde", word: `~`, single: true},
		{name: "process substitution", word: `<(printf x)`, single: true},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			src := `echo ` + test.word
			response, err := Certify(mustRequest(t, src))
			if err != nil {
				t.Fatalf("Certify error = %v", err)
			}
			word := findCommandSite(t, response.Results[0], 0).Argv[1]
			if word.Text != nil || word.Single != test.single {
				t.Fatalf("word facts = %#v, want nil text and single=%t", word, test.single)
			}
		})
	}
}

func TestEmitQuotedAndUnquotedSplittingHazards(t *testing.T) {
	tests := []struct {
		word   string
		single bool
	}{
		{word: `$X`, single: false},
		{word: `"$X"`, single: true},
		{word: `"$@"`, single: false},
		{word: `"$*"`, single: true},
		{word: `${array[@]}`, single: false},
		{word: `"${array[@]}"`, single: false},
		{word: `"${array[*]}"`, single: true},
		{word: `"${array[0]}"`, single: true},
	}
	for _, test := range tests {
		t.Run(test.word, func(t *testing.T) {
			response, err := Certify(mustRequest(t, `echo `+test.word))
			if err != nil {
				t.Fatalf("Certify error = %v", err)
			}
			word := findCommandSite(t, response.Results[0], 0).Argv[1]
			if word.Text != nil || word.Single != test.single {
				t.Fatalf("word facts = %#v, want nil text and single=%t", word, test.single)
			}
		})
	}
}

func TestEmitQuotedParameterOperandAtIsNotSingle(t *testing.T) {
	response, err := Certify(mustRequest(t, `echo "${x:+$@}"`))
	if err != nil {
		t.Fatalf("Certify error = %v", err)
	}
	word := findCommandSite(t, response.Results[0], 0).Argv[1]
	if word.Text != nil || word.Single {
		t.Fatalf("quoted parameter operand = %#v, want unknown text and cardinality", word)
	}
}

func TestEmitQuotedIndirectParameterNeverOverclaimsSingle(t *testing.T) {
	tests := []struct {
		word   string
		single bool
	}{
		{word: `"${ref}"`, single: true},
		{word: `"${!scalar_ref}"`, single: false},
		{word: `"${!array_ref}"`, single: false},
		{word: `"prefix-${!ref}-suffix"`, single: false},
	}
	for _, test := range tests {
		t.Run(test.word, func(t *testing.T) {
			response, err := Certify(mustRequest(t, `echo `+test.word))
			if err != nil {
				t.Fatalf("Certify error = %v", err)
			}
			word := findCommandSite(t, response.Results[0], 0).Argv[1]
			if word.Text != nil || word.Single != test.single {
				t.Fatalf("indirect parameter facts = %#v, want nil text and single=%t", word, test.single)
			}
		})
	}
}

func TestEmitArithmeticExpansionIsSingleScalar(t *testing.T) {
	tests := []string{`$((1+2))`, `pre$((1+2))post`, `"$((1+2))"`}
	for _, raw := range tests {
		t.Run(raw, func(t *testing.T) {
			response, err := Certify(mustRequest(t, `echo `+raw))
			if err != nil {
				t.Fatalf("Certify error = %v", err)
			}
			word := findCommandSite(t, response.Results[0], 0).Argv[1]
			if word.Text != nil || !word.Single {
				t.Fatalf("arithmetic facts = %#v, want nil text and one field", word)
			}
		})
	}
}

func TestEmitArithmeticExpansionStillTraversesNestedExecution(t *testing.T) {
	const src = `echo $(( $(doc-lattice check) + 1 ))`
	response, err := Certify(mustRequest(t, src))
	if err != nil {
		t.Fatalf("Certify error = %v", err)
	}
	events := response.Results[0].Events
	if len(events) != 2 || events[0].Kind != "command_site" || events[1].Kind != "command_site" {
		t.Fatalf("events = %#v, want outer and nested command sites", events)
	}
	word := events[0].Argv[1]
	if word.Text != nil || !word.Single {
		t.Fatalf("outer arithmetic facts = %#v, want nil text and one field", word)
	}
}

func TestEmitQuotedNestedParameterCardinality(t *testing.T) {
	tests := []struct {
		word   string
		single bool
	}{
		{word: `"${x:+$@}"`, single: false},
		{word: `"${x:+${array[@]}}"`, single: false},
		{word: `"${x/a/$@}"`, single: false},
		{word: `"${x:+${!prefix@}}"`, single: false},
		{word: `"${x:+$*}"`, single: true},
		{word: `"${x:+${array[*]}}"`, single: true},
		{word: `"${x:+$(printf x)}"`, single: true},
	}
	for _, test := range tests {
		t.Run(test.word, func(t *testing.T) {
			response, err := Certify(mustRequest(t, `echo `+test.word))
			if err != nil {
				t.Fatalf("Certify error = %v", err)
			}
			word := findCommandSite(t, response.Results[0], 0).Argv[1]
			if word.Text != nil || word.Single != test.single {
				t.Fatalf("word facts = %#v, want nil text and single=%t", word, test.single)
			}
		})
	}
}

func TestEmitEscapedAndQuotedLiteralMetacharacters(t *testing.T) {
	tests := []struct {
		word string
		text string
	}{
		{word: `\*.go`, text: `*.go`},
		{word: `\~`, text: `~`},
		{word: `\{a,b\}`, text: `{a,b}`},
		{word: `"*.go"`, text: `*.go`},
		{word: `do"c-"lattice`, text: `doc-lattice`},
	}
	for _, test := range tests {
		t.Run(test.word, func(t *testing.T) {
			response, err := Certify(mustRequest(t, `echo `+test.word))
			if err != nil {
				t.Fatalf("Certify error = %v", err)
			}
			word := findCommandSite(t, response.Results[0], 0).Argv[1]
			if word.Text == nil || *word.Text != test.text || !word.Single {
				t.Fatalf("word facts = %#v, want text %q and single", word, test.text)
			}
		})
	}
}

func TestEmitCrossPartGlobMatrix(t *testing.T) {
	tests := []struct {
		word   string
		text   *string
		single bool
	}{
		{word: `["a"]`, text: nil, single: false},
		{word: `a["b"]c`, text: nil, single: false},
		{word: `[a"b"]`, text: nil, single: false},
		{word: `[\]]`, text: nil, single: false},
		{word: `[!a]`, text: nil, single: false},
		{word: `[!]]`, text: nil, single: false},
		{word: "[\\\n\"a\"]", text: nil, single: false},
		{word: `a"b"*`, text: nil, single: false},
		{word: `a"b"?`, text: nil, single: false},
		{word: `"[a]"`, text: stringPointer(`[a]`), single: true},
		{word: `\["a"]`, text: stringPointer(`[a]`), single: true},
		{word: `["a"\]`, text: stringPointer(`[a]`), single: true},
		{word: `["a"`, text: stringPointer(`[a`), single: true},
		{word: `"["a]`, text: stringPointer(`[a]`), single: true},
		{word: `[a"]"`, text: stringPointer(`[a]`), single: true},
		{word: `[]`, text: nil, single: false},
		{word: `[!]`, text: nil, single: false},
		{word: `[^]`, text: nil, single: false},
		{word: `[""]`, text: nil, single: false},
		{word: `['']`, text: nil, single: false},
		{word: `[!`, text: stringPointer(`[!`), single: true},
		{word: `[^`, text: stringPointer(`[^`), single: true},
		{word: `a"*"b`, text: stringPointer(`a*b`), single: true},
		{word: `a\?b`, text: stringPointer(`a?b`), single: true},
	}
	for _, test := range tests {
		t.Run(test.word, func(t *testing.T) {
			response, err := Certify(mustRequest(t, `echo `+test.word))
			if err != nil {
				t.Fatalf("Certify error = %v", err)
			}
			word := findCommandSite(t, response.Results[0], 0).Argv[1]
			if word.Single != test.single || (word.Text == nil) != (test.text == nil) || word.Text != nil && *word.Text != *test.text {
				t.Fatalf("word facts = %#v, want text %v and single=%t", word, test.text, test.single)
			}
		})
	}
}

func TestEmitGlobLookalikeCorpusDoesNotPanic(t *testing.T) {
	words := []string{`[`, `]`, `[]`, `[!]`, `[^]`, `[!`, `[^`, `[[`, `]]`, `[a`, `a]`, `["a"]`, `[""]`, `['']`, `\[\]`, `[\]]`, `a"*"b`, `a\?b`}
	for _, raw := range words {
		t.Run(raw, func(t *testing.T) {
			response, err := Certify(mustRequest(t, `echo `+raw))
			if err != nil {
				t.Fatalf("Certify valid source error = %v", err)
			}
			events := response.Results[0].Events
			if len(events) != 1 || events[0].Kind != "command_site" {
				t.Fatalf("events = %#v, want one stable command site", events)
			}
		})
	}
}

func TestEmitIncompleteBraceSequenceIsLiteral(t *testing.T) {
	response, err := Certify(mustRequest(t, `echo {a..}`))
	if err != nil {
		t.Fatalf("Certify error = %v", err)
	}
	word := findCommandSite(t, response.Results[0], 0).Argv[1]
	if word.Text == nil || *word.Text != "{a..}" || !word.Single {
		t.Fatalf("incomplete brace sequence = %#v, want known literal {a..} and single", word)
	}
}

func TestEmitOverlappingBraceSeparatorsAreLiteralWithoutPanic(t *testing.T) {
	response, err := Certify(mustRequest(t, `echo {1...3}`))
	if err != nil {
		t.Fatalf("Certify error = %v", err)
	}
	word := findCommandSite(t, response.Results[0], 0).Argv[1]
	if word.Text == nil || *word.Text != "{1...3}" || !word.Single {
		t.Fatalf("overlapping brace separators = %#v, want known literal {1...3} and single", word)
	}
}

func TestEmitBraceExpansionMatrix(t *testing.T) {
	tests := []struct {
		word   string
		text   *string
		single bool
	}{
		{word: `{a,b}`, text: nil, single: false},
		{word: `{a,'b'}`, text: nil, single: false},
		{word: `{a..z}`, text: nil, single: false},
		{word: `{1..3}`, text: nil, single: false},
		{word: `{1..3..2}`, text: nil, single: false},
		{word: "{1..\\\n3}", text: nil, single: false},
		{word: `{a..}`, text: stringPointer(`{a..}`), single: true},
		{word: `{..b}`, text: stringPointer(`{..b}`), single: true},
		{word: `{a..1}`, text: stringPointer(`{a..1}`), single: true},
		{word: `{1..3..x}`, text: stringPointer(`{1..3..x}`), single: true},
		{word: `{1...3}`, text: stringPointer(`{1...3}`), single: true},
		{word: `{1....3}`, text: stringPointer(`{1....3}`), single: true},
		{word: `{1..3..2..4}`, text: stringPointer(`{1..3..2..4}`), single: true},
		{word: `{1..3..}`, text: stringPointer(`{1..3..}`), single: true},
		{word: `x{1...3}y`, text: stringPointer(`x{1...3}y`), single: true},
		{word: `{1...3}{4..6}`, text: nil, single: false},
		{word: `{a..\c}`, text: stringPointer(`{a..c}`), single: true},
		{word: `{a.."c"}`, text: stringPointer(`{a..c}`), single: true},
		{word: `{a}`, text: stringPointer(`{a}`), single: true},
		{word: `\{a,b\}`, text: stringPointer(`{a,b}`), single: true},
		{word: `"{a,b}"`, text: stringPointer(`{a,b}`), single: true},
	}
	for _, test := range tests {
		t.Run(test.word, func(t *testing.T) {
			response, err := Certify(mustRequest(t, `echo `+test.word))
			if err != nil {
				t.Fatalf("Certify error = %v", err)
			}
			word := findCommandSite(t, response.Results[0], 0).Argv[1]
			if word.Single != test.single || (word.Text == nil) != (test.text == nil) || word.Text != nil && *word.Text != *test.text {
				t.Fatalf("word facts = %#v, want text %v and single=%t", word, test.text, test.single)
			}
		})
	}
}

func TestEmitBraceAndTildeLookalikeCorpusDoesNotPanic(t *testing.T) {
	words := []string{
		`{1...3}`,
		`x{1....3}y`,
		`{1..2..3..4}`,
		`{1..}`,
		`{..3}`,
		`{...}`,
		`{{{1...3}}}`,
		"{1..\\\n3}",
		`bad-name=~`,
		"x=\\\n~",
		`x\=~`,
	}
	for _, raw := range words {
		t.Run(raw, func(t *testing.T) {
			response, err := Certify(mustRequest(t, `echo `+raw))
			if err != nil {
				t.Fatalf("Certify valid source error = %v", err)
			}
			events := response.Results[0].Events
			if len(events) != 1 || events[0].Kind != "command_site" {
				t.Fatalf("events = %#v, want one stable command site", events)
			}
		})
	}
}

func TestEmitANSIQuotedLiteral(t *testing.T) {
	response, err := Certify(mustRequest(t, `echo $'doc\x2dlattice'`))
	if err != nil {
		t.Fatalf("Certify error = %v", err)
	}
	word := findCommandSite(t, response.Results[0], 0).Argv[1]
	if word.Text == nil || *word.Text != "doc-lattice" || !word.Single {
		t.Fatalf("ANSI-quoted word = %#v, want known doc-lattice and single", word)
	}
}

func TestEmitLocaleQuotedLiteralIsNotEnvironmentIndependent(t *testing.T) {
	response, err := Certify(mustRequest(t, `echo $"doc-lattice"`))
	if err != nil {
		t.Fatalf("Certify error = %v", err)
	}
	word := findCommandSite(t, response.Results[0], 0).Argv[1]
	if word.Text != nil || !word.Single {
		t.Fatalf("locale-quoted word = %#v, want unknown text and single", word)
	}
}

func TestCertifyRetainsTerminalParseRefusalAfterSites(t *testing.T) {
	const src = `doc-lattice check; echo "$(`
	response, err := Certify(mustRequest(t, src))
	if err != nil {
		t.Fatalf("Certify error = %v", err)
	}
	events := response.Results[0].Events
	if len(events) != 2 {
		t.Fatalf("events = %#v, want command site and terminal refusal", events)
	}
	if events[0].Kind != "command_site" || events[0].Ordinal != 0 {
		t.Fatalf("first event = %#v, want command site ordinal 0", events[0])
	}
	if got := events[1]; got.Kind != "refusal" || got.Code != "syntax-error" || got.StartByte != 25 || got.EndByte != 25 {
		t.Fatalf("terminal event = %#v, want current raw syntax-error [25, 25)", got)
	}
}

func TestCertifyNestedSitesKeepWalkerOrdinalsInSourceOrder(t *testing.T) {
	const src = `echo "$(doc-lattice lint)"; doc-lattice check`
	response, err := Certify(mustRequest(t, src))
	if err != nil {
		t.Fatalf("Certify error = %v", err)
	}
	events := response.Results[0].Events
	if len(events) != 3 {
		t.Fatalf("events = %#v, want three command sites", events)
	}
	wantStarts := []int{0, 8, 28}
	for index, event := range events {
		if event.Kind != "command_site" || event.Ordinal != index || event.StartByte != wantStarts[index] {
			t.Errorf("event %d = %#v, want command ordinal %d at %d", index, event, index, wantStarts[index])
		}
	}
}

func TestCertifyParameterOperandProcessSubstitutionEndsInRefusal(t *testing.T) {
	const src = `echo ${x:+<(doc-lattice check)}`
	response, err := Certify(mustRequest(t, src))
	if err != nil {
		t.Fatalf("Certify error = %v", err)
	}
	events := response.Results[0].Events
	if len(events) != 2 || events[0].Kind != "command_site" || events[0].Ordinal != 0 {
		t.Fatalf("events = %#v, want outer command site then refusal", events)
	}
	wantStart, wantEnd := len(`echo ${x:+`), len(src)-1
	if got := events[1]; got.Kind != "refusal" || got.Code != "unsupported-construct" || got.StartByte != wantStart || got.EndByte != wantEnd {
		t.Fatalf("terminal event = %#v, want unsupported process-substitution span [%d, %d)", got, wantStart, wantEnd)
	}
}

func TestCertifyReplacementProcessSubstitutionStopsBeforeLaterSite(t *testing.T) {
	const src = `echo ${x/a/<(doc-lattice check)}; doc-lattice later`
	response, err := Certify(mustRequest(t, src))
	if err != nil {
		t.Fatalf("Certify error = %v", err)
	}
	result := response.Results[0]
	if len(result.Events) != 2 || result.Events[0].Kind != "command_site" || result.Events[0].Ordinal != 0 {
		t.Fatalf("events = %#v, want outer command site then terminal refusal only", result.Events)
	}
	wantStart := len(`echo ${x/a/`)
	wantEnd := wantStart + len(`<(doc-lattice check)`)
	if got := result.Events[1]; got.Kind != "refusal" || got.Code != "unsupported-construct" || got.StartByte != wantStart || got.EndByte != wantEnd {
		t.Fatalf("terminal event = %#v, want raw replacement refusal [%d, %d)", got, wantStart, wantEnd)
	}
	if result.WorkUnits != 20 {
		t.Fatalf("work_units = %d, want current raw Task 5 accounting 20", result.WorkUnits)
	}
}

func TestEmitRejectsMalformedUpstreamFacts(t *testing.T) {
	positionedWord := func(start, end uint) *syntax.Word {
		return &syntax.Word{Parts: []syntax.WordPart{&syntax.Lit{
			ValuePos: syntax.NewPos(start, 1, start+1),
			ValueEnd: syntax.NewPos(end, 1, end+1),
			Value:    "x",
		}}}
	}
	validCall := &syntax.CallExpr{Args: []*syntax.Word{positionedWord(0, 1)}}
	invalidWord := &syntax.Word{Parts: []syntax.WordPart{&syntax.Lit{Value: "x"}}}
	tests := []struct {
		name string
		site commandSite
		src  string
	}{
		{name: "missing call", site: commandSite{}, src: "x"},
		{name: "missing call word parts", site: commandSite{call: &syntax.CallExpr{Args: []*syntax.Word{{}}}}, src: "x"},
		{name: "invalid site position", site: commandSite{call: &syntax.CallExpr{Args: []*syntax.Word{invalidWord}}, argv: []*syntax.Word{invalidWord}}, src: ""},
		{name: "reversed site position", site: commandSite{call: &syntax.CallExpr{Args: []*syntax.Word{positionedWord(2, 1)}}, argv: []*syntax.Word{positionedWord(2, 1)}}, src: "xx"},
		{name: "site beyond source", site: commandSite{call: &syntax.CallExpr{Args: []*syntax.Word{positionedWord(0, 2)}}, argv: []*syntax.Word{positionedWord(0, 2)}}, src: "x"},
		{name: "missing assignment name", site: commandSite{call: validCall, assignments: []*syntax.Assign{{Value: positionedWord(0, 1)}}}, src: "x"},
		{name: "empty assignment name", site: commandSite{call: validCall, assignments: []*syntax.Assign{{Name: &syntax.Lit{ValuePos: syntax.NewPos(0, 1, 1), ValueEnd: syntax.NewPos(1, 1, 2)}}}}, src: "xx"},
		{name: "invalid assignment position", site: commandSite{call: validCall, assignments: []*syntax.Assign{{Name: &syntax.Lit{Value: "X"}}}}, src: "x"},
		{name: "reversed assignment position", site: commandSite{call: validCall, assignments: []*syntax.Assign{{Name: &syntax.Lit{ValuePos: syntax.NewPos(1, 1, 2), ValueEnd: syntax.NewPos(2, 1, 3), Value: "X"}, Value: positionedWord(0, 0)}}}, src: "xx"},
		{name: "assignment beyond source", site: commandSite{call: validCall, assignments: []*syntax.Assign{{Name: &syntax.Lit{ValuePos: syntax.NewPos(0, 1, 1), ValueEnd: syntax.NewPos(2, 1, 3), Value: "X"}}}}, src: "xx"},
		{name: "missing word parts", site: commandSite{call: validCall, argv: []*syntax.Word{{}}}, src: "x"},
		{name: "invalid word position", site: commandSite{call: validCall, argv: []*syntax.Word{invalidWord}}, src: "x"},
		{name: "reversed word position", site: commandSite{call: validCall, argv: []*syntax.Word{positionedWord(2, 1)}}, src: "xx"},
		{name: "word beyond source", site: commandSite{call: validCall, argv: []*syntax.Word{positionedWord(0, 2)}}, src: "x"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			_, err := emitCommandSite(test.site, 0, test.src)
			if err == nil || err.Error() != "invalid syntax facts" {
				t.Fatalf("emitCommandSite error = %v, want stable invalid syntax facts", err)
			}
		})
	}
}

func TestEmitRejectsOversizedSiteArraysBeforeMaterialization(t *testing.T) {
	word := &syntax.Word{Parts: []syntax.WordPart{&syntax.Lit{
		ValuePos: syntax.NewPos(0, 1, 1),
		ValueEnd: syntax.NewPos(1, 1, 2),
		Value:    "x",
	}}}
	call := &syntax.CallExpr{Args: []*syntax.Word{word}}
	tests := []commandSite{
		{call: call, argv: make([]*syntax.Word, maxArgvWordsPerSite+1)},
		{call: call, assignments: make([]*syntax.Assign, maxAssignmentsPerSite+1)},
	}
	for index, site := range tests {
		if _, err := emitCommandSite(site, 0, "x"); err != errInvalidEmission {
			t.Errorf("oversized case %d error = %v, want stable invalid emission", index, err)
		}
	}
}

func TestCertifyPreservesMultiSourceOrderWorkAndEmptyArrays(t *testing.T) {
	request := mustRequest(t, "", `doc-lattice check`, " \t\n")
	response, err := Certify(request)
	if err != nil {
		t.Fatalf("Certify error = %v", err)
	}
	if len(response.Results) != len(request.Sources) {
		t.Fatalf("results = %#v, want %d", response.Results, len(request.Sources))
	}
	for index, result := range response.Results {
		if result.ID != index {
			t.Errorf("result %d id = %d, want %d", index, result.ID, index)
		}
		if result.WorkUnits <= 0 {
			t.Errorf("result %d work = %d, want positive", index, result.WorkUnits)
		}
		if result.Events == nil {
			t.Errorf("result %d events are nil, want a required array", index)
		}
	}
	if len(response.Results[0].Events) != 0 || len(response.Results[2].Events) != 0 {
		t.Fatalf("empty source events = (%#v, %#v), want empty arrays", response.Results[0].Events, response.Results[2].Events)
	}
	if site := findCommandSite(t, response.Results[1], 0); site.StartByte != 0 || site.EndByte != 17 {
		t.Fatalf("middle result site = %#v, want [0, 17)", site)
	}
}

func TestEncodeResponseRejectsKnownNonSingleWord(t *testing.T) {
	text := "known"
	response := &Response{Results: []Result{{Events: []Event{{
		Kind: "command_site",
		Argv: []Word{{Text: &text, Single: false}},
	}}}}}
	if _, err := EncodeResponse(response); err == nil {
		t.Fatal("EncodeResponse accepted text != nil with single=false")
	}
}
