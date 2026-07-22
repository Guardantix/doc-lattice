// Package main implements context-carrying shell syntax traversal.
package main

import (
	"strings"

	"mvdan.cc/sh/v3/syntax"
)

type commandSite struct {
	call        *syntax.CallExpr
	argv        []*syntax.Word
	assignments []*syntax.Assign
}

type walker struct {
	src        string
	sites      []commandSite
	refusals   []rawRefusal
	work       int
	depth      int
	nodes      int
	events     int
	childSteps int
	checkSteps int
	workLimit  int
	depthCap   int
	eventCap   int
	stop       bool
}

func walk(stmts []*syntax.Stmt, src string) (sites []commandSite, refusals []rawRefusal, work int) {
	w := newWalker(src)
	if status := w.validateStatements(stmts); status != structureValid {
		w.requestTerminal(nil, terminalCode(status), true)
		return w.sites, w.refusals, w.work
	}
	for _, stmt := range stmts {
		if !w.enterChild() {
			break
		}
		w.dispatch(stmt, "command-and-redirects", 1)
		if w.stop {
			break
		}
	}
	return w.sites, w.refusals, w.work
}

func newWalker(src string) *walker {
	return &walker{
		src:       src,
		workLimit: visitorNodeCap,
		depthCap:  visitorDepthCap,
		eventCap:  eventCap,
	}
}

func (w *walker) dispatch(node syntax.Node, role string, depth int) {
	if node == nil || w.stop {
		return
	}
	name, known := syntaxNodeName(node)
	if known && name == "" {
		return
	}
	if !w.visit(node, depth) {
		return
	}
	if w.events >= w.eventCap {
		w.requestTerminal(node, "event-cap", false)
		return
	}
	if status := w.validateNodeStructure(node); status != structureValid {
		w.requestTerminal(node, terminalCode(status), true)
		return
	}
	if !known {
		w.requestTerminal(node, "unsupported-construct", false)
		return
	}
	disposition, ok := certifiedConstructs[constructKey{node: name, role: role}]
	if !ok {
		disposition, ok = certifiedConstructs[constructKey{node: name, role: "*"}]
	}
	if !ok {
		w.requestTerminal(node, "unsupported-construct", false)
		return
	}
	switch disposition {
	case "traverse":
		w.traverse(node, role, depth)
	case "ignore":
		return
	case "refuse":
		w.requestTerminal(node, "unsupported-construct", false)
	default:
		w.requestTerminal(node, "unsupported-construct", false)
	}
}

func (w *walker) traverse(node syntax.Node, role string, depth int) {
	switch node := node.(type) {
	case *syntax.File:
		w.walkStatements(node.Stmts, depth+1)
	case *syntax.Stmt:
		w.walkStmt(node, depth)
	case *syntax.CallExpr:
		w.emitSite(node)
		if w.stop {
			return
		}
		for _, assign := range node.Assigns {
			if !w.enterChild() {
				return
			}
			w.dispatch(assign, "value", depth+1)
			if w.stop {
				return
			}
		}
		for _, arg := range node.Args {
			if !w.enterChild() {
				return
			}
			w.consumeWord(arg, depth+1)
			if w.stop {
				return
			}
		}
	case *syntax.Assign:
		w.walkAssign(node, depth)
	case *syntax.Redirect:
		w.walkRedirect(node, role, depth)
	case *syntax.BinaryCmd:
		w.dispatch(node.X, "command-and-redirects", depth+1)
		if w.stop {
			return
		}
		w.dispatch(node.Y, "command-and-redirects", depth+1)
	case *syntax.Block:
		w.walkStatements(node.Stmts, depth+1)
	case *syntax.Subshell:
		w.walkStatements(node.Stmts, depth+1)
	case *syntax.CmdSubst:
		w.walkStatements(node.Stmts, depth+1)
	case *syntax.FuncDecl:
		w.dispatch(node.Body, "command-and-redirects", depth+1)
	case *syntax.IfClause:
		w.walkStatements(node.Cond, depth+1)
		if w.stop {
			return
		}
		w.walkStatements(node.Then, depth+1)
		if w.stop {
			return
		}
		w.dispatch(node.Else, "condition-and-body", depth+1)
	case *syntax.WhileClause:
		w.walkStatements(node.Cond, depth+1)
		if w.stop {
			return
		}
		w.walkStatements(node.Do, depth+1)
	case *syntax.ForClause:
		if node.Loop != nil {
			loopRole := "loop-selector"
			if _, ok := node.Loop.(*syntax.WordIter); ok {
				loopRole = "loop-items"
			}
			w.dispatch(node.Loop, loopRole, depth+1)
			if w.stop {
				return
			}
		}
		w.walkStatements(node.Do, depth+1)
	case *syntax.WordIter:
		for _, item := range node.Items {
			if !w.enterChild() {
				return
			}
			w.consumeWord(item, depth+1)
			if w.stop {
				return
			}
		}
	case *syntax.CaseClause:
		w.consumeWord(node.Word, depth+1)
		if w.stop {
			return
		}
		for _, item := range node.Items {
			if !w.enterChild() {
				return
			}
			w.dispatch(item, "patterns-and-body", depth+1)
			if w.stop {
				return
			}
		}
	case *syntax.CaseItem:
		for _, pattern := range node.Patterns {
			if !w.enterChild() {
				return
			}
			w.consumeWord(pattern, depth+1)
			if w.stop {
				return
			}
		}
		w.walkStatements(node.Stmts, depth+1)
	default:
		w.requestTerminal(node, "unsupported-construct", false)
	}
}

