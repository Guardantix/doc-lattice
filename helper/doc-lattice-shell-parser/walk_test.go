// Package main tests context-carrying shell syntax traversal.
package main

import (
	"fmt"
	"strings"
	"testing"

	"mvdan.cc/sh/v3/syntax"
)

func TestWalkCertifiesSimpleCommand(t *testing.T) {
	const src = `doc-lattice check`
	stmts, refusal := parseStatements(src)
	if refusal != nil {
		t.Fatalf("parseStatements refusal = %#v, want none", refusal)
	}

	sites, refusals, _ := walk(stmts, src)
	if len(sites) != 1 || len(refusals) != 0 {
		t.Fatalf("walk returned %d sites and %d refusals, want 1 and 0", len(sites), len(refusals))
	}
	if len(sites[0].argv) != 2 {
		t.Fatalf("walk site argv length = %d, want 2", len(sites[0].argv))
	}
}

func TestWalkRefusesUnmodeledConstruct(t *testing.T) {
	const src = `cat <(doc-lattice check)`
	stmts, refusal := parseStatements(src)
	if refusal != nil {
		t.Fatalf("parseStatements refusal = %#v, want none", refusal)
	}

	sites, refusals, _ := walk(stmts, src)
	if len(sites) != 1 || len(refusals) != 1 || refusals[0].code != "unsupported-construct" {
		t.Fatalf("sites, refusals = (%d, %#v), want outer site then one ProcSubst refusal", len(sites), refusals)
	}
}

func TestWalkTraversesCommandSubstInArgv(t *testing.T) {
	const src = `echo "$(doc-lattice lint)"`
	stmts, refusal := parseStatements(src)
	if refusal != nil {
		t.Fatalf("parseStatements refusal = %#v, want none", refusal)
	}

	sites, refusals, _ := walk(stmts, src)
	if len(refusals) != 0 || len(sites) != 2 {
		t.Fatalf("walk returned %d sites and refusals %#v, want 2 and none", len(sites), refusals)
	}
}

func TestWalkTraversesCommandSubstInsideParameterOperand(t *testing.T) {
	const src = `echo "${x:+$(doc-lattice check)}"`
	stmts, refusal := parseStatements(src)
	if refusal != nil {
		t.Fatalf("parseStatements refusal = %#v, want none", refusal)
	}

	sites, refusals, _ := walk(stmts, src)
	if len(refusals) != 0 || len(sites) != 2 {
		t.Fatalf("walk returned %d sites and refusals %#v, want outer and nested sites only", len(sites), refusals)
	}
}

func TestWalkRefusesProcessSubstInsideParameterOperand(t *testing.T) {
	const src = `echo ${x:+<(doc-lattice check)}`
	stmts, refusal := parseStatements(src)
	if refusal != nil {
		t.Fatalf("parseStatements refusal = %#v, want none", refusal)
	}

	sites, refusals, _ := walk(stmts, src)
	if len(sites) != 1 || len(refusals) != 1 || refusals[0].code != "unsupported-construct" {
		var debug strings.Builder
		if len(stmts) > 0 {
			_ = syntax.DebugPrint(&debug, stmts[0])
		}
		t.Fatalf("walk returned %d sites and refusals %#v, want outer site and one ProcSubst refusal; AST:\n%s", len(sites), refusals, debug.String())
	}
	wantStart, wantEnd := strings.Index(src, "<("), strings.LastIndex(src, "}")
	if refusals[0].startByte != wantStart || refusals[0].endByte != wantEnd {
		t.Fatalf("refusal = %#v, want table-owned literal span [%d, %d)", refusals[0], wantStart, wantEnd)
	}
}

func TestWalkParameterOperandProcessSubstitutionContexts(t *testing.T) {
	tests := []struct {
		name   string
		source string
		refuse bool
	}{
		{name: "input", source: `echo ${x:+<(doc-lattice check)}`, refuse: true},
		{name: "output", source: `echo ${x:+>(doc-lattice check)}`, refuse: true},
		{name: "continued opener", source: "echo ${x:+<\\\n(doc-lattice check)}", refuse: true},
		{name: "escaped redirect", source: `echo ${x:+\<(doc-lattice check)}`},
		{name: "escaped paren", source: `echo ${x:+<\(doc-lattice check)}`},
		{name: "quoted redirect", source: `echo ${x:+"<"(doc-lattice check)}`},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			stmts, parseRefusal := parseStatements(test.source)
			if parseRefusal != nil {
				t.Fatalf("parseStatements refusal = %#v, want none", parseRefusal)
			}
			sites, refusals, _ := walk(stmts, test.source)
			if len(sites) != 1 || len(refusals) != btoi(test.refuse) {
				t.Fatalf("walk returned %d sites and refusals %#v, want outer site and refuse=%t", len(sites), refusals, test.refuse)
			}
			if test.refuse && refusals[0].code != "unsupported-construct" {
				t.Fatalf("refusal = %#v, want unsupported-construct", refusals[0])
			}
		})
	}
}

func TestWalkRefusesReplacementProcessSubstitutionBeforeLaterCommand(t *testing.T) {
	const src = `echo ${x/a/<(doc-lattice check)}; doc-lattice later`
	stmts, parseRefusal := parseStatements(src)
	if parseRefusal != nil {
		t.Fatalf("parseStatements refusal = %#v, want none", parseRefusal)
	}
	sites, refusals, work := walk(stmts, src)
	if len(sites) != 1 || len(refusals) != 1 || refusals[0].code != "unsupported-construct" {
		var debug strings.Builder
		for _, stmt := range stmts {
			_ = syntax.DebugPrint(&debug, stmt)
		}
		t.Fatalf("walk returned %d sites, refusals %#v, work %d; want outer site, terminal replacement refusal, and no later site; AST:\n%s", len(sites), refusals, work, debug.String())
	}
	wantStart := strings.Index(src, "<(")
	wantEnd := strings.Index(src, ")}") + 1
	if refusals[0].startByte != wantStart || refusals[0].endByte != wantEnd {
		t.Fatalf("refusal = %#v, want current raw replacement span [%d, %d)", refusals[0], wantStart, wantEnd)
	}
	if work != 19 {
		t.Fatalf("work = %d, want current raw Task 5 accounting 19", work)
	}

	const escaped = `echo ${x/a/\<(doc-lattice check)}; doc-lattice later`
	escapedStmts, escapedParseRefusal := parseStatements(escaped)
	if escapedParseRefusal != nil {
		t.Fatalf("escaped parse refusal = %#v, want none", escapedParseRefusal)
	}
	escapedSites, escapedRefusals, escapedWork := walk(escapedStmts, escaped)
	if len(escapedSites) != 2 || len(escapedRefusals) != 0 || escapedWork != work {
		t.Fatalf("escaped walk = %d sites, refusals %#v, work %d; want two sites, no refusal, and stable work %d", len(escapedSites), escapedRefusals, escapedWork, work)
	}
}

func TestWalkRefusesCollapsedProcessSubstitutionInParameterWordFields(t *testing.T) {
	tests := []struct {
		name   string
		source string
	}{
		{name: "expansion word", source: `echo ${x#<(doc-lattice check)}`},
		{name: "replacement original", source: `echo ${x/<(doc-lattice check)/safe}`},
		{name: "replacement with", source: `echo ${x/safe/<(doc-lattice check)}`},
		{name: "replacement output", source: `echo ${x/safe/>(doc-lattice check)}`},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			stmts, parseRefusal := parseStatements(test.source)
			if parseRefusal != nil {
				t.Fatalf("parseStatements refusal = %#v, want none", parseRefusal)
			}
			sites, refusals, _ := walk(stmts, test.source)
			if len(sites) != 1 || len(refusals) != 1 || refusals[0].code != "unsupported-construct" {
				var debug strings.Builder
				_ = syntax.DebugPrint(&debug, stmts[0])
				t.Fatalf("walk returned %d sites and refusals %#v, want outer site and terminal refusal; AST:\n%s", len(sites), refusals, debug.String())
			}
		})
	}
}

func TestWalkParameterWordProcessSubstitutionLookalikes(t *testing.T) {
	tests := []struct {
		name   string
		source string
	}{
		{name: "replacement escaped redirect", source: `echo ${x/safe/\<(doc-lattice check)}`},
		{name: "replacement escaped paren", source: `echo ${x/safe/<\(doc-lattice check)}`},
		{name: "replacement quoted redirect", source: `echo ${x/safe/"<"(doc-lattice check)}`},
		{name: "original escaped redirect", source: `echo ${x/\<(doc-lattice check)/safe}`},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			stmts, parseRefusal := parseStatements(test.source)
			if parseRefusal != nil {
				t.Fatalf("parseStatements refusal = %#v, want none", parseRefusal)
			}
			sites, refusals, _ := walk(stmts, test.source)
			if len(sites) != 1 || len(refusals) != 0 {
				t.Fatalf("walk returned %d sites and refusals %#v, want clean outer site", len(sites), refusals)
			}
		})
	}
}

