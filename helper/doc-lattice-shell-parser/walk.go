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
	workLimit  int
	depthCap   int
	eventCap   int
	stop       bool
	certified  map[syntax.Node]certificationState
}

func walk(stmts []*syntax.Stmt, src string) (sites []commandSite, refusals []rawRefusal, work int) {
	w := newWalker(src)
	for _, stmt := range stmts {
		if !w.certifyTree(stmt, 1) {
			return w.sites, w.refusals, w.work
		}
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
		certified: make(map[syntax.Node]certificationState),
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
	if !w.consumeCertified(node, depth) {
		return
	}
	if !known {
		w.requestTerminal(node, "unsupported-construct", false)
		return
	}
	rule, ok := lookupCertifiedConstruct(name, role)
	if !ok {
		w.requestTerminal(node, "unsupported-construct", false)
		return
	}
	switch rule.disposition {
	case "traverse":
		w.traverse(node, role, depth)
	case "ignore":
		return
	case "refuse":
		w.requestRefusal(node, rule.code)
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
	if !supportedRedirectOperator(redirect.Op) {
		w.requestRefusal(redirect, "redirect-unsupported")
		return
	}
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
	for _, part := range word.Parts {
		if !w.enterChild() {
			return
		}
		w.consumeWordPart(part, depth+1, false)
		if w.stop {
			return
		}
	}
}

func (w *walker) consumeWordPart(part syntax.WordPart, depth int, quoted bool) {
	if part == nil || w.stop || syntaxNodeIsNil(part) {
		return
	}
	switch part := part.(type) {
	case *syntax.Lit:
		w.visit(part, depth)
	case *syntax.SglQuoted:
		if !w.visit(part, depth) {
			return
		}
		if part.Dollar && decodeANSIValue(part.Value).unsafe {
			w.requestTerminal(part, "unsupported-construct", false)
		}
	case *syntax.DblQuoted:
		if !w.visit(part, depth) {
			return
		}
		for _, nested := range part.Parts {
			if !w.enterChild() {
				return
			}
			w.consumeWordPart(nested, depth+1, true)
			if w.stop {
				return
			}
		}
	case *syntax.CmdSubst:
		w.dispatch(part, "body-statements", depth)
	case *syntax.ProcSubst:
		w.dispatch(part, "word-part", depth)
	case *syntax.ExtGlob:
		classification := classifyExtGlob(part, w.src)
		if classification.unsafe {
			if w.visit(part, depth) {
				w.requestTerminal(part, "unsupported-construct", false)
			}
		} else if classification.execution || part == nil {
			w.dispatch(part, "opaque-execution", depth)
		} else if w.visit(part, depth) {
			w.consumeNestedExecution(part, depth, quoted)
		}
	default:
		if w.visit(part, depth) {
			w.consumeNestedExecution(part, depth, quoted)
		}
	}
}

func (w *walker) consumeNestedExecution(node syntax.Node, depth int, quoted bool) {
	w.consumeNestedExecutionIn(node, depth, false, quoted, nil, "")
}

func (w *walker) consumeNestedExecutionIn(node syntax.Node, depth int, parameterOperand, quoted bool, owner syntax.Node, ownerCode string) bool {
	if owner == nil {
		if rule, ok := localConstructRefusal(node); ok {
			owner = node
			ownerCode = rule.code
		}
	}
	next := 0
	for {
		child, ok := nextStructuralChild(node, &next)
		if !ok {
			return false
		}
		if !w.enterChild() {
			return w.stop
		}
		childIsParameterOperand := false
		if parameter, ok := node.(*syntax.ParamExp); ok {
			childIsParameterOperand = parameterWordOperand(parameter, child)
		}
		childQuoted := quoted
		if _, ok := child.(*syntax.DblQuoted); ok {
			childQuoted = true
		}
		switch child := child.(type) {
		case *syntax.CmdSubst:
			if owner != nil {
				w.requestRefusal(owner, ownerCode)
				return true
			}
			w.dispatch(child, "body-statements", depth+1)
		case *syntax.ProcSubst:
			w.dispatch(child, "word-part", depth+1)
		case *syntax.Lit:
			if parameterOperand && !quoted && literalHasProcessSubstitution(child, w.src) {
				w.dispatch(child, "parameter-operand-process-substitution", depth+1)
			}
		default:
			if w.consumeNestedExecutionIn(child, depth+1, childIsParameterOperand, childQuoted, owner, ownerCode) {
				return true
			}
		}
		if w.stop {
			return true
		}
	}
}

func lookupCertifiedConstruct(name, role string) (constructRule, bool) {
	rule, ok := certifiedConstructs[constructKey{node: name, role: role}]
	if !ok {
		rule, ok = certifiedConstructs[constructKey{node: name, role: "*"}]
	}
	return rule, ok
}

func localConstructRefusal(node syntax.Node) (constructRule, bool) {
	name, known := syntaxNodeName(node)
	if !known || name == "" {
		return constructRule{}, false
	}
	rule, ok := lookupCertifiedConstruct(name, "nested-word-execution")
	return rule, ok && rule.disposition == "refuse" && reasonScopes[rule.code] == "subtree-local"
}

func parameterWordOperand(parameter *syntax.ParamExp, child syntax.Node) bool {
	word, ok := child.(*syntax.Word)
	if !ok || word == nil || parameter == nil {
		return false
	}
	if parameter.Exp != nil && word == parameter.Exp.Word {
		return true
	}
	return parameter.Repl != nil && (word == parameter.Repl.Orig || word == parameter.Repl.With)
}

func literalHasProcessSubstitution(literal *syntax.Lit, src string) bool {
	if literal == nil || !literal.Pos().IsValid() || !literal.End().IsValid() {
		return true
	}
	start, end := int(literal.Pos().Offset()), int(literal.End().Offset())
	if start < 0 || start > end || end > len(src) {
		return true
	}
	raw := src[start:end]
	var opener byte
	for index := 0; index < len(raw); index++ {
		if raw[index] == '\\' {
			if index+1 < len(raw) && raw[index+1] == '\n' {
				index++
				continue
			}
			opener = 0
			index++
			continue
		}
		if raw[index] == '(' && opener != 0 {
			return true
		}
		if raw[index] == '<' || raw[index] == '>' {
			opener = raw[index]
		} else {
			opener = 0
		}
	}
	return false
}

func (w *walker) enterChild() bool {
	if w.stop {
		return false
	}
	w.childSteps++
	return true
}

func (w *walker) visit(node syntax.Node, depth int) bool {
	return w.consumeCertified(node, depth)
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
	if reasonScopes[code] != "terminal" {
		code = "unsupported-construct"
	}
	start, end := 0, 0
	if !pointSpan {
		start, end = w.terminalNodeSpan(node)
	}
	w.appendTerminalRefusal(start, end, code)
}

func (w *walker) requestRefusal(node syntax.Node, code string) {
	switch reasonScopes[code] {
	case "terminal":
		w.requestTerminal(node, code, false)
	case "subtree-local":
		w.requestLocalRefusal(node, code)
	default:
		w.requestTerminal(node, "unsupported-construct", false)
	}
}

func (w *walker) requestLocalRefusal(node syntax.Node, code string) {
	if w.stop {
		return
	}
	if reasonScopes[code] != "subtree-local" {
		w.requestTerminal(node, "unsupported-construct", false)
		return
	}
	if w.events >= w.eventCap {
		w.requestTerminal(node, "event-cap", false)
		return
	}
	if w.work+1 > w.workLimit {
		w.requestTerminal(node, "work-cap", false)
		return
	}
	start, end, ok := w.completeNodeSpan(node)
	if !ok {
		w.requestTerminal(node, "unsupported-construct", false)
		return
	}
	w.refusals = append(w.refusals, rawRefusal{code: code, startByte: start, endByte: end})
	w.events++
	w.work++
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
	start, end, ok := w.completeNodeSpan(node)
	if !ok {
		return 0, 0
	}
	return start, end
}

func (w *walker) terminalNodeSpan(node syntax.Node) (int, int) {
	// A terminal may be requested after work is exhausted. Recover its span
	// with a separate fixed-depth budget so the final event can remain in source order.
	start, end, ok := w.completeNodeSpanWithin(node, visitorDepthCap+1)
	if !ok {
		return 0, 0
	}
	return start, end
}

func (w *walker) completeNodeSpan(node syntax.Node) (int, int, bool) {
	limit := min(max(w.depthCap+1, 1), visitorDepthCap+1)
	limit = min(limit, max(w.workLimit-w.work+1, 1))
	return w.completeNodeSpanWithin(node, limit)
}

func (w *walker) completeNodeSpanWithin(node syntax.Node, limit int) (int, int, bool) {
	if node == nil || syntaxNodeIsNil(node) || limit < 1 {
		return 0, 0, false
	}
	start, startOK := boundedStart(node, limit)
	end, endOK := boundedEnd(node, limit)
	if !startOK || !endOK {
		return 0, 0, false
	}
	if start < 0 || start > end || end > len(w.src) {
		return 0, 0, false
	}
	return start, end, true
}

type certificationFrame struct {
	node  syntax.Node
	depth int
	next  int
}

type certificationState uint8

const (
	certificationUnseen certificationState = iota
	certificationVisiting
	certificationComplete
)

// certifyTree claims one AST tree, rejecting cycles and nodes owned by another edge or root.
func (w *walker) certifyTree(root syntax.Node, depth int) bool {
	if w.stop {
		return false
	}
	if root == nil || syntaxNodeIsNil(root) {
		w.requestTerminal(nil, "unsupported-construct", true)
		return false
	}
	stack := []certificationFrame{{node: root, depth: depth, next: -1}}
	for len(stack) > 0 {
		if w.stop {
			return false
		}
		frame := &stack[len(stack)-1]
		if frame.next < 0 {
			if w.nodeCertification(frame.node) != certificationUnseen {
				w.requestTerminal(frame.node, "unsupported-construct", true)
				return false
			}
			if !w.chargeNode(frame.node, frame.depth) {
				return false
			}
			w.setNodeCertification(frame.node, certificationVisiting)
			if !localStructureValid(frame.node) {
				w.requestTerminal(frame.node, "unsupported-construct", true)
				return false
			}
			frame.next = 0
		}
		child, ok := nextStructuralChild(frame.node, &frame.next)
		if !ok {
			w.setNodeCertification(frame.node, certificationComplete)
			stack = stack[:len(stack)-1]
			continue
		}
		if child == nil || syntaxNodeIsNil(child) {
			w.requestTerminal(frame.node, "unsupported-construct", true)
			return false
		}
		stack = append(stack, certificationFrame{node: child, depth: frame.depth + 1, next: -1})
	}
	return true
}

// consumeCertified separates semantic reuse from structural ownership checks.
func (w *walker) consumeCertified(node syntax.Node, depth int) bool {
	if w.stop {
		return false
	}
	switch w.nodeCertification(node) {
	case certificationComplete:
		if depth > w.depthCap {
			w.requestTerminal(node, "depth-cap", false)
			return false
		}
		return true
	case certificationVisiting:
		w.requestTerminal(node, "unsupported-construct", true)
		return false
	default:
		return w.certifyTree(node, depth)
	}
}

func (w *walker) chargeNode(node syntax.Node, depth int) bool {
	if w.events >= w.eventCap {
		w.requestTerminal(node, "event-cap", false)
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

func (w *walker) nodeCertification(node syntax.Node) certificationState {
	name, known := syntaxNodeName(node)
	if !known || name == "" {
		return certificationUnseen
	}
	return w.certified[node]
}

func (w *walker) setNodeCertification(node syntax.Node, state certificationState) {
	if name, known := syntaxNodeName(node); known && name != "" {
		w.certified[node] = state
	}
}

func localStructureValid(node syntax.Node) bool {
	present := func(child syntax.Node) bool {
		return child != nil && !syntaxNodeIsNil(child)
	}
	switch node := node.(type) {
	case *syntax.Assign:
		return node.Name != nil || node.Value != nil
	case *syntax.Redirect:
		return node.Word != nil
	case *syntax.CallExpr:
		return len(node.Assigns) > 0 || len(node.Args) > 0
	case *syntax.FuncDecl:
		return node.Body != nil
	case *syntax.ForClause:
		return present(node.Loop)
	case *syntax.WordIter:
		return node.Name != nil
	case *syntax.BinaryCmd:
		return node.X != nil && node.Y != nil
	case *syntax.Word:
		return len(node.Parts) > 0
	case *syntax.ParamExp:
		paramPresent := present(node.Param)
		nestedPresent := present(node.NestedParam)
		indexPresent := present(node.Index)
		if node.NestedParam != nil && !nestedPresent || node.Index != nil && !indexPresent ||
			paramPresent && nestedPresent {
			return false
		}
		for _, modifier := range node.Modifiers {
			if !present(modifier) {
				return false
			}
		}
		if node.Slice != nil {
			if node.Slice.Offset != nil && !present(node.Slice.Offset) ||
				node.Slice.Length != nil && !present(node.Slice.Length) {
				return false
			}
		}
		if node.Short {
			return !node.Rbrace.IsValid() && paramPresent && !nestedPresent &&
				(node.Dollar.IsValid() || indexPresent)
		}
		if !node.Dollar.IsValid() || !node.Rbrace.IsValid() {
			return false
		}
		payloadPresent := present(node.Flags) || indexPresent || len(node.Modifiers) > 0 ||
			node.Slice != nil || node.Repl != nil || node.Names != 0 || node.Exp != nil
		return paramPresent || nestedPresent || payloadPresent
	case *syntax.ArithmExp:
		return present(node.X)
	case *syntax.ArithmCmd:
		return present(node.X)
	case *syntax.BinaryArithm:
		return present(node.X) && present(node.Y)
	case *syntax.UnaryArithm:
		return present(node.X)
	case *syntax.ParenArithm:
		return present(node.X)
	case *syntax.FlagsArithm:
		return node.Flags != nil
	case *syntax.CaseClause:
		return node.Word != nil
	case *syntax.CaseItem:
		return len(node.Patterns) > 0
	case *syntax.TestClause:
		return present(node.X)
	case *syntax.BinaryTest:
		return present(node.X) && present(node.Y)
	case *syntax.UnaryTest:
		return present(node.X)
	case *syntax.ParenTest:
		return present(node.X)
	case *syntax.DeclClause:
		return node.Variant != nil
	case *syntax.ArrayElem:
		return present(node.Index) || node.Value != nil
	case *syntax.ExtGlob:
		return node.Pattern != nil
	case *syntax.CoprocClause:
		return node.Stmt != nil
	case *syntax.LetClause:
		return len(node.Exprs) > 0
	case *syntax.BraceExp:
		return len(node.Elems) > 0
	case *syntax.TestDecl:
		return node.Description != nil && node.Body != nil
	}
	return true
}

// nextStructuralChild enumerates one edge at a time without materializing child slices.
func nextStructuralChild(node syntax.Node, next *int) (syntax.Node, bool) {
	for {
		index := *next
		*next = index + 1
		switch node := node.(type) {
		case *syntax.File:
			if index < len(node.Stmts) {
				return node.Stmts[index], true
			}
			index -= len(node.Stmts)
			if index < len(node.Last) {
				return &node.Last[index], true
			}
		case *syntax.Stmt:
			if index < len(node.Comments) {
				return &node.Comments[index], true
			}
			index -= len(node.Comments)
			if index == 0 {
				if node.Cmd != nil {
					return node.Cmd, true
				}
				continue
			}
			if index-1 < len(node.Redirs) {
				return node.Redirs[index-1], true
			}
		case *syntax.Assign:
			switch index {
			case 0:
				if node.Name != nil {
					return node.Name, true
				}
			case 1:
				if node.Index != nil {
					return node.Index, true
				}
			case 2:
				if node.Array != nil {
					return node.Array, true
				}
			case 3:
				if node.Value != nil {
					return node.Value, true
				}
			default:
				return nil, false
			}
			continue
		case *syntax.Redirect:
			switch index {
			case 0:
				if node.N != nil {
					return node.N, true
				}
			case 1:
				if node.Word != nil {
					return node.Word, true
				}
			case 2:
				if node.Hdoc != nil {
					return node.Hdoc, true
				}
			default:
				return nil, false
			}
			continue
		case *syntax.CallExpr:
			if index < len(node.Assigns) {
				return node.Assigns[index], true
			}
			index -= len(node.Assigns)
			if index < len(node.Args) {
				return node.Args[index], true
			}
		case *syntax.Subshell:
			if index < len(node.Stmts) {
				return node.Stmts[index], true
			}
			index -= len(node.Stmts)
			if index < len(node.Last) {
				return &node.Last[index], true
			}
		case *syntax.Block:
			if index < len(node.Stmts) {
				return node.Stmts[index], true
			}
			index -= len(node.Stmts)
			if index < len(node.Last) {
				return &node.Last[index], true
			}
		case *syntax.IfClause:
			if index < len(node.Cond) {
				return node.Cond[index], true
			}
			index -= len(node.Cond)
			if index < len(node.CondLast) {
				return &node.CondLast[index], true
			}
			index -= len(node.CondLast)
			if index < len(node.Then) {
				return node.Then[index], true
			}
			index -= len(node.Then)
			if index < len(node.ThenLast) {
				return &node.ThenLast[index], true
			}
			index -= len(node.ThenLast)
			if index == 0 {
				if node.Else != nil {
					return node.Else, true
				}
				continue
			}
			index--
			if index < len(node.Last) {
				return &node.Last[index], true
			}
		case *syntax.WhileClause:
			if index < len(node.Cond) {
				return node.Cond[index], true
			}
			index -= len(node.Cond)
			if index < len(node.CondLast) {
				return &node.CondLast[index], true
			}
			index -= len(node.CondLast)
			if index < len(node.Do) {
				return node.Do[index], true
			}
			index -= len(node.Do)
			if index < len(node.DoLast) {
				return &node.DoLast[index], true
			}
		case *syntax.ForClause:
			if index == 0 {
				if node.Loop != nil {
					return node.Loop, true
				}
				continue
			}
			if index-1 < len(node.Do) {
				return node.Do[index-1], true
			}
			index -= 1 + len(node.Do)
			if index < len(node.DoLast) {
				return &node.DoLast[index], true
			}
		case *syntax.WordIter:
			if index == 0 {
				return node.Name, true
			}
			if index-1 < len(node.Items) {
				return node.Items[index-1], true
			}
		case *syntax.CStyleLoop:
			var child syntax.Node
			switch index {
			case 0:
				child = node.Init
			case 1:
				child = node.Cond
			case 2:
				child = node.Post
			default:
				return nil, false
			}
			if child != nil {
				return child, true
			}
			continue
		case *syntax.BinaryCmd:
			if index == 0 {
				return node.X, true
			}
			if index == 1 {
				return node.Y, true
			}
		case *syntax.FuncDecl:
			offset := 0
			if node.Name != nil {
				if index == 0 {
					return node.Name, true
				}
				offset = 1
			}
			nameIndex := index - offset
			if nameIndex >= 0 && nameIndex < len(node.Names) {
				return node.Names[nameIndex], true
			}
			if index == offset+len(node.Names) {
				return node.Body, true
			}
		case *syntax.Word:
			if index < len(node.Parts) {
				return node.Parts[index], true
			}
		case *syntax.DblQuoted:
			if index < len(node.Parts) {
				return node.Parts[index], true
			}
		case *syntax.CmdSubst:
			if index < len(node.Stmts) {
				return node.Stmts[index], true
			}
			index -= len(node.Stmts)
			if index < len(node.Last) {
				return &node.Last[index], true
			}
		case *syntax.ParamExp:
			switch index {
			case 0:
				if node.Flags != nil {
					return node.Flags, true
				}
			case 1:
				if node.Param != nil {
					return node.Param, true
				}
			case 2:
				if node.NestedParam != nil {
					return node.NestedParam, true
				}
			case 3:
				if node.Index != nil {
					return node.Index, true
				}
			default:
				modifierIndex := index - 4
				if modifierIndex < len(node.Modifiers) {
					return node.Modifiers[modifierIndex], true
				}
				index = modifierIndex - len(node.Modifiers)
				switch index {
				case 0:
					if node.Slice != nil && node.Slice.Offset != nil {
						return node.Slice.Offset, true
					}
				case 1:
					if node.Slice != nil && node.Slice.Length != nil {
						return node.Slice.Length, true
					}
				case 2:
					if node.Repl != nil && node.Repl.Orig != nil {
						return node.Repl.Orig, true
					}
				case 3:
					if node.Repl != nil && node.Repl.With != nil {
						return node.Repl.With, true
					}
				case 4:
					if node.Exp != nil && node.Exp.Word != nil {
						return node.Exp.Word, true
					}
				default:
					return nil, false
				}
			}
			continue
		case *syntax.ArithmExp:
			if index == 0 && node.X != nil {
				return node.X, true
			}
		case *syntax.ArithmCmd:
			if index == 0 && node.X != nil {
				return node.X, true
			}
		case *syntax.BinaryArithm:
			if index == 0 {
				return node.X, true
			}
			if index == 1 {
				return node.Y, true
			}
		case *syntax.UnaryArithm:
			if index == 0 {
				return node.X, true
			}
		case *syntax.ParenArithm:
			if index == 0 && node.X != nil {
				return node.X, true
			}
		case *syntax.FlagsArithm:
			if index == 0 {
				return node.Flags, true
			}
			if index == 1 && node.X != nil {
				return node.X, true
			}
		case *syntax.CaseClause:
			if index == 0 {
				return node.Word, true
			}
			if index-1 < len(node.Items) {
				return node.Items[index-1], true
			}
			index -= 1 + len(node.Items)
			if index < len(node.Last) {
				return &node.Last[index], true
			}
		case *syntax.CaseItem:
			if index < len(node.Comments) {
				return &node.Comments[index], true
			}
			index -= len(node.Comments)
			if index < len(node.Patterns) {
				return node.Patterns[index], true
			}
			index -= len(node.Patterns)
			if index < len(node.Stmts) {
				return node.Stmts[index], true
			}
			index -= len(node.Stmts)
			if index < len(node.Last) {
				return &node.Last[index], true
			}
		case *syntax.TestClause:
			if index == 0 && node.X != nil {
				return node.X, true
			}
		case *syntax.BinaryTest:
			if index == 0 {
				return node.X, true
			}
			if index == 1 {
				return node.Y, true
			}
		case *syntax.UnaryTest:
			if index == 0 {
				return node.X, true
			}
		case *syntax.ParenTest:
			if index == 0 && node.X != nil {
				return node.X, true
			}
		case *syntax.DeclClause:
			if index == 0 {
				return node.Variant, true
			}
			if index-1 < len(node.Args) {
				return node.Args[index-1], true
			}
		case *syntax.ArrayExpr:
			if index < len(node.Elems) {
				return node.Elems[index], true
			}
			index -= len(node.Elems)
			if index < len(node.Last) {
				return &node.Last[index], true
			}
		case *syntax.ArrayElem:
			if index == 0 && node.Index != nil {
				return node.Index, true
			}
			if index == 1 && node.Value != nil {
				return node.Value, true
			}
			if index < 2 {
				continue
			}
			if index-2 >= 0 && index-2 < len(node.Comments) {
				return &node.Comments[index-2], true
			}
		case *syntax.ExtGlob:
			if index == 0 {
				return node.Pattern, true
			}
		case *syntax.ProcSubst:
			if index < len(node.Stmts) {
				return node.Stmts[index], true
			}
			index -= len(node.Stmts)
			if index < len(node.Last) {
				return &node.Last[index], true
			}
		case *syntax.TimeClause:
			if index == 0 && node.Stmt != nil {
				return node.Stmt, true
			}
		case *syntax.CoprocClause:
			if index == 0 && node.Name != nil {
				return node.Name, true
			}
			if index == 0 {
				continue
			}
			if index == 1 {
				return node.Stmt, true
			}
		case *syntax.LetClause:
			if index < len(node.Exprs) {
				return node.Exprs[index], true
			}
		case *syntax.BraceExp:
			if index < len(node.Elems) {
				return node.Elems[index], true
			}
		case *syntax.TestDecl:
			if index == 0 && node.Description != nil {
				return node.Description, true
			}
			if index == 0 {
				continue
			}
			if index == 1 {
				return node.Body, true
			}
		default:
			return nil, false
		}
		return nil, false
	}
}

type endCandidate struct {
	node     syntax.Node
	adjust   int
	useStart bool
}

// boundedEnd resolves recursive end methods with an explicit candidate stack.
func boundedEnd(node syntax.Node, limit int) (int, bool) {
	stack := []endCandidate{{node: node}}
	best := 0
	resolved := false
	positionsValid := true
	for steps := 0; len(stack) > 0; steps++ {
		if steps >= limit || !positionsValid {
			return 0, false
		}
		candidate := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		if candidate.node == nil || syntaxNodeIsNil(candidate.node) {
			return 0, false
		}
		if candidate.useStart {
			start, ok := boundedStart(candidate.node, limit-steps)
			if !ok {
				return 0, false
			}
			result, ok := checkedAddInt(start, candidate.adjust)
			if !ok {
				return 0, false
			}
			best = max(best, result)
			resolved = true
			continue
		}
		addResult := func(pos syntax.Pos, adjust int) {
			combined, ok := checkedAddInt(candidate.adjust, adjust)
			if !ok {
				positionsValid = false
				return
			}
			result, ok := positionOffset(pos, combined)
			if !ok {
				positionsValid = false
				return
			}
			best = max(best, result)
			resolved = true
		}
		addTextEnd := func(pos syntax.Pos, text string) {
			delta, ok := checkedAddInt(1, len(text))
			if !ok {
				positionsValid = false
				return
			}
			addResult(pos, delta)
		}
		push := func(child syntax.Node, adjust int) {
			combined, ok := checkedAddInt(candidate.adjust, adjust)
			if !ok {
				positionsValid = false
				return
			}
			stack = append(stack, endCandidate{node: child, adjust: combined})
		}
		switch node := candidate.node.(type) {
		case *syntax.File:
			if len(node.Last) > 0 {
				comment := node.Last[len(node.Last)-1]
				addTextEnd(comment.Hash, comment.Text)
			} else if len(node.Stmts) > 0 {
				stmt := node.Stmts[len(node.Stmts)-1]
				push(stmt, 0)
				if stmt != nil && len(stmt.Comments) > 0 {
					push(&stmt.Comments[0], 0)
				}
			} else {
				resolved = true
			}
		case *syntax.Comment:
			addTextEnd(node.Hash, node.Text)
		case *syntax.Stmt:
			if node.Semicolon.IsValid() {
				delta := 1
				if node.Coprocess || node.Disown {
					delta++
				}
				addResult(node.Semicolon, delta)
				continue
			}
			delta := 0
			if node.Negated {
				delta = 1
			}
			addResult(node.Position, delta)
			if node.Cmd != nil {
				push(node.Cmd, 0)
			}
			if len(node.Redirs) > 0 {
				push(node.Redirs[len(node.Redirs)-1], 0)
			}
		case *syntax.Assign:
			if node.Value != nil {
				push(node.Value, 0)
			} else if node.Array != nil {
				push(node.Array, 0)
			} else if node.Index != nil {
				push(node.Index, 2)
			} else if node.Name != nil {
				delta := 0
				if !node.Naked {
					delta = 1
				}
				push(node.Name, delta)
			} else {
				return 0, false
			}
		case *syntax.Redirect:
			if node.Hdoc != nil {
				push(node.Hdoc, 0)
			} else {
				push(node.Word, 0)
			}
		case *syntax.CallExpr:
			if len(node.Args) > 0 {
				push(node.Args[len(node.Args)-1], 0)
			} else if len(node.Assigns) > 0 {
				push(node.Assigns[len(node.Assigns)-1], 0)
			} else {
				return 0, false
			}
		case *syntax.Subshell:
			addResult(node.Rparen, 1)
		case *syntax.Block:
			addResult(node.Rbrace, 1)
		case *syntax.IfClause:
			addResult(node.FiPos, 2)
		case *syntax.WhileClause:
			addResult(node.DonePos, 4)
		case *syntax.ForClause:
			addResult(node.DonePos, 4)
		case *syntax.WordIter:
			if len(node.Items) > 0 {
				push(node.Items[len(node.Items)-1], 0)
			} else {
				if node.Name == nil {
					return 0, false
				}
				addResult(node.Name.ValueEnd, 0)
				if node.InPos.IsValid() {
					addResult(node.InPos, 2)
				}
			}
		case *syntax.CStyleLoop:
			addResult(node.Rparen, 2)
		case *syntax.BinaryCmd:
			push(node.Y, 0)
		case *syntax.FuncDecl:
			push(node.Body, 0)
		case *syntax.Word:
			if len(node.Parts) == 0 {
				return 0, false
			}
			push(node.Parts[len(node.Parts)-1], 0)
		case *syntax.Lit:
			addResult(node.ValueEnd, 0)
		case *syntax.SglQuoted:
			addResult(node.Right, 1)
		case *syntax.DblQuoted:
			addResult(node.Right, 1)
		case *syntax.CmdSubst:
			addResult(node.Right, 1)
		case *syntax.ParamExp:
			if !node.Short {
				addResult(node.Rbrace, 1)
			} else if node.Index != nil {
				push(node.Index, 1)
			} else {
				push(node.Param, 0)
			}
		case *syntax.ArithmExp:
			delta := 2
			if node.Bracket {
				delta = 1
			}
			addResult(node.Right, delta)
		case *syntax.ArithmCmd:
			addResult(node.Right, 2)
		case *syntax.BinaryArithm:
			push(node.Y, 0)
		case *syntax.UnaryArithm:
			if node.Post {
				addResult(node.OpPos, 2)
			} else {
				push(node.X, 0)
			}
		case *syntax.ParenArithm:
			addResult(node.Rparen, 1)
		case *syntax.FlagsArithm:
			if node.X != nil {
				push(node.X, 0)
			} else {
				push(node.Flags, 1)
			}
		case *syntax.CaseClause:
			addResult(node.Esac, 4)
		case *syntax.CaseItem:
			if node.OpPos.IsValid() {
				addResult(node.OpPos, len(node.Op.String()))
			} else if len(node.Last) > 0 {
				comment := node.Last[len(node.Last)-1]
				addTextEnd(comment.Hash, comment.Text)
			} else if len(node.Stmts) > 0 {
				push(node.Stmts[len(node.Stmts)-1], 0)
			} else {
				return 0, false
			}
		case *syntax.TestClause:
			addResult(node.Right, 2)
		case *syntax.BinaryTest:
			push(node.Y, 0)
		case *syntax.UnaryTest:
			push(node.X, 0)
		case *syntax.ParenTest:
			addResult(node.Rparen, 1)
		case *syntax.DeclClause:
			if len(node.Args) > 0 {
				push(node.Args[len(node.Args)-1], 0)
			} else {
				push(node.Variant, 0)
			}
		case *syntax.ArrayExpr:
			addResult(node.Rparen, 1)
		case *syntax.ArrayElem:
			if node.Value != nil {
				push(node.Value, 0)
			} else {
				combined, ok := checkedAddInt(candidate.adjust, 1)
				if !ok {
					positionsValid = false
					continue
				}
				stack = append(stack, endCandidate{node: node.Index, adjust: combined, useStart: true})
			}
		case *syntax.ExtGlob:
			push(node.Pattern, 1)
		case *syntax.ProcSubst:
			addResult(node.Rparen, 1)
		case *syntax.TimeClause:
			if node.Stmt != nil {
				push(node.Stmt, 0)
			} else {
				addResult(node.Time, 4)
			}
		case *syntax.CoprocClause:
			push(node.Stmt, 0)
		case *syntax.LetClause:
			if len(node.Exprs) == 0 {
				return 0, false
			}
			push(node.Exprs[len(node.Exprs)-1], 0)
		case *syntax.BraceExp:
			if len(node.Elems) == 0 {
				return 0, false
			}
			push(node.Elems[len(node.Elems)-1], 1)
		case *syntax.TestDecl:
			push(node.Body, 0)
		default:
			return 0, false
		}
	}
	if !positionsValid {
		return 0, false
	}
	return best, resolved
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
				stmt := node.Stmts[0]
				if stmt == nil {
					return 0, false
				}
				if len(stmt.Comments) > 0 && stmt.Position.After(stmt.Comments[0].Hash) {
					return positionOffset(stmt.Comments[0].Hash, adjust)
				}
				return positionOffset(stmt.Position, adjust)
			} else if len(node.Last) > 0 {
				return positionOffset(node.Last[0].Hash, adjust)
			} else {
				return 0, true
			}
		case *syntax.Comment:
			return positionOffset(node.Hash, adjust)
		case *syntax.Stmt:
			return positionOffset(node.Position, adjust)
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
				return positionOffset(node.OpPos, adjust)
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
			return positionOffset(node.Lparen, adjust)
		case *syntax.Block:
			return positionOffset(node.Lbrace, adjust)
		case *syntax.IfClause:
			return positionOffset(node.Position, adjust)
		case *syntax.WhileClause:
			return positionOffset(node.WhilePos, adjust)
		case *syntax.ForClause:
			return positionOffset(node.ForPos, adjust)
		case *syntax.WordIter:
			current = node.Name
		case *syntax.CStyleLoop:
			return positionOffset(node.Lparen, adjust)
		case *syntax.BinaryCmd:
			current = node.X
		case *syntax.FuncDecl:
			return positionOffset(node.Position, adjust)
		case *syntax.Word:
			if len(node.Parts) == 0 {
				return 0, false
			}
			current = node.Parts[0]
		case *syntax.Lit:
			return positionOffset(node.ValuePos, adjust)
		case *syntax.SglQuoted:
			return positionOffset(node.Left, adjust)
		case *syntax.DblQuoted:
			return positionOffset(node.Left, adjust)
		case *syntax.CmdSubst:
			return positionOffset(node.Left, adjust)
		case *syntax.ParamExp:
			if node.Dollar.IsValid() {
				return positionOffset(node.Dollar, adjust)
			}
			current = node.Param
		case *syntax.ArithmExp:
			return positionOffset(node.Left, adjust)
		case *syntax.ArithmCmd:
			return positionOffset(node.Left, adjust)
		case *syntax.BinaryArithm:
			current = node.X
		case *syntax.UnaryArithm:
			if node.Post {
				current = node.X
			} else {
				return positionOffset(node.OpPos, adjust)
			}
		case *syntax.ParenArithm:
			return positionOffset(node.Lparen, adjust)
		case *syntax.FlagsArithm:
			current = node.Flags
			var ok bool
			adjust, ok = checkedAddInt(adjust, -1)
			if !ok {
				return 0, false
			}
		case *syntax.CaseClause:
			return positionOffset(node.Case, adjust)
		case *syntax.CaseItem:
			if len(node.Patterns) == 0 {
				return 0, false
			}
			current = node.Patterns[0]
		case *syntax.TestClause:
			return positionOffset(node.Left, adjust)
		case *syntax.BinaryTest:
			current = node.X
		case *syntax.UnaryTest:
			return positionOffset(node.OpPos, adjust)
		case *syntax.ParenTest:
			return positionOffset(node.Lparen, adjust)
		case *syntax.DeclClause:
			current = node.Variant
		case *syntax.ArrayExpr:
			return positionOffset(node.Lparen, adjust)
		case *syntax.ArrayElem:
			if node.Index != nil {
				current = node.Index
			} else {
				current = node.Value
			}
		case *syntax.ExtGlob:
			return positionOffset(node.OpPos, adjust)
		case *syntax.ProcSubst:
			return positionOffset(node.OpPos, adjust)
		case *syntax.TimeClause:
			return positionOffset(node.Time, adjust)
		case *syntax.CoprocClause:
			return positionOffset(node.Coproc, adjust)
		case *syntax.LetClause:
			return positionOffset(node.Let, adjust)
		case *syntax.BraceExp:
			if len(node.Elems) == 0 {
				return 0, false
			}
			current = node.Elems[0]
			var ok bool
			adjust, ok = checkedAddInt(adjust, -1)
			if !ok {
				return 0, false
			}
		case *syntax.TestDecl:
			return positionOffset(node.Position, adjust)
		default:
			return 0, false
		}
	}
	return 0, false
}

func checkedAddInt(left, right int) (int, bool) {
	maxInt := int(^uint(0) >> 1)
	minInt := -maxInt - 1
	if right > 0 && left > maxInt-right || right < 0 && left < minInt-right {
		return 0, false
	}
	return left + right, true
}

func positionOffset(pos syntax.Pos, adjust int) (int, bool) {
	if !pos.IsValid() {
		return 0, false
	}
	offset := pos.Offset()
	maxInt := int(^uint(0) >> 1)
	if offset > uint(maxInt) {
		return 0, false
	}
	result, ok := checkedAddInt(int(offset), adjust)
	if !ok || result < 0 {
		return 0, false
	}
	return result, true
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

func supportedRedirectOperator(operator syntax.RedirOperator) bool {
	switch operator {
	case syntax.RdrOut, syntax.AppOut, syntax.RdrIn, syntax.RdrInOut,
		syntax.DplIn, syntax.DplOut, syntax.RdrClob, syntax.Hdoc,
		syntax.DashHdoc, syntax.WordHdoc, syntax.RdrAll, syntax.AppAll:
		return true
	default:
		return false
	}
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