func (w *walker) walkStmt(stmt *syntax.Stmt, depth int) {
	var command syntax.Command
	commandStart := 0
	if stmt.Cmd != nil && !syntaxNodeIsNil(stmt.Cmd) {
		start, _ := w.nodeSpan(stmt.Cmd)
		command = stmt.Cmd
		commandStart = start
	}
	commandWalked := command == nil
	for _, redirect := range stmt.Redirs {
		redirectStart, _ := w.nodeSpan(redirect)
		if !commandWalked && commandStart <= redirectStart {
			if !w.enterChild() {
				return
			}
			w.dispatch(command, commandRole(command), depth+1)
			commandWalked = true
			if w.stop {
				return
			}
		}
		if !w.enterChild() {
			return
		}
		w.dispatch(redirect, redirectRole(redirect), depth+1)
		if w.stop {
			return
		}
	}
	if !commandWalked && w.enterChild() {
		w.dispatch(command, commandRole(command), depth+1)
	}
}

func (w *walker) walkStatements(stmts []*syntax.Stmt, depth int) {
	for _, stmt := range stmts {
		if !w.enterChild() {
			return
		}
		w.dispatch(stmt, "command-and-redirects", depth)
		if w.stop {
			return
		}
	}

}

func (w *walker) walkAssign(assign *syntax.Assign, depth int) {
	if assign.Index != nil {
		w.dispatch(assign.Index, "assignment-index", depth+1)
		if w.stop {
			return
		}
	}
	if assign.Array != nil {
		w.dispatch(assign.Array, "assignment-array", depth+1)
		if w.stop {
			return
		}
	}
	if assign.Value != nil {
		w.consumeWord(assign.Value, depth+1)
	}
}

func (w *walker) walkRedirect(redirect *syntax.Redirect, role string, depth int) {
	switch role {
	case "target-word-expansion":
		w.consumeWord(redirect.Word, depth+1)
	case "unquoted-heredoc-body":
		w.consumeWord(redirect.Hdoc, depth+1)
	}
}

func (w *walker) consumeWord(word *syntax.Word, depth int) {
	if word == nil || w.stop {
		return
	}
	if !w.visit(word, depth) {
		return
	}
	if status := w.validateNodeStructure(word); status != structureValid {
		w.requestTerminal(word, terminalCode(status), true)
		return
	}
	for _, part := range word.Parts {
		if !w.enterChild() {
			return
		}
		w.consumeWordPart(part, depth+1)
		if w.stop {
			return
		}
	}
}

func (w *walker) consumeWordPart(part syntax.WordPart, depth int) {
	if part == nil || w.stop || syntaxNodeIsNil(part) {
		return
	}
	switch part := part.(type) {
	case *syntax.Lit, *syntax.SglQuoted:
		w.visit(part, depth)
	case *syntax.DblQuoted:
		if !w.visit(part, depth) {
			return
		}
		if status := w.validateNodeStructure(part); status != structureValid {
			w.requestTerminal(part, terminalCode(status), true)
			return
		}
		for _, nested := range part.Parts {
			if !w.enterChild() {
				return
			}
			w.consumeWordPart(nested, depth+1)
			if w.stop {
				return
			}
		}
	case *syntax.CmdSubst:
		w.dispatch(part, "body-statements", depth)
	default:
		w.dispatch(part, "word-part", depth)
	}
}

func (w *walker) enterChild() bool {
	if w.stop {
		return false
	}
	w.childSteps++
	return true
}

func (w *walker) visit(node syntax.Node, depth int) bool {
	if w.stop {
		return false
	}
	w.work++
	w.nodes++
	w.depth = max(w.depth, depth)
	if w.work > w.workLimit {
		w.requestTerminal(node, "work-cap", false)
		return false
	}
	if depth > w.depthCap {
		w.requestTerminal(node, "depth-cap", false)
		return false
	}
	return true
}

func (w *walker) emitSite(call *syntax.CallExpr) {
	if w.stop {
		return
	}
	if w.events >= w.eventCap {
		w.requestTerminal(call, "event-cap", false)
		return
	}
	w.sites = append(w.sites, commandSite{
		call:        call,
		argv:        call.Args,
		assignments: call.Assigns,
	})
	w.chargeEvent(call)
}