func TestWalkOuterQuotedParameterProcessSubstitutionIsLiteral(t *testing.T) {
	tests := []struct {
		name   string
		source string
	}{
		{name: "expansion", source: `echo "${x:+<(doc-lattice check)}"; echo later`},
		{name: "replacement original", source: `echo "${x/<(doc-lattice check)/safe}"; echo later`},
		{name: "replacement with", source: `echo "${x/safe/<(doc-lattice check)}"; echo later`},
		{name: "continued expansion", source: "echo \"${x:+<\\\n(doc-lattice check)}\"; echo later"},
		{name: "escaped replacement", source: `echo "${x/safe/\<(doc-lattice check)}"; echo later`},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			stmts, parseRefusal := parseStatements(test.source)
			if parseRefusal != nil {
				t.Fatalf("parseStatements refusal = %#v, want none", parseRefusal)
			}
			sites, refusals, _ := walk(stmts, test.source)
			if len(sites) != 2 || len(refusals) != 0 {
				t.Fatalf("walk returned %d sites and refusals %#v, want quoted literal outer site and later site", len(sites), refusals)
			}
		})
	}
}

func TestParseRejectsProcessSubstitutionInParameterIndex(t *testing.T) {
	const src = `echo ${x[<(doc-lattice check)]}`
	_, refusal := parseStatements(src)
	if refusal == nil || refusal.code != "syntax-error" {
		t.Fatalf("parse refusal = %#v, want pinned parser syntax-error before walking arithmetic index", refusal)
	}
}

func TestWalkTraversesCommandSubstitutionInReplacementWord(t *testing.T) {
	const src = `echo ${x/safe/$(doc-lattice check)}`
	stmts, parseRefusal := parseStatements(src)
	if parseRefusal != nil {
		t.Fatalf("parseStatements refusal = %#v, want none", parseRefusal)
	}
	sites, refusals, _ := walk(stmts, src)
	if len(sites) != 2 || len(refusals) != 0 {
		t.Fatalf("walk returned %d sites and refusals %#v, want outer and nested command sites", len(sites), refusals)
	}
}

func btoi(value bool) int {
	if value {
		return 1
	}
	return 0
}

func TestWalkAcceptsParsedEmptyCommandSubstitution(t *testing.T) {
	const src = `echo "$()"`
	stmts, refusal := parseStatements(src)
	if refusal != nil {
		t.Fatalf("parseStatements refusal = %#v, want none", refusal)
	}

	sites, refusals, _ := walk(stmts, src)
	if len(sites) != 1 || len(refusals) != 0 {
		t.Fatalf("walk returned %d sites and %d refusals, want outer site and no refusal", len(sites), len(refusals))
	}
}

func TestWalkTraversesCommandSubstInAssignmentValue(t *testing.T) {
	const src = `value=$(doc-lattice check) echo ok`
	stmts, refusal := parseStatements(src)
	if refusal != nil {
		t.Fatalf("parseStatements refusal = %#v, want none", refusal)
	}

	sites, refusals, _ := walk(stmts, src)
	if len(refusals) != 0 || len(sites) != 2 {
		t.Fatalf("walk returned %d sites and refusals %#v, want 2 and none", len(sites), refusals)
	}
}

func TestWalkTraversesCompoundStatements(t *testing.T) {
	tests := []struct {
		name      string
		src       string
		wantSites int
	}{
		{name: "binary block and subshell", src: `{ echo left; } && (echo right)`, wantSites: 2},
		{name: "if else", src: `if echo cond; then echo yes; else echo no; fi`, wantSites: 3},
		{name: "while", src: `while echo cond; do echo body; done`, wantSites: 2},
		{name: "function", src: `f() { echo body; }`, wantSites: 1},
		{name: "for word", src: `for x in "$(echo item)"; do echo body; done`, wantSites: 2},
		{name: "case", src: `case "$(echo selector)" in x) echo body;; esac`, wantSites: 2},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			stmts, refusal := parseStatements(test.src)
			if refusal != nil {
				t.Fatalf("parseStatements refusal = %#v, want none", refusal)
			}
			sites, refusals, _ := walk(stmts, test.src)
			if len(sites) != test.wantSites || len(refusals) != 0 {
				t.Fatalf("walk returned %d sites and %d refusals, want %d and 0", len(sites), len(refusals), test.wantSites)
			}
		})
	}
}

func TestWalkTraversesRedirectExpansions(t *testing.T) {
	tests := []struct {
		name      string
		src       string
		wantSites int
	}{
		{name: "target", src: `echo outer >"$(echo target)"`, wantSites: 2},
		{name: "unquoted heredoc", src: "cat <<EOF\n$(echo body)\nEOF\n", wantSites: 2},
		{name: "quoted heredoc", src: "cat <<'EOF'\n$(echo inert)\nEOF\n", wantSites: 1},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			stmts, refusal := parseStatements(test.src)
			if refusal != nil {
				t.Fatalf("parseStatements refusal = %#v, want none", refusal)
			}
			sites, refusals, _ := walk(stmts, test.src)
			if len(sites) != test.wantSites || len(refusals) != 0 {
				t.Fatalf("walk returned %d sites and %d refusals, want %d and 0", len(sites), len(refusals), test.wantSites)
			}
		})
	}
}

func TestWalkOrdersLeadingRedirectBeforeCommand(t *testing.T) {
	const src = `>"$(nested)" outer`
	stmts, refusal := parseStatements(src)
	if refusal != nil {
		t.Fatalf("parseStatements refusal = %#v, want none", refusal)
	}
	sites, refusals, _ := walk(stmts, src)
	if len(sites) != 2 || len(refusals) != 0 {
		t.Fatalf("walk returned %d sites and %d refusals, want 2 and 0", len(sites), len(refusals))
	}
	if got := sites[0].argv[0].Lit(); got != "nested" {
		t.Fatalf("first site head = %q, want nested", got)
	}
}

func TestWalkTreatsEscapedHeredocDelimiterAsQuoted(t *testing.T) {
	const src = "cat <<\\EOF\n$(echo inert)\nEOF\n"
	stmts, refusal := parseStatements(src)
	if refusal != nil {
		t.Fatalf("parseStatements refusal = %#v, want none", refusal)
	}
	redirect := stmts[0].Redirs[0]
	if role := redirectRole(redirect); role != "quoted-heredoc-body" {
		t.Fatalf("redirectRole = %q, want quoted-heredoc-body", role)
	}
	sites, refusals, _ := walk(stmts, src)
	if len(sites) != 1 || len(refusals) != 0 {
		t.Fatalf("walk returned %d sites and %d refusals, want 1 and 0", len(sites), len(refusals))
	}
}

func TestWalkRefusesAssignmentIndexAndArray(t *testing.T) {
	name := func() *syntax.Lit {
		return &syntax.Lit{
			ValuePos: syntax.NewPos(0, 1, 1), ValueEnd: syntax.NewPos(1, 1, 2), Value: "x",
		}
	}
	tests := []struct {
		name   string
		assign *syntax.Assign
	}{
		{
			name: "index",
			assign: &syntax.Assign{Name: name(), Index: &syntax.Word{Parts: []syntax.WordPart{&syntax.Lit{
				ValuePos: syntax.NewPos(0, 1, 1), ValueEnd: syntax.NewPos(1, 1, 2), Value: "0",
			}}}},
		},
		{
			name: "array",
			assign: &syntax.Assign{Name: name(), Array: &syntax.ArrayExpr{
				Lparen: syntax.NewPos(0, 1, 1), Rparen: syntax.NewPos(1, 1, 2),
			}},
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			w := newWalker("x")
			w.dispatch(test.assign, "value", 1)
			if len(w.refusals) == 0 || w.refusals[0].code != "unsupported-construct" {
				t.Errorf("assignment refusals = %#v, want unsupported-construct", w.refusals)
			}
		})
	}
}

