import math
from copy import deepcopy
from typing import Any

# ----------------------------------------------------------------------
# Public API (exactly as required)
# ----------------------------------------------------------------------
class ParseError(Exception):
    """Custom exception raised for syntax or evaluation errors."""
    pass


def parse(
    expr: str,
    variables=None,
    trace=False,
    mode="eval",
    precision=None,
) -> dict:
    """
    Parses and evaluates a mathematical / logical expression string.

    Returns the contract dict (including "warnings").
    """
    # ------------------------------------------------------------------
    # 1️⃣ Split multi‑statement input
    # ------------------------------------------------------------------
    stmts = [s.strip() for s in expr.split(';') if s.strip()]
    final_result = None
    ast_output: list[dict] = []
    local_vars = deepcopy(variables) if variables else {}   # async‑safe copy

    parser_trace = trace
    warnings_list: list[str] = []

    for i, stmt in enumerate(stmts):
        tokens = tokenize(stmt)
        parser = Parser(tokens,
                        symtab=local_vars,
                        trace=parser_trace)

        try:
            value, ast_node = parser.parse_expression()
        except ZeroDivisionError as zde:
            raise ZeroDivisionError(str(zde))

        if parser.pos < len(parser.tokens):
            raise ParseError(
                f"Unexpected trailing tokens after expression: "
                f"{parser.tokens[parser.pos:]}"
            )

        # ----------------------------------------------------------------
        # 2️⃣ Record AST (if requested) and apply rounding once
        # ----------------------------------------------------------------
        if mode in ("ast", "both"):
            ast_output.append(ast_node)

        if isinstance(value, float) and precision is not None:
            value = round(value, precision)

        final_result = value
        local_vars.update(parser.symtab)   # keep assignments

        # Accumulate any warnings produced during parsing/evaluation
        if parser.warnings:
            warnings_list.extend(parser.warnings)

        # ----------------------------------------------------------------
        # 3️⃣ Accumulate trace (prefix with statement number)
        # ----------------------------------------------------------------
        if parser_trace and parser.trace_log:
            prefixed = [f"[Stmt {i+1}] {line}" for line in parser.trace_log]
            parser.trace_log[:] = prefixed   # replace original list

    trace_list = parser.trace_log if trace else None

    # ------------------------------------------------------------------
    # 4️⃣ Determine result type string
    # ------------------------------------------------------------------
    if isinstance(final_result, bool):
        res_type = "bool"
    elif isinstance(final_result, int):
        res_type = "int"
    elif isinstance(final_result, float):
        res_type = "float"
    else:
        raise ParseError("Unsupported evaluation result")

    # ------------------------------------------------------------------
    # 5️⃣ Build final AST when requested
    # ------------------------------------------------------------------
    if ast_output and mode == "both":
        full_ast = {"type": "program", "statements": ast_output}
    elif ast_output:
        full_ast = {"type": "program", "statements": ast_output}
    else:
        full_ast = None

    return {
        "result": final_result,
        "type": res_type,
        "ast": full_ast,
        "trace": trace_list,
        "variables": local_vars,
        "warnings": warnings_list if warnings_list else None,   # always present
    }