func (w *walker) chargeEvent(node syntax.Node) {
	w.events++
	w.work++
	if w.work > w.workLimit {
		w.requestTerminal(node, "work-cap", false)
	}
}

func (w *walker) requestTerminal(node syntax.Node, code string, pointSpan bool) {
	if w.stop {
		return
	}
	if w.events >= w.eventCap {
		code = "event-cap"
	} else if code == "work-cap" || w.work+1 > w.workLimit {
		code = "work-cap"
	}
	start, end := 0, 0
	if !pointSpan {
		start, end = w.nodeSpan(node)
	}
	w.appendTerminalRefusal(start, end, code)
}

func (w *walker) appendTerminalRefusal(start, end int, code string) {
	if w.stop {
		return
	}
	// The chosen terminal refusal is the final charged event and is not recursively cap-checked.
	w.refusals = append(w.refusals, rawRefusal{code: code, startByte: start, endByte: end})
	w.events++
	w.work++
	w.stop = true
}

func (w *walker) nodeSpan(node syntax.Node) (int, int) {
	if node == nil || syntaxNodeIsNil(node) {
		return 0, 0
	}
	limit := min(max(w.depthCap+1, 1), visitorDepthCap+1)
	limit = min(limit, max(w.workLimit-w.work+1, 1))
	start, startOK := boundedBoundary(node, false, limit)
	end, endOK := boundedBoundary(node, true, limit)
	if !startOK || !endOK {
		return 0, 0
	}
	start = min(max(start, 0), len(w.src))
	end = min(max(end, start), len(w.src))
	return start, end
}

type structureStatus uint8

const (
	structureValid structureStatus = iota
	structureMalformed
	structureExhausted
)

func terminalCode(status structureStatus) string {
	if status == structureExhausted {
		return "work-cap"
	}
	return "unsupported-construct"
}

func (w *walker) checkNode(node syntax.Node, required bool) structureStatus {
	checkLimit := min(visitorNodeCap, max(w.workLimit, 1))
	if w.checkSteps >= checkLimit {
		return structureExhausted
	}
	w.checkSteps++
	if node == nil {
		if required {
			return structureMalformed
		}
		return structureValid
	}
	if syntaxNodeIsNil(node) {
		return structureMalformed
	}
	return structureValid
}

func (w *walker) validateStatements(stmts []*syntax.Stmt) structureStatus {
	for _, stmt := range stmts {
		if status := w.checkNode(stmt, true); status != structureValid {
			return status
		}
	}
	return structureValid
}

func (w *walker) validateWords(words []*syntax.Word) structureStatus {
	for _, word := range words {
		if status := w.checkNode(word, true); status != structureValid {
			return status
		}
	}
	return structureValid
}

func (w *walker) validateWordParts(parts []syntax.WordPart) structureStatus {
	for _, part := range parts {
		if status := w.checkNode(part, true); status != structureValid {
			return status
		}
	}
	return structureValid
}