func TestWalkAppliesWildcardOnlyOutsideTraverseContainers(t *testing.T) {
	tests := []struct {
		source      string
		wantRefusal bool
	}{
		{source: `((1 + 2))`, wantRefusal: true},
		{source: `echo "$value"`, wantRefusal: false},
	}
	for _, test := range tests {
		stmts, refusal := parseStatements(test.source)
		if refusal != nil {
			t.Fatalf("parseStatements(%q) refusal = %#v, want none", test.source, refusal)
		}
		_, refusals, _ := walk(stmts, test.source)
		if test.wantRefusal && (len(refusals) == 0 || refusals[0].code != "unsupported-construct") {
			t.Errorf("walk(%q) refusals = %#v, want unsupported-construct", test.source, refusals)
		}
		if !test.wantRefusal && len(refusals) != 0 {
			t.Errorf("walk(%q) refusals = %#v, want none for an in-container word fact", test.source, refusals)
		}
	}
}

func TestWalkTerminalRefusalSuppressesLaterSiblings(t *testing.T) {
	const src = `((1)); echo after`
	stmts, refusal := parseStatements(src)
	if refusal != nil {
		t.Fatalf("parseStatements refusal = %#v, want none", refusal)
	}
	sites, refusals, _ := walk(stmts, src)
	if len(sites) != 0 {
		t.Fatalf("sites = %d, want no site after terminal refusal", len(sites))
	}
	if len(refusals) != 1 || refusals[0].code != "unsupported-construct" {
		t.Fatalf("refusals = %#v, want one unsupported-construct", refusals)
	}
}

func TestWalkTerminalRefusalCapPrecedenceIsSingleEvent(t *testing.T) {
	const src = `((1))`
	stmts, refusal := parseStatements(src)
	if refusal != nil {
		t.Fatalf("parseStatements refusal = %#v, want none", refusal)
	}
	command := stmts[0].Cmd
	tests := []struct {
		name      string
		configure func(*walker)
		wantCode  string
		wantWork  int
	}{
		{
			name:      "work cap replaces unsupported",
			configure: func(w *walker) { w.workLimit = 1 },
			wantCode:  "work-cap",
			wantWork:  3,
		},
		{
			name: "event cap wins over work cap",
			configure: func(w *walker) {
				w.workLimit = 1
				w.eventCap = 0
			},
			wantCode: "event-cap",
			wantWork: 1,
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			w := newWalker(src)
			test.configure(w)
			w.dispatch(command, "command", 1)
			if len(w.refusals) != 1 || w.refusals[0].code != test.wantCode {
				t.Fatalf("refusals = %#v, want one %s", w.refusals, test.wantCode)
			}
			if !w.stop || w.events != 1 || w.work != test.wantWork {
				t.Fatalf("stop, events, work = (%t, %d, %d), want (true, 1, %d)", w.stop, w.events, w.work, test.wantWork)
			}
		})
	}
}

func TestWalkDispatchUsesExactRoleBeforeWildcard(t *testing.T) {
	const src = `value=$(echo nested) echo outer`
	stmts, refusal := parseStatements(src)
	if refusal != nil {
		t.Fatalf("parseStatements refusal = %#v, want none", refusal)
	}
	call := stmts[0].Cmd.(*syntax.CallExpr)

	exact := newWalker(src)
	exact.dispatch(call.Assigns[0], "value", 1)
	if len(exact.sites) != 1 || len(exact.refusals) != 0 {
		t.Fatalf("exact-role dispatch returned %d sites and %d refusals, want 1 and 0", len(exact.sites), len(exact.refusals))
	}

	wildcard := newWalker(src)
	wildcard.dispatch(call.Assigns[0], "wrong-role", 1)
	if len(wildcard.refusals) != 1 || wildcard.refusals[0].code != "unsupported-construct" {
		t.Fatalf("wrong-role dispatch refusals = %#v, want unsupported-construct", wildcard.refusals)
	}
}

func TestWalkCapsEmitOneTerminalRefusal(t *testing.T) {
	const src = `echo one; echo two`
	stmts, refusal := parseStatements(src)
	if refusal != nil {
		t.Fatalf("parseStatements refusal = %#v, want none", refusal)
	}
	tests := []struct {
		name      string
		configure func(*walker)
		wantCode  string
	}{
		{name: "work", configure: func(w *walker) { w.workLimit = 1 }, wantCode: "work-cap"},
		{name: "depth", configure: func(w *walker) { w.depthCap = 1 }, wantCode: "depth-cap"},
		{name: "event", configure: func(w *walker) { w.eventCap = 0 }, wantCode: "event-cap"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			w := newWalker(src)
			test.configure(w)
			w.walkStatements(stmts, 1)
			if len(w.refusals) != 1 || w.refusals[0].code != test.wantCode {
				t.Fatalf("cap refusals = %#v, want one %s", w.refusals, test.wantCode)
			}
			if !w.stop {
				t.Fatal("cap breach did not stop traversal")
			}
			if w.work != w.nodes+w.events {
				t.Fatalf("work = %d, nodes + events = %d", w.work, w.nodes+w.events)
			}
		})
	}
}

func TestWalkWorkCapIncludesEmittedEvents(t *testing.T) {
	lit := &syntax.Lit{
		ValuePos: syntax.NewPos(0, 1, 1),
		ValueEnd: syntax.NewPos(1, 1, 2),
		Value:    "x",
	}
	call := &syntax.CallExpr{Args: []*syntax.Word{{Parts: []syntax.WordPart{lit}}}}
	w := newWalker("x")
	w.workLimit = 3
	w.dispatch(call, "argv", 1)

	if len(w.sites) != 1 {
		t.Fatalf("sites = %d, want the crossing command-site event retained", len(w.sites))
	}
	if len(w.refusals) != 1 || w.refusals[0].code != "work-cap" {
		t.Fatalf("refusals = %#v, want one terminal work-cap", w.refusals)
	}
	if !w.stop {
		t.Fatal("work-cap event did not stop traversal")
	}
	if w.nodes != 3 || w.events != 2 || w.work != 5 {
		t.Fatalf("nodes, events, work = (%d, %d, %d), want (3, 2, 5)", w.nodes, w.events, w.work)
	}
}

func TestWalkEventCapPrecedesWorkCapAtEveryCrossing(t *testing.T) {
	t.Run("site charge", func(t *testing.T) {
		word := &syntax.Word{Parts: []syntax.WordPart{&syntax.Lit{
			ValuePos: syntax.NewPos(0, 1, 1), ValueEnd: syntax.NewPos(1, 1, 2), Value: "x",
		}}}
		w := newWalker("x")
		w.eventCap = 1
		w.workLimit = 3
		w.dispatch(&syntax.CallExpr{Args: []*syntax.Word{word}}, "argv", 1)
		if len(w.sites) != 1 {
			t.Fatalf("sites = %d, want the crossing site retained", len(w.sites))
		}
		if len(w.refusals) != 1 || w.refusals[0].code != "event-cap" {
			t.Fatalf("refusals = %#v, want one event-cap", w.refusals)
		}
	})

	t.Run("node visit", func(t *testing.T) {
		w := newWalker("x")
		w.eventCap = 1
		w.events = 1
		w.workLimit = 0
		w.dispatch(&syntax.ArithmCmd{}, "command", 1)
		if len(w.refusals) != 1 || w.refusals[0].code != "event-cap" {
			t.Fatalf("refusals = %#v, want one event-cap", w.refusals)
		}
		if w.events != 2 || !w.stop {
			t.Fatalf("events, stop = (%d, %t), want (2, true)", w.events, w.stop)
		}
	})
}

func TestWalkUnknownNodeFailsClosedWithBoundedPointSpan(t *testing.T) {
	w := newWalker("abc")
	w.dispatch(unknownWalkNode{}, "anything", 1)
	if len(w.refusals) != 1 || w.refusals[0].code != "unsupported-construct" {
		t.Fatalf("unknown-node refusals = %#v, want unsupported-construct", w.refusals)
	}
	if got := w.refusals[0]; got.startByte != 0 || got.endByte != 0 {
		t.Fatalf("unknown-node span = [%d, %d), want bounded [0, 0)", got.startByte, got.endByte)
	}
	if !w.stop {
		t.Fatal("unknown node did not stop traversal")
	}
}

func TestWalkBoundedSpansMatchParsedNodes(t *testing.T) {
	const src = `>lead value=$(echo nested) echo "$value" >tail; if echo yes; then echo ok; fi`
	stmts, refusal := parseStatements(src)
	if refusal != nil {
		t.Fatalf("parseStatements refusal = %#v, want none", refusal)
	}
	w := newWalker(src)
	for _, stmt := range stmts {
		syntax.Walk(stmt, func(node syntax.Node) bool {
			if node == nil {
				return true
			}
			gotStart, gotEnd := w.nodeSpan(node)
			wantStart, wantEnd := 0, 0
			if pos := node.Pos(); pos.IsValid() {
				wantStart = min(int(pos.Offset()), len(src))
			}
			if pos := node.End(); pos.IsValid() {
				wantEnd = min(max(int(pos.Offset()), wantStart), len(src))
			}
			if gotStart != wantStart || gotEnd != wantEnd {
				t.Errorf("%T span = [%d, %d), want parsed [%d, %d)", node, gotStart, gotEnd, wantStart, wantEnd)
			}
			return true
		})
	}
}

