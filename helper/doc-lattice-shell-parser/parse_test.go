// Package main tests the shell-parser statement acquisition behavior.
package main

import "testing"

func TestParseCanonicalDrainAndDedup(t *testing.T) {
	const src = `doc-lattice check; echo "$(`
	stmts, refusal := parseStatements(src)
	if len(stmts) != 1 {
		t.Fatalf("parseStatements returned %d statements, want 1", len(stmts))
	}
	if refusal == nil || refusal.code != "syntax-error" {
		t.Fatalf("parseStatements refusal = %#v, want syntax-error", refusal)
	}
	if refusal.startByte < 0 || refusal.endByte < refusal.startByte || refusal.endByte > len(src) {
		t.Fatalf("parseStatements refusal span = [%d, %d), want a valid source span", refusal.startByte, refusal.endByte)
	}
}

func TestParseCleanHasNoRefusal(t *testing.T) {
	stmts, refusal := parseStatements(`doc-lattice check; doc-lattice lint`)
	if len(stmts) != 2 || refusal != nil {
		t.Fatalf("parseStatements returned %d statements and refusal %#v, want 2 statements and no refusal", len(stmts), refusal)
	}
}

func TestParseIncompleteBinaryRetainsNoPartialStatement(t *testing.T) {
	stmts, refusal := parseStatements(`doc-lattice linear && (`)
	if len(stmts) != 0 {
		t.Fatalf("parseStatements returned %d statements, want none", len(stmts))
	}
	if refusal == nil || refusal.code != "syntax-error" {
		t.Fatalf("parseStatements refusal = %#v, want syntax-error", refusal)
	}
}

func TestParseEmptyAndWhitespaceAreClean(t *testing.T) {
	for _, src := range []string{"", " \t\r\n"} {
		stmts, refusal := parseStatements(src)
		if len(stmts) != 0 || refusal != nil {
			t.Errorf("parseStatements(%q) returned %d statements and refusal %#v, want neither", src, len(stmts), refusal)
		}
	}
}