func (w *walker) validateNodeStructure(node syntax.Node) structureStatus {
	switch node := node.(type) {
	case *syntax.File:
		return w.validateStatements(node.Stmts)
	case *syntax.Stmt:
		if status := w.checkNode(node.Cmd, false); status != structureValid {
			return status
		}
		for _, redirect := range node.Redirs {
			if status := w.checkNode(redirect, true); status != structureValid {
				return status
			}
		}
	case *syntax.Assign:
		if node.Name == nil && node.Value == nil {
			return structureMalformed
		}
		if status := w.checkNode(node.Index, false); status != structureValid {
			return status
		}
		if node.Array != nil {
			if status := w.checkNode(node.Array, true); status != structureValid {
				return status
			}
		}
		if node.Value != nil {
			if status := w.checkNode(node.Value, true); status != structureValid {
				return status
			}
		}
	case *syntax.Redirect:
		if status := w.checkNode(node.Word, true); status != structureValid {
			return status
		}
		if status := w.validateNodeStructure(node.Word); status != structureValid {
			return status
		}
		if node.Hdoc != nil {
			if status := w.validateNodeStructure(node.Hdoc); status != structureValid {
				return status
			}
		}
	case *syntax.CallExpr:
		if len(node.Assigns) == 0 && len(node.Args) == 0 {
			return structureMalformed
		}
		for _, assign := range node.Assigns {
			if status := w.checkNode(assign, true); status != structureValid {
				return status
			}
		}
		return w.validateWords(node.Args)
	case *syntax.Block:
		return w.validateStatements(node.Stmts)
	case *syntax.Subshell:
		return w.validateStatements(node.Stmts)
	case *syntax.CmdSubst:
		return w.validateStatements(node.Stmts)
	case *syntax.FuncDecl:
		return w.checkNode(node.Body, true)
	case *syntax.IfClause:
		if status := w.validateStatements(node.Cond); status != structureValid {
			return status
		}
		if status := w.validateStatements(node.Then); status != structureValid {
			return status
		}
		if node.Else != nil {
			return w.checkNode(node.Else, true)
		}
		return structureValid
	case *syntax.WhileClause:
		if status := w.validateStatements(node.Cond); status != structureValid {
			return status
		}
		return w.validateStatements(node.Do)
	case *syntax.ForClause:
		if status := w.checkNode(node.Loop, false); status != structureValid {
			return status
		}
		return w.validateStatements(node.Do)
	case *syntax.WordIter:
		if status := w.checkNode(node.Name, true); status != structureValid {
			return status
		}
		return w.validateWords(node.Items)
	case *syntax.CStyleLoop:
		for _, child := range []syntax.Node{node.Init, node.Cond, node.Post} {
			if status := w.checkNode(child, false); status != structureValid {
				return status
			}
		}
	case *syntax.BinaryCmd:
		if status := w.checkNode(node.X, true); status != structureValid {
			return status
		}
		return w.checkNode(node.Y, true)
	case *syntax.Word:
		if len(node.Parts) == 0 {
			return structureMalformed
		}
		return w.validateWordParts(node.Parts)
	case *syntax.DblQuoted:
		return w.validateWordParts(node.Parts)
	case *syntax.ParamExp:
		if !node.Dollar.IsValid() && node.Param == nil {
			return structureMalformed
		}
		if node.Short && node.Index == nil && node.Param == nil {
			return structureMalformed
		}
		for _, child := range []syntax.Node{node.NestedParam, node.Index} {
			if status := w.checkNode(child, false); status != structureValid {
				return status
			}
		}
	case *syntax.BinaryArithm:
		if status := w.checkNode(node.X, true); status != structureValid {
			return status
		}
		return w.checkNode(node.Y, true)
	case *syntax.UnaryArithm:
		return w.checkNode(node.X, true)
	case *syntax.FlagsArithm:
		if status := w.checkNode(node.Flags, true); status != structureValid {
			return status
		}
		return w.checkNode(node.X, false)
	case *syntax.CaseClause:
		if status := w.checkNode(node.Word, true); status != structureValid {
			return status
		}
		for _, item := range node.Items {
			if status := w.checkNode(item, true); status != structureValid {
				return status
			}
		}
	case *syntax.CaseItem:
		if len(node.Patterns) == 0 {
			return structureMalformed
		}
		if status := w.validateWords(node.Patterns); status != structureValid {
			return status
		}
		return w.validateStatements(node.Stmts)
	case *syntax.BinaryTest:
		if status := w.checkNode(node.X, true); status != structureValid {
			return status
		}
		return w.checkNode(node.Y, true)
	case *syntax.UnaryTest:
		return w.checkNode(node.X, true)
	case *syntax.DeclClause:
		if status := w.checkNode(node.Variant, true); status != structureValid {
			return status
		}
		for _, arg := range node.Args {
			if status := w.checkNode(arg, true); status != structureValid {
				return status
			}
		}
	case *syntax.ArrayElem:
		if node.Index == nil && node.Value == nil {
			return structureMalformed
		}
		if status := w.checkNode(node.Index, false); status != structureValid {
			return status
		}
		if node.Value != nil {
			return w.checkNode(node.Value, true)
		}
		return structureValid
	case *syntax.ExtGlob:
		return w.checkNode(node.Pattern, true)
	case *syntax.TimeClause:
		if node.Stmt != nil {
			return w.checkNode(node.Stmt, true)
		}
		return structureValid
	case *syntax.CoprocClause:
		return w.checkNode(node.Stmt, true)
	case *syntax.LetClause:
		if len(node.Exprs) == 0 {
			return structureMalformed
		}
		for _, expr := range node.Exprs {
			if status := w.checkNode(expr, true); status != structureValid {
				return status
			}
		}
	case *syntax.BraceExp:
		if len(node.Elems) == 0 {
			return structureMalformed
		}
		return w.validateWords(node.Elems)
	case *syntax.TestDecl:
		return w.checkNode(node.Body, true)
	}
	return structureValid
}