func TestWalkStatementSpanMatchesTrailingAndInterleavedRedirects(t *testing.T) {
	for _, src := range []string{
		`echo >out after`,
		`>lead echo after`,
		`echo before >mid after`,
	} {
		t.Run(src, func(t *testing.T) {
			stmts, refusal := parseStatements(src)
			if refusal != nil {
				t.Fatalf("parseStatements refusal = %#v, want none", refusal)
			}
			w := newWalker(src)
			gotStart, gotEnd := w.nodeSpan(stmts[0])
			wantStart := int(stmts[0].Pos().Offset())
			wantEnd := int(stmts[0].End().Offset())
			if gotStart != wantStart || gotEnd != wantEnd {
				t.Fatalf("Stmt span = [%d, %d), want parsed [%d, %d)", gotStart, gotEnd, wantStart, wantEnd)
			}
		})
	}
}

func TestWalkZeroValueNodesAreCapSafe(t *testing.T) {
	nodes := []syntax.Node{
		&syntax.File{}, &syntax.Comment{}, &syntax.Stmt{}, &syntax.Assign{}, &syntax.Redirect{},
		&syntax.CallExpr{}, &syntax.Subshell{}, &syntax.Block{}, &syntax.IfClause{},
		&syntax.WhileClause{}, &syntax.ForClause{}, &syntax.WordIter{}, &syntax.CStyleLoop{},
		&syntax.BinaryCmd{}, &syntax.FuncDecl{}, &syntax.Word{}, &syntax.Lit{},
		&syntax.SglQuoted{}, &syntax.DblQuoted{}, &syntax.CmdSubst{}, &syntax.ParamExp{},
		&syntax.ArithmExp{}, &syntax.ArithmCmd{}, &syntax.BinaryArithm{}, &syntax.UnaryArithm{},
		&syntax.ParenArithm{}, &syntax.FlagsArithm{}, &syntax.CaseClause{}, &syntax.CaseItem{},
		&syntax.TestClause{}, &syntax.BinaryTest{}, &syntax.UnaryTest{}, &syntax.ParenTest{},
		&syntax.DeclClause{}, &syntax.ArrayExpr{}, &syntax.ArrayElem{}, &syntax.ExtGlob{},
		&syntax.ProcSubst{}, &syntax.TimeClause{}, &syntax.CoprocClause{}, &syntax.LetClause{},
		&syntax.BraceExp{}, &syntax.TestDecl{},
	}
	if len(nodes) != 43 {
		t.Fatalf("zero-value coverage = %d, want 43 pinned concrete types", len(nodes))
	}
	for _, node := range nodes {
		t.Run(fmt.Sprintf("%T", node), func(t *testing.T) {
			w := newWalker("")
			w.workLimit = 0
			w.dispatch(node, "zero-value-cap", 1)
			if len(w.refusals) != 1 || w.refusals[0].code != "work-cap" {
				t.Fatalf("refusals = %#v, want one work-cap", w.refusals)
			}
		})
	}
}

func TestWalkMalformedNodesFailClosedWithoutPanicking(t *testing.T) {
	dispatch := func(node syntax.Node, role string) func() ([]rawRefusal, bool) {
		return func() ([]rawRefusal, bool) {
			w := newWalker("")
			w.dispatch(node, role, 1)
			return w.refusals, w.stop
		}
	}
	tests := []struct {
		name     string
		wantCode string
		run      func() ([]rawRefusal, bool)
	}{
		{
			name:     "statement source order",
			wantCode: "unsupported-construct",
			run: func() ([]rawRefusal, bool) {
				_, refusals, _ := walk([]*syntax.Stmt{{Cmd: &syntax.CallExpr{}}}, "")
				return refusals, len(refusals) > 0
			},
		},
		{
			name:     "normal dispatch",
			wantCode: "unsupported-construct",
			run:      dispatch(&syntax.Word{}, "word-part"),
		},
		{
			name:     "cap-triggered span",
			wantCode: "work-cap",
			run: func() ([]rawRefusal, bool) {
				w := newWalker("")
				w.workLimit = 0
				w.dispatch(&syntax.Word{}, "word-part", 1)
				return w.refusals, w.stop
			},
		},
		{name: "empty case", wantCode: "unsupported-construct", run: dispatch(&syntax.CaseClause{}, "selector-word")},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			refusals, stopped := test.run()
			if len(refusals) != 1 || refusals[0].code != test.wantCode {
				t.Fatalf("refusals = %#v, want one %s", refusals, test.wantCode)
			}
			if refusals[0].startByte != 0 || refusals[0].endByte != 0 {
				t.Fatalf("span = [%d, %d), want [0, 0)", refusals[0].startByte, refusals[0].endByte)
			}
			if !stopped {
				t.Fatal("malformed node did not stop traversal")
			}
		})
	}
}

func TestWalkMalformedInteriorChildrenFailClosed(t *testing.T) {
	lit := func(offset uint, value string) *syntax.Lit {
		return &syntax.Lit{
			ValuePos: syntax.NewPos(offset, 1, offset+1),
			ValueEnd: syntax.NewPos(offset+uint(len(value)), 1, offset+uint(len(value))+1),
			Value:    value,
		}
	}
	word := func(offset uint, value string) *syntax.Word {
		return &syntax.Word{Parts: []syntax.WordPart{lit(offset, value)}}
	}
	stmt := func(offset uint, value string) *syntax.Stmt {
		return &syntax.Stmt{
			Position: syntax.NewPos(offset, 1, offset+1),
			Cmd:      &syntax.CallExpr{Args: []*syntax.Word{word(offset, value)}},
		}
	}
	var typedNilCommand *syntax.CallExpr
	var typedNilIndex *syntax.UnaryArithm
	var typedNilLoop *syntax.WordIter
	var typedNilPart *syntax.DblQuoted

	tests := []struct {
		name string
		node syntax.Node
		role string
	}{
		{name: "file statements", node: &syntax.File{Stmts: []*syntax.Stmt{stmt(0, "a"), nil, stmt(2, "b")}}, role: "top-level-statements"},
		{name: "statement redirects", node: &syntax.Stmt{Position: syntax.NewPos(0, 1, 1), Cmd: &syntax.CallExpr{Args: []*syntax.Word{word(0, "a")}}, Redirs: []*syntax.Redirect{nil}}, role: "command-and-redirects"},
		{name: "typed nil statement command", node: &syntax.Stmt{Position: syntax.NewPos(0, 1, 1), Cmd: typedNilCommand}, role: "command-and-redirects"},
		{name: "call assignments", node: &syntax.CallExpr{Assigns: []*syntax.Assign{{Name: lit(0, "a")}, nil, {Name: lit(2, "b")}}}, role: "argv"},
		{name: "call arguments", node: &syntax.CallExpr{Args: []*syntax.Word{word(0, "a"), nil, word(2, "b")}}, role: "argv"},
		{name: "typed nil assignment index", node: &syntax.Assign{Name: lit(0, "a"), Index: typedNilIndex}, role: "value"},
		{name: "word parts", node: &syntax.Word{Parts: []syntax.WordPart{lit(0, "a"), typedNilPart, lit(2, "b")}}, role: "word-part"},
		{name: "double quoted parts", node: &syntax.DblQuoted{Parts: []syntax.WordPart{lit(0, "a"), typedNilPart}}, role: "word-part"},
		{name: "block statements", node: &syntax.Block{Stmts: []*syntax.Stmt{nil}}, role: "body-statements"},
		{name: "subshell statements", node: &syntax.Subshell{Stmts: []*syntax.Stmt{nil}}, role: "body-statements"},
		{name: "command substitution statements", node: &syntax.CmdSubst{Stmts: []*syntax.Stmt{nil}}, role: "body-statements"},
		{name: "if condition statements", node: &syntax.IfClause{Cond: []*syntax.Stmt{nil}}, role: "condition-and-body"},
		{name: "if body statements", node: &syntax.IfClause{Then: []*syntax.Stmt{nil}}, role: "condition-and-body"},
		{name: "while condition statements", node: &syntax.WhileClause{Cond: []*syntax.Stmt{nil}}, role: "condition-and-body"},
		{name: "while body statements", node: &syntax.WhileClause{Do: []*syntax.Stmt{nil}}, role: "condition-and-body"},
		{name: "typed nil loop", node: &syntax.ForClause{Loop: typedNilLoop}, role: "loop-body-and-selector"},
		{name: "for body statements", node: &syntax.ForClause{Do: []*syntax.Stmt{nil}}, role: "loop-body-and-selector"},
		{name: "word iterator items", node: &syntax.WordIter{Name: lit(0, "x"), Items: []*syntax.Word{nil}}, role: "loop-items"},
		{name: "binary operand", node: &syntax.BinaryCmd{X: stmt(0, "a")}, role: "operand-statements"},
		{name: "function body", node: &syntax.FuncDecl{}, role: "body"},
		{name: "case items", node: &syntax.CaseClause{Word: word(0, "x"), Items: []*syntax.CaseItem{nil}}, role: "selector-word"},
		{name: "case patterns", node: &syntax.CaseItem{Patterns: []*syntax.Word{word(0, "a"), nil}}, role: "patterns-and-body"},
		{name: "case body statements", node: &syntax.CaseItem{Patterns: []*syntax.Word{word(0, "a")}, Stmts: []*syntax.Stmt{nil}}, role: "patterns-and-body"},
		{name: "redirect target", node: &syntax.Redirect{}, role: "target-word-expansion"},
		{name: "redirect heredoc body", node: &syntax.Redirect{Word: word(0, "eof"), Hdoc: &syntax.Word{}}, role: "unquoted-heredoc-body"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			w := newWalker("a b")
			w.dispatch(test.node, test.role, 1)
			if len(w.sites) != 0 {
				t.Fatalf("sites = %#v, want none from malformed structure", w.sites)
			}
			if len(w.refusals) != 1 || w.refusals[0].code != "unsupported-construct" {
				t.Fatalf("refusals = %#v, want one unsupported-construct", w.refusals)
			}
			if got := w.refusals[0]; got.startByte != 0 || got.endByte != 0 {
				t.Fatalf("span = [%d, %d), want clamped [0, 0)", got.startByte, got.endByte)
			}
			if !w.stop {
				t.Fatal("malformed child did not stop traversal")
			}
		})
	}

	t.Run("top-level statements", func(t *testing.T) {
		sites, refusals, _ := walk([]*syntax.Stmt{stmt(0, "a"), nil, stmt(2, "b")}, "a b")
		if len(sites) != 0 || len(refusals) != 1 || refusals[0].code != "unsupported-construct" {
			t.Fatalf("sites, refusals = (%#v, %#v), want none and one unsupported-construct", sites, refusals)
		}
		if got := refusals[0]; got.startByte != 0 || got.endByte != 0 {
			t.Fatalf("span = [%d, %d), want clamped [0, 0)", got.startByte, got.endByte)
		}
	})
}