# ----------------------------------------------------------------------
# 1️⃣ Tokenizer – unchanged (tuple‑based tokens)
# ----------------------------------------------------------------------
def tokenize(s: str):
    """Convert the input string into a list of tokens."""
    tokens = []
    i = 0
    while i < len(s):
        ch = s[i]

        if ch.isspace():
            i += 1
            continue

        # ---- Numbers (int or float) ----
        if ch.isdigit() or ch == '.':
            num_str = ''
            dot_seen = False
            while i < len(s) and (s[i].isdigit() or s[i] == '.'):
                if s[i] == '.':
                    if dot_seen:
                        raise ParseError("Invalid number format – multiple dots")
                    dot_seen = True
                num_str += s[i]
                i += 1
            try:
                val = float(num_str) if dot_seen else int(num_str)
            except ValueError as e:
                raise ParseError(f"Invalid numeric literal: {num_str}") from e
            tokens.append(('NUMBER', val))
            continue

        # ---- Identifiers (variables, constants, functions, keywords) ----
        if ch.isalpha() or ch == '_':
            ident = ''
            while i < len(s) and (s[i].isalnum() or s[i] == '_'):
                ident += s[i]
                i += 1
            tokens.append(('IDENT', ident))
            continue

        # ---- Operators & parentheses (including multi‑char ops) ----
        if ch == ',':
            tokens.append(('COMMA', ','))
            i += 1
            continue

        if ch in '+-*/%&|^~<>=!()':
            if ch == '*' and i + 1 < len(s) and s[i + 1] == '*':
                tokens.append(('OP', '**'))
                i += 2
                continue
            if ch == '=' and i + 1 < len(s) and s[i + 1] == '=':
                tokens.append(('OP', '=='))
                i += 2
                continue
            if ch == '!' and i + 1 < len(s) and s[i + 1] == '=':
                tokens.append(('OP', '!='))
                i += 2
                continue
            if ch == '<' and i + 1 < len(s) and s[i + 1] == '=':
                tokens.append(('OP', '<='))
                i += 2
                continue
            if ch == '>' and i + 1 < len(s) and s[i + 1] == '=':
                tokens.append(('OP', '>='))
                i += 2
                continue

            op_type = ('LPAREN' if ch == '(' else 'RPAREN' if ch == ')' else 'OP')
            tokens.append((op_type, ch))
            i += 1
            continue

        raise ParseError(f"Unexpected character: {ch}")

    return tokens