func boundedBoundary(node syntax.Node, end bool, limit int) (int, bool) {
	if !end {
		return boundedStart(node, limit)
	}
	current := node
	adjust := 0
	for range limit {
		if current == nil || syntaxNodeIsNil(current) {
			return 0, false
		}
		switch node := current.(type) {
		case *syntax.File:
			if end {
				if len(node.Last) > 0 {
					comment := node.Last[len(node.Last)-1]
					return positionOffset(comment.Hash, adjust+1+len(comment.Text)), true
				}
				if len(node.Stmts) == 0 {
					return 0, true
				}
				current = node.Stmts[len(node.Stmts)-1]
			} else {
				if len(node.Stmts) > 0 {
					current = node.Stmts[0]
				} else if len(node.Last) > 0 {
					return positionOffset(node.Last[0].Hash, adjust), true
				} else {
					return 0, true
				}
			}
		case *syntax.Comment:
			if end {
				adjust += 1 + len(node.Text)
			}
			return positionOffset(node.Hash, adjust), true
		case *syntax.Stmt:
			if !end {
				return positionOffset(node.Position, adjust), true
			}
			if node.Semicolon.IsValid() {
				delta := 1
				if node.Coprocess || node.Disown {
					delta++
				}
				return positionOffset(node.Semicolon, adjust+delta), true
			}
			if len(node.Redirs) > 0 && node.Cmd != nil && !syntaxNodeIsNil(node.Cmd) {
				lastRedirect := node.Redirs[len(node.Redirs)-1]
				commandStart, commandOK := boundedStart(node.Cmd, limit)
				redirectStart, redirectOK := boundedStart(lastRedirect, limit)
				if commandOK && (!redirectOK || commandStart > redirectStart) {
					current = node.Cmd
				} else {
					current = lastRedirect
				}
			} else if len(node.Redirs) > 0 {
				current = node.Redirs[len(node.Redirs)-1]
			} else if node.Cmd != nil {
				current = node.Cmd
			} else {
				delta := 0
				if node.Negated {
					delta = 1
				}
				return positionOffset(node.Position, adjust+delta), true
			}
		case *syntax.Assign:
			if !end {
				if node.Name != nil {
					current = node.Name
				} else {
					current = node.Value
				}
			} else if node.Value != nil {
				current = node.Value
			} else if node.Array != nil {
				current = node.Array
			} else if node.Index != nil {
				current = node.Index
				adjust += 2
			} else {
				current = node.Name
				if !node.Naked {
					adjust++
				}
			}
		case *syntax.Redirect:
			if !end {
				if node.N != nil {
					current = node.N
				} else {
					return positionOffset(node.OpPos, adjust), true
				}
			} else if node.Hdoc != nil {
				current = node.Hdoc
			} else {
				current = node.Word
			}
		case *syntax.CallExpr:
			if !end {
				if len(node.Assigns) > 0 {
					current = node.Assigns[0]
				} else if len(node.Args) > 0 {
					current = node.Args[0]
				} else {
					return 0, false
				}
			} else if len(node.Args) > 0 {
				current = node.Args[len(node.Args)-1]
			} else if len(node.Assigns) > 0 {
				current = node.Assigns[len(node.Assigns)-1]
			} else {
				return 0, false
			}
		case *syntax.Subshell:
			if end {
				return positionOffset(node.Rparen, adjust+1), true
			}
			return positionOffset(node.Lparen, adjust), true
		case *syntax.Block:
			if end {
				return positionOffset(node.Rbrace, adjust+1), true
			}
			return positionOffset(node.Lbrace, adjust), true
		case *syntax.IfClause:
			if end {
				return positionOffset(node.FiPos, adjust+2), true
			}
			return positionOffset(node.Position, adjust), true
		case *syntax.WhileClause:
			if end {
				return positionOffset(node.DonePos, adjust+4), true
			}
			return positionOffset(node.WhilePos, adjust), true
		case *syntax.ForClause:
			if end {
				return positionOffset(node.DonePos, adjust+4), true
			}
			return positionOffset(node.ForPos, adjust), true
		case *syntax.WordIter:
			if !end {
				current = node.Name
			} else if len(node.Items) > 0 {
				current = node.Items[len(node.Items)-1]
			} else {
				nameEnd, ok := positionOffset(node.Name.ValueEnd, 0), node.Name != nil
				inEnd := positionOffset(node.InPos, 2)
				if ok && nameEnd > inEnd {
					return nameEnd + adjust, true
				}
				return inEnd + adjust, true
			}
		case *syntax.CStyleLoop:
			if end {
				return positionOffset(node.Rparen, adjust+2), true
			}
			return positionOffset(node.Lparen, adjust), true
		case *syntax.BinaryCmd:
			if end {
				current = node.Y
			} else {
				current = node.X
			}
		case *syntax.FuncDecl:
			if end {
				current = node.Body
			} else {
				return positionOffset(node.Position, adjust), true
			}
		case *syntax.Word:
			if len(node.Parts) == 0 {
				return 0, false
			}
			if end {
				current = node.Parts[len(node.Parts)-1]
			} else {
				current = node.Parts[0]
			}
		case *syntax.Lit:
			if end {
				return positionOffset(node.ValueEnd, adjust), true
			}
			return positionOffset(node.ValuePos, adjust), true
		case *syntax.SglQuoted:
			if end {
				return positionOffset(node.Right, adjust+1), true
			}
			return positionOffset(node.Left, adjust), true
		case *syntax.DblQuoted:
			if end {
				return positionOffset(node.Right, adjust+1), true
			}
			return positionOffset(node.Left, adjust), true
		case *syntax.CmdSubst:
			if end {
				return positionOffset(node.Right, adjust+1), true
			}
			return positionOffset(node.Left, adjust), true
		case *syntax.ParamExp:
			if !end {
				if node.Dollar.IsValid() {
					return positionOffset(node.Dollar, adjust), true
				}
				current = node.Param
			} else if !node.Short {
				return positionOffset(node.Rbrace, adjust+1), true
			} else if node.Index != nil {
				current = node.Index
				adjust++
			} else {
				current = node.Param
			}
		case *syntax.ArithmExp:
			if end {
				delta := 2
				if node.Bracket {
					delta = 1
				}
				return positionOffset(node.Right, adjust+delta), true
			}
			return positionOffset(node.Left, adjust), true
		case *syntax.ArithmCmd:
			if end {
				return positionOffset(node.Right, adjust+2), true
			}
			return positionOffset(node.Left, adjust), true
		case *syntax.BinaryArithm:
			if end {
				current = node.Y
			} else {
				current = node.X
			}
		case *syntax.UnaryArithm:
			if node.Post {
				if end {
					return positionOffset(node.OpPos, adjust+2), true
				}
				current = node.X
			} else if end {
				current = node.X
			} else {
				return positionOffset(node.OpPos, adjust), true
			}
		case *syntax.ParenArithm:
			if end {
				return positionOffset(node.Rparen, adjust+1), true
			}
			return positionOffset(node.Lparen, adjust), true
		case *syntax.FlagsArithm:
			if !end {
				current = node.Flags
				adjust--
			} else if node.X != nil {
				current = node.X
			} else {
				current = node.Flags
				adjust++
			}
		case *syntax.CaseClause:
			if end {
				return positionOffset(node.Esac, adjust+4), true
			}
			return positionOffset(node.Case, adjust), true
		case *syntax.CaseItem:
			if !end {
				if len(node.Patterns) == 0 {
					return 0, false
				}
				current = node.Patterns[0]
			} else if node.OpPos.IsValid() {
				return positionOffset(node.OpPos, adjust+len(node.Op.String())), true
			} else if len(node.Last) > 0 {
				comment := node.Last[len(node.Last)-1]
				return positionOffset(comment.Hash, adjust+1+len(comment.Text)), true
			} else if len(node.Stmts) > 0 {
				current = node.Stmts[len(node.Stmts)-1]
			} else {
				return 0, true
			}
		case *syntax.TestClause:
			if end {
				return positionOffset(node.Right, adjust+2), true
			}
			return positionOffset(node.Left, adjust), true
		case *syntax.BinaryTest:
			if end {
				current = node.Y
			} else {
				current = node.X
			}
		case *syntax.UnaryTest:
			if end {
				current = node.X
			} else {
				return positionOffset(node.OpPos, adjust), true
			}
		case *syntax.ParenTest:
			if end {
				return positionOffset(node.Rparen, adjust+1), true
			}
			return positionOffset(node.Lparen, adjust), true
		case *syntax.DeclClause:
			if !end {
				current = node.Variant
			} else if len(node.Args) > 0 {
				current = node.Args[len(node.Args)-1]
			} else {
				current = node.Variant
			}
		case *syntax.ArrayExpr:
			if end {
				return positionOffset(node.Rparen, adjust+1), true
			}
			return positionOffset(node.Lparen, adjust), true
		case *syntax.ArrayElem:
			if !end {
				if node.Index != nil {
					current = node.Index
				} else {
					current = node.Value
				}
			} else if node.Value != nil {
				current = node.Value
			} else {
				start, ok := boundedStart(node.Index, limit)
				return start + adjust + 1, ok
			}
		case *syntax.ExtGlob:
			if end {
				current = node.Pattern
				adjust++
			} else {
				return positionOffset(node.OpPos, adjust), true
			}
		case *syntax.ProcSubst:
			if end {
				return positionOffset(node.Rparen, adjust+1), true
			}
			return positionOffset(node.OpPos, adjust), true
		case *syntax.TimeClause:
			if !end {
				return positionOffset(node.Time, adjust), true
			}
			if node.Stmt == nil {
				return positionOffset(node.Time, adjust+4), true
			}
			current = node.Stmt
		case *syntax.CoprocClause:
			if end {
				current = node.Stmt
			} else {
				return positionOffset(node.Coproc, adjust), true
			}
		case *syntax.LetClause:
			if !end {
				return positionOffset(node.Let, adjust), true
			}
			if len(node.Exprs) == 0 {
				return 0, false
			}
			current = node.Exprs[len(node.Exprs)-1]
		case *syntax.BraceExp:
			if len(node.Elems) == 0 {
				return 0, false
			}
			if end {
				current = node.Elems[len(node.Elems)-1]
				adjust++
			} else {
				current = node.Elems[0]
				adjust--
			}
		case *syntax.TestDecl:
			if end {
				current = node.Body
			} else {
				return positionOffset(node.Position, adjust), true
			}
		default:
			return 0, false
		}
	}
	return 0, false
}