func TestWalkRejectsMalformedCallOwnedStructureBeforeSite(t *testing.T) {
	lit := func() *syntax.Lit {
		return &syntax.Lit{
			ValuePos: syntax.NewPos(0, 1, 1), ValueEnd: syntax.NewPos(1, 1, 2), Value: "x",
		}
	}
	var typedNilPart *syntax.DblQuoted
	var typedNilIndex *syntax.UnaryArithm
	tests := []struct {
		name string
		call *syntax.CallExpr
	}{
		{name: "empty word", call: &syntax.CallExpr{Args: []*syntax.Word{{}}}},
		{name: "typed nil word part", call: &syntax.CallExpr{Args: []*syntax.Word{{Parts: []syntax.WordPart{typedNilPart}}}}},
		{name: "empty assignment", call: &syntax.CallExpr{Assigns: []*syntax.Assign{{}}}},
		{name: "typed nil assignment index", call: &syntax.CallExpr{Assigns: []*syntax.Assign{{Name: lit(), Index: typedNilIndex}}}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			w := newWalker("x")
			w.dispatch(test.call, "argv", 1)
			if len(w.sites) != 0 {
				t.Fatalf("sites = %#v, want no site for malformed call-owned structure", w.sites)
			}
			if len(w.refusals) != 1 || w.refusals[0].code != "unsupported-construct" {
				t.Fatalf("refusals = %#v, want one unsupported-construct", w.refusals)
			}
			if got := w.refusals[0]; got.startByte != 0 || got.endByte != 0 {
				t.Fatalf("span = [%d, %d), want [0, 0)", got.startByte, got.endByte)
			}
		})
	}
}

func TestWalkStructuralCertificationUsesPublicWorkEnvelope(t *testing.T) {
	word := func(offset uint) *syntax.Word {
		return &syntax.Word{Parts: []syntax.WordPart{&syntax.Lit{
			ValuePos: syntax.NewPos(offset, 1, offset+1),
			ValueEnd: syntax.NewPos(offset+1, 1, offset+2),
			Value:    "x",
		}}}
	}
	args := make([]*syntax.Word, 64)
	for index := range args {
		args[index] = word(uint(index))
	}

	t.Run("flat call cap", func(t *testing.T) {
		w := newWalker(strings.Repeat("x", len(args)))
		w.workLimit = 4
		w.dispatch(&syntax.CallExpr{Args: args}, "argv", 1)
		if len(w.sites) != 0 {
			t.Fatalf("sites = %d, want none before full call certification", len(w.sites))
		}
		if len(w.refusals) != 1 || w.refusals[0].code != "work-cap" {
			t.Fatalf("refusals = %#v, want one work-cap", w.refusals)
		}
		if w.nodes != 5 || w.events != 1 || w.work != 6 {
			t.Fatalf("nodes, events, work = (%d, %d, %d), want charged crossing (5, 1, 6)", w.nodes, w.events, w.work)
		}
	})

	t.Run("uncapped flat call", func(t *testing.T) {
		w := newWalker(strings.Repeat("x", len(args)))
		w.dispatch(&syntax.CallExpr{Args: args}, "argv", 1)
		if len(w.refusals) != 0 || len(w.sites) != 1 {
			t.Fatalf("sites, refusals = (%d, %#v), want one and none", len(w.sites), w.refusals)
		}
		if w.nodes != 129 || w.events != 1 || w.work != 130 {
			t.Fatalf("nodes, events, work = (%d, %d, %d), want unique-node accounting (129, 1, 130)", w.nodes, w.events, w.work)
		}
	})
}

func TestWalkTopLevelCertificationChargesBeforeCap(t *testing.T) {
	stmts := make([]*syntax.Stmt, visitorNodeCap+1)
	for index := range stmts {
		stmts[index] = &syntax.Stmt{}
	}
	sites, refusals, work := walk(stmts, "")
	if len(sites) != 0 || len(refusals) != 1 || refusals[0].code != "work-cap" {
		t.Fatalf("sites, refusals = (%d, %#v), want none and one work-cap", len(sites), refusals)
	}
	if work != visitorNodeCap+2 {
		t.Fatalf("work = %d, want charged node crossing plus terminal event %d", work, visitorNodeCap+2)
	}
}

func TestWalkCertificationAccountsAliasAndCycleTerminals(t *testing.T) {
	lit := &syntax.Lit{
		ValuePos: syntax.NewPos(0, 1, 1), ValueEnd: syntax.NewPos(1, 1, 2), Value: "x",
	}
	word := &syntax.Word{Parts: []syntax.WordPart{lit}}
	w := newWalker("x")
	w.dispatch(&syntax.CallExpr{Args: []*syntax.Word{word, word}}, "argv", 1)
	if w.nodes != 3 || w.events != 1 || w.work != 4 || len(w.refusals) != 1 {
		t.Fatalf("alias accounting = nodes %d, events %d, work %d, refusals %#v; want 3, 1, 4, one", w.nodes, w.events, w.work, w.refusals)
	}

	cycle := &syntax.UnaryArithm{OpPos: syntax.NewPos(0, 1, 1)}
	cycle.X = cycle
	w = newWalker("x")
	w.dispatch(cycle, "word-part", 1)
	if w.nodes != 1 || w.events != 1 || w.work != 2 || len(w.refusals) != 1 {
		t.Fatalf("cycle accounting = nodes %d, events %d, work %d, refusals %#v; want 1, 1, 2, one", w.nodes, w.events, w.work, w.refusals)
	}
}

