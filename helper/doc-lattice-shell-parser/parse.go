// Package main implements Bash statement acquisition for the shell-parser helper.
package main

import (
	"strings"

	"mvdan.cc/sh/v3/syntax"
)

type rawRefusal struct {
	code               string
	startByte, endByte int
}

func parseStatements(src string) (stmts []*syntax.Stmt, refusal *rawRefusal) {
	parser := syntax.NewParser(syntax.Variant(syntax.LangBash))
	for stmt, err := range parser.StmtsSeq(strings.NewReader(src)) {
		if err != nil {
			if refusal == nil {
				start, end := errorSpan(err, len(src))
				refusal = &rawRefusal{
					code:      "syntax-error",
					startByte: start,
					endByte:   end,
				}
			}
			continue
		}
		if refusal != nil || stmt == nil {
			continue
		}
		stmts = append(stmts, stmt)
	}
	return stmts, refusal
}

func errorSpan(err error, srcLen int) (int, int) {
	var pos syntax.Pos
	switch err := err.(type) {
	case syntax.ParseError:
		pos = err.Pos
	case syntax.LangError:
		pos = err.Pos
	default:
		return srcLen, srcLen
	}
	if !pos.IsValid() || pos.Offset() > uint(srcLen) {
		return srcLen, srcLen
	}
	offset := int(pos.Offset())
	return offset, offset
}