func boundedStart(node syntax.Node, limit int) (int, bool) {
	current := node
	adjust := 0
	for range limit {
		if current == nil || syntaxNodeIsNil(current) {
			return 0, false
		}
		switch node := current.(type) {
		case *syntax.File:
			if len(node.Stmts) > 0 {
				current = node.Stmts[0]
			} else if len(node.Last) > 0 {
				return positionOffset(node.Last[0].Hash, adjust), true
			} else {
				return 0, true
			}
		case *syntax.Comment:
			return positionOffset(node.Hash, adjust), true
		case *syntax.Stmt:
			return positionOffset(node.Position, adjust), true
		case *syntax.Assign:
			if node.Name != nil {
				current = node.Name
			} else {
				current = node.Value
			}
		case *syntax.Redirect:
			if node.N != nil {
				current = node.N
			} else {
				return positionOffset(node.OpPos, adjust), true
			}
		case *syntax.CallExpr:
			if len(node.Assigns) > 0 {
				current = node.Assigns[0]
			} else if len(node.Args) > 0 {
				current = node.Args[0]
			} else {
				return 0, false
			}
		case *syntax.Subshell:
			return positionOffset(node.Lparen, adjust), true
		case *syntax.Block:
			return positionOffset(node.Lbrace, adjust), true
		case *syntax.IfClause:
			return positionOffset(node.Position, adjust), true
		case *syntax.WhileClause:
			return positionOffset(node.WhilePos, adjust), true
		case *syntax.ForClause:
			return positionOffset(node.ForPos, adjust), true
		case *syntax.WordIter:
			current = node.Name
		case *syntax.CStyleLoop:
			return positionOffset(node.Lparen, adjust), true
		case *syntax.BinaryCmd:
			current = node.X
		case *syntax.FuncDecl:
			return positionOffset(node.Position, adjust), true
		case *syntax.Word:
			if len(node.Parts) == 0 {
				return 0, false
			}
			current = node.Parts[0]
		case *syntax.Lit:
			return positionOffset(node.ValuePos, adjust), true
		case *syntax.SglQuoted:
			return positionOffset(node.Left, adjust), true
		case *syntax.DblQuoted:
			return positionOffset(node.Left, adjust), true
		case *syntax.CmdSubst:
			return positionOffset(node.Left, adjust), true
		case *syntax.ParamExp:
			if node.Dollar.IsValid() {
				return positionOffset(node.Dollar, adjust), true
			}
			current = node.Param
		case *syntax.ArithmExp:
			return positionOffset(node.Left, adjust), true
		case *syntax.ArithmCmd:
			return positionOffset(node.Left, adjust), true
		case *syntax.BinaryArithm:
			current = node.X
		case *syntax.UnaryArithm:
			if node.Post {
				current = node.X
			} else {
				return positionOffset(node.OpPos, adjust), true
			}
		case *syntax.ParenArithm:
			return positionOffset(node.Lparen, adjust), true
		case *syntax.FlagsArithm:
			current = node.Flags
			adjust--
		case *syntax.CaseClause:
			return positionOffset(node.Case, adjust), true
		case *syntax.CaseItem:
			if len(node.Patterns) == 0 {
				return 0, false
			}
			current = node.Patterns[0]
		case *syntax.TestClause:
			return positionOffset(node.Left, adjust), true
		case *syntax.BinaryTest:
			current = node.X
		case *syntax.UnaryTest:
			return positionOffset(node.OpPos, adjust), true
		case *syntax.ParenTest:
			return positionOffset(node.Lparen, adjust), true
		case *syntax.DeclClause:
			current = node.Variant
		case *syntax.ArrayExpr:
			return positionOffset(node.Lparen, adjust), true
		case *syntax.ArrayElem:
			if node.Index != nil {
				current = node.Index
			} else {
				current = node.Value
			}
		case *syntax.ExtGlob:
			return positionOffset(node.OpPos, adjust), true
		case *syntax.ProcSubst:
			return positionOffset(node.OpPos, adjust), true
		case *syntax.TimeClause:
			return positionOffset(node.Time, adjust), true
		case *syntax.CoprocClause:
			return positionOffset(node.Coproc, adjust), true
		case *syntax.LetClause:
			return positionOffset(node.Let, adjust), true
		case *syntax.BraceExp:
			if len(node.Elems) == 0 {
				return 0, false
			}
			current = node.Elems[0]
			adjust--
		case *syntax.TestDecl:
			return positionOffset(node.Position, adjust), true
		default:
			return 0, false
		}
	}
	return 0, false
}