func TestWalkRejectsStructuralCyclesAndAliasesBeforeSemanticTraversal(t *testing.T) {
	litWord := func(value string) *syntax.Word {
		return &syntax.Word{Parts: []syntax.WordPart{&syntax.Lit{
			ValuePos: syntax.NewPos(0, 1, 1),
			ValueEnd: syntax.NewPos(uint(len(value)), 1, uint(len(value))+1),
			Value:    value,
		}}}
	}
	callStmt := func(call *syntax.CallExpr) *syntax.Stmt {
		return &syntax.Stmt{Position: syntax.NewPos(0, 1, 1), Cmd: call}
	}

	t.Run("traversed if else cycle", func(t *testing.T) {
		body := callStmt(&syntax.CallExpr{Args: []*syntax.Word{litWord("nested")}})
		clause := &syntax.IfClause{Then: []*syntax.Stmt{body}}
		clause.Else = clause
		w := newWalker("nested")
		w.eventCap = 16
		w.dispatch(clause, "condition-and-body", 1)
		if len(w.sites) != 0 {
			t.Fatalf("sites = %d, want none before cyclic tree certification", len(w.sites))
		}
		if len(w.refusals) != 1 || w.refusals[0].code != "unsupported-construct" {
			t.Fatalf("refusals = %#v, want one unsupported-construct", w.refusals)
		}
	})

	t.Run("wide argv alias", func(t *testing.T) {
		word := litWord("x")
		args := make([]*syntax.Word, 250_000)
		for index := range args {
			args[index] = word
		}
		w := newWalker("x")
		w.dispatch(&syntax.CallExpr{Args: args}, "argv", 1)
		if len(w.sites) != 0 || len(w.refusals) != 1 || w.refusals[0].code != "unsupported-construct" {
			t.Fatalf("sites, refusals = (%d, %#v), want none and one alias refusal", len(w.sites), w.refusals)
		}
		if w.nodes != 3 || w.events != 1 || w.work != 4 || w.childSteps != 0 {
			t.Fatalf("nodes, events, work, semantic steps = (%d, %d, %d, %d), want (3, 1, 4, 0)", w.nodes, w.events, w.work, w.childSteps)
		}
	})

	t.Run("shared across top-level roots", func(t *testing.T) {
		shared := &syntax.CallExpr{Args: []*syntax.Word{litWord("shared")}}
		sites, refusals, work := walk([]*syntax.Stmt{callStmt(shared), callStmt(shared)}, "shared")
		if len(sites) != 0 || len(refusals) != 1 || refusals[0].code != "unsupported-construct" {
			t.Fatalf("sites, refusals = (%d, %#v), want none and one alias refusal", len(sites), refusals)
		}
		if work != 6 {
			t.Fatalf("work = %d, want five unique nodes plus one event", work)
		}
	})
}

func TestWalkCertifiedTreeIsReusableOnlyBySemanticPhase(t *testing.T) {
	const src = `echo "$(nested)"`
	stmts, refusal := parseStatements(src)
	if refusal != nil {
		t.Fatalf("parseStatements refusal = %#v, want none", refusal)
	}
	w := newWalker(src)
	if !w.certifyTree(stmts[0], 1) {
		t.Fatalf("valid preflight refusals = %#v", w.refusals)
	}
	nodes := w.nodes
	w.dispatch(stmts[0], "command-and-redirects", 1)
	if len(w.sites) != 2 || len(w.refusals) != 0 {
		t.Fatalf("semantic reuse returned %d sites and refusals %#v, want 2 and none", len(w.sites), w.refusals)
	}
	if w.nodes != nodes || w.work != w.nodes+w.events {
		t.Fatalf("semantic reuse changed nodes/work to (%d, %d), want nodes %d and exact accounting", w.nodes, w.work, nodes)
	}
}

func TestWalkRejectsMissingMandatoryPinnedChildrenBeforeSite(t *testing.T) {
	litWord := func(value string) *syntax.Word {
		return &syntax.Word{Parts: []syntax.WordPart{&syntax.Lit{
			ValuePos: syntax.NewPos(0, 1, 1),
			ValueEnd: syntax.NewPos(uint(len(value)), 1, uint(len(value))+1),
			Value:    value,
		}}}
	}
	commandWord := func(command syntax.Command) *syntax.Word {
		return &syntax.Word{Parts: []syntax.WordPart{&syntax.CmdSubst{
			Stmts: []*syntax.Stmt{{Position: syntax.NewPos(0, 1, 1), Cmd: command}},
		}}}
	}
	validBody := &syntax.Stmt{
		Position: syntax.NewPos(0, 1, 1),
		Cmd:      &syntax.CallExpr{Args: []*syntax.Word{litWord("nested")}},
	}
	var typedNilLoop *syntax.WordIter
	var typedNilWord *syntax.Word
	tests := []struct {
		name string
		arg  *syntax.Word
	}{
		{name: "for loop", arg: commandWord(&syntax.ForClause{Do: []*syntax.Stmt{validBody}})},
		{name: "arithmetic expansion", arg: &syntax.Word{Parts: []syntax.WordPart{&syntax.ArithmExp{}}}},
		{name: "arithmetic command", arg: commandWord(&syntax.ArithmCmd{})},
		{name: "parenthesized arithmetic", arg: &syntax.Word{Parts: []syntax.WordPart{&syntax.ArithmExp{X: &syntax.ParenArithm{}}}}},
		{name: "test clause", arg: commandWord(&syntax.TestClause{})},
		{name: "parenthesized test", arg: commandWord(&syntax.TestClause{X: &syntax.ParenTest{}})},
		{name: "test declaration description", arg: commandWord(&syntax.TestDecl{Body: validBody})},
		{name: "typed nil for loop", arg: commandWord(&syntax.ForClause{Loop: typedNilLoop, Do: []*syntax.Stmt{validBody}})},
		{name: "typed nil arithmetic expansion", arg: &syntax.Word{Parts: []syntax.WordPart{&syntax.ArithmExp{X: typedNilWord}}}},
		{name: "typed nil arithmetic command", arg: commandWord(&syntax.ArithmCmd{X: typedNilWord})},
		{name: "typed nil parenthesized arithmetic", arg: &syntax.Word{Parts: []syntax.WordPart{&syntax.ArithmExp{X: &syntax.ParenArithm{X: typedNilWord}}}}},
		{name: "typed nil test clause", arg: commandWord(&syntax.TestClause{X: typedNilWord})},
		{name: "typed nil parenthesized test", arg: commandWord(&syntax.TestClause{X: &syntax.ParenTest{X: typedNilWord}})},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			w := newWalker("nested")
			w.dispatch(&syntax.CallExpr{Args: []*syntax.Word{litWord("outer"), test.arg}}, "argv", 1)
			if len(w.sites) != 0 {
				t.Fatalf("sites = %d, want none before mandatory-child certification", len(w.sites))
			}
			if len(w.refusals) != 1 || w.refusals[0].code != "unsupported-construct" {
				t.Fatalf("refusals = %#v, want one unsupported-construct", w.refusals)
			}
		})
	}
}

func TestWalkKeepCommentsFileParityAndAccounting(t *testing.T) {
	for _, src := range []string{"# lead\necho x", "echo x # tail"} {
		t.Run(src, func(t *testing.T) {
			file, err := syntax.NewParser(syntax.KeepComments(true)).Parse(strings.NewReader(src), "")
			if err != nil {
				t.Fatalf("Parse returned %v", err)
			}
			w := newWalker(src)
			w.dispatch(file, "top-level-statements", 1)
			if len(w.sites) != 1 || len(w.refusals) != 0 {
				t.Fatalf("sites, refusals = (%d, %#v), want one and none", len(w.sites), w.refusals)
			}
			gotStart, gotEnd := w.nodeSpan(file)
			wantStart := int(file.Pos().Offset())
			wantEnd := int(file.End().Offset())
			if gotStart != wantStart || gotEnd != wantEnd {
				t.Fatalf("File span = [%d, %d), want parsed [%d, %d)", gotStart, gotEnd, wantStart, wantEnd)
			}
			commentCount := 0
			nodeCount := 0
			syntax.Walk(file, func(node syntax.Node) bool {
				if node != nil {
					nodeCount++
				}
				if _, ok := node.(*syntax.Comment); ok {
					commentCount++
				}
				return true
			})
			if commentCount != 1 {
				t.Fatalf("parsed comments = %d, want 1", commentCount)
			}
			if w.nodes != nodeCount || w.work != w.nodes+w.events {
				t.Fatalf("nodes, events, work = (%d, %d, %d), want parsed nodes %d plus events", w.nodes, w.events, w.work, nodeCount)
			}
		})
	}

	t.Run("comment-only file", func(t *testing.T) {
		const src = `# only`
		file, err := syntax.NewParser(syntax.KeepComments(true)).Parse(strings.NewReader(src), "")
		if err != nil {
			t.Fatalf("Parse returned %v", err)
		}
		w := newWalker(src)
		w.dispatch(file, "top-level-statements", 1)
		if len(w.sites) != 0 || len(w.refusals) != 0 || w.nodes != 2 || w.work != 2 {
			t.Fatalf("sites, refusals, nodes, work = (%d, %#v, %d, %d), want no events and File+Comment", len(w.sites), w.refusals, w.nodes, w.work)
		}
	})
}