# ----------------------------------------------------------------------
# 2️⃣ Parser – recursive‑descent with full grammar extensions & fixes
# ----------------------------------------------------------------------
class Parser:
    """Recursive‑descent parser that builds an AST and emits a verbose trace."""
    def __init__(self, tokens, symtab=None, trace=False):
        self.tokens = tokens
        self.pos = 0
        # Re‑create built‑ins per instance → async safety
        self.symtab = {
            "PI": math.pi,
            "E": math.e,
            "TAU": 2 * math.pi,
        }
        if symtab:
            self.symtab.update(symtab)   # allow shadowing (except constants)
        self.trace_log: list[str] = [] if trace else None
        self.warnings: list[str] = []

    def current(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def consume(self, expected_type=None, expected_value=None):
        tok = self.current()
        if tok is None:
            raise ParseError("Unexpected end of expression")
        typ, val = tok
        if (expected_type and typ != expected_type) or \
           (expected_value and val != expected_value):
            expect = f"{expected_type!r},{expected_value!r}" if expected_type else repr(expected_value)
            actual = f"{typ!r},{val!r}"
            raise ParseError(f"Expected {expect} but found {actual}")
        self.pos += 1
        return tok

    def _log(self, msg: str):
        """Append a trace entry (only when tracing is enabled)."""
        if self.trace_log is not None:
            self.trace_log.append(msg)

    # ------------------------------------------------------------------
    # Top‑level expression – may be an assignment or logical_or
    # ------------------------------------------------------------------
    def parse_expression(self) -> tuple[Any, dict]:
        tok = self.current()
        if tok and tok[0] == 'IDENT' and \
           (self.pos + 1 < len(self.tokens)) and \
           self.tokens[self.pos + 1][0] == 'OP' and self.tokens[self.pos + 1][1] == '=':
            return self.parse_assignment()
        else:
            return self.parse_logical_or()

    # ------------------------------------------------------------------
    def parse_assignment(self) -> tuple[Any, dict]:
        var_name = self.consume(expected_type='IDENT')[1]
        if var_name.upper() in {"PI", "E", "TAU"}:
            raise ParseError(f"Cannot reassign built‑in constant: {var_name}")
        self.consume(expected_value='=')          # consume '='
        rhs_val, rhs_node = self.parse_expression()
        self.symtab[var_name] = rhs_val
        ast_node = {
            "type": "assign",
            "var": var_name,
            "value": rhs_val,
            "rhs_ast": rhs_node,
        }
        self._log(f"Assigned {var_name} = {rhs_val}")
        return rhs_val, ast_node

    # ------------------------------------------------------------------
    def parse_logical_or(self) -> tuple[Any, dict]:
        # Logical OR. FIX: consume the 'or' keyword before parsing the RHS so
        # the token stream stays valid (the original never consumed it, which
        # made `a or b` raise "Undefined identifier: or"). eval_ast replay
        # still short-circuits structurally.
        left_val, left_node = self.parse_logical_and()
        while True:
            tok = self.current()
            if not tok or tok[0] != 'IDENT' or tok[1].lower() != 'or':
                break
            self.consume()  # consume 'or'
            rhs_val, rhs_node = self.parse_logical_and()
            result = bool(left_val) or bool(rhs_val)
            ast_node = {"type": "logical_or",
                        "left": left_node,
                        "right": rhs_node}
            self._log(f"logical_or: {bool(left_val)} or {bool(rhs_val)} = {result}")
            left_val, left_node = result, ast_node
        return left_val, left_node

    # ------------------------------------------------------------------
    def parse_logical_and(self) -> tuple[Any, dict]:
        # Logical AND. FIX: consume the 'and' keyword before parsing the RHS
        # (original never consumed it, so `a and b` raised "Undefined
        # identifier: and"). eval_ast replay still short-circuits structurally.
        left_val, left_node = self.parse_comparison()
        while True:
            tok = self.current()
            if not tok or tok[0] != 'IDENT' or tok[1].lower() != 'and':
                break
            self.consume()  # consume 'and'
            rhs_val, rhs_node = self.parse_comparison()
            result = bool(left_val) and bool(rhs_val)
            ast_node = {"type": "logical_and",
                        "left": left_node,
                        "right": rhs_node}
            self._log(f"logical_and: {bool(left_val)} and {bool(rhs_val)} = {result}")
            left_val, left_node = result, ast_node
        return left_val, left_node

    # ------------------------------------------------------------------
    def parse_comparison(self) -> tuple[Any, dict]:
        left_val, left_node = self.parse_add_sub()
        while True:
            tok = self.current()
            if not tok or tok[0] != 'OP' or tok[1] not in {'>', '<', '>=', '<=', '==', '!='}:
                break
            op_tok = self.consume(expected_value=tok[1])
            right_val, right_node = self.parse_add_sub()
            result = {
                '>': left_val > right_val,
                '<': left_val < right_val,
                '>=': left_val >= right_val,
                '<=': left_val <= right_val,
                '==': left_val == right_val,
                '!=': left_val != right_val,
            }[op_tok[1]]
            ast_node = {"type": "comparison",
                        "op": op_tok[1],
                        "left": left_node,
                        "right": right_node}
            self._log(f"comparison {op_tok[1]}: {left_val} ? {right_val} = {result}")
            left_val, left_node = result, ast_node
        return left_val, left_node

    # ------------------------------------------------------------------
    def parse_add_sub(self) -> tuple[Any, dict]:
        left_val, left_node = self.parse_mul_div()
        while True:
            tok = self.current()
            if not tok or tok[0] != 'OP' or tok[1] not in ('+', '-'):
                break
            op_tok = self.consume(expected_value=tok[1])
            right_val, right_node = self.parse_mul_div()

            # ---- Type‑coercion warning: bool mixed with int/float ----
            if isinstance(left_val, bool) or isinstance(right_val, bool):
                self.warnings.append(
                    f"Type coercion: boolean used in arithmetic context at operator '{op_tok[1]}'"
                )

            result = left_val + right_val if op_tok[1] == '+' else left_val - right_val
            ast_node = {"type": "binary_op",
                        "op": op_tok[1],
                        "left": left_node,
                        "right": right_node}
            self._log(f"binary_op {op_tok[1]}: {left_val} {op_tok[1]} {right_val} = {result}")
            left_val, left_node = result, ast_node
        return left_val, left_node

    # ------------------------------------------------------------------
    def parse_mul_div(self) -> tuple[Any, dict]:
        left_val, left_node = self.parse_unary()
        while True:
            tok = self.current()
            if not tok or tok[0] != 'OP' or tok[1] not in ('*', '/', '%'):
                break
            op_tok = self.consume(expected_value=tok[1])
            right_val, right_node = self.parse_unary()

            # ---- Potential division‑by‑zero warning (only for literal 0) ----
            if op_tok[1] == '/' and isinstance(right_val, (int, float)) and right_val == 0:
                self.warnings.append(
                    f"Potential division by zero in expression at operator '{op_tok[1]}'"
                )

            result = (
                left_val * right_val if op_tok[1] == '*'
                else left_val / right_val if op_tok[1] == '/'
                else left_val % right_val
            )
            ast_node = {"type": "binary_op",
                        "op": op_tok[1],
                        "left": left_node,
                        "right": right_node}
            self._log(f"binary_op {op_tok[1]}: {left_val} {op_tok[1]} {right_val} = {result}")
            left_val, left_node = result, ast_node
        return left_val, left_node

    # ------------------------------------------------------------------
    def parse_unary(self) -> tuple[Any, dict]:
        tok = self.current()
        if tok and tok[0] == 'OP' and tok[1] in ('+', '-') and \
           (self.pos == 0 or self.tokens[self.pos - 1][0] in ('OP', 'LPAREN', 'RPAREN')):
            op_tok = self.consume(expected_value=tok[1])
            operand_val, operand_node = self.parse_unary()
            result = -operand_val if op_tok[1] == '-' else +operand_val
            ast_node = {"type": "unary_op",
                        "op": op_tok[1],
                        "operand": operand_node}
            self._log(f"unary {op_tok[1]}: {operand_val} → {result}")
            return result, ast_node

        if tok and tok[0] == 'IDENT' and tok[1].lower() == 'not':
            self.consume(expected_value='not')
            val, node = self.parse_unary()
            result = not bool(val)
            ast_node = {"type": "unary_op",
                        "op": "not",
                        "operand": node}
            self._log(f"logical_not: not {val} → {result}")
            return result, ast_node

        return self.parse_power()

    # ------------------------------------------------------------------
    def parse_power(self) -> tuple[Any, dict]:
        # Exponentiation (added): binds tighter than unary, right-associative.
        # base ** exponent ; the exponent may itself be unary, e.g. 2 ** -1.
        base_val, base_node = self.parse_primary()
        tok = self.current()
        if tok and tok[0] == 'OP' and tok[1] == '**':
            self.consume(expected_value='**')
            exp_val, exp_node = self.parse_unary()
            result = base_val ** exp_val
            node = {"type": "binary_op", "op": "**",
                    "left": base_node, "right": exp_node}
            self._log(f"binary_op **: {base_val} ** {exp_val} = {result}")
            return result, node
        return base_val, base_node

    def parse_primary(self) -> tuple[Any, dict]:
        tok = self.current()
        if not tok:
            raise ParseError("Unexpected end while expecting a primary")

        typ, val = tok
        if typ == 'NUMBER':
            self.consume(expected_type='NUMBER')
            node = {"type": "number", "value": val}
            self._log(f"constant number: {val}")
            return val, node

        if typ == 'IDENT':
            name = self.consume(expected_type='IDENT')[1]

            # ---- Function call detection (name followed by '(') ----
            nxt = self.current()
            if nxt and nxt[0] == 'LPAREN':
                return self.parse_function_call(name)

            # ---- Constant lookup (PI, E, TAU) ----
            upper_name = name.upper()
            if upper_name in {"PI", "E", "TAU"}:
                const_val = {"PI": math.pi,
                            "E": math.e,
                            "TAU": 2 * math.pi}[upper_name]
                node = {"type": "constant", "value": const_val}
                self._log(f"constant {name}: {const_val}")
                return const_val, node

            # ---- Variable lookup (user‑provided or previously assigned) ----
            if name in self.symtab:
                val = self.symtab[name]
                node = {"type": "variable", "name": name, "value": val}
                self._log(f"lookup variable {name}: {val}")
                return val, node

            raise ParseError(f"Undefined identifier: {name}")

        if typ == 'LPAREN':
            self.consume(expected_value='(')
            value, inner_node = self.parse_expression()
            closing = self.current()
            if not closing or closing[1] != ')':
                raise ParseError("Missing closing parenthesis")
            self.consume(expected_value=')')
            node = {"type": "parenthesized", "expr_ast": inner_node}
            self._log(f"group: ({value}) → {value}")
            return value, node

        raise ParseError(f"Unexpected token in primary: {tok}")

    # ------------------------------------------------------------------
    def parse_function_call(self, func_name: str) -> tuple[Any, dict]:
        self._log(f"Calling function {func_name}")
        if not (self.current() and self.current()[0] == 'LPAREN'):
            raise ParseError("Expected '(' after function name")
        self.consume(expected_value='(')

        args = []
        arg_nodes = []

        first = True
        while True:
            tok = self.current()
            if not tok or tok[1] == ')':
                break
            if not first:
                self.consume(expected_value=',')
            else:
                first = False

            val, node = self.parse_expression()
            args.append(val)
            arg_nodes.append(node)

        if not (self.current() and self.current()[1] == ')'):
            raise ParseError("Expected ')' to close function call")
        self.consume(expected_value=')')

        result, fn_node = evaluate_function(func_name.lower(), args, arg_nodes)
        node = {"type": "function_call",
                "name": func_name,
                "args_ast": arg_nodes,
                "result": result}
        self._log(f"function {func_name}({', '.join(str(a) for a in args)}) → {result}")
        return result, node


# ----------------------------------------------------------------------
# 3️⃣ Function evaluation – central dispatch for built‑ins
# ----------------------------------------------------------------------
def evaluate_function(name: str, args: list, arg_asts: list):
    """Resolve a function name to its Python implementation and build an AST node."""
    def unary(f):      return lambda x: f(x)
    def binary(f):     return lambda *xs: f(*xs) if len(xs)==2 else f(*xs)

    funcs = {
        "sin":   unary(math.sin),
        "cos":   unary(math.cos),
        "tan":   unary(math.tan),
        "sqrt":  unary(math.sqrt),
        "log":   unary(math.log),
        "log10": unary(math.log10),
        "abs":   unary(abs),
        "floor": unary(math.floor),
        "ceil":  unary(math.ceil),
        "round": lambda x, n=None: round(x, n) if n is not None else round(x),
        "min":   binary(min),
        "max":   binary(max),
        "clamp": lambda v, lo, hi: max(lo, min(v, hi)),
        "pow":   lambda b, e: b ** e,
        "factorial": lambda n: math.factorial(int(n)) if isinstance(n,int) and n>=0 \
                               else (_ for _ in ()).throw(ParseError("factorial requires non‑negative integer"))
    }

    if name not in funcs:
        raise ParseError(f"Unknown function: {name}")

    try:
        impl = funcs[name]
        if name in {"min", "max"}:
            result = impl(*args)
        elif name == "clamp":
            if len(args) != 3:
                raise ParseError("clamp requires exactly three arguments")
            result = impl(args[0], args[1], args[2])
        elif name == "pow":
            if len(args) != 2:
                raise ParseError("pow requires exactly two arguments")
            result = impl(args[0], args[1])
        elif name == "factorial":
            if len(args) != 1:
                raise ParseError("factorial requires exactly one argument")
            result = impl(args[0])
        else:   # unary functions (including round with optional ndigits)
            if name == "round" and len(args) == 2:
                result = impl(args[0], args[1])
            elif len(args) == 1:
                result = impl(args[0])
            else:
                raise ParseError(f"{name} expects a single argument")
    except Exception as exc:
        raise ParseError(f"Error in function '{name}': {exc}")

    ast_node = {"type": "function_call",
                "name": name,
                "args_ast": arg_asts,
                "result": result}
    return result, ast_node


# ----------------------------------------------------------------------
# 4️⃣ eval_ast – evaluate an AST without re‑parsing (required)
# ----------------------------------------------------------------------
def eval_ast(ast_node: dict,
             variables: dict = None,
             trace: bool = False):
    """
    Evaluate an AST produced by `parse(..., mode="ast")` or `parse(..., mode="both")`.
    Returns the same contract dict as `parse()`.
    """
    if variables is None:
        variables = {}

    local_vars = deepcopy(variables)   # async‑safe copy
    warnings: list[str] = []

    def _log(msg: str):
        return msg if trace else None

    def _eval(node) -> tuple[Any, list[str]]:
        ntype = node.get("type")
        if ntype == "number":
            val = node["value"]
            _log(f"constant number: {val}")
            return val, [_log(f"constant number: {val}")]

        if ntype == "constant":
            val = node["value"]
            _log(f"constant: {val}")
            return val, [_log(f"constant: {val}")]

        if ntype == "variable":
            name = node["name"]
            try:
                val = local_vars[name]
            except KeyError:
                raise ParseError(f"Undefined variable: {name}")
            _log(f"lookup variable {name}: {val}")
            return val, [_log(f"lookup variable {name}: {val}")]

        if ntype == "binary_op":
            op = node["op"]
            left_val, ltrc = _eval(node["left"])
            right_val, rtrc = _eval(node["right"])

            # Type‑coercion warning (same rule as parser)
            if isinstance(left_val, bool) or isinstance(right_val, bool):
                wmsg = f"Type coercion: boolean used in arithmetic context at operator '{op}'"
                warnings.append(wmsg)

            result = {
                '+': left_val + right_val,
                '-': left_val - right_val,
                '*': left_val * right_val,
                '/': (lambda a, b: a / b if b != 0 else (_ for _ in ()).throw(ZeroDivisionError("division by zero")))(left_val, right_val),
                '%': left_val % right_val,
                '**': left_val ** right_val,
            }[op]
            msg = f"binary_op {op}: {left_val} {op} {right_val} = {result}"
            return result, ltrc + rtrc + [_log(msg)]

        if ntype == "unary_op":
            op = node["op"]
            operand_val, otrc = _eval(node["operand"])
            if op == '-':
                res = -operand_val
            elif op == 'not':
                res = not bool(operand_val)
            else:   # '+'
                res = +operand_val
            msg = f"unary {op}: {operand_val} → {res}"
            return res, otrc + [_log(msg)]

        if ntype == "comparison":
            left_val, ltrc = _eval(node["left"])
            right_val, rtrc = _eval(node["right"])
            op = node["op"]
            result = {
                '>': left_val > right_val,
                '<': left_val < right_val,
                '>=': left_val >= right_val,
                '<=': left_val <= right_val,
                '==': left_val == right_val,
                '!=': left_val != right_val,
            }[op]
            msg = f"comparison {op}: {left_val} ? {right_val} = {result}"
            return result, ltrc + rtrc + [_log(msg)]

        if ntype == "logical_or":
            left_val, ltrc = _eval(node["left"])
            if bool(left_val):
                msg = f"logical_or short‑circuited (LHS true) → True"
                return True, ltrc + [_log(msg)]
            right_val, rtrc = _eval(node["right"])
            result = bool(left_val) or bool(right_val)
            msg = f"logical_or: {left_val} or {right_val} = {result}"
            return result, ltrc + rtrc + [_log(msg)]

        if ntype == "logical_and":
            left_val, ltrc = _eval(node["left"])
            if not bool(left_val):
                msg = f"logical_and short‑circuited (LHS false) → False"
                return False, ltrc + [_log(msg)]
            right_val, rtrc = _eval(node["right"])
            result = bool(left_val) and bool(right_val)
            msg = f"logical_and: {left_val} and {right_val} = {result}"
            return result, ltrc + rtrc + [_log(msg)]

        if ntype == "assign":
            var_name = node["var"]
            rhs_val, trc = _eval(node["rhs_ast"])
            local_vars[var_name] = rhs_val
            msg = f"assign: {var_name} = {rhs_val}"
            return rhs_val, trc + [_log(msg)]

        if ntype == "function_call":
            fname = node["name"].lower()
            arg_vals = [ _eval(a)[0] for a in node["args_ast"] ]
            result, _fn_ast = evaluate_function(fname, arg_vals, node["args_ast"])
            msg = f"function {fname}({', '.join(str(v) for v in arg_vals)}) → {result}"
            return result, [_log(msg)]

        if ntype == "program":
            final_val = None
            stmt_trace = []
            for stmt_node in node["statements"]:
                val, trc = _eval(stmt_node)
                stmt_trace.extend(trc)
                final_val = val
            return final_val, stmt_trace

        if ntype == "parenthesized":
            return _eval(node["expr_ast"])

        raise ParseError(f"Unsupported AST node type: {ntype}")

    result, trace_list = _eval(ast_node)

    # Map Python types to contract strings
    if isinstance(result, bool):
        res_type_str = "bool"
    elif isinstance(result, int):
        res_type_str = "int"
    elif isinstance(result, float):
        res_type_str = "float"
    else:
        raise ParseError("Unsupported evaluation result type")

    return {
        "result": result,
        "type": res_type_str,
        "ast": ast_node,
        "trace": trace_list if trace else None,
        "variables": local_vars,
        "warnings": warnings if warnings else None,
    }


# ----------------------------------------------------------------------
# 5️⃣ lint – static validation without evaluation (required)
# ----------------------------------------------------------------------
def lint(expr: str, variables: dict = None):
    """
    Validate an expression syntactically and semantically.
    Returns {"valid": bool, "errors": [...], "warnings": [...] or None}.
    """
    if variables is None:
        variables = {}

    errors = []
    warnings = []

    # ---- Tokenisation (catches malformed numbers, unknown chars) ----
    try:
        tokens = tokenize(expr)
    except ParseError as e:
        errors.append(str(e))
        return {"valid": False, "errors": errors, "warnings": []}

    # ---- Parenthesis balance check ----
    stack = []
    for typ, _ in tokens:
        if typ == 'LPAREN':
            stack.append(None)
        elif typ == 'RPAREN':
            if not stack:
                errors.append("unmatched closing parenthesis")
                return {"valid": False, "errors": errors, "warnings": []}
            stack.pop()
    if stack:
        errors.append("unmatched opening parenthesis")

    # ---- Minimal parser to detect undefined identifiers / unknown functions ----
    class LintParser(Parser):
        def __init__(self, tokens, symtab=None):
            super().__init__(tokens, symtab=symtab, trace=False)
            self.undefined = set()
            self.unknown_funcs = set()

        def _check_identifier(self, name: str):
            # built‑ins are always defined
            if name.upper() in {"PI", "E", "TAU"}:
                return
            if name not in self.symtab:
                self.undefined.add(name)

        def parse_primary(self):
            tok = self.current()
            if not tok:
                raise ParseError("Unexpected end while expecting a primary")
            typ, val = tok
            if typ == 'NUMBER':
                self.consume(expected_type='NUMBER')
                return val, {"type": "number", "value": val}
            elif typ == 'IDENT':
                # Peek (do NOT consume) so super().parse_primary() does the
                # real parsing. The original consumed the IDENT here, so
                # super() then saw the NEXT token and raised Unexpected
                # token in primary for any bare variable such as x + 1.
                nxt = self.tokens[self.pos + 1] if self.pos + 1 < len(self.tokens) else None
                if not (nxt and nxt[0] == 'LPAREN'):
                    self._check_identifier(val)
                return super().parse_primary()
            elif typ == 'LPAREN':
                return super().parse_primary()
            else:
                raise ParseError(f"Unexpected token in primary: {tok}")

        def parse_function_call(self, func_name: str):
            if func_name.lower() not in {
                "sin","cos","tan","sqrt","log","log10",
                "abs","floor","ceil","round","min","max",
                "clamp","pow","factorial"
            }:
                self.unknown_funcs.add(func_name)
            return super().parse_function_call(func_name)

    # Symbol table for linting: built‑ins + user variables
    symtab = {
        "PI": math.pi,
        "E": math.e,
        "TAU": 2 * math.pi,
    }
    if variables:
        symtab.update(variables)

    try:
        lp = LintParser(tokens, symtab=symtab)
        # Process each statement separately (split by ';')
        stmts = [s.strip() for s in expr.split(';') if s.strip()]
        for stmt in stmts:
            lp.tokens = tokenize(stmt)   # re‑tokenise per statement
            lp.pos = 0
            lp.parse_expression()
            if lp.pos < len(lp.tokens):
                raise ParseError(f"Unexpected trailing tokens: {lp.tokens[lp.pos:]}")
    except ParseError as pe:
        errors.append(str(pe))
    finally:
        if lp.undefined:
            warnings.extend([f"Undefined variable reference: {v}" for v in sorted(lp.undefined)])
        if lp.unknown_funcs:
            warnings.extend([f"Unknown function call: {fn}" for fn in sorted(lp.unknown_funcs)])

    # ---- Depth warning (max nesting > 10) ----
    depth = 0
    max_depth = 0
    for typ, _ in tokens:
        if typ == 'LPAREN':
            depth += 1
            if depth > max_depth:
                max_depth = depth
        elif typ == 'RPAREN':
            depth -= 1
    if max_depth > 10:
        warnings.append(f"Deeply nested expression (max depth {max_depth}) – may affect performance")

    valid = not errors
    return {"valid": valid, "errors": errors, "warnings": warnings if warnings else None}


# ----------------------------------------------------------------------
# End of file – ready for OracleAI backend import.
# ----------------------------------------------------------------------