func positionOffset(pos syntax.Pos, adjust int) int {
	if !pos.IsValid() {
		return 0
	}
	return max(int(pos.Offset())+adjust, 0)
}

func commandRole(command syntax.Command) string {
	switch command.(type) {
	case *syntax.CallExpr:
		return "argv"
	case *syntax.BinaryCmd:
		return "operand-statements"
	case *syntax.Block, *syntax.Subshell:
		return "body-statements"
	case *syntax.FuncDecl:
		return "body"
	case *syntax.IfClause, *syntax.WhileClause:
		return "condition-and-body"
	case *syntax.ForClause:
		return "loop-body-and-selector"
	case *syntax.CaseClause:
		return "selector-word"
	default:
		return "command"
	}
}

func redirectRole(redirect *syntax.Redirect) string {
	if redirect.Op != syntax.Hdoc && redirect.Op != syntax.DashHdoc {
		return "target-word-expansion"
	}
	if heredocDelimiterQuoted(redirect.Word) {
		return "quoted-heredoc-body"
	}
	return "unquoted-heredoc-body"
}

func heredocDelimiterQuoted(word *syntax.Word) bool {
	if word == nil {
		return false
	}
	for _, part := range word.Parts {
		if part == nil || syntaxNodeIsNil(part) {
			continue
		}
		switch part := part.(type) {
		case *syntax.Lit:
			if strings.Contains(part.Value, `\`) {
				return true
			}
		case *syntax.SglQuoted, *syntax.DblQuoted:
			return true
		default:
			return true
		}
	}
	return false
}

func syntaxNodeName(node syntax.Node) (string, bool) {
	switch node := node.(type) {
	case *syntax.File:
		return knownNodeName(node, "File")
	case *syntax.Comment:
		return knownNodeName(node, "Comment")
	case *syntax.Stmt:
		return knownNodeName(node, "Stmt")
	case *syntax.Assign:
		return knownNodeName(node, "Assign")
	case *syntax.Redirect:
		return knownNodeName(node, "Redirect")
	case *syntax.CallExpr:
		return knownNodeName(node, "CallExpr")
	case *syntax.Subshell:
		return knownNodeName(node, "Subshell")
	case *syntax.Block:
		return knownNodeName(node, "Block")
	case *syntax.IfClause:
		return knownNodeName(node, "IfClause")
	case *syntax.WhileClause:
		return knownNodeName(node, "WhileClause")
	case *syntax.ForClause:
		return knownNodeName(node, "ForClause")
	case *syntax.WordIter:
		return knownNodeName(node, "WordIter")
	case *syntax.CStyleLoop:
		return knownNodeName(node, "CStyleLoop")
	case *syntax.BinaryCmd:
		return knownNodeName(node, "BinaryCmd")
	case *syntax.FuncDecl:
		return knownNodeName(node, "FuncDecl")
	case *syntax.Word:
		return knownNodeName(node, "Word")
	case *syntax.Lit:
		return knownNodeName(node, "Lit")
	case *syntax.SglQuoted:
		return knownNodeName(node, "SglQuoted")
	case *syntax.DblQuoted:
		return knownNodeName(node, "DblQuoted")
	case *syntax.CmdSubst:
		return knownNodeName(node, "CmdSubst")
	case *syntax.ParamExp:
		return knownNodeName(node, "ParamExp")
	case *syntax.ArithmExp:
		return knownNodeName(node, "ArithmExp")
	case *syntax.ArithmCmd:
		return knownNodeName(node, "ArithmCmd")
	case *syntax.BinaryArithm:
		return knownNodeName(node, "BinaryArithm")
	case *syntax.UnaryArithm:
		return knownNodeName(node, "UnaryArithm")
	case *syntax.ParenArithm:
		return knownNodeName(node, "ParenArithm")
	case *syntax.FlagsArithm:
		return knownNodeName(node, "FlagsArithm")
	case *syntax.CaseClause:
		return knownNodeName(node, "CaseClause")
	case *syntax.CaseItem:
		return knownNodeName(node, "CaseItem")
	case *syntax.TestClause:
		return knownNodeName(node, "TestClause")
	case *syntax.BinaryTest:
		return knownNodeName(node, "BinaryTest")
	case *syntax.UnaryTest:
		return knownNodeName(node, "UnaryTest")
	case *syntax.ParenTest:
		return knownNodeName(node, "ParenTest")
	case *syntax.DeclClause:
		return knownNodeName(node, "DeclClause")
	case *syntax.ArrayExpr:
		return knownNodeName(node, "ArrayExpr")
	case *syntax.ArrayElem:
		return knownNodeName(node, "ArrayElem")
	case *syntax.ExtGlob:
		return knownNodeName(node, "ExtGlob")
	case *syntax.ProcSubst:
		return knownNodeName(node, "ProcSubst")
	case *syntax.TimeClause:
		return knownNodeName(node, "TimeClause")
	case *syntax.CoprocClause:
		return knownNodeName(node, "CoprocClause")
	case *syntax.LetClause:
		return knownNodeName(node, "LetClause")
	case *syntax.BraceExp:
		return knownNodeName(node, "BraceExp")
	case *syntax.TestDecl:
		return knownNodeName(node, "TestDecl")
	default:
		return "", false
	}
}

func syntaxNodeIsNil(node syntax.Node) bool {
	name, known := syntaxNodeName(node)
	return known && name == ""
}

func knownNodeName[T any](node *T, name string) (string, bool) {
	if node == nil {
		return "", true
	}
	return name, true
}