func TestWalkCertificationCoversPinnedCommentCollections(t *testing.T) {
	comment := func(offset uint) syntax.Comment {
		return syntax.Comment{Hash: syntax.NewPos(offset, 1, offset+1), Text: " c"}
	}
	word := func() *syntax.Word {
		return &syntax.Word{Parts: []syntax.WordPart{&syntax.Lit{
			ValuePos: syntax.NewPos(0, 1, 1), ValueEnd: syntax.NewPos(1, 1, 2), Value: "x",
		}}}
	}
	tests := []struct {
		name      string
		node      syntax.Node
		wantNodes int
	}{
		{name: "file last", node: &syntax.File{Last: []syntax.Comment{comment(0)}}, wantNodes: 2},
		{name: "statement comments", node: &syntax.Stmt{Comments: []syntax.Comment{comment(0)}}, wantNodes: 2},
		{name: "subshell last", node: &syntax.Subshell{Last: []syntax.Comment{comment(0)}}, wantNodes: 2},
		{name: "block last", node: &syntax.Block{Last: []syntax.Comment{comment(0)}}, wantNodes: 2},
		{name: "command substitution last", node: &syntax.CmdSubst{Last: []syntax.Comment{comment(0)}}, wantNodes: 2},
		{name: "if comments", node: &syntax.IfClause{
			CondLast: []syntax.Comment{comment(0)}, ThenLast: []syntax.Comment{comment(1)}, Last: []syntax.Comment{comment(2)},
		}, wantNodes: 4},
		{name: "while comments", node: &syntax.WhileClause{
			CondLast: []syntax.Comment{comment(0)}, DoLast: []syntax.Comment{comment(1)},
		}, wantNodes: 3},
		{name: "for comments", node: &syntax.ForClause{
			Loop:   &syntax.WordIter{Name: &syntax.Lit{ValuePos: syntax.NewPos(0, 1, 1), ValueEnd: syntax.NewPos(1, 1, 2), Value: "x"}},
			DoLast: []syntax.Comment{comment(1)},
		}, wantNodes: 4},
		{name: "case clause last", node: &syntax.CaseClause{Word: word(), Last: []syntax.Comment{comment(1)}}, wantNodes: 4},
		{name: "case item comments", node: &syntax.CaseItem{
			Patterns: []*syntax.Word{word()}, Comments: []syntax.Comment{comment(1)}, Last: []syntax.Comment{comment(2)},
		}, wantNodes: 5},
		{name: "array expression last", node: &syntax.ArrayExpr{Last: []syntax.Comment{comment(0)}}, wantNodes: 2},
		{name: "array element comments after absent value", node: &syntax.ArrayElem{Index: word(), Comments: []syntax.Comment{comment(1)}}, wantNodes: 4},
		{name: "process substitution last", node: &syntax.ProcSubst{Last: []syntax.Comment{comment(0)}}, wantNodes: 2},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			w := newWalker("xxx")
			if !w.certifyTree(test.node, 1) {
				t.Fatalf("certification refusals = %#v", w.refusals)
			}
			if w.nodes != test.wantNodes || w.work != test.wantNodes {
				t.Fatalf("nodes, work = (%d, %d), want field-complete count %d", w.nodes, w.work, test.wantNodes)
			}
		})
	}
}

func TestWalkBoundsRecursiveSpanBeforeCaps(t *testing.T) {
	deepAssign := func(levels int) *syntax.Assign {
		leaf := &syntax.Word{Parts: []syntax.WordPart{&syntax.Lit{
			ValuePos: syntax.NewPos(1, 1, 2), ValueEnd: syntax.NewPos(2, 1, 3), Value: "x",
		}}}
		var index syntax.ArithmExpr = leaf
		for range levels {
			index = &syntax.UnaryArithm{OpPos: syntax.NewPos(0, 1, 1), X: index}
		}
		return &syntax.Assign{
			Name:  &syntax.Lit{ValuePos: syntax.NewPos(0, 1, 1), ValueEnd: syntax.NewPos(1, 1, 2), Value: "a"},
			Index: index,
		}
	}
	tests := []struct {
		name      string
		levels    int
		configure func(*walker)
		wantCode  string
	}{
		{name: "depth", levels: 4096, configure: func(w *walker) { w.depthCap = 0 }, wantCode: "depth-cap"},
		{name: "work", levels: 128, configure: func(w *walker) { w.workLimit = 0 }, wantCode: "work-cap"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			w := newWalker("ax")
			test.configure(w)
			w.dispatch(deepAssign(test.levels), "value", 1)
			if len(w.refusals) != 1 || w.refusals[0].code != test.wantCode {
				t.Fatalf("refusals = %#v, want one %s", w.refusals, test.wantCode)
			}
			if got := w.refusals[0]; got.startByte != 0 || got.endByte != 0 {
				t.Fatalf("bounded fallback span = [%d, %d), want [0, 0)", got.startByte, got.endByte)
			}
		})
	}
}

func TestWalkEventCapPrecedesUnknownRefusalEmission(t *testing.T) {
	w := newWalker("abc")
	w.eventCap = 0
	w.dispatch(unknownWalkNode{}, "anything", 1)
	if len(w.refusals) != 1 || w.refusals[0].code != "event-cap" {
		t.Fatalf("unknown node at event cap produced %#v, want one event-cap", w.refusals)
	}
}

func TestWalkDoesNotChargeTypedNilChildren(t *testing.T) {
	t.Run("dispatch", func(t *testing.T) {
		w := newWalker("")
		var clause *syntax.IfClause
		w.dispatch(clause, "condition-and-body", 1)
		if w.work != 0 || w.nodes != 0 || len(w.refusals) != 0 {
			t.Fatalf("typed-nil dispatch left work %d, nodes %d, refusals %#v; want all zero", w.work, w.nodes, w.refusals)
		}
	})

	t.Run("statement command makes parent malformed", func(t *testing.T) {
		var call *syntax.CallExpr
		stmt := &syntax.Stmt{Cmd: call}
		_, refusals, work := walk([]*syntax.Stmt{stmt}, "")
		if work != 2 || len(refusals) != 1 || refusals[0].code != "unsupported-construct" {
			t.Fatalf("typed-nil command left work %d and refusals %#v, want 2 and unsupported-construct", work, refusals)
		}
	})

	t.Run("word part makes parent malformed", func(t *testing.T) {
		w := newWalker("")
		var quoted *syntax.DblQuoted
		word := &syntax.Word{Parts: []syntax.WordPart{quoted}}
		w.consumeWord(word, 1)
		if w.work != 2 || w.nodes != 1 || len(w.refusals) != 1 || w.refusals[0].code != "unsupported-construct" {
			t.Fatalf("typed-nil word part left work %d, nodes %d, refusals %#v; want 2, 1, and unsupported-construct", w.work, w.nodes, w.refusals)
		}
	})
}

func TestWalkStopsChildLoopsImmediately(t *testing.T) {
	litWord := func(offset uint, value string) *syntax.Word {
		return &syntax.Word{Parts: []syntax.WordPart{&syntax.Lit{
			ValuePos: syntax.NewPos(offset, 1, offset+1),
			ValueEnd: syntax.NewPos(offset+uint(len(value)), 1, offset+uint(len(value))+1),
			Value:    value,
		}}}
	}
	procWord := func(offset uint) *syntax.Word {
		return &syntax.Word{Parts: []syntax.WordPart{&syntax.ProcSubst{
			OpPos:  syntax.NewPos(offset, 1, offset+1),
			Rparen: syntax.NewPos(offset+2, 1, offset+3),
		}}}
	}

	t.Run("call assignments and args", func(t *testing.T) {
		first := &syntax.Assign{
			Name: &syntax.Lit{ValuePos: syntax.NewPos(0, 1, 1), ValueEnd: syntax.NewPos(1, 1, 2), Value: "x"},
			Array: &syntax.ArrayExpr{
				Lparen: syntax.NewPos(2, 1, 3), Rparen: syntax.NewPos(3, 1, 4),
			},
		}
		second := &syntax.Assign{
			Name:  &syntax.Lit{ValuePos: syntax.NewPos(4, 1, 5), ValueEnd: syntax.NewPos(5, 1, 6), Value: "y"},
			Value: litWord(6, "ok"),
		}
		call := &syntax.CallExpr{Assigns: []*syntax.Assign{first, second}, Args: []*syntax.Word{litWord(9, "later")}}
		w := newWalker("x=() y=ok later")
		w.dispatch(call, "argv", 1)
		if w.childSteps != 1 {
			t.Fatalf("child loop steps = %d, want 1", w.childSteps)
		}
	})

	t.Run("word iterator items", func(t *testing.T) {
		iter := &syntax.WordIter{
			Name:  &syntax.Lit{ValuePos: syntax.NewPos(0, 1, 1), ValueEnd: syntax.NewPos(1, 1, 2), Value: "x"},
			Items: []*syntax.Word{procWord(2), litWord(6, "later")},
		}
		w := newWalker("x <() later")
		w.dispatch(iter, "loop-items", 1)
		if w.childSteps != 2 {
			t.Fatalf("child loop steps = %d, want 2", w.childSteps)
		}
	})

	t.Run("case items and patterns", func(t *testing.T) {
		clause := &syntax.CaseClause{
			Word: litWord(0, "x"),
			Items: []*syntax.CaseItem{
				{Patterns: []*syntax.Word{procWord(2)}},
				{Patterns: []*syntax.Word{litWord(6, "later")}},
			},
		}
		w := newWalker("x <() later")
		w.dispatch(clause, "selector-word", 1)
		if w.childSteps != 4 {
			t.Fatalf("child loop steps = %d, want 4", w.childSteps)
		}
	})
}

func TestWalkStopsBeforeScanningRemainingRedirects(t *testing.T) {
	redirect := func(offset uint) *syntax.Redirect {
		return &syntax.Redirect{
			OpPos: syntax.NewPos(offset, 1, offset+1),
			Word: &syntax.Word{Parts: []syntax.WordPart{&syntax.Lit{
				ValuePos: syntax.NewPos(offset+1, 1, offset+2),
				ValueEnd: syntax.NewPos(offset+2, 1, offset+3),
				Value:    "x",
			}}},
		}
	}
	stmt := &syntax.Stmt{
		Position: syntax.NewPos(0, 1, 1),
		Cmd: &syntax.ArithmCmd{
			Left:  syntax.NewPos(0, 1, 1),
			Right: syntax.NewPos(3, 1, 4),
			X: &syntax.Word{Parts: []syntax.WordPart{&syntax.Lit{
				ValuePos: syntax.NewPos(2, 1, 3), ValueEnd: syntax.NewPos(3, 1, 4), Value: "1",
			}}},
		},
		Redirs: []*syntax.Redirect{redirect(6), redirect(9), redirect(12)},
	}
	w := newWalker("((1))>x >x >x")
	w.dispatch(stmt, "command-and-redirects", 1)
	if w.childSteps != 1 {
		t.Fatalf("child loop steps = %d, want only the terminal command step", w.childSteps)
	}
	if len(w.refusals) != 1 || w.refusals[0].code != "unsupported-construct" {
		t.Fatalf("refusals = %#v, want one unsupported-construct", w.refusals)
	}
}

func TestWalkBoundsStructuralPreflightAtCaps(t *testing.T) {
	word := &syntax.Word{Parts: []syntax.WordPart{&syntax.Lit{
		ValuePos: syntax.NewPos(0, 1, 1), ValueEnd: syntax.NewPos(1, 1, 2), Value: "x",
	}}}
	args := make([]*syntax.Word, 1024)
	for index := range args {
		args[index] = word
	}

	t.Run("event cap skips checks", func(t *testing.T) {
		w := newWalker("x")
		w.eventCap = 0
		w.dispatch(&syntax.CallExpr{Args: args}, "argv", 1)
		if len(w.refusals) != 1 || w.refusals[0].code != "event-cap" {
			t.Fatalf("refusals = %#v, want one event-cap", w.refusals)
		}
		if w.nodes != 0 || w.events != 1 || w.work != 1 {
			t.Fatalf("nodes, events, work = (%d, %d, %d), want no node scan and one event", w.nodes, w.events, w.work)
		}
	})

	t.Run("work envelope bounds checks", func(t *testing.T) {
		w := newWalker("x")
		w.workLimit = 1
		w.dispatch(&syntax.CallExpr{Args: args}, "argv", 1)
		if len(w.sites) != 0 {
			t.Fatalf("sites = %d, want none before incomplete certification", len(w.sites))
		}
		if len(w.refusals) != 1 || w.refusals[0].code != "work-cap" {
			t.Fatalf("refusals = %#v, want one work-cap", w.refusals)
		}
		if w.nodes != 2 || w.events != 1 || w.work != 3 {
			t.Fatalf("nodes, events, work = (%d, %d, %d), want charged crossing (2, 1, 3)", w.nodes, w.events, w.work)
		}
	})
}

func TestWalkWorkIsDeterministic(t *testing.T) {
	const src = `echo "$(doc-lattice lint)"`
	stmts, refusal := parseStatements(src)
	if refusal != nil {
		t.Fatalf("parseStatements refusal = %#v, want none", refusal)
	}
	_, _, first := walk(stmts, src)
	_, _, second := walk(stmts, src)
	if first <= 0 || first != second {
		t.Fatalf("walk work = (%d, %d), want equal positive values", first, second)
	}
}

func TestWalkNodeSwitchCoversPinnedASTTypesAndCertifiedTable(t *testing.T) {
	nodes := []syntax.Node{
		&syntax.File{}, &syntax.Comment{}, &syntax.Stmt{}, &syntax.Assign{}, &syntax.Redirect{},
		&syntax.CallExpr{}, &syntax.Subshell{}, &syntax.Block{}, &syntax.IfClause{},
		&syntax.WhileClause{}, &syntax.ForClause{}, &syntax.WordIter{}, &syntax.CStyleLoop{},
		&syntax.BinaryCmd{}, &syntax.FuncDecl{}, &syntax.Word{}, &syntax.Lit{},
		&syntax.SglQuoted{}, &syntax.DblQuoted{}, &syntax.CmdSubst{}, &syntax.ParamExp{},
		&syntax.ArithmExp{}, &syntax.ArithmCmd{}, &syntax.BinaryArithm{}, &syntax.UnaryArithm{},
		&syntax.ParenArithm{}, &syntax.FlagsArithm{}, &syntax.CaseClause{}, &syntax.CaseItem{},
		&syntax.TestClause{}, &syntax.BinaryTest{}, &syntax.UnaryTest{}, &syntax.ParenTest{},
		&syntax.DeclClause{}, &syntax.ArrayExpr{}, &syntax.ArrayElem{}, &syntax.ExtGlob{},
		&syntax.ProcSubst{}, &syntax.TimeClause{}, &syntax.CoprocClause{}, &syntax.LetClause{},
		&syntax.BraceExp{}, &syntax.TestDecl{},
	}
	for _, node := range nodes {
		name, known := syntaxNodeName(node)
		if !known || name == "" {
			t.Fatalf("syntaxNodeName(%T) = (%q, %t), want a known name", node, name, known)
		}
		hasRow := false
		for key := range certifiedConstructs {
			if key.node == name {
				hasRow = true
				break
			}
		}
		if !hasRow {
			t.Errorf("syntax node %s has no certified construct row", name)
		}
	}

	certifiedNames := make(map[string]struct{})
	for key := range certifiedConstructs {
		certifiedNames[key.node] = struct{}{}
	}
	if len(certifiedNames) != certifiedNodeTypeCount {
		t.Fatalf("certified table covers %d node names, want certifiedNodeTypeCount %d", len(certifiedNames), certifiedNodeTypeCount)
	}
}

func TestWalkDispatchTraversesFileRow(t *testing.T) {
	const src = `doc-lattice check`
	stmts, refusal := parseStatements(src)
	if refusal != nil {
		t.Fatalf("parseStatements refusal = %#v, want none", refusal)
	}
	w := newWalker(src)
	w.dispatch(&syntax.File{Stmts: stmts}, "top-level-statements", 1)
	if len(w.sites) != 1 || len(w.refusals) != 0 {
		t.Fatalf("File dispatch returned %d sites and %d refusals, want 1 and 0", len(w.sites), len(w.refusals))
	}
}

type unknownWalkNode struct{}

func (unknownWalkNode) Pos() syntax.Pos { return syntax.NewPos(99, 1, 100) }
func (unknownWalkNode) End() syntax.Pos { return syntax.NewPos(2, 1, 3) }
